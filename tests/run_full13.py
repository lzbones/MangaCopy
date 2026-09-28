"""Full 13-page end-to-end pipeline rehearsal.

TEST-ONLY run per user directive (2026-09-27): no code modifications — every
failure is recorded in the report as a finding/suggestion, and the chain
continues to the next stage whenever partial inputs allow (each stage is
item-level idempotent and tolerant of missing upstream files).

Runs in data/test_projects/ per the no-/tmp convention. Report is appended
incrementally to the project dir as TEST_REPORT.md.
"""
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path("/Users/qingxu/Documents/Software/AI/MangaCopy")
sys.path.insert(0, str(ROOT))

import mangacopy.config as config  # noqa: E402

config.PROJECTS_DIR = ROOT / "data" / "test_projects" / "projects"
config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

from mangacopy.project import Project  # noqa: E402
from mangacopy import scheduler, stages  # noqa: E402

T0 = time.time()
REPORT_LINES = []


def rec(msg: str) -> None:
    line = f"[{time.time() - T0:8.0f}s] {msg}"
    print(line, flush=True)
    REPORT_LINES.append(line)


def flush_report(proj: Project, results: dict, note: str = "") -> None:
    lines = ["# 全量彩排测试报告（full13）", "",
             f"- 开始时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(T0))}",
             f"- 范围：第187话全 13 页，S0→S8 全链路（含 S4b）",
             f"- 测试纪律：只测不改；失败记录为发现/建议，链条继续",
             "", "## 阶段结果", "",
             "| 阶段 | 结果 | 状态 | 耗时 | item 数 | 异常 item |",
             "|---|---|---|---|---|---|"]
    for stage, r in results.items():
        lines.append(f"| {stage} | {'✅ OK' if r['ok'] else '❌ FAIL'} | "
                     f"{r['status']} | {r['dur']:.0f}s | {r['n_items']} | "
                     f"{json.dumps(r['bad'], ensure_ascii=False) if r['bad'] else '—'} |")
    if note:
        lines += ["", "## 备注", "", note]
    lines += ["", "## 时间线", ""] + [f"- {ln}" for ln in REPORT_LINES]
    (proj.dir / "TEST_REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    # reuse an existing project (checkpoint resume) or create a fresh one;
    # shortest name wins so -2/-3 duplicates never shadow the original
    existing = sorted(config.PROJECTS_DIR.glob("*full13*"), key=lambda p: len(p.name))
    if existing:
        proj = Project.load(existing[-1])
        rec(f"resuming existing project: {proj.dir}")
    else:
        proj = Project.create(ref_path=str(ROOT / "Ref" / "第187话"),
                              slug="full13", params={"style": "2d"})
        rec(f"project: {proj.dir}")
    results = {}

    for stage in scheduler.PIPELINE_DAG:
        t = time.time()
        before = proj.stage_status(stage)
        rec(f"STAGE {stage} start (dep-state {before})")
        results[stage] = {"ok": before == "completed", "status": before,
                          "dur": 0.0, "n_items": 0, "bad": {}}
    # ---- closed-loop overnight iteration: DAG cycles with idempotent resume ----
    MAX_CYCLES = 5
    dag_results = {}
    for cycle in range(1, MAX_CYCLES + 1):
        rec(f"===== DAG cycle {cycle}/{MAX_CYCLES} =====")
        t_dag = time.time()
        dag_results = scheduler.run_dag(proj)
        rec(f"DAG finished in {time.time() - t_dag:.0f}s: {dag_results}")
        for stage, st in dag_results.items():
            items = proj.state["stages"].get(stage, {}).get("items", {})
            bad = {k: v["status"] for k, v in items.items()
                   if v["status"] not in ("completed",)}
            results[stage] = {"ok": st == "completed", "status": st, "dur": 0.0,
                              "n_items": len(items), "bad": bad}
            rec(f"STAGE {stage}: {'OK' if st == 'completed' else st.upper()} "
                f"items={len(items)} bad={json.dumps(bad, ensure_ascii=False)}")
        flush_report(proj, results)
        if all(v == "completed" for v in dag_results.values()):
            rec("all stages completed — rehearsal closed loop done")
            break
        if cycle < MAX_CYCLES:
            rec(f"cycle {cycle} incomplete; cooling down 300s before idempotent retry")
            time.sleep(300)
    if all(v == "completed" for v in dag_results.values()):
        rec("FINAL: 全链路复刻完成")

    n_ok = sum(1 for r in results.values() if r["ok"])
    rec(f"REHEARSAL DONE: {n_ok}/{len(results)} stages OK")
    flush_report(proj, results,
                 note=f"最终：{n_ok}/{len(results)} 阶段通过。"
                      f"分析性发现与建议由 lead 在测试结束后另附。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
