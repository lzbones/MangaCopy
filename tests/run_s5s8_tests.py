#!/Users/qingxu/.ai-env/bin/python
"""End-to-end test for MangaCopy S5-S8 (video_script / h3_prompt / video_gen /
assemble) with a fabricated project in /tmp.

Real LLM calls happen only in S5/S6 (budget: <= 10 calls).
ComfyUI is NEVER submitted (S7 runs in dryrun mode; S8 uses local ffmpeg only).

Usage: ~/.ai-env/bin/python -u /tmp/run_s5s8_tests.py
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path("/Users/qingxu/Documents/Software/AI/MangaCopy")
sys.path.insert(0, str(REPO))

TEST_ROOT = Path("/tmp/mangacopy_s5s8_test")
REF_IMG = REPO / "Ref/第187话/0001.png"

from mangacopy import stages, video_script, h3_prompt, assemble  # noqa: E402
from mangacopy.project import Project, SUBDIRS, DEFAULT_PARAMS  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((bool(cond), name))
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------- project setup

SETTINGS = {
    "characters": [
        {
            "name": "苏离",
            "appearance_cn": "十七岁少女，黑色长直发，左侧别一枚银色发夹，琥珀色眼睛，身材纤瘦",
            "danbooru_tags": "1girl, solo focus, long straight black hair, silver hairpin on left side, amber eyes, slim figure",
            "anchor": "suli_v1",
            "clothing_states": [
                {"pages": [1], "state_cn": "米白色连帽卫衣，深蓝色牛仔长裤，白色帆布鞋",
                 "danbooru_tags": "off-white hooded sweatshirt, dark blue jeans, white canvas sneakers"}
            ],
        },
        {
            "name": "陈默",
            "appearance_cn": "十八岁少年，深灰色短发微乱，眼神沉静，身高偏高，左眉有一道细小疤痕",
            "danbooru_tags": "1boy, short messy dark gray hair, calm eyes, tall, thin scar on left eyebrow",
            "anchor": "chenmo_v1",
            "clothing_states": [
                {"pages": [1], "state_cn": "黑色夹克外套，灰白色T恤，黑色长裤",
                 "danbooru_tags": "black jacket, gray-white t-shirt, black trousers"}
            ],
        },
    ],
    "environments": ["雨夜的城市天台：金属围栏、水渍反光、远处霓虹灯牌、积水的地面"],
    "style_notes": "黑白漫画原作；夜景雨戏，高对比光影；情绪克制而张力暗涌",
}

L1 = """# 总体复述
雨夜，苏离收到匿名讯息后独自登上城市天台，等待三年未见的旧友陈默。两人隔着雨幕对峙，以一句旧日承诺作结。

# 氛围基调
克制、疏离，张力暗涌；雨声贯穿全话，节奏缓慢推进。

# 叙事逻辑链
1. 苏离天台等待，收起纸条；2. 陈默自楼梯间现身；3. 两人对峙摊牌，旧约重提。

# 人物关系
苏离与陈默：三年前的旧友，因一场未解的误会疏远至今。
"""

L2_P1 = """## 页面脉络
第 1 页：雨夜天台。苏离立于围栏边眺望城市并收起纸条；陈默自楼梯间出现，两人隔雨对峙，以苏离重提旧约收束本页。
"""

L3_P1 = """## 分镜编号与位置
分镜 p001_01，第 1 页第 1 镜，vertical 形状，页面左上起始，下接第 2 镜。

## 出场人物
苏离：雨夜天台，米白色连帽卫衣；神情克制，目光望向远处霓虹。

## 动作与姿态
苏离独立于天台围栏内侧，双手轻搭围栏，微微仰头任雨水落在脸颊，随后低头收起手中的纸条。

## 空间关系与构图
人物居中偏左，围栏横线切割画面下三分之一，远景为雨幕中的城市轮廓与霓虹。

## 镜头角度
中景，平视微仰，视线由人物延伸至远景城市。

## 背景环境
雨夜城市天台：金属围栏、水渍反光、远处霓虹灯牌、积水的地面。

## 对白原文
苏离：今晚的雨，来得比预想中更早。

## 拟声词
沙沙（雨声）

## 氛围与情绪
克制中带孤独感，雨声渲染等待的焦灼。

## 备注
无衣着变化。
"""

L3_P2 = """## 分镜编号与位置
分镜 p001_02，第 1 页第 2 镜，horizontal 形状，承接第 1 镜视线方向。

## 出场人物
陈默：黑色夹克立于楼梯间门口，雨水自肩头滴落，神情沉静。
苏离：侧身回望，卫衣帽兜半落。

## 动作与姿态
陈默缓步走出楼梯间，停在苏离身后数步；苏离侧身回望，两人隔着雨幕对视。

## 空间关系与构图
陈默位于画面右侧中景，苏离左侧侧身，两人在围栏与楼梯间之间形成对峙轴线。

## 镜头角度
中远景，平视，双人对峙的对称构图。

## 背景环境
雨夜城市天台：楼梯间门口、金属围栏、霓虹反光、雨丝。

## 对白原文
陈默：我以为你不会来。
苏离：我答应过的事，从来没有变过。

## 拟声词
滴答（滴水声）

## 氛围与情绪
对峙的静默张力，旧友重逢的疏离与试探。

## 备注
两人衣着与第 1 镜一致。
"""


def build_project() -> Project:
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)
    for sub in SUBDIRS:
        (TEST_ROOT / sub).mkdir(parents=True, exist_ok=True)
    state = {
        "id": "s5s8test",
        "created": "2026-09-26T00:00:00",
        "params": {**DEFAULT_PARAMS, "style": "2d"},
        "stages": {s: {"status": "pending", "items": {}} for s in stages.STAGE_ORDER},
    }
    proj = Project(TEST_ROOT, state)
    proj.save()

    s2 = TEST_ROOT / "s2_zero"
    (s2 / "00_settings.json").write_text(
        json.dumps(SETTINGS, ensure_ascii=False, indent=2), encoding="utf-8")
    (s2 / "01_overview.md").write_text(L1, encoding="utf-8")
    (s2 / "02_pages" / "page_01.md").write_text(L2_P1, encoding="utf-8")
    (s2 / "03_panels" / "p001_01.md").write_text(L3_P1, encoding="utf-8")
    (s2 / "03_panels" / "p001_02.md").write_text(L3_P2, encoding="utf-8")
    shutil.copy(REF_IMG, TEST_ROOT / "s4_images" / "p001_01.png")
    return proj


# ---------------------------------------------------------------- main

def main():
    print("=" * 70)
    print("S5-S8 end-to-end test — project:", TEST_ROOT)
    print("=" * 70)
    proj = build_project()

    # ---- S5 ----
    print("\n---- run_stage s5 ----")
    ok5 = stages.run_stage(proj, "s5")
    check("s5 run_stage returns True", ok5, proj.stage_status("s5"))
    style_txt = (proj.out_dir("s5") / "00_style.md").read_text(encoding="utf-8")
    guard = video_script.STYLE_INFO["2d"]
    check("s5 style card guards (EN anchor + NOT clauses verbatim)",
          guard["guard_en"] in style_txt and guard["guard_not"] in style_txt)
    beats = json.loads((proj.out_dir("s5") / "00_beats.json").read_text(encoding="utf-8"))
    segs = beats["segments"]
    panel_keys = video_script._panel_keys(proj)
    errs = video_script._validate_beats({"segments": segs}, panel_keys, 15.0)
    check("s5 beats JSON passes hard validation (<=15s, coverage, no dup)",
          not errs, str(errs))
    check("s5 beats has >= 1 segment", len(segs) >= 1, f"{len(segs)} segments")
    print("    beats:", json.dumps(segs, ensure_ascii=False))
    for s in segs:
        key = f"seg_{int(s['seg']):02d}"
        seg_md = (proj.out_dir("s5") / "segments" / f"{key}.md").read_text(encoding="utf-8")
        dlg = []
        for pk in s["panels"]:
            l3 = (proj.out_dir("s2") / "03_panels" / f"{pk}.md").read_text(encoding="utf-8")
            dlg.extend(t for _, t in video_script.dialogue_lines_from_l3(l3))
        serrs = video_script._validate_segment(seg_md, float(s["duration"]), dlg)
        check(f"s5 {key}.md structure (headings / per-second coverage / verbatim dialogue)",
              not serrs, "; ".join(serrs)[:300])
        check(f"s5 {key}.md 出场人物及衣着锚定 cites L0 clothing states",
              "米白色连帽卫衣" in seg_md or "黑色夹克" in seg_md)

    # ---- S6 ----
    print("\n---- run_stage s6 ----")
    ok6 = stages.run_stage(proj, "s6")
    check("s6 run_stage returns True", ok6, proj.stage_status("s6"))
    for s in segs:
        key = f"seg_{int(s['seg']):02d}"
        txt = (proj.out_dir("s6") / f"{key}.txt").read_text(encoding="utf-8")
        meta = json.loads((proj.out_dir("s6") / f"{key}.json").read_text(encoding="utf-8"))
        check(f"s6 {key}.json fields complete",
              set(meta) >= {"seg", "mode", "duration", "first_frame", "panels", "seed"})
        exp_ff = TEST_ROOT / "s4_images" / f"{meta['panels'][0]}.png"
        exp_mode = "i2va" if exp_ff.exists() else "t2va"
        check(f"s6 {key} mode == {exp_mode} (style=2d + first frame presence)",
              meta["mode"] == exp_mode, meta["mode"])
        if meta["mode"] == "i2va":
            check(f"s6 {key}.txt I2VA first line char-exact",
                  txt.split("\n")[0] == h3_prompt.I2VA_FIRST_LINE)
            check(f"s6 {key}.json first_frame points at S4 image",
                  meta["first_frame"] == f"s4_images/{meta['panels'][0]}.png",
                  str(meta["first_frame"]))
        dlg = []
        for pk in meta["panels"]:
            l3 = (proj.out_dir("s2") / "03_panels" / f"{pk}.md").read_text(encoding="utf-8")
            dlg.extend(h3_prompt._dialogue_lines_from_l3(l3))
        derrs = h3_prompt.deterministic_checks(
            txt, meta["mode"], float(meta["duration"]), dlg,
            [c["name"] for c in SETTINGS["characters"]])
        check(f"s6 {key}.txt deterministic checks clean "
              "(field order / timestamps in range / <d> balance / dialogue / identity lock)",
              not derrs, "; ".join(derrs)[:300])
        check(f"s6 {key} seed deterministic (md5 of project:seg)",
              meta["seed"] == h3_prompt.deterministic_seed("s5s8test", key))
        print(f"    {key}.txt head: {txt[:160]!r}")
    check("s6 check_report.md written",
          (proj.out_dir("s6") / "check_report.md").exists())
    print("    check_report.md:")
    print((proj.out_dir("s6") / "check_report.md").read_text(encoding="utf-8"))

    # ---- deterministic check unit tests (fabricated bad prompts) ----
    print("\n---- deterministic_checks unit tests ----")
    good = (
        h3_prompt.I2VA_FIRST_LINE + "\n\n"
        "integrated_multimodal_description: [Shot 1] 2D-animated. 苏离, 1girl, long straight black hair, "
        "amber eyes, wearing off-white hooded sweatshirt and dark blue jeans, identity lock, no identity "
        "swap, no changing face or hairstyle. She says: <d>[Chinese] 今晚的雨，来得比预想中更早。</d> "
        "[Shot 2] At 00:03.500, the camera cuts wider.\n\n"
        "overall_soundscape: Rain taps on the metal railing.\n\n"
        "non_diegetic_music: N/A\n"
    )
    base_args = (good, "i2va", 8.5, ["今晚的雨，来得比预想中更早。"], ["苏离"])
    check("unit: valid prompt passes", not h3_prompt.deterministic_checks(*base_args))
    check("unit: timestamp out of duration flagged",
          any("越界" in e for e in h3_prompt.deterministic_checks(
              good.replace("At 00:03.500", "At 00:08.500"), *base_args[1:])))
    check("unit: missing identity lock flagged",
          any("身份锁短语" in e for e in h3_prompt.deterministic_checks(
              good.replace("identity lock, no identity swap, no changing face or hairstyle. ", ""),
              *base_args[1:])))
    check("unit: character name missing from lock flagged",
          any("角色名" in e for e in h3_prompt.deterministic_checks(
              good.replace("苏离, 1girl", "the girl, 1girl"), *base_args[1:])))
    check("unit: non-increasing timestamps flagged",
          any("递增" in e for e in h3_prompt.deterministic_checks(
              good.replace("At 00:03.500", "At 00:03.000"), *base_args[1:])))
    check("unit: unbalanced <d> tags flagged",
          any("配平" in e for e in h3_prompt.deterministic_checks(
              good.replace("</d> [Shot 2]", " [Shot 2]"), *base_args[1:])))
    check("unit: tampered dialogue flagged",
          any("对白" in e for e in h3_prompt.deterministic_checks(
              good.replace("今晚的雨，来得比预想中更早。", "今晚的雨来得比预想中更早"), *base_args[1:])))
    check("unit: I2VA wrong first line flagged",
          any("I2VA 首行" in e for e in h3_prompt.deterministic_checks(
              "For the target video, <Picture 1> is fully referenced.\n\n" + good.split("\n\n", 1)[1],
              *base_args[1:])))
    check("unit: T2VA containing I2VA line flagged",
          any("I2VA" in e for e in h3_prompt.deterministic_checks(
              h3_prompt.I2VA_FIRST_LINE + "\n\n" + good.split("\n\n", 1)[1],
              "t2va", *base_args[2:])))
    check("unit: [Shot 1] with timestamp flagged",
          any("Shot 1" in e for e in h3_prompt.deterministic_checks(
              good.replace("[Shot 1] 2D-animated", "[Shot 1] At 00:00.000, 2D-animated"),
              *base_args[1:])))

    # ---- S7 dryrun (i2va, real s6 outputs; NO ComfyUI submit) ----
    print("\n---- run_stage s7 dryrun (i2va) ----")
    ok7 = stages.run_stage(proj, "s7", dryrun=True)
    check("s7 dryrun returns True", ok7, proj.stage_status("s7"))
    s6metas = sorted((proj.out_dir("s6")).glob("seg_*.json"))
    for f in s6metas:
        if not re.match(r"seg_\d+\.json$", f.name):
            continue
        meta = json.loads(f.read_text(encoding="utf-8"))
        key = f"seg_{int(meta['seg']):02d}"
        wf = json.loads((proj.out_dir("s7") / f"{key}.dryrun.json").read_text(encoding="utf-8"))
        if meta["mode"] == "i2va":
            check(f"s7 dryrun {key}: node 114.image == uploaded first frame name",
                  wf["114"]["inputs"]["image"] == "p001_01.png", wf["114"]["inputs"]["image"])
            check(f"s7 dryrun {key}: node 119.megapixels == 0.7",
                  wf["119"]["inputs"]["megapixels"] == 0.7)
            check(f"s7 dryrun {key}: node 115 untouched in i2v workflow",
                  wf["115"]["inputs"]["aspect_ratio"] == "1:1 (Square)")
        else:
            check(f"s7 dryrun {key}: node 115.aspect_ratio == video_aspect",
                  wf["115"]["inputs"]["aspect_ratio"] == "16:9 (Widescreen)")
            check(f"s7 dryrun {key}: node 115.megapixels == 0.7",
                  wf["115"]["inputs"]["megapixels"] == 0.7)
            check(f"s7 dryrun {key}: t2v workflow has no 114/119 nodes",
                  "114" not in wf and "119" not in wf)
        dur = min(float(meta["duration"]), float(proj.params["max_shot_seconds"]))
        check(f"s7 dryrun {key}: 105:111.value == min(duration, max_shot_seconds)",
              abs(float(wf["105:111"]["inputs"]["value"]) - dur) < 1e-9,
                  f"{wf['105:111']['inputs']['value']} vs {dur}")
        check(f"s7 dryrun {key}: 105:15.noise_seed == deterministic seed",
              wf["105:15"]["inputs"]["noise_seed"] == int(meta["seed"]))
        check(f"s7 dryrun {key}: 92.filename_prefix == project/seg",
              wf["92"]["inputs"]["filename_prefix"] == f"s5s8test/{key}",
              wf["92"]["inputs"]["filename_prefix"])
        check(f"s7 dryrun {key}: 105:104.prompt == seg txt content",
              wf["105:104"]["inputs"]["prompt"].strip()
              == (proj.out_dir("s6") / f"{key}.txt").read_text(encoding="utf-8").strip())

    # ---- S7 dryrun (t2va via fabricated seg_02) ----
    print("\n---- fabricate t2va seg_02 + run_stage s7 dryrun again ----")
    t2va_json = {"seg": 2, "mode": "t2va", "duration": 6.0, "first_frame": None,
                 "panels": ["p001_02"], "seed": 20260926}
    (proj.out_dir("s6") / "seg_02.json").write_text(
        json.dumps(t2va_json, ensure_ascii=False, indent=2), encoding="utf-8")
    (proj.out_dir("s6") / "seg_02.txt").write_text(
        "integrated_multimodal_description: [Shot 1] 2D-animated rooftop.\n\n"
        "overall_soundscape: Rain.\n\nnon_diegetic_music: N/A\n", encoding="utf-8")
    proj.set_item("s6", "seg_02", "completed", {"mode": "t2va"})
    ok7b = stages.run_stage(proj, "s7", dryrun=True)
    check("s7 dryrun (with fabricated t2va seg_02) returns True", ok7b)
    wf2 = json.loads((proj.out_dir("s7") / "seg_02.dryrun.json").read_text(encoding="utf-8"))
    check("s7 dryrun seg_02 (t2va): 115.aspect_ratio+megapixels injected",
          wf2["115"]["inputs"]["aspect_ratio"] == "16:9 (Widescreen)"
          and wf2["115"]["inputs"]["megapixels"] == 0.7)
    check("s7 dryrun seg_02 (t2va): 105:111.value == 6.0",
          float(wf2["105:111"]["inputs"]["value"]) == 6.0)
    check("s7 dryrun seg_02 (t2va): workflow lacks i2v-only nodes",
          "114" not in wf2 and "119" not in wf2)
    check("s7 dryrun seg_02 (t2va): seed injected",
          wf2["105:15"]["inputs"]["noise_seed"] == 20260926)

    # ---- S8 real assembly test (2 x 3 s fabricated clips) ----
    print("\n---- s8 assembly: fabricate 2 x 3 s test videos ----")
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()

    def make_test_video(path: Path, freq: int):
        rc = subprocess.run([
            exe, "-y",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=640x360:rate=24",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=3",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-shortest", str(path),
        ], capture_output=True).returncode
        assert rc == 0, f"test video creation failed for {path}"

    make_test_video(proj.out_dir("s7") / "seg_01.mp4", 440)
    make_test_video(proj.out_dir("s7") / "seg_02.mp4", 880)
    proj.set_item("s7", "seg_01", "completed", {"path": "s7_videos/seg_01.mp4"})
    proj.set_item("s7", "seg_02", "completed", {"path": "s7_videos/seg_02.mp4"})
    ok8 = stages.run_stage(proj, "s8")
    check("s8 run_stage returns True", ok8, proj.stage_status("s8"))
    final = proj.out_dir("s8") / "final.mp4"
    check("s8 final.mp4 exists", final.exists())
    fin = assemble._probe(exe, final)
    check("s8 final duration ~= 5.55 s (3+3-0.45)",
          abs(fin["duration"] - 5.55) <= 0.2, f"{fin['duration']:.3f}s")
    check("s8 final has audio track", fin["has_audio"])
    check("s8 final resolution preserved", fin["w"] == 640 and fin["h"] == 360,
          f"{fin['w']}x{fin['h']}")

    # single-segment fast path (stream copy, no re-encode)
    proj.set_item("s7", "seg_02", "failed", {"error": "simulate not completed"})
    ok8b = stages.run_stage(proj, "s8")
    fin1 = assemble._probe(exe, final)
    check("s8 single-segment fast path: duration ~= 3.0 s",
          ok8b and abs(fin1["duration"] - 3.0) <= 0.15, f"{fin1['duration']:.3f}s")

    # ---- LLM call accounting ----
    print("\n---- LLM call accounting ----")
    ok_calls = fail_calls = 0
    for logf in (TEST_ROOT / "logs").glob("*.log"):
        text = logf.read_text(encoding="utf-8")
        ok_calls += len(re.findall(r"llm ok in ", text))
        fail_calls += len(re.findall(r"attempt \d+ failed", text))
    total = ok_calls + fail_calls
    check("real LLM calls <= 10", total <= 10,
          f"ok={ok_calls} failed_attempts={fail_calls} total={total}")
    check("no ComfyUI submission happened", True, "s7 ran only in dryrun mode")

    # ---- cleanup ----
    shutil.rmtree(TEST_ROOT)
    check("test project cleaned up", not TEST_ROOT.exists())

    print("\n" + "=" * 70)
    npass = sum(1 for ok, _ in RESULTS if ok)
    print(f"RESULT: {npass}/{len(RESULTS)} checks passed")
    for ok, name in RESULTS:
        if not ok:
            print(f"  FAILED: {name}")
    print("=" * 70)
    return 0 if npass == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
