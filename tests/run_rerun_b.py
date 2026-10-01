#!/usr/bin/env python3
"""Run Scheme B: Full 13-page chapter end-to-end rerun with LLM optimizations.

Features:
1. Target: data/test_projects/projects/20260930_opt_full13
2. Real-time hardware monitoring:
   - DGX Spark (166.111.50.17:4000): active in-flight semaphore usage (0/2, 1/2, 2/2).
   - NVIDIA RTX PRO 6000 (192.168.50.254:8188): ComfyUI running/pending queue.
   - Writes periodic telemetry to logs/hardware_monitor.log and RERUN_PROGRESS.json.
3. Closed-loop DAG orchestration via mangacopy.scheduler.
4. Timing measurement for each stage (S2..S8) logged to stage_timing.json.
5. Post-completion export of the 4 independent manga directories and final video verification.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from PIL import Image

ROOT = Path("/Users/qingxu/Documents/Software/AI/MangaCopy")
sys.path.insert(0, str(ROOT))

import mangacopy.config as config
from mangacopy import assemble, llm, scheduler, stages
from mangacopy.project import Project

PROJ_DIR = ROOT / "data" / "test_projects" / "projects" / "20260930_opt_full13"
MONITOR_INTERVAL = 10  # seconds
T0 = time.time()


def log(msg: str) -> None:
    elapsed = time.time() - T0
    m, s = divmod(int(elapsed), 60)
    h, m = divmod(m, 60)
    t_str = f"[{h:02d}:{m:02d}:{s:02d}]"
    line = f"{t_str} {msg}"
    print(line, flush=True)
    with open(PROJ_DIR / "logs" / "rerun_runner.log", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


class HardwareMonitor(threading.Thread):
    def __init__(self, proj: Project, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.proj = proj
        self.stop_event = stop_event
        self.log_file = proj.dir / "logs" / "hardware_monitor.log"
        self.json_file = proj.dir / "RERUN_PROGRESS.json"

    def run(self):
        while not self.stop_event.is_set():
            t_now = time.time()
            # 1. Spark concurrency
            # llm._LLM_SEM initial value is LLM_MAX_CONCURRENT (2)
            try:
                busy_sparks = config.LLM_MAX_CONCURRENT - llm._LLM_SEM._value
            except Exception:
                busy_sparks = 0

            # 2. ComfyUI queue on RTX PRO 6000
            comfy_running = comfy_pending = -1
            try:
                req = urllib.request.urlopen(f"http://{config.COMFY_HOST}:{config.COMFY_PORT}/queue", timeout=3)
                q_data = json.loads(req.read())
                comfy_running = len(q_data.get("queue_running", []))
                comfy_pending = len(q_data.get("queue_pending", []))
            except Exception:
                pass

            # 3. Stage progress
            stage_stats = {}
            for st in stages.STAGE_ORDER:
                status = self.proj.stage_status(st)
                items = self.proj.state["stages"].get(st, {}).get("items", {})
                completed = sum(1 for it in items.values() if it.get("status") == "completed")
                failed = sum(1 for it in items.values() if it.get("status") == "failed")
                total = len(items)
                stage_stats[st] = {
                    "status": status,
                    "completed": completed,
                    "failed": failed,
                    "total": total,
                }

            # 4. Telemetry record
            telemetry = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "elapsed_seconds": int(t_now - T0),
                "spark": {
                    "endpoint": config.LLM_BASE_URL,
                    "busy_instances": max(0, busy_sparks),
                    "capacity": config.LLM_MAX_CONCURRENT,
                    "utilization_pct": (max(0, busy_sparks) / config.LLM_MAX_CONCURRENT) * 100,
                },
                "pro6000": {
                    "endpoint": f"{config.COMFY_HOST}:{config.COMFY_PORT}",
                    "queue_running": comfy_running,
                    "queue_pending": comfy_pending,
                    "busy": comfy_running > 0,
                },
                "stages": stage_stats,
            }

            try:
                self.json_file.write_text(json.dumps(telemetry, ensure_ascii=False, indent=2), encoding="utf-8")
                with open(self.log_file, "a", encoding="utf-8") as fh:
                    fh.write(
                        f"{telemetry['timestamp']} | Spark: {telemetry['spark']['busy_instances']}/2 | "
                        f"PRO6000: run={comfy_running},pend={comfy_pending} | "
                        f"Active Stages: {[s for s, v in stage_stats.items() if v['status'] in ('in_progress', 'running') or (v['total'] > 0 and v['status'] != 'completed')]}\n"
                    )
            except Exception:
                pass

            time.sleep(MONITOR_INTERVAL)


def export_manga_directories(proj: Project) -> None:
    log("---- Exporting 4 Independent Manga Directories ----")
    dir_panels_pure = proj.dir / "manga_panels_pure"
    dir_panels_typeset = proj.dir / "manga_panels_typeset"
    dir_pages_pure = proj.dir / "manga_pages_pure"
    dir_pages_typeset = proj.dir / "manga_pages_typeset"

    for d in [dir_panels_pure, dir_panels_typeset, dir_pages_pure, dir_pages_typeset]:
        d.mkdir(parents=True, exist_ok=True)

    s4_img_dir = proj.out_dir("s4")
    s4_txt_dir = proj.out_dir("s4b")

    all_panels = sorted(s4_img_dir.glob("*.png"))
    log(f"Exporting {len(all_panels)} panels to manga_panels_pure and manga_panels_typeset...")
    for p in all_panels:
        shutil.copy2(p, dir_panels_pure / p.name)
        t_p = s4_txt_dir / p.name
        if t_p.exists():
            shutil.copy2(t_p, dir_panels_typeset / p.name)
        else:
            shutil.copy2(p, dir_panels_typeset / p.name)

    log("Compositing 13 full pages for manga_pages_pure and manga_pages_typeset...")
    for page_no in range(1, 14):
        p_json_file = proj.out_dir("s1") / f"page_{page_no:02d}.json"
        if not p_json_file.exists():
            continue
        p_data = json.loads(p_json_file.read_text(encoding="utf-8"))
        ref_file = proj.dir / "source" / f"{page_no:04d}.png"
        if not ref_file.exists():
            ref_file = ROOT / "Ref" / "第187话" / f"{page_no:04d}.png"

        with Image.open(ref_file) as r_im:
            W, H = r_im.size

        for mode, out_dir in [("pure", dir_pages_pure), ("typeset", dir_pages_typeset)]:
            page_canvas = Image.new("RGB", (W, H), "white")
            for panel in p_data["panels"]:
                key = panel["key"]
                x, y, pw, ph = panel["bbox"]
                left = int(round(x * W))
                top = int(round(y * H))
                width = int(round(pw * W))
                height = int(round(ph * H))

                pan_file = (dir_panels_typeset if mode == "typeset" else dir_panels_pure) / f"{key}.png"
                if pan_file.exists():
                    with Image.open(pan_file) as p_im:
                        resized = p_im.resize((width, height), Image.Resampling.LANCZOS)
                        page_canvas.paste(resized, (left, top))
            page_canvas.save(out_dir / f"page_{page_no:02d}.png")
    log("Manga 4-directory export completed!")


def main():
    log("=================================================================")
    log("Starting Scheme B: Full 13-Page Reproduction Rerun")
    log(f"Project Target: {PROJ_DIR}")
    log("=================================================================")

    proj = Project.load(PROJ_DIR)
    stop_event = threading.Event()
    monitor = HardwareMonitor(proj, stop_event)
    monitor.start()

    timing_file = proj.dir / "stage_timing.json"
    timing_data = {}
    if timing_file.exists():
        try:
            timing_data = json.loads(timing_file.read_text(encoding="utf-8"))
        except Exception:
            timing_data = {}

    log("Initial stage status:")
    for s in stages.STAGE_ORDER:
        log(f"  {s}: {proj.stage_status(s)}")

    # Run DAG with automatic cycle retry if needed
    MAX_CYCLES = 3
    dag_results = {}
    t_pipeline_start = time.time()

    for cycle in range(1, MAX_CYCLES + 1):
        log(f"\n>>>>> Launching DAG Execution (Cycle {cycle}/{MAX_CYCLES}) <<<<<")
        t_cycle_start = time.time()
        dag_results = scheduler.run_dag(proj)
        log(f"Cycle {cycle} finished in {time.time() - t_cycle_start:.1f}s: {dag_results}")

        if all(v == "completed" for v in dag_results.values()):
            log("ALL STAGES COMPLETED SUCCESSFULLY!")
            break
        else:
            unfin = [k for k, v in dag_results.items() if v != "completed"]
            log(f"Cycle {cycle} incomplete stages: {unfin}. Waiting 60s before retry...")
            time.sleep(60)

    t_pipeline_end = time.time()
    total_dur = t_pipeline_end - t_pipeline_start

    # Stop monitor
    stop_event.set()
    monitor.join(timeout=5)

    # Post processing
    final_mp4 = proj.dir / "s8_final" / "final.mp4"
    if final_mp4.exists():
        size_mb = final_mp4.stat().st_size / (1024 * 1024)
        log(f"Final MP4 generated: {final_mp4} ({size_mb:.1f} MB)")
        try:
            export_manga_directories(proj)
        except Exception as exc:
            log(f"Error during manga export: {exc}")
    else:
        log("WARNING: Final MP4 not found in s8_final/")

    log(f"Scheme B execution finished. Total pipeline wall time: {total_dur:.1f}s ({total_dur / 60:.1f} min)")


if __name__ == "__main__":
    main()
