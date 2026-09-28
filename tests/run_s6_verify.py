"""S6 re-verification on the s5s8_test project (real LLM, no ComfyUI).
Regenerates seg_01 + seg_02 with the json_mode-fallback fix and 900s timeouts,
then runs the six-item check + fix loop. Zero ComfyUI calls.
"""
import sys
import time
from pathlib import Path

ROOT = Path("/Users/qingxu/Documents/Software/AI/MangaCopy")
sys.path.insert(0, str(ROOT))

from mangacopy import h3_prompt  # noqa: E402
from mangacopy.project import Project  # noqa: E402

t0 = time.time()
proj = Project.load(Path("/tmp/mangacopy_s5s8_test"))
print(f"project {proj.id} loaded", flush=True)
ok = h3_prompt.run(proj)
print(f"s6 result: {ok} in {time.time() - t0:.1f}s", flush=True)
for f in sorted((proj.out_dir("s6")).glob("*")):
    print(f"  {f.name} ({f.stat().st_size}B)", flush=True)
    if f.suffix == ".json":
        print("   ", f.read_text(encoding="utf-8")[:400], flush=True)
print("S6 DONE", flush=True)
