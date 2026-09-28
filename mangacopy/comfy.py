"""ComfyUI API client (plain requests).

Workflow templates live in Api/*.json (see comfy.load_workflow). Prompts are
submitted via POST /prompt, polled via GET /history/{prompt_id}, and artifacts
downloaded via GET /view. Images are uploaded via POST /upload/image.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import requests

from . import config

CLIENT_ID = uuid.uuid4().hex


class ComfyError(Exception):
    pass


def _base() -> str:
    return f"http://{config.COMFY_HOST}:{config.COMFY_PORT}"


def _get(path: str, timeout: float = 30.0) -> requests.Response:
    try:
        return requests.get(f"{_base()}{path}", timeout=timeout)
    except requests.RequestException as exc:
        raise ComfyError(f"ComfyUI request failed ({path}): {type(exc).__name__}: {exc}") from exc


def health() -> dict:
    """GET /system_stats. Raises ComfyError on failure."""
    resp = _get("/system_stats")
    if resp.status_code != 200:
        raise ComfyError(f"health check failed: HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def queue_status() -> dict:
    """GET /queue. Raises ComfyError on failure."""
    resp = _get("/queue")
    if resp.status_code != 200:
        raise ComfyError(f"queue status failed: HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def load_workflow(name: str) -> dict:
    """Load an API-format workflow from Api/{name}.json (e.g. name="API - Wai"
    reads "Api/API - Wai.json")."""
    path = config.API_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def set_node(workflow: dict, node_id: str, **inputs) -> None:
    """Overwrite existing inputs of workflow[node_id] in place. Raises KeyError
    if the node or any requested input key does not exist."""
    if node_id not in workflow:
        raise KeyError(f"workflow has no node {node_id!r}")
    node = workflow[node_id]
    if "inputs" not in node:
        raise KeyError(f"node {node_id!r} has no 'inputs' dict")
    for key, value in inputs.items():
        if key not in node["inputs"]:
            raise KeyError(f"node {node_id!r} has no input {key!r}")
        node["inputs"][key] = value


def submit(workflow: dict) -> str:
    """POST /prompt with {"prompt": workflow, "client_id": CLIENT_ID};
    returns the prompt_id. Raises ComfyError on non-200 or missing prompt_id."""
    try:
        resp = requests.post(
            f"{_base()}/prompt",
            json={"prompt": workflow, "client_id": CLIENT_ID},
            timeout=60,
        )
    except requests.RequestException as exc:
        raise ComfyError(f"submit failed: {type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise ComfyError(f"submit failed: HTTP {resp.status_code}: {resp.text[:500]}")
    prompt_id = resp.json().get("prompt_id")
    if not prompt_id:
        raise ComfyError(f"submit response missing prompt_id: {resp.text[:300]}")
    return prompt_id


def _error_summary(entry: dict) -> str:
    parts = []
    for msg in entry.get("status", {}).get("messages", []) or []:
        if not isinstance(msg, (list, tuple)) or len(msg) < 2:
            continue
        mtype, data = msg[0], msg[1]
        if mtype == "execution_error" and isinstance(data, dict):
            parts.append(
                f"node {data.get('node_id')} ({data.get('node_type')}): "
                f"{data.get('exception_message')}"
            )
        elif mtype == "execution_interrupted":
            parts.append("execution interrupted")
    if not parts:
        parts.append(json.dumps(entry.get("status", {}), ensure_ascii=False)[:300])
    return "; ".join(parts)


def wait(prompt_id: str, timeout: int = None, poll: float = 5.0) -> dict:
    """Poll GET /history/{prompt_id} until the entry for prompt_id appears.
    Returns the history entry. Raises ComfyError if the entry status is
    "error" (with a node error summary) or on timeout (timeout defaults to
    config.COMFY_TIMEOUT)."""
    effective_timeout = config.COMFY_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + effective_timeout
    url = f"/history/{prompt_id}"
    while True:
        resp = _get(url)
        if resp.status_code != 200:
            raise ComfyError(f"history poll failed: HTTP {resp.status_code}: {resp.text[:300]}")
        entry = resp.json().get(prompt_id)
        if entry is not None:
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                raise ComfyError(f"prompt {prompt_id} execution error: {_error_summary(entry)}")
            if status.get("completed", True):
                return entry
        if time.monotonic() >= deadline:
            raise ComfyError(
                f"timeout after {effective_timeout}s waiting for prompt {prompt_id}"
            )
        time.sleep(poll)


def fetch_outputs(history_entry: dict, dest_dir: Path) -> list:
    """Download every image/video listed in the history entry's outputs to
    dest_dir (file name = ComfyUI-side filename). Returns local paths."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    out_paths = []
    for node_out in (history_entry.get("outputs") or {}).values():
        for key in ("images", "videos"):
            for item in node_out.get(key) or []:
                filename = item.get("filename")
                if not filename:
                    continue
                params = {
                    "filename": filename,
                    "subfolder": item.get("subfolder", ""),
                    "type": item.get("type", "output"),
                }
                try:
                    resp = requests.get(f"{_base()}/view", params=params, timeout=300)
                except requests.RequestException as exc:
                    raise ComfyError(
                        f"download failed for {params}: {type(exc).__name__}: {exc}"
                    ) from exc
                if resp.status_code != 200:
                    raise ComfyError(
                        f"download failed for {params}: HTTP {resp.status_code}: {resp.text[:300]}"
                    )
                target = dest / Path(filename).name
                target.write_bytes(resp.content)
                out_paths.append(target)
    return out_paths


def upload_image(path) -> str:
    """POST /upload/image (multipart, overwrite=true); returns the ComfyUI-side
    file name (response JSON "name" field)."""
    p = Path(path)
    try:
        with p.open("rb") as fh:
            resp = requests.post(
                f"{_base()}/upload/image",
                files={"image": (p.name, fh)},
                data={"overwrite": "true"},
                timeout=300,
            )
    except requests.RequestException as exc:
        raise ComfyError(f"upload failed for {p}: {type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise ComfyError(f"upload failed for {p}: HTTP {resp.status_code}: {resp.text[:300]}")
    name = resp.json().get("name")
    if not name:
        raise ComfyError(f"upload response missing 'name': {resp.text[:300]}")
    return name
