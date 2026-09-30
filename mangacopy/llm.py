"""LLM client for the OpenAI-compatible spark endpoint (via litellm).

Uses plain requests, no openai SDK. Key behaviours:
- STREAMING (2026-09-28): every request uses stream=True and consumes the SSE
  stream. The endpoint is a DGX Spark (GB10) — large prompts take tens of
  minutes of prefill+generation, so non-streaming requests made the read
  timeout an effective total-duration cap and big calls could never fit. With
  streaming, the timeout bounds only silence between chunks.
- session affinity: top-level payload field metadata.session_id (per-call uuid
  unless the caller supplies one; callers should generate a POOL — see
  new_session_pool — one id per DGX, assigned per independent unit).
- json_mode fallback: the endpoint may reject response_format={"type":"json_object"};
  on a 4xx whose body mentions response_format/json the parameter is dropped and
  the request retried once (does not consume retry budget). A second glitch is
  handled the same way: a stream that ends with no text deltas (the streaming
  twin of content=null) also triggers one free retry without response_format.
- retries: timeouts / connection errors / 5xx responses are retried LLM_RETRY
  times with 5s/15s backoff (timeouts use 60s/180s); failure raises LLMError
  carrying the last response snippet.
"""

from __future__ import annotations

import base64
import json
import re
import sys
import threading
import time
import uuid
from pathlib import Path

import requests

from . import config

_RETRY_BACKOFF = [5, 15]        # connection errors / 5xx
_TIMEOUT_BACKOFF = [60, 180]     # read timeouts: usually an endpoint slow-peak,
                                 # immediate retries would just burn 10 more minutes

# Global hard cap on in-flight LLM connections (user 2026-09-27: the two DGX
# machines handle ONE concurrent request each). Every chat()/chat_vision()
# call — from any stage, any DAG branch, any worker thread — must acquire
# this semaphore, so total concurrent spark connections never exceed 2.
_LLM_SEM = threading.Semaphore(config.LLM_MAX_CONCURRENT)

# Process-wide dual-DGX session pair (2026-09-28 user directive: true two-way
# parallelism). Exactly one session per machine, shared by ALL stages; callers
# distribute these to their units for stickiness. _acquire_session pairs the
# connection slot with a session: concurrent calls NEVER share one, so the two
# in-flight requests always land on different DGX machines.
def new_session_id() -> str:
    return uuid.uuid4().hex


_SESS_LOCK = threading.Lock()
_BUSY_SESSIONS: set = set()


def new_session_pool(n: int | None = None) -> list:
    """Return a fresh pool of n (default config.LLM_SESSION_POOL) distinct session IDs.
    Generating fresh session IDs per stage ensures LiteLLM router distributes traffic
    dynamically across both DGX machines rather than pinning all tasks to a single static hash."""
    count = max(1, int(n)) if n is not None else max(1, config.LLM_SESSION_POOL)
    return [new_session_id() for _ in range(count)]


def _acquire_session(preferred: str | None = None) -> str:
    """Block for an LLM concurrency slot (capped at config.LLM_MAX_CONCURRENT=2).
    Concurrent calls hold distinct session IDs so LiteLLM maps them to different DGX machines."""
    _LLM_SEM.acquire()
    with _SESS_LOCK:
        if preferred is not None and preferred not in _BUSY_SESSIONS:
            sess = preferred
        else:
            sess = new_session_id()
        _BUSY_SESSIONS.add(sess)
    return sess


def _release_session(sess: str) -> None:
    with _SESS_LOCK:
        _BUSY_SESSIONS.discard(sess)
    _LLM_SEM.release()


class LLMError(Exception):
    pass


def chat(
    messages: list,
    *,
    json_mode: bool = False,
    max_tokens: int | None = None,
    timeout: int | None = None,
    session_id: str | None = None,
) -> str:
    """POST {base}/chat/completions and return the assistant message content.
    Serialized by the slot allocator: <= config.LLM_MAX_CONCURRENT in-flight
    connections, and concurrent calls never share a session (= one per DGX)."""
    sess = _acquire_session(session_id)
    try:
        return _chat_locked(
            messages,
            json_mode=json_mode,
            max_tokens=max_tokens,
            timeout=timeout,
            session_id=sess,
        )
    finally:
        _release_session(sess)


def _chat_locked(
    messages: list,
    *,
    json_mode: bool = False,
    max_tokens: int | None = None,
    timeout: int | None = None,
    session_id: str | None = None,
) -> str:
    url = f"{config.LLM_BASE_URL}/chat/completions"
    active_session_id = session_id or new_session_id()
    headers = {
        "Authorization": f"Bearer {config.LLM_API_KEY}",
        "x-litellm-session-id": active_session_id,
    }
    payload = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "max_tokens": config.LLM_MAX_TOKENS if max_tokens is None else max_tokens,
        "session_id": active_session_id,
        "metadata": {"session_id": active_session_id},
        # Streaming (2026-09-28, user diagnosis of GB10 hardware): the endpoint
        # is a DGX Spark (GB10) with limited compute — large prompts take tens
        # of minutes of prefill+generation. Non-streaming requests made the
        # read-timeout an effective TOTAL-duration cap, so big calls could
        # never fit. With stream=True bytes flow throughout generation and the
        # timeout only bounds silence between chunks.
        "stream": True,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    retries_left = config.LLM_RETRY
    backoff_idx = 0
    last_err = "no request attempted"

    while True:
        try:
            resp = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=config.LLM_TIMEOUT if timeout is None else timeout,
                stream=True,
            )
        except requests.Timeout as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            if retries_left > 0:
                time.sleep(_TIMEOUT_BACKOFF[min(backoff_idx, len(_TIMEOUT_BACKOFF) - 1)])
                backoff_idx += 1
                retries_left -= 1
                # Refresh session ID on timeout to break sticky affinity on stalled node
                active_session_id = new_session_id()
                headers["x-litellm-session-id"] = active_session_id
                payload["session_id"] = active_session_id
                payload["metadata"]["session_id"] = active_session_id
                continue
            raise LLMError(f"LLM request failed after {config.LLM_RETRY} retries; last error: {last_err}") from exc
        except requests.ConnectionError as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            if retries_left > 0:
                time.sleep(_RETRY_BACKOFF[min(backoff_idx, len(_RETRY_BACKOFF) - 1)])
                backoff_idx += 1
                retries_left -= 1
                # Refresh session ID on connection drop to allow failover
                active_session_id = new_session_id()
                headers["x-litellm-session-id"] = active_session_id
                payload["session_id"] = active_session_id
                payload["metadata"]["session_id"] = active_session_id
                continue
            raise LLMError(f"LLM request failed after {config.LLM_RETRY} retries; last error: {last_err}") from exc

        if resp.status_code == 200:
            # Consume the SSE stream: accumulate content deltas. reasoning
            # deltas and keep-alives keep bytes flowing, so the read timeout
            # bounds only silence between chunks, not total duration.
            content_parts: list[str] = []
            t_first = None            # first delta (content OR reasoning)
            n_reason_chars = 0
            t_start = time.time()      # includes queue + prefill in ttft
            try:
                for line in resp.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        d = json.loads(data_str)
                    except ValueError:
                        continue
                    choices = d.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    c = delta.get("content")
                    rc = delta.get("reasoning_content")
                    if t_first is None and (c or rc):
                        t_first = time.time()
                    if isinstance(rc, str):
                        n_reason_chars += len(rc)
                    if isinstance(c, str):
                        content_parts.append(c)
            except (requests.Timeout, requests.ConnectionError,
                    requests.exceptions.ChunkedEncodingError) as exc:
                # ChunkedEncodingError (2026-09-28): the server drops long SSE
                # streams mid-way (~15 min observed on GB10/litellm). Retry —
                # partial streamed JSON is unusable, so restart the request.
                last_err = f"{type(exc).__name__} mid-stream: {exc}"
                resp.close()
                if retries_left > 0:
                    time.sleep(_TIMEOUT_BACKOFF[min(backoff_idx, len(_TIMEOUT_BACKOFF) - 1)])
                    backoff_idx += 1
                    retries_left -= 1
                    # Refresh session ID on mid-stream drop
                    active_session_id = new_session_id()
                    headers["x-litellm-session-id"] = active_session_id
                    payload["session_id"] = active_session_id
                    payload["metadata"]["session_id"] = active_session_id
                    continue
                raise LLMError(f"LLM stream failed after {config.LLM_RETRY} retries; last error: {last_err}") from exc
            finally:
                resp.close()

            content = "".join(content_parts)
            if not content.strip():
                if json_mode:
                    # vLLM+json_object intermittent glitch (observed 2026-09-26
                    # on larger prompts): the stream ends with no text deltas
                    # (the streaming twin of content=null). Empirically the
                    # SAME prompt succeeds without response_format; drop it and
                    # retry once for free.
                    payload.pop("response_format", None)
                    json_mode = False
                    continue
                # Empty stream WITHOUT json constraints: the endpoint is in a
                # minutes-scale "empty-stream regime" (observed 2026-09-28:
                # even 8-token probes return no content, alternating with
                # healthy windows). Retryable with the timeout backoff rather
                # than failing the whole stage fast; long regimes are absorbed
                # by the DAG cycle retries one level up.
                last_err = "empty streamed content (endpoint empty-stream regime)"
                if retries_left > 0:
                    time.sleep(_TIMEOUT_BACKOFF[min(backoff_idx, len(_TIMEOUT_BACKOFF) - 1)])
                    backoff_idx += 1
                    retries_left -= 1
                    continue
                raise LLMError(
                    f"LLM stream returned no content after {config.LLM_RETRY} retries "
                    f"(endpoint empty-stream regime)")
            # performance telemetry (2026-09-28 user-approved): ttft covers
            # queue+prefill+first reasoning token; decode spans the rest.
            t_total = time.time() - t_start
            ttft = (t_first - t_start) if t_first is not None else t_total
            print(f"[llm] ttft={ttft:.1f}s total={t_total:.1f}s "
                  f"content={len(content)}c reasoning={n_reason_chars}c "
                  f"decode={t_total - ttft:.1f}s", file=sys.stderr, flush=True)
            return content

        body = resp.text or ""
        resp.close()
        if 400 <= resp.status_code < 500:
            if json_mode and ("response_format" in body or "json" in body.lower()):
                # Endpoint rejects response_format: drop it and retry once for free.
                payload.pop("response_format", None)
                json_mode = False
                continue
            raise LLMError(f"LLM HTTP {resp.status_code} (not retryable): {body[:500]}")

        last_err = f"HTTP {resp.status_code}: {body[:500]}"
        if retries_left > 0:
            time.sleep(_RETRY_BACKOFF[min(backoff_idx, len(_RETRY_BACKOFF) - 1)])
            backoff_idx += 1
            retries_left -= 1
            continue
        raise LLMError(
            f"LLM request failed after {config.LLM_RETRY} retries; last error: {last_err}"
        )


def chat_vision(
    text_prompt: str,
    image_paths: list,
    *,
    json_mode: bool = False,
    max_tokens: int | None = None,
    timeout: int | None = None,
    session_id: str | None = None,
) -> str:
    """chat() with an OpenAI vision content array: text item first, then one
    image_url item per image (data:image/{ext};base64,...; ext inferred from
    the file name, default png)."""
    content = [{"type": "text", "text": text_prompt}]
    for p in image_paths:
        p = Path(p)
        ext = p.suffix.lower().lstrip(".") or "png"
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/{ext};base64,{b64}"},
            }
        )
    return chat(
        [{"role": "user", "content": content}],
        json_mode=json_mode,
        max_tokens=max_tokens,
        timeout=timeout,
        session_id=session_id,
    )


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def _scan_balanced(text: str, start: int) -> tuple[str | None, int]:
    """Return (balanced_substring, end_index) starting at text[start], which must
    be '{' or '['. Returns (None, start) if unbalanced."""
    open_c = text[start]
    close_c = "}" if open_c == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0 and c == close_c:
                return text[start : i + 1], i + 1
    return None, start


def extract_json(text: str):
    """Robustly extract the first balanced JSON object or array from text.
    Strips ```json fences first. Returns the parsed value; raises LLMError if
    nothing parseable is found."""
    if not isinstance(text, str):
        raise LLMError(f"extract_json expects str, got {type(text).__name__}")
    t = text.strip()
    m = _FENCE_RE.search(t)
    if m:
        t = m.group(1).strip()
    for i, c in enumerate(t):
        if c in "{[":
            candidate, _ = _scan_balanced(t, i)
            if candidate is None:
                continue
            try:
                return json.loads(candidate)
            except ValueError:
                continue
    raise LLMError(f"no balanced JSON object/array found in: {text[:300]!r}")
