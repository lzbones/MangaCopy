"""Project: one reproduction run, persisted as data/projects/<dir>/project.json.

The in-memory `state` dict mirrors project.json (id / created / params /
stages). Mutating helpers (set_stage, set_item) write through to disk so a
crashed pipeline can resume from the last completed unit.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

from . import config
from .stages import STAGE_ORDER

# Cross-thread safety for concurrent stage writes (DAG runner, 2026-09-27):
# same Project instance is shared by parallel stage threads; RLock so
# set_stage/set_item can mutate+save atomically.
_STATE_LOCK = threading.RLock()

# Subdirectories created for every project.
SUBDIRS = [
    "source",
    "logs",
    "s1_understand",
    "s2_zero",
    "s2_zero/02_pages",
    "s2_zero/03_panels",
    "s2_check",
    "s3_prompts",
    "s3_check",
    "s4_images",
    "s4_validate",
    "s4_text",
    "s5_video",
    "s6_h3",
    "s7_videos",
    "s8_final",
]

# stage id -> output directory name (relative to the project dir)
OUT_DIR_MAP = {
    "s0": "source",
    "s1": "s1_understand",
    "s2": "s2_zero",
    "s2_check": "s2_check",
    "s3": "s3_prompts",
    "s3_check": "s3_check",
    "s4": "s4_images",
    "s4_validate": "s4_validate",
    "s4b": "s4_text",
    "s5": "s5_video",
    "s6": "s6_h3",
    "s7": "s7_videos",
    "s8": "s8_final",
}

DEFAULT_PARAMS = {
    "style": "2d",
    "dialogue_lang": "zh",
    "megapixels": 0.4,
    "video_aspect": "16:9 (Widescreen)",
    "max_shot_seconds": 15.0,
}


class Project:
    def __init__(self, dir, state: dict):
        self.dir = Path(dir)
        self.state = state

    # ---- attributes -------------------------------------------------------
    @property
    def id(self) -> str:
        return self.state["id"]

    @property
    def params(self) -> dict:
        return self.state["params"]

    # ---- construction -----------------------------------------------------
    @classmethod
    def create(cls, ref_path: str, slug: str, params: dict | None = None) -> "Project":
        """Create data/projects/<YYYYMMDD>_<slug> (appending -2, -3, ... on name
        collision) with its subdirectories and a fresh project.json."""
        config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        base = f"{datetime.now().strftime('%Y%m%d')}_{slug}"
        proj_dir = config.PROJECTS_DIR / base
        counter = 2
        while proj_dir.exists():
            proj_dir = config.PROJECTS_DIR / f"{base}-{counter}"
            counter += 1
        for sub in SUBDIRS:
            (proj_dir / sub).mkdir(parents=True, exist_ok=True)

        merged = {
            "ref_path": str(Path(ref_path).expanduser().resolve()),
            **DEFAULT_PARAMS,
            **(params or {}),
        }
        merged["ref_path"] = str(Path(merged["ref_path"]).expanduser().resolve())
        state = {
            "id": proj_dir.name,
            "created": datetime.now().isoformat(timespec="seconds"),
            "params": merged,
            "stages": {s: {"status": "pending", "items": {}} for s in STAGE_ORDER},
        }
        proj = cls(proj_dir, state)
        proj.save()
        return proj

    @classmethod
    def load(cls, proj_dir) -> "Project":
        d = Path(proj_dir)
        state = json.loads((d / "project.json").read_text(encoding="utf-8"))
        return cls(d, state)

    # ---- persistence ------------------------------------------------------
    def save(self) -> None:
        """Write the in-memory state back to project.json (atomic replace)."""
        with _STATE_LOCK:
            tmp = self.dir / "project.json.tmp"
            tmp.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self.dir / "project.json")

    # ---- stage / item status ---------------------------------------------
    def stage_status(self, stage: str) -> str:
        return self.state["stages"].get(stage, {}).get("status", "pending")

    def set_stage(self, stage: str, status: str, note: str | None = None) -> None:
        with _STATE_LOCK:
            st = self.state["stages"].setdefault(stage, {"status": "pending", "items": {}})
            st["status"] = status
            if note is not None:
                st["note"] = note
            self.save()

    def item_status(self, stage: str, key: str) -> str | None:
        return (
            self.state["stages"].get(stage, {}).get("items", {}).get(key, {}).get("status")
        )

    def set_item(self, stage: str, key: str, status: str, data=None) -> None:
        with _STATE_LOCK:
            st = self.state["stages"].setdefault(stage, {"status": "pending", "items": {}})
            st.setdefault("items", {})[key] = {"status": status, "data": data}
            self.save()

    # ---- paths & logging --------------------------------------------------
    def out_dir(self, stage: str) -> Path:
        try:
            name = OUT_DIR_MAP[stage]
        except KeyError:
            raise KeyError(
                f"no output directory mapped for stage {stage!r}; known: {sorted(OUT_DIR_MAP)}"
            ) from None
        return self.dir / name

    def get_logger(self, stage: str) -> logging.Logger:
        """Logger writing to logs/{stage}.log (append) plus stderr. Loggers of
        the same name are reused (no duplicate handlers)."""
        name = f"mangacopy.project.{self.id}.{stage}"
        logger = logging.getLogger(name)
        if not logger.handlers:
            logger.setLevel(logging.INFO)
            logger.propagate = False
            fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
            logs_dir = self.dir / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(logs_dir / f"{stage}.log", mode="a", encoding="utf-8")
            fh.setFormatter(fmt)
            sh = logging.StreamHandler(sys.stderr)
            sh.setFormatter(fmt)
            logger.addHandler(fh)
            logger.addHandler(sh)
        return logger
