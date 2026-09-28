"""S1/S2 acceptance test: real LLM calls, no ComfyUI.

Project lives under /tmp/mangacopy_s1s2_test (config.PROJECTS_DIR is
monkeypatched; base-layer code untouched). Reuses an existing project so the
rerun doubles as the idempotency check.
"""
import glob
import json
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = "/Users/qingxu/Documents/Software/AI/MangaCopy"
sys.path.insert(0, ROOT)

import mangacopy.config as config  # noqa: E402

TEST_ROOT = Path("/tmp/mangacopy_s1s2_test")
TEST_ROOT.mkdir(parents=True, exist_ok=True)
config.PROJECTS_DIR = TEST_ROOT / "projects"

from mangacopy.project import Project  # noqa: E402
from mangacopy.stages import run_stage  # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[test] {tag} {name}" + (f" | {detail}" if detail else ""), flush=True)
    (PASSED if cond else FAILED).append(name)
    return cond


def read_text_safe(path: Path):
    return path.read_text(encoding="utf-8") if path.exists() else None


def count_calls(log_path: Path):
    if not log_path.exists():
        return 0, 0, []
    text = log_path.read_text(encoding="utf-8")
    durs = [float(m) for m in re.findall(r"llm ok in ([0-9.]+)s", text)]
    return len(durs), len(re.findall(r"attempt \d+ failed", text)), durs


def has_cjk(s: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in s)


def verify_s1(proj):
    s1_dir = proj.out_dir("s1")
    for pg in (1, 2):
        pj_path = s1_dir / f"page_{pg:02d}.json"
        if not check(f"s1 page_{pg:02d}.json exists", pj_path.exists()):
            continue
        pj = json.loads(pj_path.read_text())
        check(f"s1 p{pg} top-level fields",
              all(k in pj for k in ("page", "file", "summary", "notes", "panels")))
        check(f"s1 p{pg} panels non-empty", len(pj["panels"]) > 0,
              f"{len(pj['panels'])} panels")
        crops_ok, bbox_ok = True, True
        dlg = []
        for p in pj["panels"]:
            if not (s1_dir / p.get("crop", "?")).exists():
                crops_ok = False
            x, y, w, h = p["bbox"]
            if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < w <= 1 and 0 < h <= 1):
                bbox_ok = False
            if p.get("key") != f"p{pg:03d}_{p['no']:02d}":
                bbox_ok = False
            dlg += [t.get("text", "") for t in p["detail"].get("dialogue", [])]
        check(f"s1 p{pg} crops exist", crops_ok)
        check(f"s1 p{pg} bbox normalized + key format", bbox_ok)
        # 汉化版对白应为中文；标题页 logo/署名等旁白转录允许非中文
        cjk_ratio = (sum(1 for t in dlg if has_cjk(t)) / len(dlg)) if dlg else 1.0
        check(f"s1 p{pg} dialogue mostly Chinese verbatim", cjk_ratio >= 0.8,
              f"{len(dlg)} entries, cjk {cjk_ratio:.0%}")
        if pg == 1:
            check("s1 p1 notes captures 汉化组/标题栏",
                  ("汉化" in pj.get("notes", "")) or ("第187话" in pj.get("notes", "")),
                  pj.get("notes", "")[:60])


def verify_s2(proj):
    s2_dir = proj.out_dir("s2")
    settings_path = s2_dir / "00_settings.json"
    if check("s2 00_settings.json exists", settings_path.exists()):
        st = json.loads(settings_path.read_text())
        check("s2 settings fields", all(k in st for k in
              ("characters", "environments", "style_notes")))
        check("s2 settings characters non-empty", len(st["characters"]) > 0,
              f"{len(st['characters'])} chars, {len(st['environments'])} envs")
        char_ok = True
        for c in st["characters"]:
            if not (isinstance(c.get("aliases"), list)
                    and c.get("gender") in ("male", "female", "unknown")
                    and isinstance(c.get("appearance_cn"), str)
                    and isinstance(c.get("danbooru_tags"), str)
                    and isinstance(c.get("anchor"), str)
                    and c["clothing_states"]):
                char_ok = False
        check("s2 character cards well-formed", char_ok)
        cov_ok, ovl_ok = True, True
        for c in st["characters"]:
            ivs = []
            for s in c["clothing_states"]:
                for part in s["pages"].split(","):
                    m = re.fullmatch(r"(\d+)(?:[-–~—](\d+))?", part.strip())
                    if m:
                        a = int(m.group(1)); b = int(m.group(2) or a)
                        ivs.append((min(a, b), max(a, b)))
            have = set()
            for a, b in ivs:
                have |= set(range(a, b + 1))
            if c["clothing_states"] and not ({1, 2} <= have):
                cov_ok = False
            s_ivs = sorted(ivs)
            for i in range(1, len(s_ivs)):
                if s_ivs[i][0] <= s_ivs[i - 1][1]:
                    ovl_ok = False
        check("s2 clothing_states cover pages 1-2", cov_ok)
        check("s2 clothing_states no overlap", ovl_ok)
        check("s2 00_settings.md exists", (s2_dir / "00_settings.md").exists())

    ov = read_text_safe(s2_dir / "01_overview.md")
    if check("s2 01_overview.md exists", ov is not None):
        check("s2 01_overview.md has sections",
              all(h in ov for h in ("# 总体复述", "# 氛围基调", "# 叙事逻辑链", "# 人物关系")))

    s1_dir = proj.out_dir("s1")
    for pg in (1, 2):
        p2 = s2_dir / "02_pages" / f"page_{pg:02d}.md"
        if check(f"s2 02_pages/page_{pg:02d}.md exists", p2.exists()):
            txt = p2.read_text()
            check(f"s2 L2 page {pg} sections",
                  all(h in txt for h in ("# 剧情脉络", "# 分镜顺序与衔接", "# 与前后页的承接")))
        s1j = json.loads((s1_dir / f"page_{pg:02d}.json").read_text())
        heads = ["## 分镜编号与位置", "## 出场人物", "## 动作与姿态", "## 空间关系与构图",
                 "## 镜头角度", "## 背景环境", "## 对白原文", "## 拟声词", "## 氛围与情绪", "## 备注"]
        for p in s1j["panels"]:
            key = p["key"]
            f3 = s2_dir / "03_panels" / f"{key}.md"
            if not check(f"s2 03_panels/{key}.md exists", f3.exists()):
                continue
            txt = f3.read_text()
            check(f"s2 L3 {key} ten fixed headers", all(h in txt for h in heads))

    rep = read_text_safe(proj.out_dir("s2_check") / "report.md")
    check("s2 check report exists", rep is not None)
    if rep is not None:
        print(f"[test] check report: rounds={len(re.findall(chr(35)+'+ ' + '第 . 轮检查', rep))}, "
              f"UNRESOLVED lines={len(re.findall('UNRESOLVED', rep))}", flush=True)

    items = proj.state["stages"]["s2"]["items"]
    bad = [k for k, v in items.items() if v["status"] != "completed"]
    check("s2 all items completed", not bad, f"items={sorted(items)} bad={bad}")


def main():
    t_start = time.time()
    existing = sorted(glob.glob(str(config.PROJECTS_DIR / "*_s1s2test")))
    if existing:
        proj = Project.load(existing[-1])
        print(f"[test] reuse project: {proj.dir}", flush=True)
    else:
        proj = Project.create(ref_path=f"{ROOT}/Ref/第187话", slug="s1s2test")
        print(f"[test] project created: {proj.dir}", flush=True)

    log1, log2 = proj.dir / "logs" / "s1.log", proj.dir / "logs" / "s2.log"
    base1, base2 = count_calls(log1)[:2], count_calls(log2)[:2]

    # ---- s0 ----
    if proj.stage_status("s0") != "completed":
        ok0 = run_stage(proj, "s0")
    else:
        ok0 = True
        print("[test] s0 already completed", flush=True)
    check("s0 completed", ok0 and proj.stage_status("s0") == "completed")
    manifest = json.loads((proj.out_dir("s0") / "manifest.json").read_text())
    check("s0 manifest 13 pages", manifest["count"] == 13, f"count={manifest['count']}")

    # ---- s1 (pages 1,2) ----
    t1 = time.time()
    ok1 = run_stage(proj, "s1", pages=[1, 2])
    print(f"[test] s1 wall time (this run): {time.time() - t1:.0f}s", flush=True)
    check("s1 completed", ok1 and proj.stage_status("s1") == "completed")
    verify_s1(proj)

    # ---- idempotent rerun: s1 pages=[1] must skip ----
    ok3 = run_stage(proj, "s1", pages=[1])
    check("s1 rerun pages=[1] completed", ok3)
    check("s1 rerun skipped page 1",
          "skip completed pages: [1]" in log1.read_text(encoding="utf-8"))

    # ---- s2 (pages 1,2) ----
    t2 = time.time()
    ok2 = run_stage(proj, "s2", pages=[1, 2])
    print(f"[test] s2 wall time (this run): {time.time() - t2:.0f}s", flush=True)
    check("s2 completed", ok2 and proj.stage_status("s2") == "completed")
    verify_s2(proj)

    # ---- LLM call accounting (this run) ----
    for label, path, base in (("s1", log1, base1), ("s2", log2, base2)):
        okc, failed, durs = count_calls(path)
        okc -= base[0]
        failed -= base[1]
        tot = okc + failed
        print(f"[test] {label} llm attempts this run: total={tot} ok={okc} "
              f"failed={failed} mean_ok={sum(durs) / max(1, len(durs)):.1f}s",
              flush=True)

    print(f"[test] {len(PASSED)} checks passed, {len(FAILED)} failed, "
          f"total wall {time.time() - t_start:.0f}s", flush=True)
    if FAILED:
        print("[test] FAILED CHECKS:\n- " + "\n- ".join(FAILED), flush=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
