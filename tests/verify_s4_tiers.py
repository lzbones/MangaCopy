"""Verify the image_quality standard/hd switch and run a real standard-tier
generation (p001_02, silver-haired swordswoman from real S1 understanding).
LLM stubbed (spark not released); ComfyUI path fully real.
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

from mangacopy import config, llm  # noqa: E402
from mangacopy import image_gen  # noqa: E402
from mangacopy.project import Project  # noqa: E402

T0 = time.time()
log = lambda m: print(f"[{time.time() - T0:6.1f}s] {m}", flush=True)  # noqa: E731

proj = Project.load(ROOT / "data" / "projects" / "20260926_s4real")

# ---- unit: both templates inject correctly ----
demo = {"panel": "p001_02", "positive": "x", "negative": "y",
        "width": 1408, "height": 1024}
wf_std = image_gen._build_workflow(demo, 42, "pid", config.IMAGE_WORKFLOWS["standard"])
assert "12" not in wf_std, "standard must not contain node 12"
assert wf_std["8"]["inputs"]["filename_prefix"] == "pid/p001_02"
assert wf_std["9"]["inputs"]["samples"] == ["5", 0], "SaveImage must chain from node 9"
wf_hd = image_gen._build_workflow(demo, 42, "pid", config.IMAGE_WORKFLOWS["hd"])
assert wf_hd["12"]["inputs"]["width"] == round(1408 * 1.5), "hd node12 = bucket*1.5"
assert wf_hd["12"]["inputs"]["height"] == round(1024 * 1.5)
log("unit PASS: standard(no node12, save<-decode) / hd(node12=2112x1536)")

# ---- fixture: p001_02 from real S1 understanding ----
src_crop = Path("/tmp/mangacopy_s1s2_test/projects/20260926_s1s2test"
                "/s1_understand/crops/p001_02.png")
crops = proj.out_dir("s1") / "crops"
crops.mkdir(parents=True, exist_ok=True)
shutil.copy(src_crop, crops / "p001_02.png")
w, h = Image.open(src_crop).size
aspect = w / h
bucket = "vertical" if aspect < 0.8 else ("horizontal" if aspect > 1.25 else "square")
bw, bh = config.IMAGE_SIZE_BUCKETS[bucket]
log(f"crop {w}x{h} aspect={aspect:.2f} -> {bucket} {bw}x{bh}")

pdir = proj.out_dir("s2") / "03_panels"
(pdir / "p001_02.md").write_text("""# 分镜 p001_02（第1页 第2镜）
## 出场人物
- 银发持剑者（1girl）：银白色长发，黑白相间帽饰，黑色无袖上衣配蓬松浅色大袖，双手握长剑竖直上举，表情紧绷。
## 动作与姿态
双手举剑蓄力/格挡，身体微侧，左侧速度线。
## 空间关系与构图
倾斜平行四边形分镜框，中景。
## 镜头角度
平视略仰。
## 背景环境
留白+横向速度线，下方碎石与放射冲击线。
## 对白原文
银发持剑者：「可惡的邪神使徒」
## 拟声词
（无）
## 氛围与情绪
紧张；黑白漫画。
""", encoding="utf-8")

positive = ("1girl, solo, silver hair, very long hair, bicolor headwear, "
            "black headwear, sleeveless top, black top, puff sleeves, detached "
            "sleeves, holding sword, gripping weapon, raised sword, both hands, "
            "bracing, sideways glance, tense expression, speed lines, "
            "motion lines, flying debris, rubble, impact effects, tilted frame, "
            "medium shot, from side, monochrome, greyscale, black and white, "
            "manga style, screentone, ink drawing, traditional manga, intense, "
            "masterpiece, best quality,")
seed = int(hashlib.md5(f"{proj.id}:p001_02".encode()).hexdigest()[:8], 16)
(proj.out_dir("s3") / "p001_02.json").write_text(json.dumps({
    "panel": "p001_02", "page": 1, "order": 2,
    "positive": positive, "negative": None, "anchor": "",
    "characters": ["银发持剑者"], "size_bucket": bucket,
    "width": bw, "height": bh, "seed": seed,
}, ensure_ascii=False), encoding="utf-8")
log(f"fixture written seed={seed}")

# ---- stub LLM (spark not released) ----
llm.chat = lambda messages, **kw: "ok"
llm.chat_vision = lambda text, paths, **kw: (
    '{"pass": true, "issues": ["validation_stub"], "score": 9}')

# ---- real run, default tier (standard) ----
from mangacopy import stages  # noqa: E402
ok = stages.run_stage(proj, "s4")
log(f"s4 (standard) result: {ok}")
img = proj.out_dir("s4") / "p001_02.png"
log(f"image: {img} ({img.stat().st_size if img.exists() else 0} bytes)")
log("ALL DONE")
