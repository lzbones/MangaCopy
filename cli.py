#!/usr/bin/env python
"""MangaCopy CLI: project creation, status inspection, staged / auto runs.

Usage (interpreter: ~/.ai-env/bin/python):
    python cli.py new --ref Ref/第187话 --slug demo [--style 2d|3d|live] [--params JSON]
    python cli.py list
    python cli.py status --project <id>
    python cli.py run --project <id> (--stage sN|auto) [--confirm] [--opts JSON] [--dryrun]

Exit codes: 0 success, 1 failure.

Engineering note (state-machine trap): --dryrun NEVER goes through
mangacopy.stages.run_stage(). run_stage marks its stage completed, and
next_pending() picks the first non-completed stage, so a dryrun that touched
run_stage would make later auto chains skip that stage. Dryrun therefore
calls the business module directly (image_gen / video_gen) with
dryrun=True, leaving project.json untouched.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

from mangacopy import config, stages
from mangacopy.project import Project

# Stages whose modules implement a dryrun mode (build workflows / log only,
# no ComfyUI submit, no LLM call, no state mutation).
DRYRUN_STAGES = ("s4", "s7")

STAGE_LABELS = {
    "s0": "ingest 源页导入",
    "s1": "understand 版面/分镜理解",
    "s2": "zero_script 复刻脚本",
    "s3": "image_prompt 生图 prompt",
    "s4": "image_gen 复刻生图+校验",
    "s5": "video_script 视频脚本",
    "s6": "h3_prompt H3 视频 prompt",
    "s7": "video_gen 分段视频生成",
    "s8": "assemble 拼接成片",
}

STATUS_CHARS = {
    "pending": ".",
    "in_progress": ">",
    "completed": "C",
    "failed": "!",
}


def _print(msg: str) -> None:
    print(msg, flush=True)


def _load_project(project_id: str) -> Project:
    proj_dir = config.PROJECTS_DIR / project_id
    if not (proj_dir / "project.json").is_file():
        candidates = [p.name for p in config.PROJECTS_DIR.glob(project_id + "*")]
        hint = f"（近似项目：{', '.join(candidates)}）" if candidates else ""
        sys.exit(f"错误：项目不存在：{project_id} {hint}")
    return Project.load(proj_dir)


def _item_counts(proj: Project, stage: str) -> dict:
    counts: dict[str, int] = {}
    for it in proj.state["stages"].get(stage, {}).get("items", {}).values():
        st = it.get("status", "?")
        counts[st] = counts.get(st, 0) + 1
    return counts


def _stage_summary_line(proj: Project, stage: str) -> str:
    st = proj.state["stages"].get(stage, {})
    status = st.get("status", "pending")
    counts = _item_counts(proj, stage)
    parts = [f"{k}={v}" for k, v in sorted(counts.items())]
    line = f"{stage} {STAGE_LABELS.get(stage, ''):<28} {status:<12}"
    if parts:
        line += "items: " + " ".join(parts)
    note = st.get("note")
    if note:
        line += f"\n    note: {note}"
    return line


def _manifest_summary(proj: Project) -> str:
    mf = proj.dir / "source" / "manifest.json"
    if not mf.is_file():
        return "manifest: 未生成（s0 未运行）"
    m = json.loads(mf.read_text(encoding="utf-8"))
    gray = sum(1 for p in m["pages"] if p.get("gray"))
    return f"manifest: {m['count']} 页（灰度 {gray} 页）"


# ---- subcommands --------------------------------------------------------------


def cmd_new(args) -> int:
    ref = Path(args.ref).expanduser()
    if not ref.exists():
        _print(f"错误：ref 路径不存在：{ref}")
        return 1
    params: dict = {}
    if args.style:
        params["style"] = args.style
    if args.params:
        try:
            extra = json.loads(args.params)
        except json.JSONDecodeError as exc:
            _print(f"错误：--params 不是合法 JSON：{exc}")
            return 1
        if not isinstance(extra, dict):
            _print("错误：--params 必须是 JSON 对象（如 '{\"style\":\"3d\"}'）")
            return 1
        params.update(extra)  # explicit params JSON wins over --style
    try:
        proj = Project.create(str(ref), args.slug, params)
    except Exception as exc:  # noqa: BLE001
        _print(f"错误：项目创建失败：{type(exc).__name__}: {exc}")
        return 1
    _print(f"项目已创建：{proj.id}")
    _print(f"目录：{proj.dir}")
    if not stages.run_stage(proj, "s0"):
        note = proj.state["stages"]["s0"].get("note", "")
        _print(f"错误：s0 ingest 失败：{note}")
        _print(f"日志：{proj.dir / 'logs' / 's0.log'}")
        return 1
    _print(f"s0 ingest 完成：{proj.dir / 'source'}")
    _print(_manifest_summary(proj))
    return 0


def cmd_list(args) -> int:  # noqa: ARG001
    if not config.PROJECTS_DIR.is_dir():
        _print(f"（暂无项目，目录 {config.PROJECTS_DIR} 不存在）")
        return 0
    projs = sorted(p for p in config.PROJECTS_DIR.iterdir() if (p / "project.json").is_file())
    if not projs:
        _print("（暂无项目）")
        return 0
    _print(f"{'项目':<24} {'创建时间':<20} 阶段（C=completed .=pending >=in_progress !=failed）")
    for p in projs:
        try:
            proj = Project.load(p)
        except Exception as exc:  # noqa: BLE001
            _print(f"{p.name:<24} 读取失败：{exc}")
            continue
        marks = "".join(
            STATUS_CHARS.get(proj.state["stages"].get(s, {}).get("status", "pending"), "?")
            for s in stages.STAGE_ORDER
        )
        _print(f"{proj.id:<24} {proj.state.get('created', '?'):<20} [{marks}]")
    return 0


def cmd_status(args) -> int:
    proj = _load_project(args.project)
    _print(f"项目：{proj.id}")
    _print(f"目录：{proj.dir}")
    _print(f"参数：{json.dumps(proj.params, ensure_ascii=False)}")
    _print(_manifest_summary(proj))
    _print("阶段状态：")
    for s in stages.STAGE_ORDER:
        _print("  " + _stage_summary_line(proj, s))
    nxt = stages.next_pending(proj)
    _print(f"下一未完成阶段：{nxt if nxt else '（全部完成）'}")
    return 0


def _run_dryrun(proj: Project, stage: str, opts: dict) -> int:
    """Dryrun special case: call the business module DIRECTLY, bypassing
    stages.run_stage so no stage/item state in project.json is touched."""
    if stage not in DRYRUN_STAGES:
        _print(f"错误：--dryrun 仅支持 {'/'.join(DRYRUN_STAGES)}（当前 --stage {stage}）")
        return 1
    module_name = stages.STAGE_MODULES[stage]
    _print(f"[dryrun] 直调 mangacopy.{module_name}.run（绕过 run_stage，不改动状态机）")
    try:
        mod = importlib.import_module(f"mangacopy.{module_name}")
    except ImportError as exc:
        _print(f"错误：模块 mangacopy.{module_name} 不可用：{exc}")
        return 1
    try:
        ok = bool(mod.run(proj, dryrun=True, **opts))
    except Exception as exc:  # noqa: BLE001
        _print(f"错误：dryrun 执行异常：{type(exc).__name__}: {exc}")
        return 1
    _print(f"[dryrun] {stage} {'完成（未提交任何生成请求，状态未变）' if ok else '失败（详见日志）'}")
    return 0 if ok else 1


def _run_auto(proj: Project, confirm: bool, opts: dict) -> int:
    """Auto chain. Default (no --confirm, no --opts): DAG-parallel — after s2,
    [s3→s4→s4b] and [s5→s6] run on separate DGX sessions while ComfyUI
    generates images; s7 overlaps s4b (user 2026-09-27 parallelism directive).
    --confirm or --opts: legacy sequential loop (confirm needs stdin between
    stages; opts carry single-stage semantics)."""
    if not confirm and not opts:
        from mangacopy import scheduler
        _print("== auto（DAG 并行）：s2 后 [s3→s4→s4b] ∥ [s5→s6]；s7 ∥ s4b ==")
        results = scheduler.run_dag(proj)
        for s in scheduler.PIPELINE_DAG:
            _print(f"  {s}: {results.get(s)}")
        bad = [s for s, v in results.items() if v != "completed"]
        if bad:
            _print(f"未完成阶段：{bad}（详见 logs/dag.log 与各阶段日志）")
            return 1
        _print("全部阶段已完成")
        return 0

    while True:
        stage = stages.next_pending(proj)
        if stage is None:
            _print("auto：全部阶段已完成")
            return 0
        _print(f"== auto：运行阶段 {stage} {STAGE_LABELS.get(stage, '')} ==")
        ok = stages.run_stage(proj, stage, **opts)
        _print(_stage_summary_line(proj, stage))
        if not ok:
            note = proj.state["stages"][stage].get("note", "")
            _print(f"阶段 {stage} 失败：{note}")
            _print(f"日志：{proj.dir / 'logs' / f'{stage}.log'}")
            return 1
        if confirm:
            try:
                ans = input("[Enter] 继续下一阶段 / [s] 跳出：")
            except EOFError:
                ans = ""
            if ans.strip().lower() == "s":
                _print("已跳出 auto 链（断点已保存，可随时续跑）")
                return 0


def cmd_run(args) -> int:
    proj = _load_project(args.project)
    opts: dict = {}
    if args.opts:
        try:
            opts = json.loads(args.opts)
        except json.JSONDecodeError as exc:
            _print(f"错误：--opts 不是合法 JSON：{exc}")
            return 1
        if not isinstance(opts, dict):
            _print("错误：--opts 必须是 JSON 对象（如 '{\"pages\":[1,2]}'）")
            return 1

    if args.dryrun:
        if args.stage == "auto":
            _print("错误：--dryrun 不支持 --stage auto（auto 依赖状态机推进）")
            return 1
        return _run_dryrun(proj, args.stage, opts)

    if args.stage == "auto":
        return _run_auto(proj, args.confirm, opts)

    if args.stage not in stages.STAGE_ORDER:
        _print(f"错误：未知阶段 '{args.stage}'（可选：{'/'.join(stages.STAGE_ORDER)} 或 auto）")
        return 1
    _print(f"== 运行阶段 {args.stage} {STAGE_LABELS.get(args.stage, '')} ==")
    ok = stages.run_stage(proj, args.stage, **opts)
    _print(_stage_summary_line(proj, args.stage))
    if not ok:
        note = proj.state["stages"][args.stage].get("note", "")
        _print(f"阶段 {args.stage} 失败：{note}")
        _print(f"日志：{proj.dir / 'logs' / f'{args.stage}.log'}")
        return 1
    return 0


# ---- argparse ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cli.py", description="MangaCopy 流水线 CLI")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("new", help="创建项目并运行 s0 ingest")
    sp.add_argument("--ref", required=True, help="参考漫画路径（zip 或文件夹）")
    sp.add_argument("--slug", required=True, help="项目短名（目录为 <日期>_<slug>）")
    sp.add_argument("--style", choices=["2d", "3d", "live"], help="风格（默认 2d，可被 --params 覆盖）")
    sp.add_argument("--params", help='项目参数 JSON（覆盖 style 等，如 \'{"style":"3d"}\'）')
    sp.set_defaults(func=cmd_new)

    sp = sub.add_parser("list", help="列出全部项目")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("status", help="查看项目逐阶段状态")
    sp.add_argument("--project", required=True, help="项目 id")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("run", help="运行单个阶段或 auto 链")
    sp.add_argument("--project", required=True, help="项目 id")
    sp.add_argument("--stage", required=True, help="s0..s8 或 auto")
    sp.add_argument("--confirm", action="store_true", help="auto 模式下每阶段完成后需确认（Enter 继续 / s 跳出）")
    sp.add_argument("--opts", help='阶段 run 的透传参数 JSON（如 \'{"pages":[1,2]}\'）')
    sp.add_argument("--dryrun", action="store_true", help="仅 s4/s7：构建工作流并打日志，不提交、不改状态")
    sp.set_defaults(func=cmd_run)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
