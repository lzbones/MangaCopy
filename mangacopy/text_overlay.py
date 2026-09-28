"""S4b text_overlay: typeset dialogue/onomatopoeia onto generated panels.

The image model cannot render legible text, so the textless panel from S4 is
kept untouched and a typeset copy is produced (user 2026-09-26 requirement):
- WHAT to write: s1_understand/page_XX.json — verbatim Chinese dialogue
  (with speakers) + onomatopoeia for that exact panel;
- WHERE to write: one vision-LLM call per panel (prompts/s4b_overlay.md)
  returns normalized boxes per line; invalid LLM output (or opts["no_llm"])
  falls back to deterministic top-right stacking in right-to-left manga
  reading order;
- HOW it is drawn: PIL + PingFang (config.TEXT_FONT_PATH) — dialogue =
  white rounded-rect bubble with black outline + wrapped text (+ optional
  tail toward the speaker); speaker "旁白" = bordered narration box;
  onomatopoeia = large stroked text near the action.
Outputs: s4_text/pXXX_YY.png (typeset). The textless s4_images/pXXX_YY.png
is never modified. Panels without any dialogue/sfx are marked completed with
lines=0 and produce no file.
opts: panels (subset), concurrency (default 3), no_llm (fallback layout
only). Zero ComfyUI calls.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import config, llm, templates
from .project import Project

_STATE_LOCK = threading.Lock()
PANEL_RE = "p%03d_%02d"


def _panel_lines(proj: Project, key: str) -> list:
    """Verbatim dialogue + onomatopoeia for one panel from the S1 output."""
    try:
        page = int(key[1:4])
        pjson = json.loads(
            (proj.out_dir("s1") / f"page_{page:02d}.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return []
    detail = next((p.get("detail", {}) for p in pjson.get("panels", [])
                   if p.get("key") == key), {})
    lines = []
    for i, d in enumerate(detail.get("dialogue", []) or []):
        text = str(d.get("text", "")).strip()
        if not text:
            continue
        speaker = str(d.get("speaker", "")).strip()
        kind = "narration" if "旁白" in speaker else "bubble"
        lines.append({"idx": len(lines), "type": kind, "text": text,
                      "speaker": speaker})
    for s in detail.get("onomatopoeia", []) or []:
        s = str(s).strip()
        if s:
            lines.append({"idx": len(lines), "type": "sfx", "text": s,
                          "speaker": ""})
    return lines


def _llm_placement(img_path: Path, lines: list, session_id: str, log) -> list | None:
    """Ask the vision LLM where each line goes. Returns placement dicts
    (normalized coords) or None on any failure."""
    text = templates.render(
        "s4b_overlay", LINES_JSON=json.dumps(lines, ensure_ascii=False, indent=1)
    )
    try:
        raw = llm.chat_vision(text, [img_path], json_mode=True,
                              session_id=session_id)
        data = llm.extract_json(raw)
    except llm.LLMError as exc:
        log.warning(f"{img_path.stem}: placement LLM failed: {str(exc)[:160]}")
        return None
    placements = data.get("placements") if isinstance(data, dict) else data
    if not isinstance(placements, list) or len(placements) != len(lines):
        return None
    out = []
    for p in placements:
        try:
            x, y = float(p["x"]), float(p["y"])
            w, h = float(p["w"]), float(p["h"])
            idx = int(p["idx"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < w <= 1 and 0 < h <= 1
                and x + w <= 1.01 and y + h <= 1.01):
            return None
        tail = p.get("tail")
        if tail is not None:
            try:
                tail = [float(tail[0]), float(tail[1])]
            except (TypeError, ValueError, IndexError):
                tail = None
        out.append({"idx": idx, "type": str(p.get("type", "bubble")),
                    "x": x, "y": y, "w": w, "h": h, "tail": tail})
    return sorted(out, key=lambda p: p["idx"])


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(config.TEXT_FONT_PATH, size,
                              index=config.TEXT_FONT_INDEX)


def _wrap(text: str, font, max_w: float) -> list:
    """Character-level wrap (CJK-safe); explicit newlines preserved; latin
    runs are kept unbroken when they fit."""
    lines = []
    for para in text.split("\n"):
        cur = ""
        for ch in para:
            if ch.isascii() and ch.isalnum():
                cur += ch
                continue
            if cur and font.getlength(cur + ch) > max_w:
                lines.append(cur)
                cur = ch if ch.strip() else ""
            else:
                cur += ch
        if cur:
            lines.append(cur)
    return lines or [""]


def _draw_box(draw: ImageDraw.ImageDraw, box: tuple, text: str, font,
              kind: str, tail: list | None, W: int, H: int) -> None:
    """Bubble / narration box with wrapped text; expands if text overflows."""
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    pad = max(10, font.size)
    wrapped = _wrap(text, font, bw - 2 * pad)
    line_h = font.size * 1.35
    need_h = len(wrapped) * line_h + 2 * pad
    if need_h > bh:  # expand vertically, keep the box center
        cy = (y0 + y1) / 2
        y0, y1 = cy - need_h / 2, cy + need_h / 2
    radius = font.size if kind == "bubble" else 2
    draw.rounded_rectangle((x0, y0, x1, y1), radius=radius,
                           fill="white", outline="black",
                           width=max(3, int((y1 - y0) * 0.006) + 3))
    if tail and kind == "bubble":
        tx, ty = tail[0] * W, tail[1] * H
        bx = x0 + (x1 - x0) * 0.5
        edge_y = y1 if ty >= (y0 + y1) / 2 else y0
        draw.polygon([(bx - (x1 - x0) * 0.15, edge_y),
                      (bx + (x1 - x0) * 0.15, edge_y), (tx, ty)],
                     fill="white", outline="black")
    ty = (y0 + y1 - len(wrapped) * line_h) / 2 + font.size * 0.28
    for ln in wrapped:
        w = font.getlength(ln)
        draw.text(((x0 + x1 - w) / 2, ty), ln, font=font,
                  fill="black", stroke_width=1, stroke_fill="black")
        ty += line_h


def _fallback_placements(lines: list, W: int, H: int, font, sfx_font) -> list:
    """Deterministic layout: bubbles top-right stacked downward (right-to-left
    manga order), narration top-left, sfx bottom-left."""
    out = []
    margin, gap = int(W * 0.03), int(H * 0.02)
    y_bubble, y_narr = margin, margin
    for ln in lines:
        f = sfx_font if ln["type"] == "sfx" else font
        wrapped = _wrap(ln["text"], f, W * 0.36)
        line_h = f.size * 1.35
        bw = min(W * 0.42,
                 max(f.getlength(max(wrapped, key=f.getlength)) + f.size * 2,
                     W * 0.18))
        bh = len(wrapped) * line_h + f.size * 2
        if ln["type"] == "sfx":
            out.append({"idx": ln["idx"], "type": "sfx",
                        "x": margin / W, "y": (H - bh - margin) / H,
                        "w": bw / W, "h": bh / H, "tail": None})
        elif ln["type"] == "narration":
            out.append({"idx": ln["idx"], "type": "narration",
                        "x": margin / W, "y": y_narr / H,
                        "w": bw / W, "h": bh / H, "tail": None})
            y_narr += bh + gap
        else:
            out.append({"idx": ln["idx"], "type": "bubble",
                        "x": (W - bw - margin) / W, "y": y_bubble / H,
                        "w": bw / W, "h": bh / H, "tail": None})
            y_bubble += bh + gap
    return out


def _process_panel(proj: Project, key: str, lines: list,
                   session_id: str, no_llm: bool, log) -> str:
    img_path = proj.out_dir("s4") / f"{key}.png"
    img = Image.open(img_path).convert("RGB")
    W, H = img.size
    base = max(24, min(64, int(H * config.TEXT_BASE_SIZE_RATIO)))
    font, sfx_font = _font(base), _font(int(base * 1.7))

    placements = None if no_llm else _llm_placement(img_path, lines, session_id, log)
    layout = "llm"
    if placements is None:
        placements = _fallback_placements(lines, W, H, font, sfx_font)
        layout = "fallback"
    by_idx = {ln["idx"]: ln for ln in lines}

    draw = ImageDraw.Draw(img)
    for p in placements:
        ln = by_idx[p["idx"]]
        box = (p["x"] * W, p["y"] * H, (p["x"] + p["w"]) * W, (p["y"] + p["h"]) * H)
        if p["type"] == "sfx":
            draw.text((box[0], box[1]), ln["text"], font=sfx_font, fill="black",
                      stroke_width=max(3, sfx_font.size // 14), stroke_fill="white")
        else:
            _draw_box(draw, box, ln["text"], font, p["type"],
                      p.get("tail"), W, H)
    out = proj.out_dir("s4b") / f"{key}.png"
    img.save(out)
    log.info(f"{key}: typeset ({len(lines)} lines, {layout}) -> {out.name}")
    return "completed"


def _typeset_batch(proj: Project, targets: list, sessions: list,
                   no_llm: bool, log) -> tuple:
    """Typeset the given panel keys (pool). Returns (ok, empty, fail)."""
    out_dir = proj.out_dir("s4b")
    ok = fail = empty = 0
    if not targets:
        return ok, empty, fail
    with ThreadPoolExecutor(max_workers=int(2)) as ex:
        futs = {}
        for i, key in enumerate(targets):
            lines = _panel_lines(proj, key)
            if not lines:
                with _STATE_LOCK:
                    proj.set_item("s4b", key, "completed", {"lines": 0})
                    empty += 1
                log.info(f"{key}: no dialogue/sfx, skip typesetting")
                continue
            futs[ex.submit(_process_panel, proj, key, lines,
                           sessions[i % len(sessions)], no_llm, log)] = key
        for fut in as_completed(futs):
            key = futs[fut]
            try:
                if fut.result() == "completed":
                    ok += 1
                    with _STATE_LOCK:
                        proj.set_item("s4b", key, "completed", {"typeset": True})
            except Exception as exc:  # noqa: BLE001 - one panel must not kill the stage
                fail += 1
                log.error(f"{key}: typeset failed: {type(exc).__name__}: {exc}")
                with _STATE_LOCK:
                    proj.set_item("s4b", key, "failed", {"error": str(exc)[:300]})
    return ok, empty, fail


def run(proj: Project, **opts) -> bool:
    log = proj.get_logger("s4b")
    img_dir, out_dir = proj.out_dir("s4"), proj.out_dir("s4b")
    out_dir.mkdir(parents=True, exist_ok=True)

    no_llm = bool(opts.get("no_llm"))
    sessions = llm.new_session_pool()

    def _sel_filter(p):
        return opts.get("panels") is None or p.stem in opts["panels"]

    def _typed(key):
        if proj.item_status("s4b", key) != "completed":
            return False
        item_data = (proj.state.get("stages", {}).get("s4b", {}).get("items", {}).get(key) or {}).get("data") or {}
        if item_data.get("lines") == 0:
            return True
        return (out_dir / f"{key}.png").exists()

    # ---- incremental pipeline consumption (2026-09-28 user 统筹 directive) ----
    # s4b no longer waits for the s4 STAGE barrier: it repeatedly typesets
    # panels whose s4 item has reached a FINAL state (completed /
    # needs_review — their image will not be regenerated), polling until s4
    # is terminal and nothing final remains un-typeset. This overlaps the
    # spark-bound typesetting with the spark-bound validation work.
    total_ok = total_empty = total_fail = 0
    deadline = time.time() + 7200  # 2 h safety cap
    pass_no = 0
    while True:
        panels = [p.stem for p in sorted(img_dir.glob("*.png")) if _sel_filter(p)]
        if not panels and proj.stage_status("s4") == "pending":
            log.error("no panel images in s4_images/ (run s4 first)")
            return False
        s4_terminal = proj.stage_status("s4") in ("completed", "failed")
        if s4_terminal:
            # final sweep: typeset every remaining image (incl. failed-item
            # panels whose image exists), matching the old stage-barrier pass
            targets = [k for k in panels if not _typed(k)]
        else:
            # incremental: only panels whose s4 item is FINAL (image stable)
            targets = [k for k in panels
                       if proj.item_status("s4", k) in ("completed", "needs_review")
                       and not _typed(k)]
        if targets:
            pass_no += 1
            log.info(f"s4b pass {pass_no}: typesetting {len(targets)} "
                     f"final-state panels (s4 {'terminal' if s4_terminal else 'in progress'})")
            ok, empty, fail = _typeset_batch(proj, targets, sessions, no_llm, log)
            total_ok += ok
            total_empty += empty
            total_fail += fail
        if s4_terminal and not targets:
            break  # everything that can be typeset is typeset
        if time.time() > deadline:
            log.warning("s4b: 2 h incremental cap reached — exiting partial")
            break
        if not s4_terminal:
            time.sleep(30)  # wait for s4 validations to finalize more panels

    log.info(f"s4b summary: completed {total_ok} / empty {total_empty} / "
             f"failed {total_fail} in {pass_no} pass(es)")
    return (total_ok + total_empty) > 0 and total_fail == 0
