"""S0 ingest: copy the reference comic (zip or folder) into source/, detect
numbered pages, probe each page with PIL (size + grayscale), and write
source/manifest.json. Idempotent: rerunning overwrites previous output.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from PIL import Image, ImageStat

from .project import Project

IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_NUM_RE = re.compile(r"\d+")
GRAY_SAT_THRESHOLD = 0.01  # mean HSV saturation (normalized to 0..1) below this -> B&W


def _page_number(stem: str) -> int | None:
    """Page number = last digit group of the file stem (handles 0001.png,
    1.png, 01.png, page_0003.jpg, 187-05.png ...). None if no digits."""
    groups = _NUM_RE.findall(stem)
    return int(groups[-1]) if groups else None


def _numbered_images(directory: Path) -> list:
    """Numbered image files directly inside directory, naturally sorted by
    (page number, file name)."""
    found = []
    for f in sorted(directory.iterdir()):
        if f.is_file() and f.suffix.lower() in IMG_EXTS:
            n = _page_number(f.stem)
            if n is not None:
                found.append((n, f))
    found.sort(key=lambda t: (t[0], t[1].name))
    return found


def _pick_source_dir(root: Path) -> Path:
    """Deepest directory under root that directly contains numbered images
    (ties at equal depth resolved by higher count, then name order)."""
    best = None  # (depth, -count, name) minimized
    candidates = [root] + [d for d in root.rglob("*") if d.is_dir()]
    for d in candidates:
        items = _numbered_images(d)
        if not items:
            continue
        depth = len(d.relative_to(root).parts)
        key = (-depth, -len(items), str(d))
        if best is None or key < best[0]:
            best = (key, d)
    if best is None:
        raise ValueError(f"no numbered images found under {root}")
    return best[1]


def run(proj: Project, **opts) -> bool:
    log = proj.get_logger("s0")
    ref = Path(proj.params["ref_path"]).expanduser()
    if not ref.exists():
        raise FileNotFoundError(f"ref_path does not exist: {ref}")
    src = proj.out_dir("s0")
    src.mkdir(parents=True, exist_ok=True)

    # Idempotency: clear previous numbered images and manifest before re-copy.
    for f in src.iterdir():
        if f.is_file() and (f.suffix.lower() in IMG_EXTS or f.name == "manifest.json"):
            f.unlink()

    with tempfile.TemporaryDirectory(prefix="mangacopy_ingest_") as tmp:
        if ref.is_file() and ref.suffix.lower() == ".zip":
            with zipfile.ZipFile(ref) as zf:
                zf.extractall(tmp)
            staging = _pick_source_dir(Path(tmp))
            log.info(f"zip extracted; using image directory: {staging}")
        elif ref.is_dir():
            staging = ref
        else:
            raise ValueError(f"ref_path must be a .zip file or a directory: {ref}")

        pages = _numbered_images(staging)
        if not pages:
            raise ValueError(f"no numbered images (png/jpg/jpeg/webp) found in {ref}")

        nums = [n for n, _ in pages]
        if len(set(nums)) != len(nums):
            log.warning("duplicate page numbers detected; later files overwrite earlier ones")
        missing = sorted(set(range(min(nums), max(nums) + 1)) - set(nums))
        if missing:
            log.warning(f"missing page numbers {missing} in {min(nums)}..{max(nums)}; continuing")

        manifest_pages = []
        for n, f in pages:
            target = src / f"{n:04d}{f.suffix.lower()}"
            shutil.copy2(f, target)
            with Image.open(target) as im:
                w, h = im.size
                sat = (
                    ImageStat.Stat(im.convert("RGB").convert("HSV").split()[1]).mean[0]
                    / 255.0
                )
            gray = sat < GRAY_SAT_THRESHOLD
            manifest_pages.append(
                {"index": n, "file": target.name, "w": w, "h": h, "gray": bool(gray)}
            )
            log.info(f"page {n}: {target.name} {w}x{h} gray={gray}")

    manifest = {"count": len(manifest_pages), "pages": manifest_pages}
    (src / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    gray_count = sum(1 for p in manifest_pages if p["gray"])
    log.info(
        f"ingest done: {manifest['count']} pages ({gray_count} grayscale) "
        f"from {ref} -> {src}/manifest.json"
    )
    return True
