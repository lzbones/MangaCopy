"""s4real: Phase F real image-generation test (ComfyUI path fully real, LLM stubbed).

Panel p001_04 (page-1 panel 4) — prompt hand-written from the REAL S1
understanding of /tmp/mangacopy_s1s2_test (white-haired staff wielder striking
two creatures, manga monochrome). Size bucket computed with S3's exact rule
from the real S1 crop. Negative falls back to the user's tuned
Api/settings_for_Wai.json api_neg_prompt (live-loaded). Validation and the
preflight LLM ping are stubbed because spark has NOT been released by the
user; the ComfyUI submit/wait/fetch path is 100% real.
"""
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path("/Users/qingxu/Documents/Software/AI/MangaCopy")
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from mangacopy import config, llm, stages  # noqa: E402
from mangacopy.project import Project  # noqa: E402

T0 = time.time()
log = lambda m: print(f"[{time.time() - T0:6.1f}s] {m}", flush=True)  # noqa: E731

# 1. project + ingest
proj = Project.create(ref_path=str(ROOT / "Ref" / "第187话"), slug="s4real",
                      params={"style": "2d"})
assert stages.run_stage(proj, "s0"), "s0 failed"
log(f"project {proj.id}")

# 2. real S1 crop of p001_04 + S3-consistent size bucket
src_crop = Path("/tmp/mangacopy_s1s2_test/projects/20260926_s1s2test"
                "/s1_understand/crops/p001_04.png")
crops_dir = proj.out_dir("s1") / "crops"
crops_dir.mkdir(parents=True, exist_ok=True)
dst_crop = crops_dir / "p001_04.png"
shutil.copy(src_crop, dst_crop)
w, h = Image.open(dst_crop).size
aspect = w / h
bucket = "vertical" if aspect < 0.8 else ("horizontal" if aspect > 1.25 else "square")
bw, bh = config.IMAGE_SIZE_BUCKETS[bucket]
log(f"crop {w}x{h} aspect={aspect:.3f} -> bucket={bucket} {bw}x{bh}")

# 3. L3 script from the real S1 understanding (validation context)
pdir = proj.out_dir("s2") / "03_panels"
pdir.mkdir(parents=True, exist_ok=True)
(pdir / "p001_04.md").write_text("""# 分镜 p001_04（第1页 第4镜）
## 出场人物
- 白发持杖者（1girl）：白色长发向后飘动，头巾/发带，黑色紧身衣缀白色斑点；右手高举杖状武器，身体前倾作挥击姿态。
- 白色尖耳兽 ×2：白色毛发、尖耳；一只前肢举长杆格挡，一只在后方张口嘶鸣。
## 动作与姿态
持杖者向左侧双兽猛力挥击，双兽迎击，激烈交锋瞬间。
## 空间关系与构图
右半近景放大持杖者，左侧双兽为中景，倾斜构图。
## 镜头角度
平视中近景，放射状速度线强化冲击。
## 背景环境
抽象战斗空间，密集斜向速度线与飞散碎石。
## 对白原文
（无）
## 拟声词
（无）
## 氛围与情绪
紧张激烈；黑白漫画，网点与排线质感。
""", encoding="utf-8")

# 4. hand-written Wai prompt (from the real S1 detail; monochrome manga)
positive = ("1girl, white hair, very long hair, flying hair, headband, "
            "black bodysuit, polka dots, holding staff, raised arm, "
            "swinging weapon, lunging, dynamic pose, action scene, "
            "2 creatures, white fur, pointed ears, monster, holding weapon, "
            "blocking, open mouth, screaming, battle, speed lines, "
            "motion lines, flying debris, rubble, impact effects, dutch angle, "
            "medium shot, monochrome, greyscale, black and white, manga style, "
            "screentone, ink drawing, traditional manga, intense, "
            "masterpiece, best quality,")
seed = int(hashlib.md5(f"{proj.id}:p001_04".encode()).hexdigest()[:8], 16)
(proj.out_dir("s3") / "p001_04.json").write_text(json.dumps({
    "panel": "p001_04", "page": 1, "order": 4,
    "positive": positive, "negative": None, "anchor": "",
    "characters": ["白发持杖者"], "size_bucket": bucket,
    "width": bw, "height": bh, "seed": seed,
}, ensure_ascii=False), encoding="utf-8")
log(f"prompt fixture written, seed={seed}")

# 5. stub the LLM touchpoints (spark NOT released; ComfyUI path stays real)
llm.chat = lambda messages, **kw: "ok"
llm.chat_vision = lambda text, paths, **kw: (
    '{"pass": true, "issues": ["validation_stub: real spark check deferred"], '
    '"score": 9}')

# 6. real generation through the actual stage path
ok = stages.run_stage(proj, "s4")
log(f"s4 result: {ok}")
img = proj.out_dir("s4") / "p001_04.png"
size = img.stat().st_size if img.exists() else 0
log(f"image: {img} ({size} bytes)")
val = proj.out_dir("s4_validate") / "p001_04.json"
if val.exists():
    log(f"validate record: {val.read_text(encoding='utf-8')[:300]}")
log("ALL DONE")
