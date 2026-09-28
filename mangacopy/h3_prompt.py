"""S6 h3_prompt: per-segment MiniMax-H3 video prompts (I2VA / T2VA).

Mode: params.style == "2d" -> I2VA with the segment's first panel S4 image as
first frame (missing image downgrades that segment to T2VA with a warning);
otherwise T2VA. Prompt structure follows MiniMaxH3 h3-prompt-writing
references/base-en.txt verbatim rules (fixed I2VA first line, three fields in
order, [Shot 1] without timestamp, later shots "[Shot N] At MM:SS.mmm",
dialogue <d>[Chinese] 原文</d>, identity lock phrase per segment).

Outputs (s6_h3/): seg_XX.txt (the H3 prompt) and seg_XX.json
({"seg","mode","duration","first_frame","panels","seed"}); check_report.md
lists per-segment check results, UNRESOLVED for issues left after the
auto-fix budget (<= 2 fix rounds).

Checkpoint items under stage "s6": "seg_XX" (prompt written) and
"seg_XX_check" ({"pass": bool, "problems": [...], "rounds": int}).

Checks per segment (all hard):
- deterministic: I2VA first-line char-exact match (T2VA: line absent), field
  order, shot numbering contiguity, timestamp regex + strict increase within
  the segment duration, <d>[Chinese] tag balance, verbatim Chinese dialogue,
  identity-lock phrase + every relevant character name present;
- LLM (s6_check.md): the six-item self-check (segment <= 15 s, <= 3 main
  characters per shot, I2VA first line, timestamps, verbatim dialogue,
  identity lock & clothing per segment). The LLM check runs only when the
  deterministic checks pass (to spend calls where they add information).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import llm, templates
from .project import Project

I2VA_FIRST_LINE = (
    "For the target video, at 0.00 seconds into the target video, "
    "<Picture 1> (from [Shot 1]) is fully referenced."
)
IDENTITY_LOCK_PHRASE = "identity lock, no identity swap, no changing face or hairstyle"

_TS_RE = re.compile(r"\[Shot (\d+)\] At (\d{2}):(\d{2})\.(\d{3})")
_SHOT_RE = re.compile(r"\[Shot (\d+)\]")

_FIELDS = ("integrated_multimodal_description:", "overall_soundscape:",
           "non_diegetic_music:")

_MAX_FIX_ROUNDS = 2
# The spark endpoint relays slowly for long-outputs; observed single-call
# latency up to ~20 min (S5 logged 1219 s), and the default 300 s read
# timeout killed every attempt of the larger S6 generation prompt. H3 prompt
# calls therefore use a 900 s per-request timeout.
_LLM_CALL_TIMEOUT = 1200  # 2026-09-27 用户终版裁定：上限 20 分钟


# ---- small helpers -----------------------------------------------------------

def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _md_section(text: str, title: str) -> str:
    m = re.search(rf"^##\s*{re.escape(title)}\s*$", text, re.MULTILINE)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"^##\s", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _dialogue_lines_from_l3(l3: str) -> list:
    sec = _md_section(l3, "对白原文")
    out = []
    for line in sec.splitlines():
        s = line.strip().lstrip("-*• ").strip()
        if not s or s.startswith("（无") or s.startswith("(无"):
            continue
        # Skip meta comments / annotations in parentheses
        if (s.startswith("（") and s.endswith("）")) or (s.startswith("(") and s.endswith(")")):
            if any(w in s for w in ("注", "本镜", "说明", "旁白说明", "画外音说明")):
                continue
        if "：" in s or ":" in s:
            speaker, text = s.split("：", 1) if "：" in s else s.split(":", 1)
            text = text.strip()
        else:
            text = s
        if text and not text.startswith("（无"):
            out.append(text)
    return out


def deterministic_seed(project_id: str, seg_key: str) -> int:
    """Reproducible 64-bit noise seed: first 16 hex digits of md5(project:seg)."""
    digest = hashlib.md5(f"{project_id}:{seg_key}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


# ---- deterministic prompt checks ----------------------------------------------

def deterministic_checks(prompt: str, mode: str, duration: float,
                         dialogue_texts: list, char_names: list) -> list:
    errs = []
    lines = prompt.split("\n")

    # 1) first line / I2VA instruction
    if mode == "i2va":
        if lines[0].rstrip() != I2VA_FIRST_LINE:
            errs.append(
                f"I2VA 首行不是逐字符官方固定句: {lines[0][:80]!r} (expected {I2VA_FIRST_LINE!r})"
            )
        if len(lines) > 1 and lines[1].strip():
            errs.append("I2VA 首行之后必须紧跟一个空行")
    else:
        if I2VA_FIRST_LINE in prompt:
            errs.append("T2VA 模式不得出现 I2VA 首行指令句")
        first_nonempty = next((ln for ln in lines if ln.strip()), "")
        if not first_nonempty.startswith(_FIELDS[0]):
            errs.append(f"T2VA 必须以 {_FIELDS[0]} 开头，实际: {first_nonempty[:60]!r}")

    # 2) field presence and order
    pos = -1
    for f in _FIELDS:
        idx = prompt.find(f)
        if idx < 0:
            errs.append(f"缺少字段 {f}")
        elif idx <= pos and idx >= 0:
            errs.append(f"字段顺序错误: {f}")
        if idx >= 0:
            pos = idx

    # 3) shots and timestamps
    ts = [(int(m.group(1)), int(m.group(2)) * 60 + int(m.group(3))
           + int(m.group(4)) / 1000.0) for m in _TS_RE.finditer(prompt)]
    bare = {int(m.group(1)) for m in _SHOT_RE.finditer(prompt)}
    if 1 not in bare:
        errs.append("缺少 [Shot 1]")
    ts_ns = [n for n, _ in ts]
    if 1 in ts_ns:
        errs.append("[Shot 1] 不得带时间戳")
    k = max(bare) if bare else 0
    if bare and bare != set(range(1, k + 1)):
        errs.append(f"Shot 编号不连续: {sorted(bare)}")
    if sorted(ts_ns) != list(range(2, k + 1)) and k >= 2:
        errs.append(f"时间戳镜头编号异常: {ts_ns}（应覆盖 2..{k}）")
    secs = [t for _, t in ts]
    if any(b <= a for a, b in zip(secs, secs[1:])):
        errs.append(f"时间戳未严格递增: {secs}")
    out_of_range = [t for t in secs if not (0 < t < duration)]
    if out_of_range:
        errs.append(f"时间戳越界（须 0<t<{duration}s）: {out_of_range}")

    # 4) <d>[Chinese] tag balance
    n_open, n_cn, n_close = (prompt.count("<d>"), prompt.count("<d>[Chinese]"),
                             prompt.count("</d>"))
    if not (n_open == n_cn == n_close):
        errs.append(f"<d>[Chinese] 标签不配平: <d>={n_open}, <d>[Chinese]={n_cn}, </d>={n_close}")

    # 5) verbatim dialogue
    missing = [t for t in dialogue_texts if t not in prompt]
    if missing:
        errs.append(f"对白未逐字出现: {missing}")

    # 6) identity lock
    if IDENTITY_LOCK_PHRASE not in prompt:
        errs.append(f"缺少逐字符身份锁短语 {IDENTITY_LOCK_PHRASE!r}")
    for name in char_names:
        if name not in prompt:
            errs.append(f"身份锁缺少角色名 {name!r}")
    return errs


# ---- LLM call helper -----------------------------------------------------------

def _json_call(label: str, prompt: str, validator, session_id: str, log,
               max_tokens=None, timeout=None):
    """json_mode call -> robust JSON extraction -> validation; one retry with
    the error list appended. Additionally: when the endpoint returns a
    degenerate response under response_format ("non-string content" — the
    spark/vllm combo intermittently emits content:null with 1-5 completion
    tokens on larger prompts), the call is retried once WITHOUT json_mode
    (extract_json handles plain-text output); this fallback does not consume
    a validation attempt. Returns (data, None) or (None, error_str)."""
    errs = []
    use_json_mode = True
    attempt = 0
    while attempt < 2:
        p = prompt
        if attempt:
            p = (
                prompt
                + "\n\n【你上一次的输出未通过校验，问题如下】\n- "
                + "\n- ".join(errs)
                + "\n请修正以上问题，重新输出完整结果（只输出 JSON）。"
            )
        t0 = time.time()
        try:
            raw = llm.chat([{"role": "user", "content": p}], json_mode=use_json_mode,
                           session_id=session_id, max_tokens=max_tokens,
                           timeout=timeout)
            data = llm.extract_json(raw)
        except llm.LLMError as exc:
            msg = f"{exc}"
            if use_json_mode and "non-string content" in msg:
                use_json_mode = False
                log.warning(f"{label}: degenerate response under response_format; "
                            f"retrying without json_mode")
                continue
            errs = [f"LLM 调用或 JSON 解析失败: {msg}"]
            log.warning(f"{label}: attempt {attempt + 1} failed: {errs[0]}")
            attempt += 1
            continue
        log.info(f"{label}: llm ok in {time.time() - t0:.1f}s"
                 + (" (no json_mode)" if not use_json_mode else ""))
        errs = validator(data)
        if not errs:
            return data, None
        log.warning(f"{label}: attempt {attempt + 1} validation errors: {errs}")
        attempt += 1
    return None, "; ".join(errs)


def _prompt_shape(d):
    if isinstance(d, dict) and isinstance(d.get("prompt"), str) and d["prompt"].strip():
        return []
    return ["输出须为 {\"prompt\": \"<完整 prompt 纯文本>\"}"]


def _check_shape(d):
    if not isinstance(d, dict) or not isinstance(d.get("pass"), bool):
        return ["输出须为 {\"pass\": true|false, \"problems\": [...]}"]
    if not isinstance(d.get("problems"), list):
        return ["problems 必须是数组"]
    return []


# ---- context builders -----------------------------------------------------------

def _character_tags(settings: dict, seg_md: str, seg_pages: list) -> tuple:
    """(tags_md, names) for the characters appearing in this segment."""
    blocks, names = [], []
    for c in settings.get("characters") or []:
        if not (isinstance(c, dict) and c.get("name") and c["name"] in seg_md):
            continue
        names.append(c["name"])
        states = []
        seen = set()
        for st in c.get("clothing_states") or []:
            if not isinstance(st, dict):
                continue
            pages = [int(p) for p in (st.get("pages") or [])
                     if isinstance(p, (int, float))]
            if not any(p in pages for p in seg_pages):
                continue
            sig = (st.get("state_cn", ""), st.get("danbooru_tags", ""))
            if sig in seen:
                continue
            seen.add(sig)
            states.append(
                f"- [涉及页 {sorted(set(seg_pages) & set(pages))}] {st.get('state_cn', '')} "
                f"| tags: {st.get('danbooru_tags', '')}"
            )
        anc = c.get("anchor") or {}
        if isinstance(anc, dict) and anc.get("character_name"):
            orig = f" from {anc['anime_origin']}" if anc.get("anime_origin") else ""
            anc_info = f"{anc['character_name']}{orig}"
        elif isinstance(anc, str) and anc.strip():
            anc_info = anc.strip()
        else:
            anc_info = "（未指定）"
        gender_str = "Female (女性)" if c.get("gender") == "female" else "Male (男性)"

        blocks.append(
            f"### {c['name']} (Gender: {gender_str})\n"
            f"- Anchored Classic Anime Character: {anc_info}\n"
            f"- Detailed Appearance (MUST strictly follow, never swap or omit): {c.get('appearance_cn', '')}\n"
            f"- Appearance tags: {c.get('danbooru_tags', '')}\n"
            + ("\n".join(states) if states else "- Clothing: （未定义）")
        )
    return ("\n".join(blocks) or "（本段无登记角色）"), names


def _dialogue_block(dialogue_texts: list) -> str:
    if not dialogue_texts:
        return "（本段无对白）"
    return "\n".join(f"- {t}" for t in dialogue_texts)


# ---- per-segment pipeline -------------------------------------------------------

def _process_segment(proj: Project, seg: dict, settings: dict, style_card: str,
                     session_id: str, log) -> dict:
    out_dir = proj.out_dir("s6")
    seg_no = int(seg["seg"])
    key = f"seg_{seg_no:02d}"
    panels = list(seg["panels"])
    duration = float(seg["duration"])
    seg_md = (proj.out_dir("s5") / "segments" / f"{key}.md").read_text(encoding="utf-8")

    # mode & first frame
    style = proj.params.get("style", "2d")
    mode = "t2va"
    first_frame = None
    if style == "2d":
        ff_rel = f"s4_images/{panels[0]}.png"
        if (proj.dir / ff_rel).exists():
            mode = "i2va"
            first_frame = ff_rel
        else:
            log.warning(f"{key}: style=2d but first frame {ff_rel} missing; "
                        f"downgrading this segment to T2VA")

    # context
    dialogue_texts = []
    for pk in panels:
        l3 = (proj.out_dir("s2") / "03_panels" / f"{pk}.md").read_text(encoding="utf-8")
        dialogue_texts.extend(_dialogue_lines_from_l3(l3))
    seg_pages = sorted({int(pk[1:4]) for pk in panels})
    tags_md, char_names = _character_tags(settings, seg_md, seg_pages)
    if mode == "i2va":
        first_frame_note = (
            f"视频首帧即 <Picture 1>（分镜 {panels[0]} 的 S4 复刻图，画面为该分镜的静态画面）。"
            f"[Shot 1] 从该首帧的画面、构图、人物位置与衣着开始，向前发展后续动作。"
        )
    else:
        first_frame_note = "（T2VA：无首帧，全部画面由文本构建）"

    base_prompt = templates.render(
        "s6_h3",
        MODE=mode,
        I2VA_FIRST_LINE=I2VA_FIRST_LINE if mode == "i2va" else "（T2VA：无首行指令）",
        FIRST_FRAME_NOTE=first_frame_note,
        SEG_KEY=key,
        DURATION=duration,
        SEG_MD=seg_md,
        STYLE_CARD=style_card,
        CHARACTER_TAGS=tags_md,
        DIALOGUE_LINES=_dialogue_block(dialogue_texts),
    )

    # generate (one LLM call per segment; JSON shape retried once by _json_call)
    data, err = _json_call(f"s6 {key} gen", base_prompt, _prompt_shape,
                           session_id, log, timeout=_LLM_CALL_TIMEOUT)
    if data is None:
        raise RuntimeError(f"{key}: H3 prompt generation failed: {err}")
    prompt = data["prompt"].strip()
    # crash checkpoint: a single LLM round costs minutes; persist each round's
    # output so an interrupted run leaves the last state inspectable on disk.
    _atomic_write_text(out_dir / f"{key}.txt", prompt + "\n")

    # check -> fix loop (<= 2 fix rounds)
    problems: list = []
    rounds = 0
    for attempt in range(_MAX_FIX_ROUNDS + 1):
        rounds = attempt
        det = deterministic_checks(prompt, mode, duration, dialogue_texts, char_names)
        if det:
            problems = det
        else:
            check_prompt = templates.render(
                "s6_check",
                SEG_KEY=key, MODE=mode, DURATION=duration, PROMPT=prompt,
                DIALOGUE_LINES=_dialogue_block(dialogue_texts),
                CHARACTER_TAGS=tags_md,
            )
            cdata, cerr = _json_call(f"s6 {key} check", check_prompt,
                                     _check_shape, session_id, log,
                                     timeout=_LLM_CALL_TIMEOUT)
            if cdata is None:
                problems = [f"LLM 自检调用失败: {cerr}"]
            else:
                problems = [] if cdata["pass"] else [
                    str(p) for p in cdata["problems"]
                ]
        if not problems:
            break
        log.warning(f"{key}: round {attempt} problems: {problems}")
        if attempt == _MAX_FIX_ROUNDS:
            break
        fix_prompt = (
            base_prompt
            + "\n\n【上一版 prompt】\n" + prompt
            + "\n\n【检查未通过项】\n- " + "\n- ".join(problems)
            + "\n请逐项修正，输出修正后的完整 prompt（保持全部硬规则与固定句逐字符不变），只输出 JSON。"
        )
        fdata, ferr = _json_call(f"s6 {key} fix{attempt + 1}", fix_prompt,
                                 _prompt_shape, session_id, log,
                                 timeout=_LLM_CALL_TIMEOUT)
        if fdata is None:
            log.error(f"{key}: fix round failed, keeping previous prompt: {ferr}")
        else:
            prompt = fdata["prompt"].strip()
            _atomic_write_text(out_dir / f"{key}.txt", prompt + "\n")  # checkpoint

    passed = not problems
    _atomic_write_text(out_dir / f"{key}.txt", prompt + "\n")
    seg_json = {
        "seg": seg_no,
        "mode": mode,
        "duration": duration,
        "first_frame": first_frame,
        "panels": panels,
        "seed": deterministic_seed(proj.id, key),
    }
    _atomic_write_json(out_dir / f"{key}.json", seg_json)
    proj.set_item("s6", key, "completed",
                  {"mode": mode, "duration": duration, "panels": panels})
    proj.set_item("s6", f"{key}_check",
                  "completed" if passed else "failed",
                  {"pass": passed, "problems": problems, "rounds": rounds})
    log.info(f"{key}: mode={mode} duration={duration}s pass={passed} rounds={rounds}")
    return {"key": key, "mode": mode, "pass": passed, "problems": problems,
            "rounds": rounds}


# ---- stage entry ------------------------------------------------------------------

def run(proj: Project, **opts) -> bool:
    log = proj.get_logger("s6")
    out_dir = proj.out_dir("s6")
    beats_path = proj.out_dir("s5") / "00_beats.json"
    style_path = proj.out_dir("s5") / "00_style.md"
    if not beats_path.exists() or not style_path.exists():
        log.error("s5 outputs missing (00_beats.json / 00_style.md); run s5 first")
        return False
    segs = json.loads(beats_path.read_text(encoding="utf-8"))["segments"]
    if not segs:
        log.error("no segments in s5 beats")
        return False
    settings = json.loads(
        (proj.out_dir("s2") / "00_settings.json").read_text(encoding="utf-8")
    )
    style_card = style_path.read_text(encoding="utf-8")
    sessions = llm.new_session_pool()  # one per DGX; per-segment affinity
    log.info(f"s6 start: {len(segs)} segments, style={proj.params.get('style')}")

    def one(i, seg):
        key = f"seg_{int(seg['seg']):02d}"
        if (proj.item_status("s6", key) == "completed"
                and (out_dir / f"{key}.txt").exists()
                and (out_dir / f"{key}.json").exists()):
            prev = (proj.state["stages"]["s6"]["items"].get(f"{key}_check")
                    or {}).get("data") or {}
            return {"key": key, "mode": "?", "pass": prev.get("pass", None),
                    "problems": prev.get("problems", []), "rounds": prev.get("rounds"),
                    "skipped": True}
        try:
            return _process_segment(proj, seg, settings, style_card,
                                    sessions[i % len(sessions)], log)
        except Exception as exc:  # noqa: BLE001 - mark item failed, continue
            log.error(f"{key}: failed: {type(exc).__name__}: {exc}")
            log.exception("traceback:")
            proj.set_item("s6", key, "failed", {"error": str(exc)})
            return {"key": key, "mode": "?", "pass": False,
                    "problems": [f"generation failed: {exc}"],
                    "rounds": None}

    results = [None] * len(segs)
    with ThreadPoolExecutor(max_workers=max(1, len(sessions))) as ex:
        futs = {ex.submit(one, i, s): i for i, s in enumerate(segs)}
        for fut in as_completed(futs):
            results[futs[fut]] = fut.result()
    failures = sum(1 for r in results if r.get("pass") is False and not r.get("skipped"))

    # check report (always written; UNRESOLVED marks leftovers)
    lines = ["# S6 H3 Prompt 检查报告", ""]
    for r in results:
        tag = "PASS" if r["pass"] else "UNRESOLVED"
        lines.append(f"## {r['key']}：{tag}" + ("（本次跳过，沿用上次结果）" if r.get("skipped") else ""))
        lines.append(f"- 结论: {tag}")
        for p in (r.get("problems") or []):
            lines.append(f"- 问题: {p}")
        if not (r.get("problems") or []):
            lines.append("- 问题: （无）")
        lines.append("")
    (out_dir / "check_report.md").write_text("\n".join(lines), encoding="utf-8")

    log.info(f"s6 summary: {len(results) - failures} ok / {failures} failed")
    # Check leftovers (UNRESOLVED) are recorded in check_report.md and in the
    # "seg_XX_check" items; they do not fail the stage because the prompts are
    # written and a rerun would skip every completed item (no progress). The
    # stage fails only when a prompt could not be generated at all.
    return failures == 0
