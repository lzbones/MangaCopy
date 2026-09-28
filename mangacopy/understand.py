"""S1 understand: two-round per-page manga comprehension.

Round 1 (layout) sends the full page image and gets panel bboxes in Japanese
right-to-left reading order. Round 2 crops each panel (3% of page width/height
extra margin per side, clamped to the page; shape=="full" keeps the whole
page) and extracts fine-grained detail (characters / verbatim dialogue /
action / setting / camera / clothing notes).

Outputs (per page): s1_understand/page_XX.json and
s1_understand/crops/pXXX_YY.png. Checkpoint items: "page_XX" under stage "s1".
A page whose LLM output fails schema validation twice is marked failed and the
stage continues with the remaining pages.
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image

from . import llm, templates
from .project import Project

PANEL_SHAPES = {"vertical", "horizontal", "square", "full"}
_EXPAND = 0.03  # crop margin: 3% of page width/height per side
_CALL_TIMEOUT = 1200  # s; 2026-09-27 用户终版裁定：上限 20 分钟

_STATE_LOCK = threading.Lock()  # Project.set_item is not thread-safe by itself


def _set_item(proj: Project, key: str, status: str, data=None) -> None:
    with _STATE_LOCK:
        proj.set_item("s1", key, status, data)


def _atomic_write_json(path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---- schema validation ------------------------------------------------------

def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_int(v) -> bool:
    if isinstance(v, int) and not isinstance(v, bool):
        return True
    return isinstance(v, float) and v.is_integer()


def _bbox_errors(bbox, where: str) -> list:
    if not isinstance(bbox, list) or len(bbox) != 4:
        return [f"{where}.bbox 必须是 [x,y,w,h] 四元数组"]
    if not all(_is_num(v) for v in bbox):
        return [f"{where}.bbox 各分量必须是数字"]
    x, y, w, h = (float(v) for v in bbox)
    if not (0 <= x <= 1 and 0 <= y <= 1):
        return [f"{where}.bbox 原点越界（须 0~1）: {bbox}"]
    if w <= 0 or h <= 0:
        return [f"{where}.bbox 宽高必须为正: {bbox}"]
    return []


def _validate_layout(data) -> list:
    if not isinstance(data, dict):
        return ["输出必须是 JSON 对象"]
    errs = []
    if not isinstance(data.get("summary"), str) or not data["summary"].strip():
        errs.append("summary 必须是非空字符串")
    if not isinstance(data.get("notes"), str):
        errs.append("notes 必须是字符串（无则空字符串）")
    panels = data.get("panels")
    if not isinstance(panels, list) or not panels:
        errs.append("panels 必须是非空数组")
        return errs
    nos = []
    for i, p in enumerate(panels):
        where = f"panels[{i}]"
        if not isinstance(p, dict):
            errs.append(f"{where} 必须是对象")
            continue
        if not _is_int(p.get("no")) or int(p["no"]) < 1:
            errs.append(f"{where}.no 必须是 >=1 的整数")
        else:
            nos.append(int(p["no"]))
        errs += _bbox_errors(p.get("bbox"), where)
        if p.get("shape") not in PANEL_SHAPES:
            errs.append(f"{where}.shape 必须是 vertical/horizontal/square/full 之一")
        if not isinstance(p.get("reason"), str):
            errs.append(f"{where}.reason 必须是字符串")
    if len(nos) != len(set(nos)):
        errs.append("panels 的 no 必须互不相同")
    return errs


_DETAIL_KEYS = ("characters", "dialogue", "onomatopoeia",
                "action", "setting", "camera", "clothing_notes")


def _validate_detail(data) -> list:
    if not isinstance(data, dict):
        return ["输出必须是 JSON 对象"]
    missing = [k for k in _DETAIL_KEYS if k not in data]
    if missing:
        return [f"缺少字段 {k}" for k in missing]
    errs = []
    for k in ("characters", "dialogue", "onomatopoeia"):
        if not isinstance(data[k], list):
            errs.append(f"{k} 必须是数组（无则空数组）")
    for k in ("action", "setting", "camera", "clothing_notes"):
        if not isinstance(data[k], str):
            errs.append(f"{k} 必须是字符串")
    for i, c in enumerate(data["characters"] if isinstance(data["characters"], list) else []):
        if not isinstance(c, dict) or not isinstance(c.get("name"), str):
            errs.append(f"characters[{i}] 必须是含 name 的对象")
    for i, t in enumerate(data["dialogue"] if isinstance(data["dialogue"], list) else []):
        if not isinstance(t, dict) or not isinstance(t.get("text"), str):
            errs.append(f"dialogue[{i}] 必须是含 text 的对象")
    return errs


def _coerce_detail(data: dict) -> dict:
    """Normalize a validated detail dict (types, '?' speakers, empty slots)."""

    def _s(v) -> str:
        return v if isinstance(v, str) else ("" if v is None else str(v))

    data["characters"] = [
        {"name": _s(c.get("name")) or "?", "appearance": _s(c.get("appearance"))}
        if isinstance(c, dict) else {"name": _s(c) or "?", "appearance": ""}
        for c in data.get("characters") or []
    ]
    data["dialogue"] = [
        {"speaker": _s(t.get("speaker")) or "?", "text": _s(t.get("text"))}
        if isinstance(t, dict) else {"speaker": "?", "text": _s(t)}
        for t in data.get("dialogue") or []
    ]
    ono = data.get("onomatopoeia")
    if isinstance(ono, str):
        ono = [ono] if ono.strip() else []
    data["onomatopoeia"] = [_s(o) for o in (ono or []) if _s(o)]
    for k in ("action", "setting", "camera", "clothing_notes"):
        data[k] = _s(data.get(k))
    return data


# ---- LLM call with one schema-failure retry ---------------------------------

def _json_call(label, prompt, images, validator, session_id, log, max_tokens=None):
    """json_mode call -> robust JSON extraction -> schema validation; on
    failure retry once with the error list appended. Returns (data, None) or
    (None, error_message)."""
    errs = []
    for attempt in range(2):
        p = prompt
        if attempt:
            p = (
                prompt
                + "\n\n【你上一次的输出未通过校验，问题如下】\n- "
                + "\n- ".join(errs)
                + "\n请修正以上问题，重新输出完整 JSON（只输出 JSON）。"
            )
        t0 = time.time()
        try:
            if images:
                raw = llm.chat_vision(p, images, json_mode=True,
                                      session_id=session_id, max_tokens=max_tokens,
                                      timeout=_CALL_TIMEOUT)
            else:
                raw = llm.chat([{"role": "user", "content": p}], json_mode=True,
                               session_id=session_id, max_tokens=max_tokens)
            data = llm.extract_json(raw)
        except llm.LLMError as exc:
            errs = [f"LLM 调用或 JSON 解析失败: {exc}"]
            log.warning(f"{label}: attempt {attempt + 1} failed: {errs[0]}")
            continue
        log.info(f"{label}: llm ok in {time.time() - t0:.1f}s")
        errs = validator(data)
        if not errs:
            return data, None
        log.warning(f"{label}: attempt {attempt + 1} schema errors: {errs}")
    return None, "; ".join(errs)


# ---- cropping ---------------------------------------------------------------

def _crop_panel(img: Image.Image, bbox, shape: str, panel_key: str,
                crops_dir) -> str:
    """Crop one panel (with 3% margin, clamped; 'full' keeps the whole page),
    save crops/<panel_key>.png, return the relative path for the page JSON."""
    w, h = img.size
    if shape == "full":
        box = (0, 0, w, h)
    else:
        x, y, pw, ph = (float(v) for v in bbox)
        x = max(0.0, x - _EXPAND)
        y = max(0.0, y - _EXPAND)
        right = min(1.0, x + pw + 2 * _EXPAND)
        bottom = min(1.0, y + ph + 2 * _EXPAND)
        left = int(round(x * w))
        top = int(round(y * h))
        right = max(left + 1, min(w, int(round(right * w))))
        bottom = max(top + 1, min(h, int(round(bottom * h))))
        box = (left, top, right, bottom)
    rel = f"crops/{panel_key}.png"
    img.crop(box).save(crops_dir / f"{panel_key}.png")
    return rel


# ---- per-page pipeline ------------------------------------------------------

def _process_page(proj: Project, page_meta: dict, session_id: str, log,
                  concurrency: int) -> bool:
    page = page_meta["index"]
    item = f"page_{page:02d}"
    panel_key_prefix = f"p{page:03d}"
    src = proj.out_dir("s0") / page_meta["file"]
    out_dir = proj.out_dir("s1")
    crops_dir = out_dir / "crops"
    t0 = time.time()

    # Round 1: full-page layout
    prompt = templates.render("s1_page_layout", PAGE_NO=page)
    data, err = _json_call(f"s1 p{page} layout", prompt, [src], _validate_layout,
                           session_id, log)
    if data is None:
        _set_item(proj, item, "failed", {"error": f"layout: {err}"})
        log.error(f"page {page}: layout failed: {err}")
        return False

    panels = sorted(data["panels"], key=lambda q: int(q["no"]))
    for q in panels:
        q["no"] = int(q["no"])
        x, y, w, h = (float(v) for v in q["bbox"])
        q["bbox"] = [min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0),
                     min(max(w, 0.005), 1.0), min(max(h, 0.005), 1.0)]

    # Round 2: per-panel crops + detail (concurrent)
    def one_panel(q):
        key = f"{panel_key_prefix}_{q['no']:02d}"
        with Image.open(src) as im:
            rel = _crop_panel(im, q["bbox"], q["shape"], key, crops_dir)
        prompt2 = templates.render("s1_panel_detail",
                                   PAGE_NO=page, PANEL_NO=q["no"])
        d2, e2 = _json_call(f"s1 {key} detail", prompt2,
                            [crops_dir / f"{key}.png"], _validate_detail,
                            session_id, log)
        if d2 is None:
            return key, None, e2
        return key, _coerce_detail(d2), None

    details, failures = {}, []
    with ThreadPoolExecutor(
        max_workers=min(concurrency, max(1, len(panels)))
    ) as ex:
        for fut in as_completed([ex.submit(one_panel, q) for q in panels]):
            key, detail, err = fut.result()
            if detail is None:
                failures.append(f"{key}: {err}")
            else:
                details[key] = detail

    if failures:
        _set_item(proj, item, "failed", {"error": "panel detail: " + "; ".join(failures)})
        log.error(f"page {page}: {len(failures)}/{len(panels)} panel details failed")
        return False

    page_json = {
        "page": page,
        "file": page_meta["file"],
        "summary": data["summary"],
        "notes": data.get("notes", ""),
        "panels": [
            {
                "no": q["no"],
                "key": f"{panel_key_prefix}_{q['no']:02d}",
                "bbox": q["bbox"],
                "shape": q["shape"],
                "crop": f"crops/{panel_key_prefix}_{q['no']:02d}.png",
                "detail": details[f"{panel_key_prefix}_{q['no']:02d}"],
            }
            for q in panels
        ],
    }
    _atomic_write_json(out_dir / f"page_{page:02d}.json", page_json)
    _set_item(proj, item, "completed", {"panels": len(panels)})
    log.info(f"page {page}: done, {len(panels)} panels, {time.time() - t0:.1f}s")
    return True


# ---- stage entry ------------------------------------------------------------

def run(proj: Project, **opts) -> bool:
    log = proj.get_logger("s1")
    manifest = json.loads(
        (proj.out_dir("s0") / "manifest.json").read_text(encoding="utf-8")
    )
    by_index = {pg["index"]: pg for pg in manifest["pages"]}
    sel = sorted(set(int(p) for p in (opts.get("pages") or sorted(by_index))))
    unknown = [p for p in sel if p not in by_index]
    if unknown:
        log.warning(f"pages not in manifest, ignored: {unknown}")
    sel = [p for p in sel if p in by_index]
    if not sel:
        log.error("no pages to process")
        return False

    concurrency = max(1, int(opts.get("concurrency", 2)))
    crops_dir = proj.out_dir("s1") / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)

    todo, skipped = [], []
    for p in sel:
        item = f"page_{p:02d}"
        if (proj.item_status("s1", item) == "completed"
                and (proj.out_dir("s1") / f"page_{p:02d}.json").exists()):
            skipped.append(p)
        else:
            todo.append(p)
    if skipped:
        log.info(f"skip completed pages: {skipped}")

    ok = fail = 0
    t0 = time.time()
    sessions = llm.new_session_pool()  # one per DGX; each page sticks to one
    if todo:
        with ThreadPoolExecutor(max_workers=min(concurrency, len(todo))) as ex:
            futs = [ex.submit(_process_page, proj, by_index[p],
                              sessions[i % len(sessions)], log, concurrency)
                    for i, p in enumerate(todo)]
            for fut in as_completed(futs):
                if fut.result():
                    ok += 1
                else:
                    fail += 1
    log.info(
        f"s1 summary: {ok} ok / {fail} failed / {len(skipped)} skipped, "
        f"{time.time() - t0:.1f}s total"
    )
    # 2026-09-28 user-approved: partial failure must FAIL the stage so the DAG
    # retries it (old ">=1 page ok" criterion stranded failed pages forever)
    return ok == len(todo)
