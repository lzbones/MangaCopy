"""S5 video_script: style card -> beat segmentation -> per-segment video scripts.

Inputs (from s2_zero): 00_settings.json (characters / environments / style_notes),
01_overview.md (L1), 02_pages/page_XX.md (L2), 03_panels/pXXX_YY.md (L3).
Panel global order = page ascending, then panel-no ascending (derived from file
names). params.style (2d/3d/live) drives the style guards.

Outputs (s5_video/): 00_style.md (style card with fixed guards), 00_beats.json
({"segments":[{"seg","panels","duration","beat"}]}), segments/seg_XX.md
(per-segment script: static->dynamic rewrite, per-second shot design, three-
track sound design, cast & clothing anchoring).

Checkpoint items under stage "s5": "style", "beats", "seg_XX".

Hard validation (code side):
- beats: coverage (exact ordered panel list, no gaps/dups), 1 <= duration <=
  max_shot_seconds, sequential seg numbering. One LLM re-segmentation on
  violation, then deterministic repair (half-split overlong segments; greedy
  sequential packing if coverage is beyond salvage).
- segment scripts: required headings, contiguous per-second coverage of the
  segment duration, verbatim Chinese dialogue lines. One LLM retry; a
  segment failing twice is marked failed and the stage continues.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import llm, templates
from .project import Project

# Fixed style guards (injected verbatim into the style card and templates).
STYLE_INFO = {
    "2d": {
        "label_cn": "平面2D赛璐璐动画",
        "guard_cn": "平面 2D 赛璐璐动画风格（cel-shaded flat 2D animation）；NOT realistic live-action（非写实真人实拍）；NOT 3D CGI（非三维 CG 渲染）",
        "guard_not": "NOT realistic live-action, NOT 3D CGI",
        "guard_en": "Flat 2D cel-shaded animation style, NOT realistic live-action, NOT 3D CGI.",
    },
    "3d": {
        "label_cn": "风格化三维CG动画",
        "guard_cn": "风格化三维 CG 动画风格（stylized 3D CGI animation）；NOT flat 2D（非平面 2D）；NOT live-action（非真人实拍）",
        "guard_not": "NOT flat 2D, NOT live-action",
        "guard_en": "Stylized 3D CGI animation style, NOT flat 2D, NOT live-action.",
    },
    "live": {
        "label_cn": "写实真人电影质感",
        "guard_cn": "写实真人电影质感（realistic live-action cinematic）；NOT 3D CGI（非三维 CG 渲染）；NOT cartoon（非卡通动画）",
        "guard_not": "NOT 3D CGI, NOT cartoon",
        "guard_en": "Realistic live-action cinematic style, NOT 3D CGI, NOT cartoon.",
    },
}

_PANEL_RE = re.compile(r"^p(\d{3})_(\d{2})$")
_SEC_LINE_RE = re.compile(
    r"^\s*(?:[-*+]\s*)?(?:\*\*)?(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)s"
)

DEFAULT_PER_PANEL_SECONDS = 5.0  # deterministic fallback beat duration estimate


# ---- small helpers ----------------------------------------------------------

def _atomic_write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _md_section(text: str, title: str) -> str:
    """Content of the '## <title>' section up to the next '## ' heading ('' if absent)."""
    m = re.search(rf"^##\s*{re.escape(title)}\s*$", text, re.MULTILINE)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"^##\s", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _digest(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("。", "！", "？", "；", ". "):
        pos = cut.rfind(sep)
        if pos > limit // 2:
            return cut[: pos + len(sep)] + "……"
    return cut + "……"


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_int(v) -> bool:
    if isinstance(v, int) and not isinstance(v, bool):
        return True
    return isinstance(v, float) and float(v).is_integer()


def _load_settings(proj: Project) -> dict:
    return json.loads(
        (proj.out_dir("s2") / "00_settings.json").read_text(encoding="utf-8")
    )


def _panel_keys(proj: Project) -> list:
    """All panel keys in global story order (page asc, panel no asc)."""
    panels_dir = proj.out_dir("s2") / "03_panels"
    keys = []
    for f in panels_dir.glob("p*.md"):
        m = _PANEL_RE.match(f.stem)
        if m:
            keys.append((int(m.group(1)), int(m.group(2)), f.stem))
    return [k for _, _, k in sorted(keys)]


def _panel_page(key: str) -> int:
    return int(key[1:4])


def _l3_text(proj: Project, key: str) -> str:
    return (proj.out_dir("s2") / "03_panels" / f"{key}.md").read_text(encoding="utf-8")


def _panel_char_count(l3: str) -> int:
    """Count main characters from the L3 '出场人物' section (non-empty lines)."""
    sec = _md_section(l3, "出场人物")
    n = 0
    for line in sec.splitlines():
        s = line.strip().lstrip("-*• ").strip()
        if not s or s.startswith("（无") or s.startswith("(无"):
            continue
        n += 1
    return n


def _panel_gist(l3: str, limit: int = 80) -> str:
    sec = _md_section(l3, "动作与姿态").strip()
    if not sec:
        sec = _md_section(l3, "氛围与情绪").strip()
    return _digest(re.sub(r"\s+", " ", sec), limit) or "（见 L3 脚本）"


def dialogue_lines_from_l3(l3: str) -> list:
    """[(speaker, text)] from the L3 '对白原文' section; skips '（无）' and meta notes."""
    sec = _md_section(l3, "对白原文")
    out = []
    for line in sec.splitlines():
        s = line.strip()
        if not s or s.startswith("（无") or s.startswith("(无"):
            continue
        # Skip meta comments / annotations in parentheses
        if (s.startswith("（") and s.endswith("）")) or (s.startswith("(") and s.endswith(")")):
            if any(w in s for w in ("注", "本镜", "说明", "旁白说明", "画外音说明")):
                continue
        s = s.lstrip("-*• ").strip()
        if (s.startswith("（") and s.endswith("）")) or (s.startswith("(") and s.endswith(")")):
            if any(w in s for w in ("注", "本镜", "说明", "旁白说明", "画外音说明")):
                continue
        if "：" in s or ":" in s:
            speaker, text = s.split("：", 1) if "：" in s else s.split(":", 1)
            speaker, text = speaker.strip(), text.strip()
        else:
            speaker, text = "?", s
        if (text.startswith("（") and text.endswith("）")) or (text.startswith("(") and text.endswith(")")):
            if any(w in text for w in ("注", "本镜", "说明")):
                continue
        if text and not text.startswith("（无") and not text.startswith("(无"):
            out.append((speaker, text))
    return out


def _settings_digest(settings: dict) -> str:
    chars = settings.get("characters") or []
    lines = []
    for c in chars:
        if isinstance(c, dict):
            states = "；".join(
                f"页{ '+'.join(str(p) for p in (st.get('pages') or [])) }: {st.get('state_cn', '')}"
                for st in (c.get("clothing_states") or [])
                if isinstance(st, dict)
            )
            lines.append(
                f"- {c.get('name', '?')}：{c.get('appearance_cn', '')}；衣着状态：{states or '（未定义）'}"
            )
    envs = settings.get("environments")
    if isinstance(envs, list):
        env_txt = "；".join(str(e) for e in envs)
    else:
        env_txt = str(envs or "（未定义）")
    return (
        "人物卡：\n" + "\n".join(lines)
        + f"\n环境：{env_txt}\n风格注记：{settings.get('style_notes', '')}"
    )


def _relevant_characters(settings: dict, seg_text: str) -> list:
    out = []
    for c in settings.get("characters") or []:
        if isinstance(c, dict) and c.get("name") and c["name"] in seg_text:
            out.append(c)
    return out


def _character_cards_md(chars: list) -> str:
    if not chars:
        return "（本段无登记角色）"
    blocks = []
    for c in chars:
        states = "；".join(
            f"[页{'+'.join(str(p) for p in (st.get('pages') or []))}] {st.get('state_cn', '')}"
            for st in (c.get("clothing_states") or [])
            if isinstance(st, dict)
        )
        anc = c.get("anchor") or {}
        if isinstance(anc, dict) and anc.get("character_name"):
            orig = f"（出品作品：《{anc['anime_origin']}》）" if anc.get("anime_origin") else ""
            anc_str = f"{anc['character_name']}{orig}"
        elif isinstance(anc, str) and anc.strip():
            anc_str = anc.strip()
        else:
            anc_str = "（未指定）"
        gender_cn = "女性" if c.get("gender") == "female" else "男性"
        blocks.append(
            f"### {c.get('name', '?')}（性别：{gender_cn} | 锚定已知经典动漫角色：{anc_str}）\n"
            f"- 核心外貌完整复述：{c.get('appearance_cn', '')}\n"
            f"- 衣着状态：{states or '（未定义）'}\n"
            f"- 英文外貌 tags：{c.get('danbooru_tags', '')}"
        )
    return "\n".join(blocks)


# ---- LLM call with one validation retry --------------------------------------

_CALL_TIMEOUT = 1200  # s; 2026-09-27 用户终版裁定：上限 20 分钟


def _json_call(label: str, prompt: str, validator, session_id: str, log,
               max_tokens=None):
    """json_mode call -> robust JSON extraction -> validation; one retry with
    the error list appended. Degenerate responses under response_format
    ("non-string content", seen intermittently from spark/vllm on large
    prompts) are retried once without json_mode at no validation cost.
    Returns (data, None) or (None, error_str)."""
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
                           timeout=_CALL_TIMEOUT)
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
        log.info(f"{label}: llm ok in {time.time() - t0:.1f}s")
        errs = validator(data)
        if not errs:
            return data, None
        log.warning(f"{label}: attempt {attempt + 1} validation errors: {errs}")
        attempt += 1
    return None, "; ".join(errs)


# ---- style card ---------------------------------------------------------------

def _make_style_card(proj: Project, session_id: str, log) -> bool:
    out_dir = proj.out_dir("s5")
    style = proj.params.get("style", "2d")
    info = STYLE_INFO.get(style)
    if info is None:
        log.error(f"unknown style {style!r}; expected one of {sorted(STYLE_INFO)}")
        return False
    prompt = templates.render(
        "s5_style",
        STYLE=style,
        STYLE_LABEL_CN=info["label_cn"],
        STYLE_GUARD_CN=info["guard_cn"],
        STYLE_GUARD_NOT=info["guard_not"],
        STYLE_GUARD_EN=info["guard_en"],
        SETTINGS_DIGEST=_settings_digest(_load_settings(proj)),
    )
    data, err = _json_call("s5 style", prompt, lambda d: (
        [] if isinstance(d, dict) and isinstance(d.get("content"), str)
        and d["content"].strip() else ["输出须为 {\"content\": \"<markdown>\"}"]
    ), session_id, log)
    if data is None:
        log.error(f"style card generation failed: {err}")
        return False
    content = data["content"].strip()
    # Deterministic guarantee: guards must appear verbatim; patch if the LLM
    # dropped or reworded them (S6 depends on the exact English sentences).
    if info["guard_not"] not in content or info["guard_en"] not in content:
        content += (
            "\n\n## 风格守卫（确定性补充）\n"
            f"{info['guard_cn']}\n\n{info['guard_not']}\n\n{info['guard_en']}\n"
        )
        log.warning("style card: guards patched deterministically")
    _atomic_write_text(out_dir / "00_style.md", content + "\n")
    proj.set_item("s5", "style", "completed", {"style": style})
    log.info(f"style card written: {out_dir / '00_style.md'}")
    return True


# ---- beat segmentation --------------------------------------------------------

def _validate_beats(data, panel_keys: list, max_s: float) -> list:
    if not isinstance(data, dict):
        return ["输出必须是 JSON 对象"]
    segs = data.get("segments")
    if not isinstance(segs, list) or not segs:
        return ["segments 必须是非空数组"]
    errs = []
    known = set(panel_keys)
    concat = []
    for i, s in enumerate(segs):
        w = f"segments[{i}]"
        if not isinstance(s, dict):
            errs.append(f"{w} 必须是对象")
            continue
        if not _is_int(s.get("seg")) or int(s["seg"]) != i + 1:
            errs.append(f"{w}.seg 必须是 {i + 1}")
        ps = s.get("panels")
        if not isinstance(ps, list) or not ps or not all(isinstance(p, str) for p in ps):
            errs.append(f"{w}.panels 必须是非空字符串数组")
            ps = []
        bad = [p for p in ps if p not in known]
        if bad:
            errs.append(f"{w}.panels 含未知分镜: {bad}")
        if not _is_num(s.get("duration")):
            errs.append(f"{w}.duration 必须是数字")
        else:
            d = float(s["duration"])
            if not (1.0 <= d <= max_s):
                errs.append(f"{w}.duration={d} 越界（须 1~{max_s} 秒）")
        if not isinstance(s.get("beat"), str) or not s["beat"].strip():
            errs.append(f"{w}.beat 必须是非空字符串")
        concat.extend(ps)
    if concat != panel_keys:
        missing = [k for k in panel_keys if concat.count(k) == 0]
        dups = sorted({k for k in concat if concat.count(k) > 1})
        unknown_order = concat != [k for k in panel_keys if k in concat]
        errs.append(
            f"分镜覆盖错误：缺 {missing}；重 {dups}；顺序与全局清单不一致={unknown_order}"
        )
    return errs


def _deterministic_beats(llm_segs, panel_keys: list, max_s: float) -> list:
    """Deterministic repair of the beat segmentation. Salvage what is usable
    from the LLM output (valid keys, in-order dedup); if coverage still fails,
    fall back to greedy sequential packing. Overlong segments are half-split
    (duration proportional to panel count); single-panel overlong segments are
    clamped to max_s. Returns renumbered segments."""
    known = set(panel_keys)

    def _dur(s, n):
        d = s.get("duration") if isinstance(s, dict) else None
        return float(d) if _is_num(d) and d else DEFAULT_PER_PANEL_SECONDS * n

    salv, used = [], set()
    for s in llm_segs if isinstance(llm_segs, list) else []:
        if not isinstance(s, dict):
            continue
        ps = [p for p in (s.get("panels") or []) if p in known and p not in used]
        if not ps:
            continue
        total = len(s.get("panels") or []) or len(ps)
        used.update(ps)
        salv.append({
            "panels": ps,
            "duration": round(_dur(s, total) * len(ps) / max(total, 1), 2),
            "beat": str(s.get("beat") or "（确定性兜底分段）"),
        })

    if [p for s in salv for p in s["panels"]] != panel_keys:
        # Beyond salvage: greedy sequential packing with a default estimate.
        salv = []
        cur = []
        for k in panel_keys:
            if cur and (len(cur) + 1) * DEFAULT_PER_PANEL_SECONDS > max_s:
                salv.append({"panels": cur,
                            "duration": round(DEFAULT_PER_PANEL_SECONDS * len(cur), 2),
                            "beat": "（确定性兜底分段）"})
                cur = []
            cur.append(k)
        if cur:
            salv.append({"panels": cur,
                        "duration": round(DEFAULT_PER_PANEL_SECONDS * len(cur), 2),
                        "beat": "（确定性兜底分段）"})

    # Half-split overlong segments (queue-based, deterministic).
    out = []
    queue = deque(salv)
    while queue:
        s = queue.popleft()
        if s["duration"] > max_s:
            if len(s["panels"]) >= 2:
                mid = len(s["panels"]) // 2
                left, right = s["panels"][:mid], s["panels"][mid:]
                dl = round(s["duration"] * len(left) / len(s["panels"]), 2)
                dr = round(s["duration"] * len(right) / len(s["panels"]), 2)
                queue.appendleft({"panels": right, "duration": dr, "beat": s["beat"]})
                queue.appendleft({"panels": left, "duration": dl, "beat": s["beat"]})
                continue
            s["duration"] = max_s
        s["duration"] = min(max(s["duration"], 1.0), max_s)
        out.append(s)

    for i, s in enumerate(out, 1):
        s["seg"] = i
    return out


def _make_beats(proj: Project, panel_keys: list, session_id: str, log) -> list | None:
    out_dir = proj.out_dir("s5")
    max_s = float(proj.params.get("max_shot_seconds", 15.0))

    l2_lines = []
    pages_dir = proj.out_dir("s2") / "02_pages"
    for f in sorted(pages_dir.glob("page_*.md")):
        body = f.read_text(encoding="utf-8")
        l2_lines.append(f"### {f.stem}\n{_digest(body, 500)}")
    l3 = {k: _l3_text(proj, k) for k in panel_keys}
    panel_lines = [
        f"- {k} | 第{ _panel_page(k) }页 | 主要角色 {_panel_char_count(l3[k])} 人 | {_panel_gist(l3[k])}"
        for k in panel_keys
    ]
    prompt = templates.render(
        "s5_beats",
        L1_OVERVIEW=_digest((proj.out_dir("s2") / "01_overview.md").read_text(encoding="utf-8"), 2500),
        L2_DIGEST="\n".join(l2_lines),
        PANEL_LIST="\n".join(panel_lines),
        MAX_SHOT_SECONDS=max_s,
    )
    data, err = _json_call(
        "s5 beats", prompt,
        lambda d: _validate_beats(d, panel_keys, max_s),
        session_id, log,
    )
    segs = None
    if data is not None:
        segs = data["segments"]
    else:
        log.warning(f"beats: LLM segmentation failed twice ({err}); repairing deterministically")
    if segs is None or _validate_beats({"segments": segs}, panel_keys, max_s):
        before = segs
        segs = _deterministic_beats(segs, panel_keys, max_s)
        log.warning(f"beats: deterministic repair applied (llm_segments={before!r} -> {segs!r})")
    _atomic_write_json(out_dir / "00_beats.json", {"segments": segs})
    proj.set_item("s5", "beats", "completed", {"segments": len(segs)})
    log.info(f"beats written: {len(segs)} segments")
    return segs


# ---- per-segment scripts -------------------------------------------------------

_SEG_HEADINGS = [
    "## 内容改写（静态到动态）",
    "## 逐秒镜头设计",
    "## 声音设计",
    "### 对白",
    "### 环境与动作音效",
    "### 配乐",
    "## 出场人物及衣着锚定",
]


def _validate_segment(content: str, duration: float, dialogue_texts: list) -> list:
    errs = [f"缺少固定标题 {h}" for h in _SEG_HEADINGS if h not in content]
    sec = _md_section(content, "逐秒镜头设计")
    intervals = []
    for line in sec.splitlines():
        m = _SEC_LINE_RE.match(line)
        if m:
            intervals.append((float(m.group(1)), float(m.group(2))))
    if not intervals:
        errs.append("逐秒镜头设计缺少 'X-Ys' 时间行")
    else:
        if intervals[0][0] != 0:
            errs.append(f"逐秒镜头设计须从 0s 开始（当前从 {intervals[0][0]}s 开始）")
        for a, b in zip(intervals, intervals[1:]):
            if a[1] != b[0]:
                errs.append(f"逐秒时间行不连续：{a[0]}-{a[1]}s 之后接 {b[0]}-{b[1]}s")
                break
        if intervals and intervals[-1][1] < float(int(duration)):
            errs.append(
                f"逐秒镜头设计未覆盖到 {duration}s（最后到 {intervals[-1][1]}s）"
            )
    missing = [t for t in dialogue_texts if t not in content]
    if missing:
        errs.append(f"对白原文未逐字出现：{missing}")
    return errs


def _make_segment_scripts(proj: Project, segs: list, sessions: list, log) -> bool:
    out_dir = proj.out_dir("s5")
    segs_dir = out_dir / "segments"
    segs_dir.mkdir(parents=True, exist_ok=True)
    max_s = float(proj.params.get("max_shot_seconds", 15.0))
    settings = _load_settings(proj)
    style_card = (out_dir / "00_style.md").read_text(encoding="utf-8")

    todo, skipped = [], []
    for s in segs:
        key = f"seg_{int(s['seg']):02d}"
        if (proj.item_status("s5", key) == "completed"
                and (segs_dir / f"{key}.md").exists()):
            skipped.append(key)
        else:
            todo.append(s)
    if skipped:
        log.info(f"skip completed segments: {skipped}")

    def one(i, s):
        seg_no = int(s["seg"])
        key = f"seg_{seg_no:02d}"
        t0 = time.time()
        panels = list(s["panels"])
        duration = float(s["duration"])
        l3_parts, dialogue_texts = [], []
        for pk in panels:
            l3 = _l3_text(proj, pk)
            l3_parts.append(f"### 分镜 {pk}（L3 全文）\n{l3.strip()}")
            dialogue_texts.extend(t for _, t in dialogue_lines_from_l3(l3))
        seg_l3 = "\n\n".join(l3_parts)
        chars = _relevant_characters(settings, seg_l3)
        prompt = templates.render(
            "s5_segment",
            STYLE_CARD=style_card,
            SEG_NO=seg_no,
            SEG_BEAT=s.get("beat", ""),
            SEG_DURATION=duration,
            SEG_PANELS_L3=seg_l3,
            CHARACTER_CARDS=_character_cards_md(chars),
            MAX_SHOT_SECONDS=max_s,
        )
        data, err = _json_call(
            f"s5 {key}", prompt,
            lambda d: (
                _validate_segment(d.get("content", "").strip(), duration, dialogue_texts)
                if isinstance(d, dict) and isinstance(d.get("content"), str)
                else ["输出须为 {\"content\": \"<markdown>\"}"]
            ),
            sessions[i % len(sessions)], log,
        )
        if data is None:
            proj.set_item("s5", key, "failed", {"error": err})
            log.error(f"{key}: segment script failed: {err}")
            return False
        _atomic_write_text(segs_dir / f"{key}.md", data["content"].strip() + "\n")
        proj.set_item("s5", key, "completed",
                      {"panels": panels, "duration": duration})
        log.info(f"{key}: done ({len(panels)} panels, {duration}s, {time.time() - t0:.1f}s)")
        return True

    ok = fail = 0
    if todo:
        with ThreadPoolExecutor(max_workers=max(1, len(sessions))) as ex:
            futs = [ex.submit(one, i, s) for i, s in enumerate(todo)]
            for fut in as_completed(futs):
                if fut.result():
                    ok += 1
                else:
                    fail += 1

    log.info(f"s5 segments: {ok} ok / {fail} failed / {len(skipped)} skipped")
    # 2026-09-28 user-approved: a partially-failed stage must FAIL so the DAG
    # retries it — the old ">=1 segment ok" criterion stranded failed segments
    # forever (s5 "completed" -> never re-run -> s6 deadlocked on missing files)
    return ok == len(todo)


# ---- stage entry ----------------------------------------------------------------

def run(proj: Project, **opts) -> bool:
    log = proj.get_logger("s5")
    panel_keys = _panel_keys(proj)
    if not panel_keys:
        log.error("no L3 panel scripts found in s2_zero/03_panels/")
        return False
    log.info(f"s5 start: {len(panel_keys)} panels, style={proj.params.get('style')}")

    sessions = llm.new_session_pool()  # one per DGX; per-segment affinity

    if (proj.item_status("s5", "style") == "completed"
            and (proj.out_dir("s5") / "00_style.md").exists()):
        log.info("skip completed style card")
    elif not _make_style_card(proj, sessions[0], log):
        return False

    beats_path = proj.out_dir("s5") / "00_beats.json"
    if proj.item_status("s5", "beats") == "completed" and beats_path.exists():
        log.info("skip completed beats")
        segs = json.loads(beats_path.read_text(encoding="utf-8"))["segments"]
        max_s = float(proj.params.get("max_shot_seconds", 15.0))
        if _validate_beats({"segments": segs}, panel_keys, max_s):
            log.warning("stored beats invalid (panels changed?); regenerating")
            segs = _make_beats(proj, panel_keys, sessions[0], log)
    else:
        segs = _make_beats(proj, panel_keys, sessions[0], log)
    if not segs:
        log.error("beat segmentation unavailable")
        return False

    return _make_segment_scripts(proj, segs, sessions, log)
