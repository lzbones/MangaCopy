"""Deferred real-LLM validation of the two generated panel images
(p001_04 hd tier, p001_02 standard tier) in the s4real project.
Uses the real image_gen._validate_panel code path + real spark vision.
Zero ComfyUI calls.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path("/Users/qingxu/Documents/Software/AI/MangaCopy")
sys.path.insert(0, str(ROOT))

from mangacopy import config, llm  # noqa: E402
from mangacopy import image_gen  # noqa: E402
from mangacopy.project import Project  # noqa: E402

t0 = time.time()
proj = Project.load(ROOT / "data" / "projects" / "20260926_s4real")
log = proj.get_logger("s4")
sid = llm.new_session_id()
print(f"session {sid}", flush=True)

for key in ("p001_04", "p001_02"):
    prompt = json.loads((proj.out_dir("s3") / f"{key}.json").read_text(encoding="utf-8"))
    img = proj.out_dir("s4") / f"{key}.png"
    v = image_gen._validate_panel(proj, prompt, img, sid, log)
    print(f"{key}: pass={v['pass']} score={v['score']} issues={v['issues']} "
          f"({time.time() - t0:.1f}s)", flush=True)
print("VALIDATION DONE", flush=True)
