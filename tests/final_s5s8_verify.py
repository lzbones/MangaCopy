#!/Users/qingxu/.ai-env/bin/python
"""Final consolidated verification for S5-S8.

Phase A (LLM): one retry of the real seg_02 t2va generation via the module's
  own _process_segment (the model glitched on it twice in the previous run).
Phase B (local): corrected deterministic_checks unit tests (fixture bugs fixed:
  both required names present in the good prompt; 3-shot monotonicity case).
Phase C (local): s7 dryrun injection verification for BOTH modes (real seg_02
  t2va if generated; seg_01 i2va fabricated content-agnostically — the real
  i2va content checks passed in the previous run, see its log).
Phase D (local): s8 xfade assembly test (2 x 3 s -> 5.55 s) + single-segment
  stream-copy fast path.
No ComfyUI submission at any point. Cleanup of all /tmp artifacts at the end.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path("/Users/qingxu/Documents/Software/AI/MangaCopy")
sys.path.insert(0, str(REPO))

SNAP = Path("/tmp/mangacopy_s5s8_snapshot")
TEST_ROOT = Path("/tmp/mangacopy_s5s8_test")

from mangacopy import stages, llm, h3_prompt, assemble  # noqa: E402
from mangacopy.project import Project  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((bool(cond), name))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main():
    print("=" * 70)
    print("Final consolidated verification — project:", TEST_ROOT)
    print("=" * 70)

    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)
    shutil.copytree(SNAP, TEST_ROOT)
    (TEST_ROOT / "logs" / "s6.log").unlink(missing_ok=True)
    (TEST_ROOT / "logs" / "s7.log").unlink(missing_ok=True)
    (TEST_ROOT / "logs" / "s8.log").unlink(missing_ok=True)
    proj = Project.load(TEST_ROOT)
    settings = json.loads(
        (proj.out_dir("s2") / "00_settings.json").read_text(encoding="utf-8"))
    beats = json.loads(
        (proj.out_dir("s5") / "00_beats.json").read_text(encoding="utf-8"))["segments"]
    seg2 = next(s for s in beats if int(s["seg"]) == 2)
    style_card = (proj.out_dir("s5") / "00_style.md").read_text(encoding="utf-8")

    # ---- Phase A: retry the real seg_02 (t2va) generation ----
    print("\n---- Phase A: real seg_02 (t2va) generation retry ----")
    log = proj.get_logger("s6")
    try:
        r = h3_prompt._process_segment(proj, seg2, settings, style_card,
                                       llm.new_session_id(), log)
    except Exception as exc:  # noqa: BLE001 - fall back to fabricated t2va below
        print(f"seg_02 retry raised: {type(exc).__name__}: {exc}")
        r = {"pass": False, "problems": [str(exc)], "rounds": None}
    seg2_ok = (proj.out_dir("s6") / "seg_02.txt").exists()
    check("seg_02 real generation retry produced files", seg2_ok,
          f"pass={r['pass']} rounds={r['rounds']} problems={r['problems']}")

    # ---- Phase B: corrected deterministic_checks unit tests ----
    print("\n---- Phase B: deterministic_checks unit tests (fixtures fixed) ----")
    good = (
        h3_prompt.I2VA_FIRST_LINE + "\n\n"
        "integrated_multimodal_description: [Shot 1] 2D-animated. 苏离, 1girl, long straight black hair, "
        "amber eyes, wearing off-white hooded sweatshirt and dark blue jeans, identity lock, no identity "
        "swap, no changing face or hairstyle, exactly one character in this shot. She says: "
        "<d>[Chinese] 今晚的雨，来得比预想中更早。</d> [Shot 2] At 00:03.500, the camera cuts wider; "
        "陈默, 1boy, short messy dark gray hair, black jacket, joins, identity lock, no identity swap, "
        "no changing face or hairstyle.\n\n"
        "overall_soundscape: Rain taps on the metal railing.\n\n"
        "non_diegetic_music: N/A\n"
    )
    both = ["苏离", "陈默"]
    base = (good, "i2va", 8.5, ["今晚的雨，来得比预想中更早。"], both)
    errs = h3_prompt.deterministic_checks(*base)
    check("unit: valid prompt passes", not errs, "; ".join(errs)[:300])
    check("unit: timestamp out of duration flagged",
          any("越界" in e for e in h3_prompt.deterministic_checks(
              good.replace("At 00:03.500", "At 00:08.500"), *base[1:])))
    check("unit: non-increasing timestamps flagged (3 shots)",
          any("递增" in e for e in h3_prompt.deterministic_checks(
              good.replace("joins,", "joins. [Shot 3] At 00:02.000, the camera holds,"),
              *base[1:])))
    check("unit: missing identity lock flagged",
          any("身份锁短语" in e for e in h3_prompt.deterministic_checks(
              good.replace("identity lock, no identity swap, no changing face or hairstyle, ", ""),
              *base[1:])))
    check("unit: character name missing from lock flagged",
          any("角色名" in e for e in h3_prompt.deterministic_checks(
              good.replace("苏离, 1girl", "the girl, 1girl"), *base[1:])))
    check("unit: unbalanced <d> tags flagged",
          any("配平" in e for e in h3_prompt.deterministic_checks(
              good.replace("</d> [Shot 2]", " [Shot 2]"), *base[1:])))
    check("unit: tampered dialogue flagged",
          any("对白" in e for e in h3_prompt.deterministic_checks(
              good.replace("今晚的雨，来得比预想中更早。", "今晚的雨来得比预想中更早"), *base[1:])))
    check("unit: I2VA wrong first line flagged",
          any("I2VA 首行" in e for e in h3_prompt.deterministic_checks(
              "For the target video, <Picture 1> is fully referenced.\n\n" + good.split("\n\n", 1)[1],
              *base[1:])))
    check("unit: T2VA containing I2VA line flagged",
          any("I2VA" in e for e in h3_prompt.deterministic_checks(
              h3_prompt.I2VA_FIRST_LINE + "\n\n" + good.split("\n\n", 1)[1],
              "t2va", *base[2:])))
    check("unit: [Shot 1] with timestamp flagged",
          any("Shot 1" in e for e in h3_prompt.deterministic_checks(
              good.replace("[Shot 1] 2D-animated", "[Shot 1] At 00:00.000, 2D-animated"),
              *base[1:])))
    check("unit: shot numbering gap flagged",
          any("不连续" in e for e in h3_prompt.deterministic_checks(
              good.replace("[Shot 2] At 00:03.500", "[Shot 3] At 00:03.500"), *base[1:])))

    # ---- Phase C: s7 dryrun injection verification (both modes) ----
    print("\n---- Phase C: s7 dryrun (i2va + t2va injection verification) ----")
    # seg_01 i2va fabricated json+txt (content-agnostic mechanism check; the real
    # i2va content passed all structural checks in the previous run's log).
    seg1_json = {"seg": 1, "mode": "i2va", "duration": 8.0,
                 "first_frame": "s4_images/p001_01.png", "panels": ["p001_01"],
                 "seed": h3_prompt.deterministic_seed("s5s8test", "seg_01")}
    (proj.out_dir("s6") / "seg_01.json").write_text(
        json.dumps(seg1_json, ensure_ascii=False, indent=2), encoding="utf-8")
    (proj.out_dir("s6") / "seg_01.txt").write_text(good, encoding="utf-8")
    proj.set_item("s6", "seg_01", "completed", {"mode": "i2va"})
    if seg2_ok:
        proj.set_item("s6", "seg_02", "completed", {"mode": "t2va"})

    ok7 = stages.run_stage(proj, "s7", dryrun=True)
    check("s7 dryrun returns True", ok7, proj.stage_status("s7"))

    wf1 = json.loads((proj.out_dir("s7") / "seg_01.dryrun.json").read_text(encoding="utf-8"))
    check("s7 dryrun seg_01 (i2va): 114.image == first frame name",
          wf1["114"]["inputs"]["image"] == "p001_01.png", wf1["114"]["inputs"]["image"])
    check("s7 dryrun seg_01 (i2va): 119.megapixels == params.megapixels",
          wf1["119"]["inputs"]["megapixels"] == proj.params["megapixels"])
    check("s7 dryrun seg_01 (i2va): 115 untouched in i2v workflow",
          wf1["115"]["inputs"]["aspect_ratio"] == "1:1 (Square)")
    check("s7 dryrun seg_01 (i2va): 105:111.value == 8.0",
          float(wf1["105:111"]["inputs"]["value"]) == 8.0)
    check("s7 dryrun seg_01 (i2va): seed + filename_prefix injected",
          wf1["105:15"]["inputs"]["noise_seed"] == seg1_json["seed"]
          and wf1["92"]["inputs"]["filename_prefix"] == "s5s8test/seg_01")

    if seg2_ok:
        wf2 = json.loads((proj.out_dir("s7") / "seg_02.dryrun.json").read_text(encoding="utf-8"))
        meta2 = json.loads((proj.out_dir("s6") / "seg_02.json").read_text(encoding="utf-8"))
        txt2 = (proj.out_dir("s6") / "seg_02.txt").read_text(encoding="utf-8")
        check("s7 dryrun seg_02 (t2va, REAL prompt): 115.aspect_ratio == video_aspect",
              wf2["115"]["inputs"]["aspect_ratio"] == proj.params["video_aspect"],
              wf2["115"]["inputs"]["aspect_ratio"])
        check("s7 dryrun seg_02 (t2va): 115.megapixels == params.megapixels",
              wf2["115"]["inputs"]["megapixels"] == proj.params["megapixels"])
        check("s7 dryrun seg_02 (t2va): t2v workflow has no 114/119 nodes",
              "114" not in wf2 and "119" not in wf2)
        dur2 = min(float(meta2["duration"]), float(proj.params["max_shot_seconds"]))
        check("s7 dryrun seg_02 (t2va): 105:111.value == min(duration, max)",
              abs(float(wf2["105:111"]["inputs"]["value"]) - dur2) < 1e-9,
              f"{wf2['105:111']['inputs']['value']} vs {dur2}")
        check("s7 dryrun seg_02 (t2va): 105:104.prompt == real seg_02.txt",
              wf2["105:104"]["inputs"]["prompt"].strip() == txt2.strip())
        # structural verification of the REAL t2va prompt (t2va rules)
        seg2_md = (proj.out_dir("s5") / "segments" / "seg_02.md").read_text(encoding="utf-8")
        _, names2 = h3_prompt._character_tags(
            settings, seg2_md, sorted({int(pk[1:4]) for pk in meta2["panels"]}))
        dlg2 = []
        for pk in meta2["panels"]:
            l3 = (proj.out_dir("s2") / "03_panels" / f"{pk}.md").read_text(encoding="utf-8")
            dlg2.extend(h3_prompt._dialogue_lines_from_l3(l3))
        derrs2 = h3_prompt.deterministic_checks(
            txt2, "t2va", float(meta2["duration"]), dlg2, names2)
        check("s6 seg_02.txt (REAL t2va) deterministic checks clean "
              "(T2VA first-field start / timestamps / <d> / dialogue / identity lock)",
              not derrs2, "; ".join(derrs2)[:400])
        check("s6 seg_02 seed deterministic",
              meta2["seed"] == h3_prompt.deterministic_seed("s5s8test", "seg_02"))
    else:
        # fallback: fabricate a t2va seg_02 for the injection mechanism check
        seg2_json = {"seg": 2, "mode": "t2va", "duration": 6.0, "first_frame": None,
                     "panels": ["p001_02"], "seed": 20260926}
        (proj.out_dir("s6") / "seg_02.json").write_text(
            json.dumps(seg2_json, ensure_ascii=False, indent=2), encoding="utf-8")
        (proj.out_dir("s6") / "seg_02.txt").write_text(
            "integrated_multimodal_description: [Shot 1] 2D-animated rooftop.\n\n"
            "overall_soundscape: Rain.\n\nnon_diegetic_music: N/A\n", encoding="utf-8")
        stages.run_stage(proj, "s7", dryrun=True)
        wf2 = json.loads((proj.out_dir("s7") / "seg_02.dryrun.json").read_text(encoding="utf-8"))
        check("s7 dryrun seg_02 (t2va, fabricated): 115 aspect+megapixels injected",
              wf2["115"]["inputs"]["aspect_ratio"] == "16:9 (Widescreen)"
              and wf2["115"]["inputs"]["megapixels"] == 0.7)

    # ---- Phase D: s8 assembly (local ffmpeg only) ----
    print("\n---- Phase D: s8 assembly ----")
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
        assert rc == 0

    make_test_video(proj.out_dir("s7") / "seg_01.mp4", 440)
    make_test_video(proj.out_dir("s7") / "seg_02.mp4", 880)
    proj.set_item("s7", "seg_01", "completed", {"path": "s7_videos/seg_01.mp4"})
    proj.set_item("s7", "seg_02", "completed", {"path": "s7_videos/seg_02.mp4"})
    ok8 = stages.run_stage(proj, "s8")
    fin = assemble._probe(exe, proj.out_dir("s8") / "final.mp4")
    check("s8 2-segment xfade assembly: duration ~= 5.55 s",
          ok8 and abs(fin["duration"] - 5.55) <= 0.2, f"{fin['duration']:.3f}s")
    check("s8 final has audio track", fin["has_audio"])

    proj.set_item("s7", "seg_02", "failed", {"error": "simulate not completed"})
    ok8b = stages.run_stage(proj, "s8")
    fin1 = assemble._probe(exe, proj.out_dir("s8") / "final.mp4")
    check("s8 single-segment fast path: duration ~= 3.0 s (stream copy)",
          ok8b and abs(fin1["duration"] - 3.0) <= 0.15, f"{fin1['duration']:.3f}s")

    # ---- accounting ----
    s6_log = (TEST_ROOT / "logs" / "s6.log").read_text(encoding="utf-8")
    ok_calls = len(re.findall(r"llm ok in ", s6_log))
    fails = len(re.findall(r"attempt \d+ failed", s6_log))
    print(f"\nseg_02 retry LLM calls: ok={ok_calls} failed_attempts={fails}")

    # ---- cleanup all /tmp artifacts ----
    shutil.rmtree(TEST_ROOT)
    shutil.rmtree(SNAP)
    check("all /tmp test artifacts cleaned up",
          not TEST_ROOT.exists() and not SNAP.exists())

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
