"""Stage registry and dispatch.

STAGE_MODULES maps each stage id to its implementing module under mangacopy/.
Modules are imported lazily at run time, so missing S1-S8 business modules only
error when their stage is actually executed.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid a runtime import cycle with mangacopy.project
    from .project import Project

STAGE_ORDER = ["s0", "s1", "s2", "s3", "s4", "s4b", "s5", "s6", "s7", "s8"]

STAGE_MODULES = {
    "s0": "ingest",
    "s1": "understand",
    "s2": "zero_script",
    "s3": "image_prompt",
    "s4": "image_gen",
    "s4b": "text_overlay",
    "s5": "video_script",
    "s6": "h3_prompt",
    "s7": "video_gen",
    "s8": "assemble",
}


def run_stage(proj: "Project", stage: str, **opts) -> bool:
    """Run one stage: set in_progress, lazily import mangacopy.<module>, call
    its run(proj, **opts), then set completed/failed. Returns True on success.
    Import failures and exceptions are recorded as a failed stage (with note)
    and reported as False rather than raised."""
    if stage not in STAGE_MODULES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {STAGE_ORDER}")
    module_name = STAGE_MODULES[stage]
    proj.set_stage(stage, "in_progress")
    log = proj.get_logger(stage)
    try:
        mod = importlib.import_module(f"mangacopy.{module_name}")
    except ImportError as exc:
        note = f"module mangacopy.{module_name} unavailable: {exc}"
        proj.set_stage(stage, "failed", note=note)
        log.error(f"stage {stage} failed: {note}")
        return False
    try:
        ok = bool(mod.run(proj, **opts))
    except Exception as exc:  # noqa: BLE001 - record any failure, keep pipeline alive
        note = f"{type(exc).__name__}: {exc}"
        proj.set_stage(stage, "failed", note=note)
        log.error(f"stage {stage} failed: {note}")
        log.exception("traceback:")
        return False
    if ok:
        proj.set_stage(stage, "completed")
        log.info(f"stage {stage} completed")
    else:
        proj.set_stage(stage, "failed", note="run() returned False")
        log.error(f"stage {stage} failed: run() returned False")
    return ok


def next_pending(proj: "Project") -> str | None:
    """First stage in STAGE_ORDER that is not completed (pending, in_progress
    or failed), enabling both checkpoint-resume and retry-after-failure.
    Returns None when every stage is completed."""
    stages = proj.state.get("stages", {})
    for stage in STAGE_ORDER:
        status = stages.get(stage, {}).get("status", "pending")
        if status != "completed":
            return stage
    return None
