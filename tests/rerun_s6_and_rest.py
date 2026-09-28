#!/Users/qingxu/.ai-env/bin/python
"""Rerun S6 (with the 900 s LLM timeout fix) + S7 dryrun + S8 from the
post-S5 snapshot. S5 is only re-entered to prove idempotency (zero LLM
calls — all items already completed). Real LLM calls: S6 only.
ComfyUI is never submitted."""

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

from mangacopy import stages, video_script, h3_prompt, assemble  # noqa: E402
from mangacopy.project import Project, DEFAULT_PARAMS  # noqa: E402

CHARACTERS = ["苏离", "陈默"]

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((bool(cond), name))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main():
    print("=" * 70)
    print("S6 rerun + downstream test — project:", TEST_ROOT)
    print("=" * 70)

    # restore snapshot (post-S5 state)
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)
    shutil.copytree(SNAP, TEST_ROOT)
    # clean slate for s6 logs (stale lines from the killed run would skew accounting)
    (TEST_ROOT / "logs" / "s6.log").unlink(missing_ok=True)
    (TEST_ROOT / "logs" / "s7.log").unlink(missing_ok=True)
    (TEST_ROOT / "logs" / "s8.log").unlink(missing_ok=True)

    proj = Project.load(TEST_ROOT)
    check("restored snapshot: s5 stage completed", proj.stage_status("s5") == "completed")

    # ---- S5 idempotency (no LLM calls expected) ----
    ok5 = stages.run_stage(proj, "s5")
    check("s5 rerun returns True (all items skipped)", ok5)
    s5_calls = len(re.findall(r"llm ok in ", (TEST_ROOT / "logs" / "s5.log").read_text(encoding="utf-8")))
    check("s5 rerun made zero additional LLM calls", s5_calls == 4, f"cumulative llm-ok count still {s5_calls}")

    beats = json.loads((proj.out_dir("s5") / "00_beats.json").read_text(encoding="utf-8"))
    segs = beats["segments"]
    print("    beats:", json.dumps(segs, ensure_ascii=False))

    # ---- S6 (timeout fix applied) ----
    print("\n---- run_stage s6 ----")
    ok6 = stages.run_stage(proj, "s6")
    check("s6 run_stage returns True", ok6, proj.stage_status("s6"))
    for s in segs:
        key = f"seg_{int(s['seg']):02d}"
        txt_path = proj.out_dir("s6") / f"{key}.txt"
        json_path = proj.out_dir("s6") / f"{key}.json"
        if not txt_path.exists() or not json_path.exists():
            check(f"s6 {key} outputs written", False, "txt/json missing")
            continue
        txt = txt_path.read_text(encoding="utf-8")
        meta = json.loads(json_path.read_text(encoding="utf-8"))
        check(f"s6 {key}.json fields complete",
              set(meta) >= {"seg", "mode", "duration", "first_frame", "panels", "seed"},
              json.dumps(meta, ensure_ascii=False))
        exp_ff = TEST_ROOT / "s4_images" / f"{meta['panels'][0]}.png"
        exp_mode = "i2va" if exp_ff.exists() else "t2va"
        check(f"s6 {key} mode == {exp_mode} (style=2d + first frame presence)",
              meta["mode"] == exp_mode, meta["mode"])
        if meta["mode"] == "i2va":
            check(f"s6 {key}.txt I2VA first line char-exact",
                  txt.split("\n")[0] == h3_prompt.I2VA_FIRST_LINE, repr(txt.split("\n")[0][:70]))
            check(f"s6 {key}.json first_frame == s4 image of first panel",
                  meta["first_frame"] == f"s4_images/{meta['panels'][0]}.png", str(meta["first_frame"]))
        dlg = []
        for pk in meta["panels"]:
            l3 = (proj.out_dir("s2") / "03_panels" / f"{pk}.md").read_text(encoding="utf-8")
            dlg.extend(h3_prompt._dialogue_lines_from_l3(l3))
        derrs = h3_prompt.deterministic_checks(
            txt, meta["mode"], float(meta["duration"]), dlg, CHARACTERS)
        check(f"s6 {key}.txt deterministic checks clean "
              "(field order / timestamps in range / <d> balance / dialogue / identity lock)",
              not derrs, "; ".join(derrs)[:400])
        check(f"s6 {key} seed deterministic", meta["seed"] == h3_prompt.deterministic_seed("s5s8test", key))
        print(f"    {key}.txt first 200 chars: {txt[:200]!r}")
    check("s6 check_report.md written", (proj.out_dir("s6") / "check_report.md").exists())
    print("    check_report.md:")
    print((proj.out_dir("s6") / "check_report.md").read_text(encoding="utf-8"))

    # ---- deterministic check unit tests ----
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
    base = (good, "i2va", 8.5, ["今晚的雨，来得比预想中更早。"], CHARACTERS)
    check("unit: valid prompt passes", not h3_prompt.deterministic_checks(*base))
    check("unit: timestamp out of duration flagged",
          any("越界" in e for e in h3_prompt.deterministic_checks(
              good.replace("At 00:03.500", "At 00:08.500"), *base[1:])))
    check("unit: missing identity lock flagged",
          any("身份锁短语" in e for e in h3_prompt.deterministic_checks(
              good.replace("identity lock, no identity swap, no changing face or hairstyle. ", ""),
              *base[1:])))
    check("unit: character name missing flagged",
          any("角色名" in e for e in h3_prompt.deterministic_checks(
              good.replace("苏离, 1girl", "the girl, 1girl"), *base[1:])))
    check("unit: non-increasing timestamps flagged",
          any("递增" in e for e in h3_prompt.deterministic_checks(
              good.replace("At 00:03.500", "At 00:03.000"), *base[1:])))
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
              h3_prompt.I2VA_FIRST_LINE + "\n\n" + good.split("\n\n", 1)[1], "t2va", *base[2:])))
    check("unit: [Shot 1] with timestamp flagged",
          any("Shot 1" in e for e in h3_prompt.deterministic_checks(
              good.replace("[Shot 1] 2D-animated", "[Shot 1] At 00:00.000, 2D-animated"), *base[1:])))

    # ---- S7 dryrun (real s6 outputs; NO ComfyUI submit) ----
    print("\n---- run_stage s7 dryrun ----")
    ok7 = stages.run_stage(proj, "s7", dryrun=True)
    check("s7 dryrun returns True", ok7, proj.stage_status("s7"))
    for f in sorted((proj.out_dir("s6")).glob("seg_*.json")):
        if not re.match(r"seg_\d+\.json$", f.name):
            continue
        meta = json.loads(f.read_text(encoding="utf-8"))
        key = f"seg_{int(meta['seg']):02d}"
        wf = json.loads((proj.out_dir("s7") / f"{key}.dryrun.json").read_text(encoding="utf-8"))
        if meta["mode"] == "i2va":
            check(f"s7 dryrun {key} (i2va): 114.image == first frame name",
                  wf["114"]["inputs"]["image"] == f"{meta['panels'][0]}.png",
                  wf["114"]["inputs"]["image"])
            check(f"s7 dryrun {key} (i2va): 119.megapixels == 0.7",
                  wf["119"]["inputs"]["megapixels"] == proj.params["megapixels"])
            check(f"s7 dryrun {key} (i2va): 115 untouched",
                  wf["115"]["inputs"]["aspect_ratio"] == "1:1 (Square)")
        else:
            check(f"s7 dryrun {key} (t2va): 115.aspect_ratio == video_aspect",
                  wf["115"]["inputs"]["aspect_ratio"] == proj.params["video_aspect"])
            check(f"s7 dryrun {key} (t2va): 115.megapixels == 0.7",
                  wf["115"]["inputs"]["megapixels"] == proj.params["megapixels"])
            check(f"s7 dryrun {key} (t2va): t2v workflow has no 114/119 nodes",
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
        check(f"s7 dryrun {key}: 105:104.prompt == seg txt",
              wf["105:104"]["inputs"]["prompt"].strip()
              == (proj.out_dir("s6") / f"{key}.txt").read_text(encoding="utf-8").strip())

    # ---- S8 real assembly (2 x 3 s fabricated clips) ----
    print("\n---- s8 assembly: 2 x 3 s fabricated test videos ----")
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
    check("s8 run_stage returns True", ok8, proj.stage_status("s8"))
    final = proj.out_dir("s8") / "final.mp4"
    check("s8 final.mp4 exists", final.exists())
    fin = assemble._probe(exe, final)
    check("s8 final duration ~= 5.55 s (3+3-0.45, xfade+acrossfade)",
          abs(fin["duration"] - 5.55) <= 0.2, f"{fin['duration']:.3f}s")
    check("s8 final has audio track", fin["has_audio"])
    check("s8 final resolution preserved", fin["w"] == 640 and fin["h"] == 360,
          f"{fin['w']}x{fin['h']}")

    proj.set_item("s7", "seg_02", "failed", {"error": "simulate not completed"})
    ok8b = stages.run_stage(proj, "s8")
    fin1 = assemble._probe(exe, final)
    check("s8 single-segment fast path: duration ~= 3.0 s (stream copy)",
          ok8b and abs(fin1["duration"] - 3.0) <= 0.15, f"{fin1['duration']:.3f}s")

    # ---- accounting & cleanup ----
    print("\n---- LLM call accounting (this rerun) ----")
    s6_log = (TEST_ROOT / "logs" / "s6.log").read_text(encoding="utf-8")
    ok_calls = len(re.findall(r"llm ok in ", s6_log))
    fail_attempts = len(re.findall(r"attempt \d+ failed", s6_log))
    print(f"s6 llm calls this run: ok={ok_calls} failed_attempts={fail_attempts} "
          f"(total incl. content-returning retries <= {ok_calls + fail_attempts})")
    check("s6 LLM calls within reasonable budget", ok_calls + fail_attempts <= 8,
          f"ok={ok_calls} failed={fail_attempts}")

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
