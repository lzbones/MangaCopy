"""S8 assemble: concatenate segment videos into s8_final/final.mp4.

Per the confirmed ruling (team-lead, 2026-09-26):
- multi-segment: video xfade d=0.45 + audio acrossfade d=0.45 (strict A/V
  alignment; both transitions occupy the same 0.45 s tail of the accumulated
  stream), output re-encoded with libx264 crf 18 — cross-segment re-encode is
  explicitly acceptable;
- single-segment: direct concat fast path (stream copy, no filters, no
  re-encode; falls back to a crf 18 re-encode only if the container cannot be
  copied);
- expected multi-segment duration = sum(d_i) - 0.45 * (N - 1).

ffmpeg binary comes from imageio_ffmpeg.get_ffmpeg_exe() (no system ffmpeg on
this machine). Segment parameters (resolution / fps / sample rate / channel
layout / audio presence) are probed first; if inconsistent, every input is
uniformly normalized inside the filter graph (scale + fps + aresample +
format) before the xfade chain — i.e. re-encoded uniformly then concatenated.
Inputs without an audio stream get a synthesized silent track so the
acrossfade chain stays valid.

Only segments whose s7 item is completed are used (s7_videos/seg_XX.mp4,
segment number order). No usable segment -> stage fails.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import imageio_ffmpeg

from .project import Project

XFADE_SECONDS = 0.45
CRF = 18
_FFMPEG_TIMEOUT = 1800  # generous: multi-segment crf 18 encodes can be slow

_SEG_RE = re.compile(r"^seg_(\d+)\.mp4$")
_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VID_RE = re.compile(r"Video:\s*.*?(\d{2,5})x(\d{2,5})")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s+fps")
_TBR_RE = re.compile(r"(\d+(?:\.\d+)?)\s+tbr")
_HZ_RE = re.compile(r"(\d+)\s+Hz")
_CH_RE = re.compile(r"\b(mono|stereo)\b")


def _ffmpeg_exe() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run_ffmpeg(exe: str, args: list, timeout: int = _FFMPEG_TIMEOUT):
    """Run ffmpeg with -hide_banner; returns (returncode, stderr_text)."""
    proc = subprocess.run(
        [exe, "-hide_banner", *args],
        capture_output=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stderr.decode("utf-8", "replace")


def _probe(exe: str, path: Path) -> dict:
    """Probe stream parameters by parsing ffmpeg -i stderr (no ffprobe binary
    ships with imageio_ffmpeg). Missing fields fall back to sane defaults."""
    rc, err = _run_ffmpeg(exe, ["-i", str(path)], timeout=60)
    m = _DUR_RE.search(err)
    duration = (
        int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        if m else 0.0
    )
    m = _VID_RE.search(err)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    fps = None
    m = _FPS_RE.search(err)
    if m:
        fps = float(m.group(1))
    else:
        m = _TBR_RE.search(err)
        if m:
            fps = float(m.group(1))
    has_audio = "Audio:" in err
    sr = int(m.group(1)) if (m := _HZ_RE.search(err)) else 48000
    ch = m.group(1) if (m := _CH_RE.search(err)) else "stereo"
    return {
        "path": path, "duration": duration, "w": w, "h": h,
        "fps": fps or 24.0, "has_audio": has_audio,
        "sample_rate": sr, "channels": ch if ch in ("mono", "stereo") else "stereo",
    }


def _completed_segments(proj: Project) -> list:
    """[(seg_no, path)] for segments whose s7 item is completed, in seg order."""
    out = []
    for f in proj.out_dir("s7").glob("seg_*.mp4"):
        m = _SEG_RE.match(f.name)
        if not m:
            continue
        no = int(m.group(1))
        if proj.item_status("s7", f"seg_{no:02d}") == "completed":
            out.append((no, f))
    return sorted(out)


def _add_silent_audio(exe: str, src: Path, sample_rate: int,
                      tmp_dir: Path) -> Path:
    """Mux a silent stereo track (duration = video duration) onto src."""
    tmp = tmp_dir / f"{src.stem}_silent.mp4"
    rc, err = _run_ffmpeg(exe, [
        "-y", "-i", str(src),
        "-f", "lavfi", "-i",
        f"anullsrc=channel_layout=stereo:sample_rate={sample_rate}",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-map", "0:v:0", "-map", "1:a:0",
        str(tmp),
    ], timeout=600)
    if rc != 0:
        raise RuntimeError(f"silent-audio mux failed for {src}: {err[-500:]}")
    return tmp


def _assemble_multi(exe: str, segs: list, probes: list, final: Path,
                    log) -> float:
    """xfade + acrossfade chain; returns the expected output duration."""
    n = len(segs)
    d = [p["duration"] for p in probes]
    ref = probes[0]
    consistent = all(
        p["w"] == ref["w"] and p["h"] == ref["h"]
        and abs(p["fps"] - ref["fps"]) < 0.01
        and p["sample_rate"] == ref["sample_rate"]
        and p["channels"] == ref["channels"]
        and p["has_audio"]
        for p in probes
    )
    # Determine target canvas resolution: prefer 16:9 or the dominant resolution across probes
    w, h, fps = ref["w"], ref["h"], ref["fps"]
    for p in probes:
        if p["w"] and p["h"] and abs(p["w"] / p["h"] - 16 / 9) < 0.1:
            w, h = p["w"], p["h"]
            break

    sr = ref["sample_rate"] if ref["has_audio"] else 48000
    layout = ref["channels"] if ref["has_audio"] else "stereo"
    if not consistent:
        log.warning(
            f"segment parameters inconsistent (res/fps/rate/layout/audio differ); "
            f"normalizing all inputs to {w}x{h}@{fps} with aspect-ratio preservation "
            f"(decrease scale + center black pad), {sr} Hz {layout}"
        )

    parts = []
    for i, p in enumerate(probes):
        vchain = []
        if not (p["w"] == w and p["h"] == h):
            vchain += [
                f"scale={w}:{h}:force_original_aspect_ratio=decrease",
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black",
            ]
        if not consistent:
            vchain += [f"fps={fps:g}"]
        vchain += ["format=yuv420p", "setsar=1"]
        parts.append(f"[{i}:v]" + ",".join(vchain) + f"[v{i}]")
        parts.append(
            f"[{i}:a]aresample={sr},"
            f"aformat=sample_fmts=fltp:channel_layouts={layout}[a{i}]"
        )

    acc = d[0]
    vcur, acur = "v0", "a0"
    for k in range(1, n):
        offset = round(acc - XFADE_SECONDS, 3)
        parts.append(
            f"[{vcur}][v{k}]xfade=transition=fade:"
            f"duration={XFADE_SECONDS}:offset={offset:g}[xv{k}]"
        )
        parts.append(f"[{acur}][a{k}]acrossfade=d={XFADE_SECONDS}[xa{k}]")
        acc = acc + d[k] - XFADE_SECONDS
        vcur, acur = f"xv{k}", f"xa{k}"

    args = ["-y"]
    for p in probes:
        args += ["-i", str(p["path"])]
    args += [
        "-filter_complex", ";".join(parts),
        "-map", f"[{vcur}]", "-map", f"[{acur}]",
        "-c:v", "libx264", "-crf", str(CRF), "-preset", "medium",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(final),
    ]
    rc, err = _run_ffmpeg(exe, args)
    if rc != 0:
        raise RuntimeError(f"xfade assembly failed (rc={rc}): {err[-800:]}")
    expected = acc
    log.info(
        f"assembled {n} segments with xfade/acrossfade d={XFADE_SECONDS}s, "
        f"crf {CRF}; expected duration {expected:.3f}s"
    )
    return expected


def run(proj: Project, **opts) -> bool:
    log = proj.get_logger("s8")
    out_dir = proj.out_dir("s8")
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / "final.mp4"

    segs = _completed_segments(proj)
    if not segs:
        log.error("no completed segment videos in s7_videos/")
        return False
    log.info(f"s8 start: {len(segs)} completed segments: "
             f"{[f'{no:02d}' for no, _ in segs]}")

    try:
        exe = _ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001 - imageio_ffmpeg failure
        log.error(f"ffmpeg unavailable: {type(exc).__name__}: {exc}")
        return False
    log.info(f"ffmpeg: {exe}")

    try:
        if len(segs) == 1:
            # Fast path: direct concat (stream copy).
            src = segs[0][1]
            rc, err = _run_ffmpeg(exe, ["-y", "-i", str(src), "-c", "copy",
                                        str(final)], timeout=600)
            if rc != 0:
                log.warning(f"stream copy failed, re-encoding at crf {CRF}: "
                            f"{err.strip().splitlines()[-1] if err.strip() else ''}")
                rc, err = _run_ffmpeg(exe, [
                    "-y", "-i", str(src),
                    "-c:v", "libx264", "-crf", str(CRF), "-preset", "medium",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                    str(final),
                ], timeout=900)
            if rc != 0:
                log.error(f"single-segment copy failed: {err[-500:]}")
                return False
            expected = None
            log.info(f"single segment: direct concat (stream copy) {src} -> {final}")
        else:
            probes = [_probe(exe, p) for _, p in segs]
            # Ensure every input has audio (acrossfade requires it).
            sr_ref = next((p["sample_rate"] for p in probes if p["has_audio"]), 48000)
            for i, p in enumerate(probes):
                if not p["has_audio"]:
                    silent = _add_silent_audio(exe, p["path"], sr_ref, out_dir)
                    log.warning(f"{p['path'].name}: no audio stream; synthesized "
                                f"silent track {silent.name}")
                    p["path"] = silent
            expected = _assemble_multi(exe, segs, probes, final, log)
            # cleanup silent-audio temp files
            for p in probes:
                if p["path"].name.endswith("_silent.mp4"):
                    p["path"].unlink(missing_ok=True)
    except (RuntimeError, subprocess.SubprocessError) as exc:
        log.error(f"s8 failed: {exc}")
        return False

    fin = _probe(exe, final)
    dur, has_audio = fin["duration"], fin["has_audio"]
    if expected is not None and abs(dur - expected) > 0.5:
        log.warning(f"final duration {dur:.3f}s deviates from expected "
                    f"{expected:.3f}s by {dur - expected:+.3f}s")
    log.info(f"final: {final} duration={dur:.3f}s audio={has_audio} "
             f"{fin['w']}x{fin['h']}@{fin['fps']:g}")
    proj.set_item("s8", "final", "completed",
                  {"path": str(final), "duration": dur,
                   "segments": len(segs), "audio": has_audio})
    return True
