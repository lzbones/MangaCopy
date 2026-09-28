"""S2 zero_script: top-down constrained reproduction script.

Layers, each frozen before the next one is generated:
- L0 settings: chunked extraction over the S1 outputs (chunk = opts["chunk"],
  default 4 pages) then one merge -> s2_zero/00_settings.json + 00_settings.md.
- L1 overview: whole-chapter retelling (context = L0 digest + per-page S1
  summaries) -> 01_overview.md.
- L2 pages: sequential per-page plot threads (context = L0 digest + L1 +
  previous page summary) -> 02_pages/page_XX.md.
- L3 panels: per-panel zero script, concurrent (context = relevant character
  cards + the page L2 text + the S1 panel detail) -> 03_panels/pXXX_YY.md.
- check (hard requirement): per-page (s2_check_page) + global (s2_check_global)
  LLM consistency checks; files named by the checker are auto-revised for at
  most 2 revise rounds; residual issues are written to s2_check/report.md
  marked UNRESOLVED.

Checkpoint items under stage "s2": "l0", "l1", "page_XX" (L2), "pXXX_YY" (L3),
"check". A unit whose LLM output fails schema validation twice is marked
failed and the stage continues; completed units are skipped on rerun.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import llm, templates
from .project import Project

_L1_HEADERS = ["# 总体复述", "# 氛围基调", "# 叙事逻辑链", "# 人物关系"]
_L2_HEADERS = ["# 剧情脉络", "# 分镜顺序与衔接", "# 与前后页的承接"]
_L3_HEADERS = [
    "## 分镜编号与位置", "## 出场人物", "## 动作与姿态", "## 空间关系与构图",
    "## 镜头角度", "## 背景环境", "## 对白原文", "## 拟声词", "## 氛围与情绪", "## 备注",
]
_GENDERS = {"male", "female", "unknown"}
_MAX_REVISE_ROUNDS = 2
_CALL_TIMEOUT = 1200  # s; 2026-09-27 用户终版裁定：上限 20 分钟；大输入任务另靠拆解（树形归并）解决

_STATE_LOCK = threading.Lock()  # Project.set_item is not thread-safe by itself


def _set_item(proj: Project, key: str, status: str, data=None) -> None:
    with _STATE_LOCK:
        proj.set_item("s2", key, status, data)


def _atomic_write_text(path, text: str) -> None:
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _atomic_write_json(path, obj) -> None:
    _atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def _load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---- LLM call with one schema-failure retry ---------------------------------

def _json_call(label, prompt, validator, session_id, log, max_tokens=None,
               repair=None):
    """json_mode call -> robust JSON extraction -> schema validation; on
    failure retry once with the error list appended. `repair(data)` may return
    (fixed_data, note) to salvage outputs that only fail on coverage/interval
    sloppiness; the repaired value must pass validation. Returns (data, None)
    or (None, error_message)."""
    errs = []
    for attempt in range(2):
        p = prompt
        if attempt:
            shown = errs[:10]
            more = f"\n- ……（其余 {len(errs) - len(shown)} 项略）" if len(errs) > 10 else ""
            p = (
                prompt
                + "\n\n【你上一次的输出未通过校验，问题如下】\n- "
                + "\n- ".join(shown) + more
                + "\n请修正以上问题，重新输出完整 JSON（只输出 JSON，结构与字段名保持不变）。"
            )
        t0 = time.time()
        try:
            raw = llm.chat([{"role": "user", "content": p}], json_mode=True,
                           session_id=session_id, max_tokens=max_tokens,
                           timeout=_CALL_TIMEOUT)
            data = llm.extract_json(raw)
        except llm.LLMError as exc:
            errs = [f"LLM 调用或 JSON 解析失败: {exc}"]
            log.warning(f"{label}: attempt {attempt + 1} failed: {errs[0]}")
            continue
        log.info(f"{label}: llm ok in {time.time() - t0:.1f}s")
        errs = validator(data)
        if not errs:
            return data, None
        if repair is not None:
            fixed, note = repair(data)
            if fixed is not None and not validator(fixed):
                log.info(f"{label}: deterministic repair applied: {note}")
                return fixed, None
        log.warning(f"{label}: attempt {attempt + 1} schema errors: {errs}")
    return None, "; ".join(errs)


# ---- page interval helpers ---------------------------------------------------

def _parse_interval(s):
    """'1-3' / '4' / '1-3,5' -> [(1,3),(4,4),(5,5)]. Unparseable parts ignored."""
    out = []
    if not isinstance(s, str):
        return out
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*(?:[-–~—]\s*(\d+))?", part)
        if m:
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else a
            out.append((min(a, b), max(a, b)))
    return out


def _interval_pages(ivs):
    pages = set()
    for a, b in ivs:
        pages.update(range(a, b + 1))
    return pages


def _range_str(pages) -> str:
    """[1,2,3,5] -> '1-3,5'."""
    pages = sorted(set(int(p) for p in pages))
    parts, i = [], 0
    while i < len(pages):
        j = i
        while j + 1 < len(pages) and pages[j + 1] == pages[j] + 1:
            j += 1
        parts.append(str(pages[i]) if i == j else f"{pages[i]}-{pages[j]}")
        i = j + 1
    return ",".join(parts)


# ---- settings (L0) validation / coercion / rendering ------------------------

def _coverage_errors(data, pages):
    """clothing_states 区间并集必须覆盖全部页码，且区间互不重叠。"""
    errs = []
    pset = set(int(p) for p in pages)
    chars = data.get("characters")
    if not isinstance(chars, list):
        return errs
    for c in chars:
        if not isinstance(c, dict):
            continue
        name = c["name"] if isinstance(c.get("name"), str) else "?"
        sts = c.get("clothing_states")
        if not isinstance(sts, list):
            continue
        ivs = []
        for st in sts:
            if isinstance(st, dict) and isinstance(st.get("pages"), str):
                ivs += _parse_interval(st["pages"])
        if not ivs:
            continue
        missing = sorted(pset - _interval_pages(ivs))
        if missing:
            errs.append(f"人物 {name} 的 clothing_states 区间未覆盖页 {missing}")
        ivs.sort()
        for j in range(1, len(ivs)):
            if ivs[j][0] <= ivs[j - 1][1]:
                errs.append(
                    f"人物 {name} 的 clothing_states 区间重叠：{ivs[j - 1]} 与 {ivs[j]}"
                )
    return errs


def _validate_settings(data, pages=None, require_coverage=True):
    """Validate settings structure; with require_coverage, additionally check
    that every character's clothing_states intervals cover `pages` without
    overlap. Coverage is only checked on structurally valid output."""
    errs = _validate_settings_struct(data)
    if require_coverage and pages is not None and not errs:
        errs += _coverage_errors(data, pages)
    return errs


def _validate_settings_struct(data):
    if not isinstance(data, dict):
        return ["输出必须是 JSON 对象"]
    errs = []
    chars = data.get("characters")
    if not isinstance(chars, list) or not chars:
        errs.append("characters 必须是非空数组")
        chars = []
    names = []
    for i, c in enumerate(chars):
        w = f"characters[{i}]"
        if not isinstance(c, dict):
            errs.append(f"{w} 必须是对象")
            continue
        if not isinstance(c.get("name"), str) or not c["name"].strip():
            errs.append(f"{w}.name 必须是非空字符串")
        else:
            names.append(c["name"].strip())
        if "aliases" not in c:
            errs.append(f"{w} 缺少 aliases")
        elif not isinstance(c["aliases"], list):
            errs.append(f"{w}.aliases 必须是数组（无别名则空数组）")
        if c.get("gender") not in _GENDERS:
            errs.append(f"{w}.gender 必须是 male/female/unknown")
        for k in ("appearance_cn", "danbooru_tags", "anchor"):
            if not isinstance(c.get(k), str):
                errs.append(f"{w}.{k} 必须是字符串")
        sts = c.get("clothing_states")
        if not isinstance(sts, list) or not sts:
            errs.append(f"{w}.clothing_states 必须是非空数组")
            continue
        for j, st in enumerate(sts):
            ww = f"{w}.clothing_states[{j}]"
            if not isinstance(st, dict):
                errs.append(f"{ww} 必须是对象")
                continue
            if not isinstance(st.get("pages"), str) or not _parse_interval(st["pages"]):
                errs.append(f"{ww}.pages 必须是页码区间串（如 '1-3' 或 '4'）")
            if not isinstance(st.get("state_cn"), str) or not st["state_cn"].strip():
                errs.append(f"{ww}.state_cn 必须是非空字符串")
            if not isinstance(st.get("danbooru_tags"), str):
                errs.append(f"{ww}.danbooru_tags 必须是字符串")
    if len(names) != len(set(names)):
        errs.append("characters 的 name 必须互不相同")
    envs = data.get("environments")
    if not isinstance(envs, list):
        errs.append("environments 必须是数组（无则空数组）")
    else:
        for i, e in enumerate(envs):
            if not isinstance(e, dict) or not isinstance(e.get("name"), str) or not e["name"].strip():
                errs.append(f"environments[{i}] 必须是含非空 name 的对象")
            elif not (isinstance(e.get("desc_cn"), str) and isinstance(e.get("danbooru_tags"), str)):
                errs.append(f"environments[{i}].desc_cn/danbooru_tags 必须是字符串")
    if not isinstance(data.get("style_notes"), str):
        errs.append("style_notes 必须是字符串")
    return errs


def _coerce_settings(data) -> dict:
    def _s(v):
        return v if isinstance(v, str) else ("" if v is None else str(v))

    def _ss(v):
        if isinstance(v, str):
            v = [v] if v.strip() else []
        return [_s(x).strip() for x in (v or []) if isinstance(x, str) and _s(x).strip()] \
            if isinstance(v, list) else []

    chars = []
    for c in data.get("characters") or []:
        if not isinstance(c, dict):
            continue
        sts = []
        for st in c.get("clothing_states") or []:
            if not isinstance(st, dict):
                continue
            sts.append({
                "pages": _s(st.get("pages")).strip(),
                "state_cn": _s(st.get("state_cn")),
                "danbooru_tags": _s(st.get("danbooru_tags")),
            })
        chars.append({
            "name": _s(c.get("name")).strip(),
            "aliases": _ss(c.get("aliases")),
            "gender": c.get("gender") if c.get("gender") in _GENDERS else "unknown",
            "appearance_cn": _s(c.get("appearance_cn")),
            "danbooru_tags": _s(c.get("danbooru_tags")),
            "anchor": _s(c.get("anchor")),
            "clothing_states": sts,
        })
    envs = []
    for e in data.get("environments") or []:
        if not isinstance(e, dict):
            continue
        envs.append({
            "name": _s(e.get("name")).strip(),
            "desc_cn": _s(e.get("desc_cn")),
            "danbooru_tags": _s(e.get("danbooru_tags")),
        })
    return {
        "characters": chars,
        "environments": envs,
        "style_notes": _s(data.get("style_notes")),
    }


def _settings_digest(settings: dict) -> str:
    lines = ["人物："]
    for c in settings["characters"]:
        alias = f"（别名：{'、'.join(c['aliases'])}）" if c["aliases"] else ""
        clothing = "；".join(
            f"第{st['pages']}页 {st['state_cn']}" for st in c["clothing_states"]
        ) or "无记录"
        lines.append(f"- {c['name']}{alias}：{c['appearance_cn']}；衣着：{clothing}")
    lines.append("环境：")
    for e in settings["environments"]:
        lines.append(f"- {e['name']}：{e['desc_cn']}")
    lines.append(f"画风：{settings['style_notes']}")
    return "\n".join(lines)


def _render_settings_md(settings: dict) -> str:
    lines = ["# 全话设定（L0，已冻结）", "", "## 人物", ""]
    for c in settings["characters"]:
        lines.append(f"### {c['name']}")
        if c["aliases"]:
            lines.append(f"- 别名：{'、'.join(c['aliases'])}")
        lines.append(f"- 性别：{c['gender']}")
        lines.append(f"- 外貌：{c['appearance_cn'] or '（未记录）'}")
        lines.append(f"- 外貌标签（danbooru）：{c['danbooru_tags'] or '（无）'}")
        if c["anchor"]:
            lines.append(f"- 相似角色锚点：{c['anchor']}")
        lines.append("- 衣着状态机：")
        for st in c["clothing_states"]:
            lines.append(f"  - 第 {st['pages']} 页：{st['state_cn']}（{st['danbooru_tags']}）")
        lines.append("")
    lines += ["## 环境", ""]
    for e in settings["environments"]:
        lines.append(f"### {e['name']}")
        lines.append(f"- 描述：{e['desc_cn']}")
        lines.append(f"- 标签（danbooru）：{e['danbooru_tags']}")
        lines.append("")
    lines += ["## 画风备注", "", settings["style_notes"] or "（无）", ""]
    return "\n".join(lines)


# ---- deterministic interval repair (merge fallback) --------------------------

def _normalize_intervals(settings: dict, pages) -> tuple:
    """Rebuild every character's clothing_states into a seamless, non-overlapping
    interval machine covering `pages`. Semantics: clothing persists until the
    next stated change — internal gaps take the previous state, leading/trailing
    gaps extend the first/last known state (harmless for pages where the
    character does not appear). Returns (new_settings, repaired_names)."""
    all_pages = sorted(set(int(p) for p in pages))
    if not all_pages:
        return settings, []
    lo, hi = all_pages[0], all_pages[-1]
    repaired, chars = [], []
    for c in settings["characters"]:
        chars.append(c)
        sts = c["clothing_states"]
        items = []  # (start, end, state_index)
        for i, s in enumerate(sts):
            for a, b in _parse_interval(s["pages"]):
                if b >= lo and a <= hi:
                    items.append((max(a, lo), min(b, hi), i))
        if not items:
            continue
        items.sort()
        page2state = {}
        for a, b, i in items:  # earliest-starting state wins an overlap page
            for pg in range(a, b + 1):
                page2state.setdefault(pg, i)
        for pg in range(lo, hi + 1):  # gaps -> previous state (first for leading)
            if pg not in page2state:
                page2state[pg] = page2state.get(pg - 1, items[0][2])
        new_states, run_state, run_start = [], None, None
        for pg in range(lo, hi + 1):
            s = page2state[pg]
            if s != run_state:
                if run_state is not None:
                    new_states.append((run_start, pg - 1, run_state))
                run_state, run_start = s, pg
        new_states.append((run_start, hi, run_state))
        c2 = dict(c)
        c2["clothing_states"] = [
            {"pages": _range_str(range(a, b + 1)),
             "state_cn": sts[i]["state_cn"],
             "danbooru_tags": sts[i]["danbooru_tags"]}
            for a, b, i in new_states
        ]
        if c2["clothing_states"] != sts:
            repaired.append(c["name"] or "?")
        chars[-1] = c2
    out = dict(settings)
    out["characters"] = chars
    return out, repaired


def _settings_repair_fn(pages):
    """repair() for _json_call: salvage structurally-valid settings whose only
    problems are interval coverage/overlap."""
    def repair(data):
        if _validate_settings_struct(data) or not isinstance(data, dict):
            return None, []
        return _normalize_intervals(_coerce_settings(data), pages)
    return repair


# ---- fixed-header markdown validation ---------------------------------------

def _content_validator(headers, require_summary=False):
    def validate(data):
        if not isinstance(data, dict):
            return ["输出必须是 JSON 对象"]
        errs = []
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            errs.append("content 必须是非空字符串")
        else:
            last = -1
            for hd in headers:
                idx = content.find(hd)
                if idx < 0:
                    errs.append(f"content 缺少固定标题 {hd}")
                elif idx <= last:
                    errs.append(f"content 固定标题顺序错误：{hd}")
                else:
                    last = idx
        if require_summary and (
            not isinstance(data.get("summary"), str) or not data["summary"].strip()
        ):
            errs.append("summary 必须是非空字符串（本页一句话摘要）")
        return errs
    return validate


# ---- issues (checker output) -------------------------------------------------

def _validate_issues(data):
    if not isinstance(data, dict):
        return ["输出必须是 JSON 对象"]
    issues = data.get("issues")
    if not isinstance(issues, list):
        return ["issues 必须是数组（无问题输出空数组）"]
    errs = []
    for i, it in enumerate(issues):
        if not isinstance(it, dict):
            errs.append(f"issues[{i}] 必须是对象")
            continue
        for k in ("file", "problem", "fix"):
            v = it.get(k)
            if not isinstance(v, str) or not v.strip():
                errs.append(f"issues[{i}].{k} 必须是非空字符串")
    return errs


def _coerce_issues(data) -> list:
    out = []
    issues = data.get("issues") if isinstance(data, dict) else None
    for it in issues or []:
        if isinstance(it, dict) and all(
            isinstance(it.get(k), str) and it[k].strip() for k in ("file", "problem", "fix")
        ):
            out.append({k: it[k].strip() for k in ("file", "problem", "fix")})
    return out


# ---- character card matching (L3) --------------------------------------------

def _norm_name(s: str) -> str:
    return re.sub(r"[\s·・。.,，:：'\"“”()（）]", "", s or "")


def _name_match(a: str, b: str) -> bool:
    a, b = _norm_name(a), _norm_name(b)
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) >= 2 and len(b) >= 2 and (a in b or b in a)


def _relevant_cards(panel_names, settings: dict, page: int) -> list:
    """Character cards relevant to one panel, with clothing_states filtered to
    the states covering this page (falls back to all states)."""
    cards = []
    for c in settings["characters"]:
        names = [c["name"]] + c["aliases"]
        if any(_name_match(pn, n) for pn in panel_names for n in names):
            cards.append(c)
    if panel_names and not cards:
        cards = list(settings["characters"])  # no name matched: keep all cards
    out = []
    for c in cards:
        sts = [st for st in c["clothing_states"]
               if page in _interval_pages(_parse_interval(st["pages"]))]
        cc = dict(c)
        cc["clothing_states"] = sts or c["clothing_states"]
        out.append(cc)
    return out


# ---- L0 ----------------------------------------------------------------------

def _s1_compact(page_json: dict) -> dict:
    """Slim S1 page json for L0 extraction (drop bbox/crop/camera)."""
    panels = []
    for p in page_json.get("panels") or []:
        d = p.get("detail") or {}
        panels.append({
            "no": p.get("no"),
            "characters": d.get("characters", []),
            "dialogue": d.get("dialogue", []),
            "action": d.get("action", ""),
            "setting": d.get("setting", ""),
            "clothing_notes": d.get("clothing_notes", ""),
        })
    return {
        "page": page_json.get("page"),
        "summary": page_json.get("summary", ""),
        "notes": page_json.get("notes", ""),
        "panels": panels,
    }


def _run_l0(proj, sel, s1_map, sessions, log, chunk_size, concurrency):
    s2_dir = proj.out_dir("s2")
    chunks = [sel[i:i + chunk_size] for i in range(0, len(sel), chunk_size)]

    def one_chunk(i, chunk):
        # per-chunk checkpoint (2026-09-27): completed chunks are reused on
        # rerun instead of being lost with a whole-stage failure
        item = f"l0_chunk_{i:02d}"
        prev = (proj.state["stages"]["s2"]["items"].get(item) or {}).get("data") or {}
        if proj.item_status("s2", item) == "completed" and prev.get("chunk") is not None:
            log.info(f"L0 chunk {_range_str(chunk)}: skip (checkpoint)")
            return chunk, prev["chunk"], None
        pages_json = json.dumps(
            [_s1_compact(s1_map[p]) for p in chunk], ensure_ascii=False, indent=1
        )
        rng = _range_str(chunk)
        prompt = templates.render("s2_l0_chunk", PAGES_JSON=pages_json, PAGE_RANGE=rng)
        # Structure only: a character appearing on part of the chunk legitimately
        # has states only for those pages; the merge produces the full machine.
        data, err = _json_call(f"s2 L0 chunk {rng}", prompt,
                               lambda d: _validate_settings(d, require_coverage=False),
                               sessions[i % len(sessions)], log)
        if data is None:
            _set_item(proj, item, "failed", {"error": err})
            return chunk, None, err
        data = _coerce_settings(data)
        _set_item(proj, item, "completed", {"chunk": data})
        return chunk, data, None

    results = [None] * len(chunks)
    with ThreadPoolExecutor(max_workers=min(concurrency, max(1, len(chunks)))) as ex:
        futs = {ex.submit(one_chunk, i, ch): i for i, ch in enumerate(chunks)}
        for fut in as_completed(futs):
            results[futs[fut]] = fut.result()

    chunks_json = []
    for chunk, data, err in results:
        if data is None:
            log.error(f"L0 chunk {_range_str(chunk)} failed: {err}")
            return None
        chunks_json.append(data)

    # ---- tree merge: pairwise reduction (user 2026-09-27 directive) ----
    # A single 7-way merge (~30-60K input tokens) starved the endpoint's
    # prefill three times today; pairwise reduction keeps every call's input
    # at 2 settings JSONs. Every node is checkpointed ("l0_mr_<a>-<b>") so a
    # mid-tree failure loses nothing; levels run on the session pool.
    nodes = [{"cids": [i], "pages": list(chunks[i]), "data": chunks_json[i]}
             for i in range(len(chunks))]
    while len(nodes) > 1:
        nxt, jobs = [], []
        k = 0
        while k < len(nodes):
            if k + 1 < len(nodes):
                jobs.append((nodes[k], nodes[k + 1]))
            else:
                nxt.append(nodes[k])  # odd node rides to the next level
            k += 2

        def one(i, a, b):
            cids = a["cids"] + b["cids"]
            # node name = full sorted chunk-id list (min/max would collide:
            # {0,1,6} and {0..6} both reduce to "00-06")
            item = "l0_mr_" + "_".join(f"{c:02d}" for c in sorted(cids))
            final = len(nodes) == 2  # this level's only pair yields the settings
            prev = (proj.state["stages"]["s2"]["items"].get(item) or {}).get("data") or {}
            if proj.item_status("s2", item) == "completed" and prev.get("data") is not None:
                log.info(f"L0 merge {item}: skip (checkpoint)")
                return {"cids": cids, "pages": sorted(set(a["pages"] + b["pages"])),
                        "data": prev["data"]}
            prompt = templates.render(
                "s2_l0_merge",
                CHUNKS_JSON=json.dumps([a["data"], b["data"]],
                                        ensure_ascii=False, indent=1),
                ALL_PAGES=_range_str(sel) if final
                else _range_str(sorted(set(a["pages"] + b["pages"]))),
            )
            validator = (lambda d: _validate_settings(d, sel)) if final \
                else (lambda d: _validate_settings(d, require_coverage=False))
            data, err = _json_call(f"s2 L0 merge {item}", prompt, validator,
                                   sessions[i % len(sessions)], log,
                                   repair=_settings_repair_fn(sel) if final else None,
                                   max_tokens=16000)  # merged settings + reasoning
                                   # easily exceed the 8000 default (48-char
                                   # case truncated at 8000 on 2026-09-28)
            if data is None:
                _set_item(proj, item, "failed", {"error": err})
                return None
            data = _coerce_settings(data)
            _set_item(proj, item, "completed", {"data": data})
            return {"cids": cids, "pages": sorted(set(a["pages"] + b["pages"])),
                    "data": data}

        level_ok = True
        with ThreadPoolExecutor(max_workers=max(1, min(len(jobs), len(sessions)))) as ex:
            futs = [ex.submit(one, i, a, b) for i, (a, b) in enumerate(jobs)]
            for fut in futs:  # submission order — keeps the tree structure (and
                r = fut.result()  # thus checkpoint names) deterministic across reruns
                if r is None:
                    level_ok = False
                else:
                    nxt.append(r)
        if not level_ok:
            log.error("L0 tree merge: a pairwise merge failed")
            return None
        nodes = nxt

    settings = nodes[0]["data"]
    _atomic_write_json(s2_dir / "00_settings.json", settings)
    (s2_dir / "00_settings.md").write_text(_render_settings_md(settings), encoding="utf-8")
    return settings


# ---- L1 ----------------------------------------------------------------------

def _run_l1(proj, sel, s1_map, digest, session_id, log):
    path = proj.out_dir("s2") / "01_overview.md"
    if proj.item_status("s2", "l1") == "completed" and path.exists():
        log.info("L1: skip (completed)")
        return path.read_text(encoding="utf-8")
    page_summaries = "\n".join(f"第 {p} 页：{s1_map[p].get('summary', '')}" for p in sel)
    prompt = templates.render("s2_l1_overview",
                              SETTINGS_DIGEST=digest, PAGE_SUMMARIES=page_summaries)
    data, err = _json_call("s2 L1 overview", prompt,
                           _content_validator(_L1_HEADERS), session_id, log)
    if data is None:
        _set_item(proj, "l1", "failed", {"error": err})
        log.error(f"L1 failed: {err}")
        return None
    _atomic_write_text(path, data["content"])
    _set_item(proj, "l1", "completed", {})
    log.info("L1: done")
    return data["content"]


# ---- L2 ----------------------------------------------------------------------

def _run_l2_pages(proj, sel, s1_map, digest, overview, session_id, log):
    """Sequential per-page L2. Returns (all_ok, summaries dict page->str)."""
    pages_dir = proj.out_dir("s2") / "02_pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    summaries, ok = {}, True
    for pos, page in enumerate(sel):
        item = f"page_{page:02d}"
        path = pages_dir / f"page_{page:02d}.md"
        if proj.item_status("s2", item) == "completed" and path.exists():
            data = (proj.state["stages"]["s2"]["items"].get(item) or {}).get("data") or {}
            summaries[page] = (data.get("summary") or s1_map[page].get("summary") or "").strip()
            log.info(f"L2 page {page}: skip (completed)")
            continue
        if pos == 0:
            prev_block = "（本页为本次处理的起始页，无前一页摘要）"
        else:
            prev_block = f"第 {sel[pos - 1]} 页摘要：{summaries.get(sel[pos - 1]) or '（缺失）'}"
        prompt = templates.render(
            "s2_l2_pages",
            PAGE_NO=page, SETTINGS_DIGEST=digest, OVERVIEW=overview,
            PREV_SUMMARY=prev_block,
            PAGE_S1=json.dumps(s1_map[page], ensure_ascii=False, indent=1),
        )
        data, err = _json_call(f"s2 L2 page {page}", prompt,
                               _content_validator(_L2_HEADERS, require_summary=True),
                               session_id, log)
        if data is None:
            _set_item(proj, item, "failed", {"error": err})
            log.error(f"L2 page {page} failed: {err}")
            ok = False
            continue
        _atomic_write_text(path, data["content"])
        summaries[page] = data["summary"].strip()
        _set_item(proj, item, "completed", {"summary": summaries[page]})
        log.info(f"L2 page {page}: done")
    return ok, summaries


# ---- L3 ----------------------------------------------------------------------

def _run_l3_panels(proj, sel, s1_map, settings, sessions, log, concurrency):
    """Concurrent per-panel L3. Returns True iff no panel failed."""
    s2_dir = proj.out_dir("s2")
    panels_dir = s2_dir / "03_panels"
    panels_dir.mkdir(parents=True, exist_ok=True)

    jobs = []
    for page in sel:
        item = f"page_{page:02d}"
        l2_path = s2_dir / "02_pages" / f"page_{page:02d}.md"
        if proj.item_status("s2", item) != "completed" or not l2_path.exists():
            log.warning(f"L3: panels of page {page} skipped (L2 not available)")
            continue
        l2_text = l2_path.read_text(encoding="utf-8")
        for p in s1_map[page].get("panels") or []:
            jobs.append((page, p, l2_text))

    def one(i, job):
        page, panel, l2_text = job
        no = int(panel.get("no", 0))
        key = f"p{page:03d}_{no:02d}"
        path = panels_dir / f"{key}.md"
        if proj.item_status("s2", key) == "completed" and path.exists():
            return key, "skip", None
        names = [c.get("name", "") for c in (panel.get("detail") or {}).get("characters", [])]
        cards = _relevant_cards(names, settings, page)
        prompt = templates.render(
            "s2_l3_panels",
            PANEL_KEY=key, PAGE_NO=page, PANEL_NO=no, SHAPE=panel.get("shape", ""),
            CHARACTER_CARDS=json.dumps(cards, ensure_ascii=False, indent=1),
            PAGE_L2=l2_text,
            PANEL_DETAIL=json.dumps(panel.get("detail") or {}, ensure_ascii=False, indent=1),
        )
        data, err = _json_call(f"s2 L3 {key}", prompt,
                               _content_validator(_L3_HEADERS),
                               sessions[i % len(sessions)], log)
        if data is None:
            return key, "failed", err
        _atomic_write_text(path, data["content"])
        return key, "ok", None

    ok = True
    with ThreadPoolExecutor(max_workers=min(concurrency, max(1, len(jobs)))) as ex:
        for fut in as_completed([ex.submit(one, i, j) for i, j in enumerate(jobs)]):
            key, status, err = fut.result()
            if status == "skip":
                log.info(f"L3 {key}: skip (completed)")
            elif status == "ok":
                _set_item(proj, key, "completed", {})
                log.info(f"L3 {key}: done")
            else:
                _set_item(proj, key, "failed", {"error": err})
                log.error(f"L3 {key} failed: {err}")
                ok = False
    return ok


# ---- check + auto-revision ----------------------------------------------------

def _norm_rel(rel: str) -> str:
    rel = (rel or "").strip().replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    if rel.startswith("s2_zero/"):
        rel = rel[len("s2_zero/"):]
    if rel == "00_settings.md":
        rel = "00_settings.json"
    return rel


def _check_pages(proj, pages, s1_map, settings, sessions, log):
    """Per-page checks (parallel across the session pool). Returns
    {page: issues | None}; None = call itself failed."""
    s2_dir = proj.out_dir("s2")
    settings_json = json.dumps(settings, ensure_ascii=False, indent=1)

    def one(i, page):
        l2_path = s2_dir / "02_pages" / f"page_{page:02d}.md"
        if not l2_path.exists():
            return page, [{
                "file": f"02_pages/page_{page:02d}.md",
                "problem": "L2 文件缺失",
                "fix": "重新生成该页 L2",
            }]
        panels_l3 = []
        for p in s1_map[page].get("panels") or []:
            key = f"p{page:03d}_{int(p.get('no', 0)):02d}"
            path = s2_dir / "03_panels" / f"{key}.md"
            text = path.read_text(encoding="utf-8") if path.exists() \
                else f"（L3 文件缺失：{key}）"
            panels_l3.append(f"### {key}\n\n{text}")
        prompt = templates.render(
            "s2_check_page",
            PAGE_NO=page,
            SETTINGS_JSON=settings_json,
            PAGE_L2=l2_path.read_text(encoding="utf-8"),
            PANELS_L3="\n\n".join(panels_l3),
            S1_RAW=json.dumps(s1_map[page], ensure_ascii=False, indent=1),
        )
        data, err = _json_call(f"s2 check page {page}", prompt,
                               _validate_issues, sessions[i % len(sessions)], log)
        if data is None:
            log.error(f"check page {page}: call failed: {err}")
            return page, None
        return page, _coerce_issues(data)

    result = {}
    with ThreadPoolExecutor(max_workers=max(1, len(sessions))) as ex:
        futs = [ex.submit(one, i, page) for i, page in enumerate(pages)]
        for fut in as_completed(futs):
            page, issues = fut.result()
            result[page] = issues
    return result


def _check_global(proj, settings, overview, summaries, session_id, log):
    """Global check. Returns (issues | None)."""
    digest = "\n".join(f"第 {p} 页：{summaries.get(p) or '（缺失）'}" for p in sorted(summaries))
    prompt = templates.render(
        "s2_check_global",
        SETTINGS_JSON=json.dumps(settings, ensure_ascii=False, indent=1),
        OVERVIEW=overview,
        PAGES_DIGEST=digest,
    )
    data, err = _json_call("s2 check global", prompt, _validate_issues,
                           session_id, log)
    if data is None:
        log.error(f"check global: call failed: {err}")
        return None
    return _coerce_issues(data)


def _revise_file(proj, rel, issues, sel, session_id, log):
    """Revise one checker-named file. Returns (status, updates) with status in
    {"revised", "failed", "missing"}; updates may carry "settings"/"overview"/
    "summary" so in-memory context stays in sync."""
    s2_dir = proj.out_dir("s2")
    path = s2_dir / rel
    resolved = path.resolve()
    if not str(resolved).startswith(str(s2_dir.resolve()) + os.sep) or not resolved.exists():
        return "missing", {}
    issue_text = "\n".join(
        f"{i + 1}. 问题：{it['problem']}\n   建议修法：{it['fix']}"
        for i, it in enumerate(issues)
    )
    cur = resolved.read_text(encoding="utf-8")

    if rel == "00_settings.json":
        prompt = (
            "以下是一份漫画复刻脚本的 L0 冻结设定 JSON：\n\n" + cur
            + "\n\n质检发现以下问题：\n" + issue_text
            + "\n\n请修订该设定 JSON，解决全部问题。结构与字段不变；"
            "每名角色的 clothing_states 区间必须无缝覆盖页码 " + _range_str(sel)
            + " 且互不重叠。只输出修订后的完整 JSON。"
        )
        data, err = _json_call(f"s2 revise {rel}", prompt,
                               lambda d: _validate_settings(d, sel), session_id, log,
                               repair=_settings_repair_fn(sel))
        if data is None:
            return "failed", {}
        new_settings = _coerce_settings(data)
        _atomic_write_json(resolved, new_settings)
        (s2_dir / "00_settings.md").write_text(
            _render_settings_md(new_settings), encoding="utf-8"
        )
        return "revised", {"settings": new_settings}

    m = re.fullmatch(r"02_pages/page_(\d+)\.md", rel)
    if m:
        prompt = (
            f"以下是一份漫画复刻脚本的 L2 分页脉络（第 {m.group(1)} 页）：\n\n" + cur
            + "\n\n质检发现以下问题：\n" + issue_text
            + "\n\n请修订该页脉络，解决全部问题：保持 markdown 结构，固定标题"
            "（# 剧情脉络 / # 分镜顺序与衔接 / # 与前后页的承接）不得增删改；"
            '输出 JSON：\n{"content": "<修订后的完整 markdown>", '
            '"summary": "<修订后本页剧情一句话摘要（30字内）>"}\n只输出 JSON。'
        )
        data, err = _json_call(f"s2 revise {rel}", prompt,
                               _content_validator(_L2_HEADERS, require_summary=True),
                               session_id, log)
        if data is None:
            return "failed", {}
        _atomic_write_text(resolved, data["content"])
        return "revised", {"summary": (int(m.group(1)), data["summary"].strip())}

    m = re.fullmatch(r"03_panels/(p\d{3}_\d{2})\.md", rel)
    if m:
        prompt = (
            f"以下是一份漫画复刻脚本的 L3 分镜脚本（{m.group(1)}）：\n\n" + cur
            + "\n\n质检发现以下问题：\n" + issue_text
            + "\n\n请修订该分镜脚本，解决全部问题：十个固定字段标题（## 级）"
            '不得增删改、逐项填写。输出 JSON：\n'
            '{"content": "<修订后的完整 markdown>"}\n只输出 JSON。'
        )
        data, err = _json_call(f"s2 revise {rel}", prompt,
                               _content_validator(_L3_HEADERS), session_id, log)
        if data is None:
            return "failed", {}
        _atomic_write_text(resolved, data["content"])
        return "revised", {}

    if rel == "01_overview.md":
        prompt = (
            "以下是一份漫画复刻脚本的 L1 总体复述：\n\n" + cur
            + "\n\n质检发现以下问题：\n" + issue_text
            + "\n\n请修订该复述，解决全部问题：保持 markdown 结构，固定标题"
            '（# 总体复述 / # 氛围基调 / # 叙事逻辑链 / # 人物关系）不得增删改。'
            '输出 JSON：\n{"content": "<修订后的完整 markdown>"}\n只输出 JSON。'
        )
        data, err = _json_call(f"s2 revise {rel}", prompt,
                               _content_validator(_L1_HEADERS), session_id, log)
        if data is None:
            return "failed", {}
        _atomic_write_text(resolved, data["content"])
        return "revised", {"overview": data["content"]}

    return "missing", {}


def _page_of_rel(rel: str):
    m = re.fullmatch(r"02_pages/page_(\d+)\.md", rel)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"03_panels/p(\d{3})_\d{2}\.md", rel)
    if m:
        return int(m.group(1))
    return None


def _run_check(proj, sel, s1_map, sessions, log):
    """Full check + auto-revision loop. Returns (ok, item_data).

    Round 1 checks every selected page + global. Named files are then revised
    (at most _MAX_REVISE_ROUNDS revise rounds); each revise round is followed
    by a re-check of the affected pages (all pages if the frozen settings
    changed) and of the global layer when L0/L1/L2 changed. Issues whose files
    could not be revised are carried forward; whatever survives the last
    re-check is reported as UNRESOLVED.
    """
    s2_dir = proj.out_dir("s2")
    check_dir = proj.out_dir("s2_check")
    settings = _coerce_settings(_load_json(s2_dir / "00_settings.json"))
    overview = (s2_dir / "01_overview.md").read_text(encoding="utf-8")
    items = proj.state.get("stages", {}).get("s2", {}).get("items", {})
    summaries = {}
    for p in sel:
        d = (items.get(f"page_{p:02d}") or {}).get("data") or {}
        summaries[p] = (d.get("summary") or "").strip()

    report = [
        "# S2 一致性检查与自动修订报告", "",
        f"- 检查范围：第 {_range_str(sel)} 页",
        f"- 检查时间：{time.strftime('%Y-%m-%d %H:%M:%S')}", "",
    ]
    revisions, unresolved, carried = [], [], []
    round_no = 0
    ok = True

    # Round-1 scope: everything.
    pages_scope, global_scope = list(sel), True
    while True:
        round_no += 1
        report.append(f"## 第 {round_no} 轮检查")
        report.append("")
        round_issues, infra_failed = [], False

        page_results = _check_pages(proj, pages_scope, s1_map, settings,
                                    sessions, log) if pages_scope else {}
        for page in sorted(page_results):
            issues = page_results[page]
            if issues is None:
                infra_failed = True
                report.append(f"### 第 {page} 页：检查调用失败")
                continue
            report.append(f"### 第 {page} 页：{len(issues)} 项问题" if issues
                          else f"### 第 {page} 页：无问题")
            for it in issues:
                report.append(f"- [{it['file']}] {it['problem']}（建议：{it['fix']}）")
                round_issues.append(it)
            report.append("")

        if global_scope:
            g_issues = _check_global(proj, settings, overview, summaries,
                                     sessions[0], log)
            if g_issues is None:
                infra_failed = True
                report.append("### 全局检查：调用失败")
            else:
                report.append(f"### 全局检查：{len(g_issues)} 项问题" if g_issues
                              else "### 全局检查：无问题")
                for it in g_issues:
                    report.append(f"- [{it['file']}] {it['problem']}（建议：{it['fix']}）")
                round_issues += g_issues
        report.append("")

        if infra_failed:
            ok = False
            report.append("结论：检查调用失败（LLM 端点异常），本次检查未完成，请重跑 s2 复检。")
            report.append("")
            unresolved = sorted(
                {(it["file"], it["problem"]) for it in round_issues}
            )
            break
        _atomic_write_text(check_dir / "report.md", "\n".join(report))

        if not round_issues and not carried:
            report.append(f"结论：第 {round_no} 轮检查通过，无遗留问题。")
            report.append("")
            unresolved = []
            break
        if round_no > _MAX_REVISE_ROUNDS:
            seen = {(it["file"], it["problem"]) for it in carried}
            unresolved = [it for it in round_issues
                          if (it["file"], it["problem"]) not in seen] + carried
            report.append(f"结论：已达最大修订轮数（{_MAX_REVISE_ROUNDS}），"
                          "以下问题遗留，标记 UNRESOLVED：")
            for it in unresolved:
                report.append(f"- UNRESOLVED [{it['file']}] {it['problem']}（建议：{it['fix']}）")
            report.append("")
            break

        # ---- revise files named in this round's issues ----
        report.append(f"### 第 {round_no} 轮自动修订")
        report.append("")
        named = {}
        for it in round_issues:
            named.setdefault(_norm_rel(it["file"]), []).append(it)
        revised_pages, carried = [], []
        changed_settings = changed_overview = changed_l2 = False
        for rel in sorted(named):
            status, updates = _revise_file(proj, rel, named[rel], sel,
                                           sessions[0], log)
            revisions.append({"round": round_no, "file": rel,
                              "issues": len(named[rel]), "status": status})
            if status == "revised":
                report.append(f"- {rel}：已按 {len(named[rel])} 项问题修订")
                if "settings" in updates:
                    settings = updates["settings"]
                    changed_settings = True
                if "overview" in updates:
                    overview = updates["overview"]
                    changed_overview = True
                if "summary" in updates:
                    pg, sm = updates["summary"]
                    summaries[pg] = sm
                    changed_l2 = True
                    _set_item(proj, f"page_{pg:02d}", "completed", {"summary": sm})
                pg = _page_of_rel(rel)
                if pg is not None and pg in sel:
                    revised_pages.append(pg)
            else:
                why = "文件缺失或路径无法识别" if status == "missing" else "修订输出未通过校验"
                report.append(f"- {rel}：未修订（{why}），原问题保留")
                carried += named[rel]
            report.append("")
        _atomic_write_text(check_dir / "report.md", "\n".join(report))

        # ---- next-round scope ----
        # If the frozen settings changed, every page may be affected; otherwise
        # only pages whose L2/L3 files were revised. Files that failed to
        # revise keep their issues in `carried` (reported UNRESOLVED), so they
        # do not need to be re-detected by the LLM.
        pages_scope = sorted(set(sel)) if changed_settings \
            else sorted(set(revised_pages))
        global_scope = changed_settings or changed_overview or changed_l2 \
            or any(_norm_rel(it["file"]) in ("00_settings.json", "01_overview.md")
                   for it in round_issues)
        if not pages_scope and not global_scope:
            unresolved = carried
            report.append("结论：无受影响的页面需要复检。以下问题遗留，标记 UNRESOLVED：")
            for it in unresolved:
                report.append(f"- UNRESOLVED [{it['file']}] {it['problem']}（建议：{it['fix']}）")
            report.append("")
            break

    report.append("## 修订记录")
    report.append("")
    if revisions:
        report.append("| 轮次 | 文件 | 问题数 | 结果 |")
        report.append("|---|---|---|---|")
        for r in revisions:
            report.append(f"| {r['round']} | {r['file']} | {r['issues']} | {r['status']} |")
    else:
        report.append("无修订。")
    report.append("")

    _atomic_write_text(check_dir / "report.md", "\n".join(report))
    item_data = {
        "rounds": round_no,
        "revisions": revisions,
        "unresolved": [
            {"file": it["file"], "problem": it["problem"]} for it in unresolved
        ],
    }
    return ok, item_data


def run(proj: Project, **opts) -> bool:
    log = proj.get_logger("s2")
    s1_dir = proj.out_dir("s1")
    s1_files = {}
    for f in s1_dir.glob("page_*.json"):
        m = re.fullmatch(r"page_(\d+)\.json", f.name)
        if m:
            s1_files[int(m.group(1))] = f
    if not s1_files:
        log.error("no S1 output (s1_understand/page_XX.json) found; run s1 first")
        return False
    sel = sorted(set(int(p) for p in (opts.get("pages") or sorted(s1_files))))
    unknown = [p for p in sel if p not in s1_files]
    if unknown:
        log.warning(f"pages without S1 output, ignored: {unknown}")
    sel = [p for p in sel if p in s1_files]
    if not sel:
        log.error("no pages to process")
        return False

    chunk_size = max(1, int(opts.get("chunk", 2)))
    concurrency = max(1, int(opts.get("concurrency", 2)))
    sessions = llm.new_session_pool()  # one per DGX; unit-level affinity
    s2_dir = proj.out_dir("s2")
    (s2_dir / "02_pages").mkdir(parents=True, exist_ok=True)
    (s2_dir / "03_panels").mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    fail = False

    # ---- L0 settings (frozen) ----
    s1_map = {p: _load_json(s1_files[p]) for p in sel}
    settings_path = s2_dir / "00_settings.json"
    if (proj.item_status("s2", "l0") == "completed" and settings_path.exists()):
        settings = _coerce_settings(_load_json(settings_path))
        stale = _validate_settings(settings, sel)
        if stale:
            log.warning(f"L0 loaded from checkpoint has issues for pages {sel}: {stale}")
        log.info("L0: skip (completed)")
    else:
        settings = _run_l0(proj, sel, s1_map, sessions, log, chunk_size, concurrency)
        if settings is None:
            _set_item(proj, "l0", "failed", {"error": "chunk/merge validation failed"})
            log.error("L0 failed")
            return False
        _set_item(proj, "l0", "completed",
                  {"characters": len(settings["characters"]),
                   "environments": len(settings["environments"])})
        log.info("L0: done")
    digest = _settings_digest(settings)

    # ---- L1 overview ----
    overview = _run_l1(proj, sel, s1_map, digest, sessions[0], log)
    if overview is None:
        return False

    # ---- L2 pages (sequential) ----
    ok_l2, summaries = _run_l2_pages(proj, sel, s1_map, digest, overview,
                                     sessions[0], log)
    if not ok_l2:
        fail = True

    # ---- L3 panels (concurrent) ----
    ok_l3 = _run_l3_panels(proj, sel, s1_map, settings, sessions, log,
                           concurrency)
    if not ok_l3:
        fail = True

    if fail:
        _set_item(proj, "check", "failed",
                  {"error": "generation incomplete; check skipped (rerun s2 to resume)"})
        log.error("s2: generation incomplete; check skipped")
        log.info(f"s2 summary: {time.time() - t0:.1f}s total")
        return False

    # ---- check + auto-revision (hard requirement) ----
    ok_check, check_data = _run_check(proj, sel, s1_map, sessions, log)
    _set_item(proj, "check", "completed" if ok_check else "failed", check_data)
    if ok_check:
        n_unresolved = len(check_data.get("unresolved", []))
        log.info(f"check done: {check_data['rounds']} round(s), "
                 f"{n_unresolved} unresolved issue(s)")
    log.info(f"s2 summary: {time.time() - t0:.1f}s total")
    return ok_check
