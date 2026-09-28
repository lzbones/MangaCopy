"""s7s8real: Phase F real video-generation e2e test, driven directly by team-lead.

Zero LLM calls (H3 prompts hand-written). Two segments:
  seg_01 i2va (upload first frame -> node 114), seg_02 t2va (16:9 via node 115),
then S8 assemble (single-seg fast path is covered implicitly by stream-copy
attempt order; multi-seg xfade/acrossfade chain is the main target).
"""
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path("/Users/qingxu/Documents/Software/AI/MangaCopy")
sys.path.insert(0, str(ROOT))

from mangacopy.project import Project  # noqa: E402
from mangacopy import stages  # noqa: E402
from PIL import Image  # noqa: E402

T0 = time.time()


def log(msg: str) -> None:
    print(f"[{time.time() - T0:7.1f}s] {msg}", flush=True)


# 1. Create real test project (kept for user review; cleanable afterwards)
proj = Project.create(
    ref_path=str(ROOT / "Ref" / "第187话"),
    slug="s7s8real",
    params={"style": "2d", "megapixels": 0.4},
)
log(f"project: {proj.dir} (id={proj.id})")

# 2. S0 ingest (local only)
assert stages.run_stage(proj, "s0"), "s0 ingest failed"
log("s0 done")

# 3. First frame for i2va: 16:9 crop from source page 1
src = proj.dir / "source" / "0001.png"
img = Image.open(src)
w, h = img.size
cw = w
ch = int(w * 9 / 16)
top = int(h * 0.30)  # upper-middle band, likely an action panel
crop = img.crop((0, top, cw, top + ch))
ff = proj.dir / "s4_images" / "p001_01.png"
crop.save(ff)
log(f"first frame: {ff} {crop.size}")

# 4. Hand-written H3 prompts (no LLM)
SEG1 = """For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.

integrated_multimodal_description: Black-and-white 2D cel animation in Japanese manga style, monochrome ink shading with screentone textures, dynamic speed lines. [Shot 1] A young woman with long dark hair in a dark battle uniform stands amid a ruined city street, fine dust drifting in the air; she keeps exactly the same face, hairstyle, body proportions and dark battle uniform as in <Picture 1> — identity lock, no identity swap, no changing face or hairstyle. Exactly one person appears. She narrows her eyes, plants her feet, then lunges forward with a swift horizontal slash of her blade; her hair trails the motion and dust bursts around her boots. The camera tracks right at medium speed to follow the lunge. She shouts <d>[Chinese] 就是现在！</d>
[Shot 2] At 00:03.500, the camera cuts to a low-angle medium shot: she lands in a crouch, blade extended, speed lines radiating behind her; the same young woman with identical face, hairstyle and dark battle uniform — identity lock, no identity swap, no changing face or hairstyle. Rubble settles around her and the dust begins to clear.

overall_soundscape: A blade slices through the air with a sharp whoosh; boots scrape concrete during the lunge; fine rubble ticks down onto pavement; wind whistles through the ruined street.

non_diegetic_music: Fast tempo taiko percussion with urgent staccato strings, rising dynamics through the lunge, ending on a single sustained low drum hit."""

SEG2 = """integrated_multimodal_description: Black-and-white 2D cel animation in Japanese manga style, monochrome ink shading with screentone textures. [Shot 1] A quiet school hallway at dusk, monochrome; a teenage girl with shoulder-length dark hair in a sailor school uniform walks slowly toward the camera — she has shoulder-length dark hair, calm dark eyes, and a navy sailor school uniform: identity lock, no identity swap, no changing face or hairstyle. Exactly one person appears. She stops by a window, gazes out at the bright sky, and closes her eyes briefly as a breeze lifts her hair; the camera pushes in with small amplitude at slow speed. She murmurs <d>[Chinese] 这场战斗……终于结束了。</d>
[Shot 2] At 00:03.000, the camera cuts to the view through the window: clouds drift slowly across the sky above the school buildings, and a few leaves swirl past the glass.

overall_soundscape: Soft indoor ambience with a faint clock ticking; footsteps echo lightly on the hallway floor; a gentle breeze flutters against the window frame.

non_diegetic_music: Slow tempo solo piano with sparse soft notes and a gentle sustained final chord."""


def seed_of(key: str) -> int:
    return int(hashlib.md5(f"{proj.id}:{key}".encode()).hexdigest()[:8], 16)


s6 = proj.out_dir("s6")
(s6 / "seg_01.txt").write_text(SEG1, encoding="utf-8")
(s6 / "seg_02.txt").write_text(SEG2, encoding="utf-8")
(s6 / "seg_01.json").write_text(json.dumps({
    "seg": 1, "mode": "i2va", "duration": 5.0,
    "first_frame": "s4_images/p001_01.png",
    "panels": ["p001_01"], "seed": seed_of("seg_01"),
}, ensure_ascii=False), encoding="utf-8")
(s6 / "seg_02.json").write_text(json.dumps({
    "seg": 2, "mode": "t2va", "duration": 5.0,
    "first_frame": None, "panels": ["p001_02"], "seed": seed_of("seg_02"),
}, ensure_ascii=False), encoding="utf-8")
log("s6 fixture written (2 segments)")

# 5. S7 real generation (serial, ~400-700s each)
ok7 = stages.run_stage(proj, "s7")
log(f"s7 result: {ok7}")

# 6. S8 assemble (local ffmpeg)
if ok7:
    ok8 = stages.run_stage(proj, "s8")
    log(f"s8 result: {ok8}")
    if ok8:
        log(f"FINAL: {proj.out_dir('s8') / 'final.mp4'}")
        for p in sorted(proj.out_dir("s7").glob("*.mp4")):
            log(f"  seg video: {p}")
log("ALL DONE")
