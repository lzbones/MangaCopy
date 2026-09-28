"""S7 video_gen: per-segment MiniMax-H3 video generation via ComfyUI.

Reads s6_h3/seg_XX.json (+ seg_XX.txt) and generates each segment serially (the
H3 model reloads between prompts, so no concurrency). Workflow selection:
mode "i2va" -> "API - video_minimax_h3_i2v" (upload first frame -> node 114,
megapixels -> node 119), mode "t2va" -> "API - video_minimax_h3_t2v"
(aspect_ratio + megapixels -> node 115). Common injections: prompt -> 105:104,
duration (capped at max_shot_seconds) -> 105:111 PrimitiveFloat, seed ->
105:15 RandomNoise, filename_prefix "{project_id}/seg_XX" -> 92 SaveVideo.

Idempotency: a segment whose s7 item is completed and whose
s7_videos/seg_XX.mp4 exists is skipped.

opts:
- dryrun (bool): build each workflow, log (and dump) the injected values,
  skip upload/submit/wait/fetch. Online preflight checks are skipped too.

A failing segment is recorded as item "failed" and the loop continues; the
stage fails only when no attempted segment succeeded.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import comfy, config
from .project import Project

WORKFLOWS = {
    "i2va": "API - video_minimax_h3_i2v",
    "t2va": "API - video_minimax_h3_t2v",
}

_SEG_JSON_RE = re.compile(r"^seg_(\d+)\.json$")


def _segment_specs(proj: Project) -> list:
    """s6_h3/seg_XX.json contents sorted by segment number."""
    specs = []
    for f in sorted(proj.out_dir("s6").glob("seg_*.json")):
        if not _SEG_JSON_RE.match(f.name):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except ValueError as exc:
            specs.append({"seg": int(_SEG_JSON_RE.match(f.name).group(1)),
                          "_bad": str(exc), "_file": f.name})
            continue
        specs.append(data)
    return sorted(specs, key=lambda s: int(s["seg"]))


def _build_workflow(proj: Project, seg: dict, prompt_text: str, seg_key: str,
                    dryrun: bool, log) -> dict:
    """Load the mode's workflow template and inject all parameters in place.
    Returns the workflow dict; injection values are logged by the caller via
    _log_injection()."""
    mode = seg["mode"]
    if mode not in WORKFLOWS:
        raise KeyError(f"unknown mode {mode!r} for {seg_key}")
    wf = comfy.load_workflow(WORKFLOWS[mode])
    megapixels = float(proj.params.get("megapixels", 0.4))
    max_s = float(proj.params.get("max_shot_seconds", 15.0))
    duration = min(float(seg["duration"]), max_s)

    if mode == "i2va":
        first_frame = Path(proj.dir) / seg["first_frame"]
        if dryrun:
            image_name = first_frame.name  # server-side name upload would return
        else:
            image_name = comfy.upload_image(first_frame)
        comfy.set_node(wf, "114", image=image_name)
        comfy.set_node(wf, "119", megapixels=megapixels)
    else:
        comfy.set_node(
            wf, "115",
            aspect_ratio=proj.params.get("video_aspect", "16:9 (Widescreen)"),
            megapixels=megapixels,
        )

    comfy.set_node(wf, "105:104", prompt=prompt_text)
    comfy.set_node(wf, "105:111", value=duration)
    comfy.set_node(wf, "105:15", noise_seed=int(seg["seed"]))
    comfy.set_node(wf, "92", filename_prefix=f"{proj.id}/{seg_key}")
    return wf


def _injection_summary(proj: Project, seg: dict, seg_key: str, wf: dict) -> dict:
    return {
        "seg_key": seg_key,
        "mode": seg["mode"],
        "workflow": WORKFLOWS[seg["mode"]],
        "114.image": wf.get("114", {}).get("inputs", {}).get("image"),
        "119.megapixels": wf.get("119", {}).get("inputs", {}).get("megapixels"),
        "115.aspect_ratio": wf.get("115", {}).get("inputs", {}).get("aspect_ratio"),
        "115.megapixels": wf.get("115", {}).get("inputs", {}).get("megapixels"),
        "105:104.prompt_chars": len(wf["105:104"]["inputs"]["prompt"]),
        "105:111.value": wf["105:111"]["inputs"]["value"],
        "105:15.noise_seed": wf["105:15"]["inputs"]["noise_seed"],
        "92.filename_prefix": wf["92"]["inputs"]["filename_prefix"],
        "first_frame": seg.get("first_frame"),
        "seed": seg.get("seed"),
        "panels": seg.get("panels"),
    }


def run(proj: Project, **opts) -> bool:
    log = proj.get_logger("s7")
    dryrun = bool(opts.get("dryrun"))
    out_dir = proj.out_dir("s7")

    # ---- pipeline consumption (2026-09-28 user 统筹 directive) ----
    # s7 launches as soon as s5's beats exist; wait for each s6 prompt to
    # appear (s6 runs in parallel) instead of blocking on the s6 stage
    # barrier, so the PRO 6000 produces videos while spark keeps working.
    beats_path = proj.out_dir("s5") / "00_beats.json"
    expected: set = set()
    if beats_path.exists():
        expected = {f"seg_{int(s['seg']):02d}"
                    for s in json.loads(beats_path.read_text(encoding="utf-8"))["segments"]}
        deadline = time.time() + 5400  # 90 min wait cap
        while True:
            have = {p.stem for p in proj.out_dir("s6").glob("seg_*.json")}
            missing = expected - have
            if not missing:
                break
            if proj.stage_status("s6") in ("completed", "failed") or time.time() > deadline:
                log.warning(f"s7: proceeding without {sorted(missing)} "
                            f"(s6 terminal or wait expired) — stage will fail "
                            f"so the DAG retries the missing segments")
                break
            log.info(f"s7: waiting for s6 segments {sorted(missing)} (s6 in progress)")
            time.sleep(60)

    specs = _segment_specs(proj)
    if not specs:
        log.error("no s6 segment json found in s6_h3/")
        return False

    todo, skipped = [], []
    for seg in specs:
        seg_key = f"seg_{int(seg['seg']):02d}"
        if (proj.item_status("s7", seg_key) == "completed"
                and (out_dir / f"{seg_key}.mp4").exists()):
            skipped.append(seg_key)
        else:
            todo.append(seg)
    if skipped:
        log.info(f"skip completed segments: {skipped}")

    if todo and not dryrun:
        # Preflight (DESIGN §8-6 subset applicable to s7: this stage makes no
        # LLM calls and no ffmpeg calls; LLM/ffmpeg checks are skipped):
        try:
            health = comfy.health()
            queue = comfy.queue_status()
            running = len(queue.get("queue_running") or [])
            pending = len(queue.get("queue_pending") or [])
            log.info(f"comfy preflight ok: version="
                     f"{health.get('system', {}).get('comfyui_version', '?')}, "
                     f"queue running={running} pending={pending}")
        except comfy.ComfyError as exc:
            log.error(f"comfy preflight failed: {exc}")
            return False

    ok = fail = 0
    for seg in todo:
        seg_key = f"seg_{int(seg['seg']):02d}"
        if seg.get("_bad"):
            log.error(f"{seg_key}: unreadable s6 json ({seg['_file']}): {seg['_bad']}")
            proj.set_item("s7", seg_key, "failed", {"error": f"bad json: {seg['_bad']}"})
            fail += 1
            continue
        txt_path = proj.out_dir("s6") / f"{seg_key}.txt"
        try:
            prompt_text = txt_path.read_text(encoding="utf-8")
        except OSError as exc:
            log.error(f"{seg_key}: missing H3 prompt file {txt_path}: {exc}")
            proj.set_item("s7", seg_key, "failed", {"error": f"missing prompt: {exc}"})
            fail += 1
            continue

        # S7 early-start (2026-09-28 user directive): s4 generates the
        # first-frame images in PARALLEL with this stage. Wait (poll 60 s,
        # max 30 min) for the segment's first frame instead of failing the
        # segment; downgrade to T2VA only after the wait budget is spent.
        if seg.get("mode") == "i2va" and seg.get("first_frame"):
            ff = Path(proj.dir) / seg["first_frame"]
            if not ff.exists():
                deadline = time.time() + 1800
                log.info(f"{seg_key}: waiting for first frame "
                         f"{seg['first_frame']} (s4 running in parallel)")
                while not ff.exists() and time.time() < deadline:
                    time.sleep(60)
                if not ff.exists():
                    log.warning(f"{seg_key}: first frame still missing after "
                                f"30 min — downgrading this segment to T2VA")
                    seg = dict(seg, mode="t2va", first_frame=None)

        try:
            wf = _build_workflow(proj, seg, prompt_text, seg_key, dryrun, log)
        except (comfy.ComfyError, KeyError, OSError) as exc:
            log.error(f"{seg_key}: workflow build failed: {type(exc).__name__}: {exc}")
            proj.set_item("s7", seg_key, "failed", {"error": f"build: {exc}"})
            fail += 1
            continue

        summary = _injection_summary(proj, seg, seg_key, wf)
        if dryrun:
            log.info(f"[dryrun] {seg_key} injected: {json.dumps(summary, ensure_ascii=False)}")
            (out_dir / f"{seg_key}.dryrun.json").write_text(
                json.dumps(wf, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            ok += 1
            continue

        try:
            pid = comfy.submit(wf)
            log.info(f"{seg_key}: submitted prompt_id={pid}")
            entry = comfy.wait(pid, timeout=config.COMFY_TIMEOUT)
            fetched = comfy.fetch_outputs(entry, out_dir)
            video = _pick_video(fetched)
            if video is None:
                raise comfy.ComfyError(f"no video in outputs: {[p.name for p in fetched]}")
            target = out_dir / f"{seg_key}.mp4"
            if video != target:
                video.replace(target)
            proj.set_item("s7", seg_key, "completed",
                          {"path": str(target), "prompt_id": pid,
                           "duration": summary["105:111.value"]})
            log.info(f"{seg_key}: video saved -> {target}")
            ok += 1
        except comfy.ComfyError as exc:
            log.error(f"{seg_key}: generation failed: {exc}")
            proj.set_item("s7", seg_key, "failed", {"error": str(exc)})
            fail += 1

    mode = " (dryrun)" if dryrun else ""
    log.info(f"s7 summary{mode}: {ok} ok / {fail} failed / {len(skipped)} skipped")
    # 2026-09-28 user-approved: partial failure must FAIL the stage so the DAG
    # retries it (old ">=1 segment ok" criterion stranded failed segments forever).
    # Additionally the criterion is against the EXPECTED segment set from the
    # s5 beats (pipeline consumption: missing s6 prompts also fail the stage
    # so the DAG retries them instead of silently assembling a partial video).
    if expected and (ok + len(skipped)) < len(expected):
        log.error(f"s7: {len(expected) - ok - len(skipped)} expected segment(s) "
                  f"missing — failing the stage for a DAG retry")
        return False
    return ok == len(todo)


def _pick_video(paths: list):
    """Prefer fetched outputs with a video container extension."""
    exts = {".mp4", ".webm", ".mov", ".mkv", ".avi"}
    for p in paths:
        if p.suffix.lower() in exts:
            return p
    return paths[0] if paths else None
