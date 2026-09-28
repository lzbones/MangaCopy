"""S3 image_prompt: per-panel Wai (Illustrious SDXL) prompt construction.

Hybrid scheme (character consistency by full repetition, no LoRA / face
reference):
- deterministic appearance block: people-count tag (1girl / 2girls / 1boy / ...)
  + per-character (anchor? + danbooru_tags + current-page clothing tags),
  assembled verbatim from s2_zero/00_settings.json so that every prompt
  unconditionally carries the complete frozen appearance of every appearing
  character;
- LLM dynamic block: scene tags only (expression / action / pose /
  interaction / composition / camera / background / atmosphere) generated
  from the L3 panel script via prompts/s3_wai.md.

Characters appearing in a panel are identified deterministically by matching
frozen names/aliases against the "## 出场人物" section of the L3 markdown;
when that finds nothing, an LLM fallback (prompts/s3_check.md) decides.

After the build a hard deterministic check runs over every prompt of the
processed set and writes s3_check/report.md:
  (1) positive contains every appearance + clothing token of its characters
      (comma-split token match, (tag:1.2) weight shells stripped first);
  (2) per-character anchor is identical across all panels of the set;
  (3) size bucket matches the actual crop aspect ratio;
  (4) positive carries no CJK residue.
Failing prompts are auto-repaired for up to 2 rounds; anything still failing
is marked UNRESOLVED (panel item failed, stage failed) so bad prompts never
flow silently into S4.

Size buckets: vertical 1024x1408 / horizontal 1408x1024 / square 1216x1216
(crop aspect < 0.8 -> vertical, > 1.25 -> horizontal, else square). Seed =
int(md5("<project_id>:<panel_key>").hexdigest()[:8], 16), fully reproducible.

opts: panels (list of panel keys, default all of 03_panels/*.md), negative
(str override of config.load_neg_prompt()), concurrency (LLM workers, default 3).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image

from . import config, llm, templates
from .project import Project

PANEL_KEY_RE = re.compile(r"^p(\d{3})_(\d{2})$")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3000-\u303f\uff01-\uffee]")
_WEIGHT_RE = re.compile(r"\((.+?):\s*-?\d+(?:\.\d+)?\)")
_PAREN_RE = re.compile(r"\((.+)\)")

_QUALITY_TAIL_DEFAULT = ",masterpiece,best quality,"
_EMPTY_SECTION_MARKERS = ("（无）", "(无)", "无出场", "无人出场", "无人物")
_MAX_REPAIR_ROUNDS = 2

_FEMALE_MARKERS = {"female", "f", "woman", "girl", "女", "女性", "少女"}
_MALE_MARKERS = {"male", "m", "man", "boy", "男", "男性", "少年"}

_STATE_LOCK = threading.Lock()  # Project.set_item is not thread-safe by itself


def _set_item(proj: Project, key: str, status: str, data=None) -> None:
    with _STATE_LOCK:
        proj.set_item("s3", key, status, data)


def _atomic_write_json(path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---- small utilities ---------------------------------------------------------

def parse_panel_key(key: str):
    """p001_02 -> (1, 2); None if not a panel key."""
    m = PANEL_KEY_RE.match(key or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _tokens(tag_str) -> list:
    if not tag_str:
        return []
    return [t.strip() for t in str(tag_str).split(",") if t.strip()]


def _strip_weight(token: str) -> str:
    """'(red eyes:1.2)' -> 'red eyes'; 'astolfo \\(fate\\)' -> 'astolfo (fate)';
    '(re:zero)' -> 're:zero'; plain tokens unchanged. Applied symmetrically to
    required and present tokens so weight shells never break matching."""
    t = token.strip().replace("\\(", "(").replace("\\)", ")")
    m = _WEIGHT_RE.fullmatch(t)
    if m:
        return m.group(1).strip()
    m = _PAREN_RE.fullmatch(t)
    if m:
        return m.group(1).strip()
    return t


def _has_cjk(s: str) -> bool:
    return bool(_CJK_RE.search(s or ""))


def _norm_gender(char: dict) -> str:
    g = str(char.get("gender", "")).strip().lower()
    if g in _FEMALE_MARKERS:
        return "female"
    if g in _MALE_MARKERS:
        return "male"
    return "unknown"


def _aliases_of(char: dict) -> list:
    raw = char.get("aliases") or []
    if isinstance(raw, str):
        raw = re.split(r"[,，]", raw)
    out = []
    for a in raw:
        a = str(a).strip()
        if a:
            out.append(a)
    return out


def _names_of(char: dict) -> list:
    names = [str(char.get("name", "")).strip()] + _aliases_of(char)
    return [n for n in names if n]


def _count_tag(chars: list) -> str:
    girls = sum(1 for c in chars if _norm_gender(c) == "female")
    boys = sum(1 for c in chars if _norm_gender(c) == "male")
    parts = []
    if girls:
        parts.append("1girl" if girls == 1 else f"{girls}girls")
    if boys:
        parts.append("1boy" if boys == 1 else f"{boys}boys")
    if not parts:
        return "no humans"
    return ", ".join(parts)


def _parse_interval(pages_str) -> list:
    """'1-13' -> [(1,13)]; '7' -> [(7,7)]; '1-3, 7' -> [(1,3),(7,7)]; None if bad."""
    segs = []
    for part in str(pages_str or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            try:
                segs.append((int(lo), int(hi)))
            except ValueError:
                return None
        else:
            try:
                segs.append((int(part), int(part)))
            except ValueError:
                return None
    return segs or None


def _clothing_for_page(char: dict, page: int):
    """(state_cn, danbooru_tags) of the clothing state covering this page;
    on multiple interval hits the LATTER state in the array wins."""
    best = None
    for st in char.get("clothing_states") or []:
        segs = _parse_interval(st.get("pages", ""))
        if not segs:
            continue
        if any(lo <= page <= hi for lo, hi in segs):
            best = st
    if best is None:
        return "", ""
    return str(best.get("state_cn", "") or ""), str(best.get("danbooru_tags", "") or "")


def _bucket_of_crop(crop_path):
    """-> (bucket, width, height, aspect) from the actual crop pixels."""
    with Image.open(crop_path) as im:
        w, h = im.size
    aspect = w / h
    if aspect < 0.8:
        bucket = "vertical"
    elif aspect > 1.25:
        bucket = "horizontal"
    else:
        bucket = "square"
    bw, bh = config.IMAGE_SIZE_BUCKETS[bucket]
    return bucket, bw, bh, aspect


def _extract_section(md: str, title: str) -> str:
    lines = md.splitlines()
    collecting = False
    buf = []
    for ln in lines:
        s = ln.strip()
        if s == f"## {title}":
            collecting = True
            continue
        if collecting and s.startswith("## "):
            break
        if collecting:
            buf.append(ln)
    return "\n".join(buf).strip()


def _present_tokens(positive: str) -> set:
    return {_strip_weight(t).lower() for t in _tokens(positive)}


# ---- character identification --------------------------------------------------

def _identify_characters(key: str, md: str, chars: list, session_id: str, log) -> list:
    """Deterministic first: match frozen names/aliases inside the L3
    '## 出场人物' section. Falls back to one LLM call (s3_check template).
    Returns the list of frozen character dicts (settings order preserved)."""
    section = _extract_section(md, "出场人物")
    if section:
        low = section.lower()
        hit = [c for c in chars if any(n.lower() in low for n in _names_of(c))]
        if hit:
            return hit
        compact = re.sub(r"\s+", "", section)
        if len(compact) <= 2 or any(m in section for m in _EMPTY_SECTION_MARKERS):
            return []  # section explicitly says nobody is on stage
        log.info(f"{key}: '出场人物' section matched no frozen name, llm fallback")
    cards = "\n\n".join(
        f"- name: {c.get('name', '')}\n"
        f"  aliases: {', '.join(_aliases_of(c))}\n"
        f"  gender: {c.get('gender', '')}\n"
        f"  appearance: {c.get('appearance_cn', '')}"
        for c in chars
    )
    prompt = templates.render("s3_check", PANEL_KEY=key,
                              CHARACTER_CARDS=cards, PANEL_SCRIPT=md)
    raw = llm.chat([{"role": "user", "content": prompt}], json_mode=True,
                   session_id=session_id)
    data = llm.extract_json(raw)
    names = data.get("characters") if isinstance(data, dict) else None
    if not isinstance(names, list):
        raise llm.LLMError(f"character fallback output must be a list, got: {raw[:200]!r}")
    by_name = {}
    for c in chars:
        for n in _names_of(c):
            by_name.setdefault(n.lower(), c)
    hit = []
    for n in names:
        c = by_name.get(str(n).strip().lower())
        if c is None:
            log.warning(f"{key}: llm returned unknown character {n!r}, ignored")
        elif c not in hit:
            hit.append(c)
    return hit


# ---- LLM dynamic block ---------------------------------------------------------

def _scene_block(key: str, md: str, session_id: str, log):
    """-> (scene_tags, quality_tail) via prompts/s3_wai.md; raises LLMError."""
    prompt = templates.render("s3_wai", PANEL_KEY=key, PANEL_SCRIPT=md)
    raw = llm.chat([{"role": "user", "content": prompt}], json_mode=True,
                   session_id=session_id)
    data = llm.extract_json(raw)
    if not isinstance(data, dict):
        raise llm.LLMError(f"scene block must be a JSON object, got: {raw[:200]!r}")
    scene = ", ".join(_tokens(data.get("scene_tags")))
    tail = data.get("quality_tail")
    tail = str(tail).strip() if isinstance(tail, str) else ""
    if not tail:
        tail = _QUALITY_TAIL_DEFAULT
    if not tail.startswith(","):
        tail = "," + tail
    if not tail.endswith(","):
        tail += ","
    return scene, tail


def _remove_cjk_tokens(tags_str: str) -> str:
    return ", ".join(t for t in _tokens(tags_str) if not _has_cjk(t))


# ---- deterministic assembly ------------------------------------------------------

def _resolve_chars(appear: list, page: int) -> list:
    out = []
    for c in appear:
        state_cn, cloth = _clothing_for_page(c, page)
        anc = c.get("anchor") or {}
        if isinstance(anc, dict):
            anchor_danbooru = str(anc.get("anchor_danbooru", "") or "").strip()
            anchor_name = str(anc.get("character_name", "") or "").strip()
            anime_origin = str(anc.get("anime_origin", "") or "").strip()
        else:
            anchor_danbooru = ""
            anchor_name = str(anc or "").strip()
            anime_origin = ""
        clean_anchor_danbooru = _remove_cjk_tokens(anchor_danbooru) if anchor_danbooru else ""
        out.append({
            "name": str(c.get("name", "") or ""),
            "anchor": clean_anchor_danbooru,
            "anchor_name": anchor_name,
            "anime_origin": anime_origin,
            "danbooru_tags": str(c.get("danbooru_tags", "") or ""),
            "clothing_state_cn": state_cn,
            "clothing_tags": cloth,
            "gender": _norm_gender(c),
        })
    return out


def _assemble(count: str, resolved: list, scene: str, tail: str) -> str:
    parts = []
    if count:
        parts.append(count)
    for cr in resolved:
        block = []
        if cr["anchor"]:
            block.append(cr["anchor"])
        if cr["danbooru_tags"]:
            block.append(cr["danbooru_tags"])
        if cr["clothing_tags"]:
            block.append(cr["clothing_tags"])
        parts.append(", ".join(b for b in block if b))
    head = ", ".join(p for p in parts if p)
    scene_clean = str(scene or "").strip().rstrip(",")
    if scene_clean:
        return f"{head}, {scene_clean}{tail}"
    return f"{head}{tail}"


def _build_one(proj: Project, key: str, order: int, chars: list,
               negative: str, session_id: str, log):
    """Build one prompt entry -> (entry_dict, None) or (None, error_string)."""
    try:
        page, _no = parse_panel_key(key)
        md_path = proj.out_dir("s2") / "03_panels" / f"{key}.md"
        crop_path = proj.out_dir("s1") / "crops" / f"{key}.png"
        md = md_path.read_text(encoding="utf-8")
        bucket, w, h, aspect = _bucket_of_crop(crop_path)
        appear = _identify_characters(key, md, chars, session_id, log)
        resolved = _resolve_chars(appear, page)
        count = _count_tag(appear)
        scene, tail = _scene_block(key, md, session_id, log)
        positive = _assemble(count, resolved, scene, tail)
        
        # 动态性别隔离（硬隔离防止生图男女颠倒）
        has_female = any(cr["gender"] == "female" for cr in resolved)
        has_male = any(cr["gender"] == "male" for cr in resolved)
        gender_neg = []
        if has_female and not has_male:
            gender_neg = ["1boy", "male", "man", "boy", "gender swap", "gender change"]
        elif has_male and not has_female:
            gender_neg = ["1girl", "female", "woman", "girl", "gender swap", "gender change"]

        neg_parts = []
        if gender_neg:
            neg_parts.append(", ".join(gender_neg))
        if negative:
            neg_parts.append(negative)
        final_negative = ", ".join(neg_parts)

        seed = int(hashlib.md5(f"{proj.id}:{key}".encode()).hexdigest()[:8], 16)
        entry = {
            "panel": key,
            "page": page,
            "order": order,
            "positive": positive,
            "negative": final_negative,
            "anchor": [cr["anchor"] for cr in resolved if cr["anchor"]],
            "characters": resolved,
            "size_bucket": bucket,
            "width": w,
            "height": h,
            "seed": seed,
            "crop_aspect": round(aspect, 4),
            "scene_tags": scene,
            "quality_tail": tail,
            "count_tag": count,
        }
        _atomic_write_json(proj.out_dir("s3") / f"{key}.json", entry)
        log.info(f"{key}: prompt built (bucket={bucket}, chars={[c['name'] for c in resolved]}, "
                 f"seed={seed}, scene_tags={len(_tokens(scene))})")
        return entry, None
    except Exception as exc:  # noqa: BLE001 - per-panel failure must not kill the stage
        return None, f"{type(exc).__name__}: {exc}"


# ---- deterministic check & repair ------------------------------------------------

def _anchor_conflicts(prompts: list) -> set:
    seen: dict = {}
    for p in prompts:
        for cr in p.get("characters", []):
            seen.setdefault(cr.get("name", ""), set()).add(cr.get("anchor", ""))
    return {n for n, s in seen.items() if len(s) > 1}


def _check_one(prompt: dict, settings: dict, crop_dir):
    """Rules (1)(3)(4) for a single prompt; rule (2) is set-wide.
    -> (issues, token_counts, neg_warning)."""
    issues = []
    by_name = {c.get("name", ""): c for c in settings.get("characters") or []}
    positive = prompt.get("positive", "")
    present = _present_tokens(positive)
    neg_warn = ""
    if not present:
        issues.append("①positive 为空")
    missing = []
    n_app = n_cloth = 0
    for cr in prompt.get("characters", []):
        c = by_name.get(cr.get("name", ""))
        if c is None:
            issues.append(f"①人物 {cr.get('name','?')} 不在 settings")
            continue
        state_cn, cloth = _clothing_for_page(c, prompt.get("page", 1))
        n_app += len(_tokens(c.get("danbooru_tags")))
        n_cloth += len(_tokens(cloth))
        for t in _tokens(c.get("danbooru_tags")) + _tokens(cloth):
            if _strip_weight(t).lower() not in present:
                missing.append(f"{cr.get('name','?')}:{t}")
    if missing:
        issues.append("①外貌/衣着 token 缺失: " + ", ".join(missing))
    crop = crop_dir / f"{prompt.get('panel','')}.png"
    try:
        bucket, w, h, _ = _bucket_of_crop(crop)
        if bucket != prompt.get("size_bucket") or (w, h) != (prompt.get("width"), prompt.get("height")):
            issues.append(f"③尺寸桶不符: crop 实测 {bucket} {w}x{h} vs prompt "
                          f"{prompt.get('size_bucket')} {prompt.get('width')}x{prompt.get('height')}")
    except Exception as exc:
        issues.append(f"③crop 读取失败: {exc}")
    bad = [t for t in _tokens(positive) if _has_cjk(t)]
    if bad:
        issues.append("④positive 中文残留: " + "; ".join(bad))
    if _has_cjk(prompt.get("negative", "")):
        neg_warn = "negative 含中文（来自用户配置 api_neg_prompt，仅提示不判失败）"
    return issues, (n_app, n_cloth), neg_warn


_REPAIR_DESC = "按当前 settings 重建人物块、重算尺寸桶、剔除动态块中文 token 后重组 positive"


def _repair_one(prompt: dict, by_name: dict, crop_dir, log) -> None:
    """One repair round: refresh character data from current settings, recompute
    the bucket from the crop, strip CJK tokens from the dynamic block, rebuild
    the positive. Covers rules (1)(2)(3) and the positive part of (4)."""
    key = prompt.get("panel", "")
    page = prompt.get("page", 1)
    for cr in prompt.get("characters", []):
        c = by_name.get(cr.get("name", ""))
        if c is None:
            log.warning(f"{key}: character {cr.get('name','?')} missing from settings, keeping stored data")
            continue
        state_cn, cloth = _clothing_for_page(c, page)
        anc = c.get("anchor") or {}
        if isinstance(anc, dict):
            anchor_danbooru = str(anc.get("anchor_danbooru", "") or "").strip()
            anchor_name = str(anc.get("character_name", "") or "").strip()
            anime_origin = str(anc.get("anime_origin", "") or "").strip()
        else:
            anchor_danbooru = ""
            anchor_name = str(anc or "").strip()
            anime_origin = ""
        cr["anchor"] = _remove_cjk_tokens(anchor_danbooru) if anchor_danbooru else ""
        cr["anchor_name"] = anchor_name
        cr["anime_origin"] = anime_origin
        cr["danbooru_tags"] = str(c.get("danbooru_tags", "") or "")
        cr["clothing_state_cn"] = state_cn
        cr["clothing_tags"] = cloth
        cr["gender"] = _norm_gender(c)
    scene = _remove_cjk_tokens(prompt.get("scene_tags", ""))
    prompt["scene_tags"] = scene
    tail = _remove_cjk_tokens(prompt.get("quality_tail") or "")
    tail = tail.replace(", ", ",") if tail else "masterpiece,best quality"
    if not tail.startswith(","):
        tail = "," + tail
    if not tail.endswith(","):
        tail += ","
    prompt["quality_tail"] = tail
    try:
        bucket, w, h, _ = _bucket_of_crop(crop_dir / f"{key}.png")
        prompt["size_bucket"], prompt["width"], prompt["height"] = bucket, w, h
    except Exception as exc:
        log.warning(f"{key}: bucket recompute failed: {exc}")
    appear = [by_name.get(cr["name"]) for cr in prompt.get("characters", [])]
    known = [c for c in appear if c is not None]
    count = _count_tag(known) if known else prompt.get("count_tag", "")
    prompt["count_tag"] = count
    prompt["anchor"] = [cr["anchor"] for cr in prompt.get("characters", []) if cr.get("anchor")]
    prompt["positive"] = _assemble(count, prompt.get("characters", []), scene, tail)
    log.info(f"{key}: repaired positive rebuilt from settings")


def _run_check(proj: Project, prompts: list, settings: dict, log) -> dict:
    """Check + up to 2 repair rounds over the processed set; write
    s3_check/report.md; update panel items and the stage-level 'check' item.
    -> summary dict {"pass": n, "repaired": n, "unresolved": [keys]}"""
    crop_dir = proj.out_dir("s1") / "crops"
    out_dir = proj.out_dir("s3")
    check_dir = proj.out_dir("s3_check")
    by_name = {c.get("name", ""): c for c in settings.get("characters") or []}
    rounds: dict = {p["panel"]: 0 for p in prompts}
    issues_map: dict = {}
    warn_map: dict = {}
    counts_map: dict = {}

    def check_all():
        conflicts = _anchor_conflicts(prompts)
        for p in prompts:
            issues, counts, neg_warn = _check_one(p, settings, crop_dir)
            bad = sorted(conflicts & {cr.get("name", "") for cr in p.get("characters", [])})
            if bad:
                issues.append("②anchor 各镜不一致: " + ", ".join(bad))
            issues_map[p["panel"]] = issues
            counts_map[p["panel"]] = counts
            if neg_warn:
                warn_map[p["panel"]] = neg_warn

    check_all()
    initial = {k: list(v) for k, v in issues_map.items()}
    for round_no in range(1, _MAX_REPAIR_ROUNDS + 1):
        failing = [p for p in prompts if issues_map.get(p["panel"])]
        if not failing:
            break
        for p in failing:
            try:
                _repair_one(p, by_name, crop_dir, log)
                _atomic_write_json(out_dir / f"{p['panel']}.json", p)
                rounds[p["panel"]] += 1
                log.info(f"{p['panel']}: repair round {round_no} applied")
            except Exception as exc:  # noqa: BLE001
                log.error(f"{p['panel']}: repair crashed: {exc}")
        check_all()

    statuses = {}
    for p in prompts:
        key = p["panel"]
        if not issues_map.get(key):
            statuses[key] = "repaired" if rounds.get(key) else "pass"
        else:
            statuses[key] = "unresolved"

    # ---- report.md -------------------------------------------------------
    n_pass = sum(1 for s in statuses.values() if s == "pass")
    n_rep = sum(1 for s in statuses.values() if s == "repaired")
    n_unres = sum(1 for s in statuses.values() if s == "unresolved")
    lines = [
        "# S3 Prompt 全面检查报告",
        "",
        f"- 项目：{proj.id}",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 检查范围：本次处理集 {len(prompts)} 条",
        f"- 结果：PASS {n_pass} / REPAIRED {n_rep} / UNRESOLVED {n_unres}",
        "- 规则：①positive 全量携带人物外貌+衣着 token（权重先剥壳） ②同角色 anchor 各镜一致 "
        "③尺寸桶与 crop 长宽比一致 ④positive 无中文残留",
        "",
    ]
    for p in prompts:
        key = p["panel"]
        st = statuses[key]
        head = {"pass": "PASS", "repaired": f"REPAIRED（修复 {rounds.get(key, 0)} 轮）",
                "unresolved": "UNRESOLVED"}[st]
        lines.append(f"## {key} — {head}")
        n_app, n_cloth = counts_map.get(key, (0, 0))
        lines.append(f"- ① token：外貌 {n_app} + 衣着 {n_cloth}；桶 {p.get('size_bucket')}"
                     f"（crop aspect {p.get('crop_aspect')}）；seed {p.get('seed')}")
        if warn_map.get(key):
            lines.append(f"- 警告：{warn_map[key]}")
        if initial.get(key):
            lines.append("- 初始问题：")
            lines += [f"  - {i}" for i in initial[key]]
        if st == "repaired":
            lines.append(f"- 修复动作：确定性重建（{_REPAIR_DESC}），复查通过")
        elif st == "unresolved":
            lines.append("- 终态未通过项：")
            lines += [f"  - {i}" for i in issues_map[key]]
        lines.append("")
    report = check_dir / "report.md"
    report.write_text("\n".join(lines), encoding="utf-8")

    # ---- items ------------------------------------------------------------
    for p in prompts:
        key = p["panel"]
        st = statuses[key]
        if st == "unresolved":
            _set_item(proj, key, "failed", {"check": "unresolved", "issues": issues_map[key],
                                            "repairs": rounds.get(key, 0)})
        else:
            _set_item(proj, key, "completed",
                      {"check": st, "repairs": rounds.get(key, 0),
                       "bucket": p.get("size_bucket"), "seed": p.get("seed"),
                       "characters": [c.get("name") for c in p.get("characters", [])]})
    _set_item(proj, "check", "completed" if not n_unres else "failed",
              {"report": "s3_check/report.md", "pass": n_pass, "repaired": n_rep,
               "unresolved": [k for k, s in statuses.items() if s == "unresolved"]})
    log.info(f"s3 check: PASS {n_pass} / REPAIRED {n_rep} / UNRESOLVED {n_unres} -> {report}")
    return {"pass": n_pass, "repaired": n_rep, "unresolved": [k for k, s in statuses.items() if s == "unresolved"]}


# ---- stage entry -----------------------------------------------------------------

def run(proj: Project, **opts) -> bool:
    log = proj.get_logger("s3")
    t0 = time.time()
    panels_dir = proj.out_dir("s2") / "03_panels"
    settings_path = proj.out_dir("s2") / "00_settings.json"

    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.error(f"settings not found: {settings_path} (run s2 first)")
        return False
    except json.JSONDecodeError as exc:
        log.error(f"settings corrupt: {exc}")
        return False
    chars = [c for c in (settings.get("characters") or []) if isinstance(c, dict) and c.get("name")]
    if not chars:
        log.error("00_settings.json has no usable characters")
        return False

    all_keys = [f.stem for f in panels_dir.glob("*.md") if parse_panel_key(f.stem)]
    all_keys.sort(key=parse_panel_key)
    if not all_keys:
        log.error(f"no 03_panels/*.md under {panels_dir}")
        return False
    order_map = {k: i + 1 for i, k in enumerate(all_keys)}

    sel = opts.get("panels") or all_keys
    if not isinstance(sel, list):
        log.error(f"opts['panels'] must be a list, got {type(sel).__name__}")
        return False
    dropped = [k for k in sel if k not in order_map]
    if dropped:
        log.warning(f"unknown panels ignored: {dropped}")
    sel = [k for k in sel if k in order_map]
    if not sel:
        log.error("panel selection is empty")
        return False

    negative = opts.get("negative") or config.load_neg_prompt()
    concurrency = max(1, int(opts.get("concurrency", 2)))
    sessions = llm.new_session_pool()  # one per DGX; per-panel affinity
    out_dir = proj.out_dir("s3")

    build_todo, check_set = [], []
    for key in sel:
        path = out_dir / f"{key}.json"
        if proj.item_status("s3", key) == "completed" and path.exists():
            try:
                check_set.append(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError as exc:
                log.warning(f"{key}: stored prompt corrupt ({exc}), rebuilding")
                build_todo.append(key)
        else:
            build_todo.append(key)
    if len(check_set) < len(sel):
        log.info(f"resume: {len(check_set)} completed prompts skip build")
    log.info(f"s3 start: {len(build_todo)} to build, {len(check_set)} kept, "
             f"concurrency={concurrency}")

    ok = fail = 0
    if build_todo:
        def work(i, key):
            entry, err = _build_one(proj, key, order_map[key], chars,
                                    negative, sessions[i % len(sessions)], log)
            if entry is None:
                _set_item(proj, key, "failed", {"error": err})
                log.error(f"{key}: build failed: {err}")
                return False
            _set_item(proj, key, "completed",
                      {"bucket": entry["size_bucket"], "seed": entry["seed"],
                       "characters": [c["name"] for c in entry["characters"]]})
            return True

        with ThreadPoolExecutor(max_workers=min(concurrency, len(build_todo))) as ex:
            futs = [ex.submit(work, i, k) for i, k in enumerate(build_todo)]
            for fut in as_completed(futs):
                if fut.result():
                    ok += 1
                else:
                    fail += 1
        # fresh-load built entries so the check phase sees what is on disk
        for key in build_todo:
            path = out_dir / f"{key}.json"
            if path.exists():
                check_set.append(json.loads(path.read_text(encoding="utf-8")))
    check_set.sort(key=lambda p: parse_panel_key(p.get("panel", "")) or (9999, 99))

    summary = {"pass": 0, "repaired": 0, "unresolved": []}
    if check_set:
        summary = _run_check(proj, check_set, settings, log)
    else:
        _set_item(proj, "check", "completed", {"report": None, "note": "nothing to check"})
        log.warning("s3 check: no prompts to check (all builds failed?)")

    log.info(f"s3 summary: build {ok} ok / {fail} failed; "
             f"check pass {summary['pass']} / repaired {summary['repaired']} / "
             f"unresolved {len(summary['unresolved'])}, {time.time() - t0:.1f}s")
    # 2026-09-28 user-approved: any failed build must FAIL the stage so the DAG
    # retries it (old ">=1 build ok" criterion stranded failed panels forever)
    if build_todo and ok != len(build_todo):
        return False
    if summary["unresolved"]:
        log.error(f"s3 unresolved after repairs: {summary['unresolved']}")
        return False
    return True
