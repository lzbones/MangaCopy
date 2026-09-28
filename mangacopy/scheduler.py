"""DAG pipeline runner (2026-09-27, user-directed parallelism).

The stage list S0..S8 looks sequential, but the real dependency structure is a
DAG — S5/S6 (video scripts/prompts) depend only on S2, NOT on S3/S4:

    s0 -> s1 -> s2 -> [ s3 -> s4 -> s4b ]      branch A: prompts + images
                   -> [ s5 -> s6 ]              branch B: video scripts

    s7 needs { s5 }   (beats define the expected segments; video_gen polls
    for each s6 prompt as it appears — pro6000 starts producing videos while
    s4/s6 still run; user 2026-09-28 统筹 directive)
    s8 needs { s7 }

After s2, branches A and B run CONCURRENTLY: two spark sessions per stage
(one per DGX, see llm.new_session_pool) plus ComfyUI image generation on the
PRO 6000 — the user's "2 spark tasks + 1 video task together" model. Later
s7 (ComfyUI videos) overlaps s4b (spark typesetting).

Failure semantics: a stage whose dependency failed (or was skipped) is
recorded as skipped in the run report and logged, but NOT written into
project.json (so `next_pending` / reruns still see it as pending and a rerun
after fixing the upstream will pick it up again). `stages.run_stage` handles
each stage's own state machine; per-stage threads are safe because
Project.set_stage/set_item/save are serialized by _STATE_LOCK and every stage
writes its own log file.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from . import stages

if TYPE_CHECKING:  # avoid a runtime import cycle
    from .project import Project

PIPELINE_DAG: dict[str, list[str]] = {
    "s0": [],
    "s1": ["s0"],
    "s2": ["s1"],
    "s3": ["s2"],
    "s4": ["s3"],
    "s4b": ["s3"],
    "s5": ["s2"],
    "s6": ["s5"],
    "s7": ["s5"],
    "s8": ["s7"],
}


def run_dag(proj: "Project", only: list | None = None) -> dict:
    """Run every not-yet-completed stage whose dependencies are satisfied,
    as many in parallel as the DAG allows. Returns {stage: "completed" |
    "failed" | "skipped"} for every stage in the DAG (already-completed
    stages are reported as completed without rerunning)."""
    log = proj.get_logger("dag")
    todo = [s for s in stages.STAGE_ORDER if only is None or s in only]
    state: dict[str, str] = {}
    cv = threading.Condition()
    active: set[str] = set()

    for s in todo:
        if proj.stage_status(s) == "completed":
            state[s] = "completed"

    def worker(stage: str) -> None:
        ok = bool(stages.run_stage(proj, stage))
        with cv:
            state[stage] = "completed" if ok else "failed"
            active.discard(stage)
            cv.notify_all()

    while True:
        with cv:
            ready = []
            for s in todo:
                if s in state:
                    continue
                deps = [d for d in PIPELINE_DAG[s] if d in todo or d in state]
                if all(state.get(d) == "completed" for d in deps):
                    ready.append(s)
                elif any(state.get(d) in ("failed", "skipped") for d in deps):
                    state[s] = "skipped"
                    log.warning(f"dag: {s} skipped (dependency not completed)")
            for s in ready:
                state[s] = "running"
                active.add(s)
                log.info(f"dag: launching {s} (parallel branches: "
                         f"{sorted(active)})")
                threading.Thread(target=worker, args=(s,), daemon=True).start()
            if not active:
                if all(s in state for s in todo):
                    break
                # undecided stages with pending-but-unfinished deps cannot
                # exist here (their deps would be running => active); guard
                # anyway to avoid spinning
                for s in todo:
                    state.setdefault(s, "skipped")
                    log.warning(f"dag: {s} force-skipped (unsatisfiable)")
                break
            cv.wait(timeout=30)

    log.info("dag finished: " + ", ".join(f"{s}={state.get(s)}" for s in todo))
    return {s: state.get(s, "skipped") for s in todo}
