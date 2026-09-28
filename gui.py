#!/usr/bin/env python
"""MangaCopy Gradio GUI: project creation, stage progress, resume, artifacts.

Run (interpreter: ~/.ai-env/bin/python):
    python gui.py [--port 7860] [--share] [--host 127.0.0.1]

Structure:
    - top bar: project dropdown + refresh + new-project form (validated)
    - stage panel: nine cards (s0..s8), each with status badge, item counts and
      run / retry-failed / log-tail buttons
    - global controls: auto-run, confirm toggle (+ "continue" button), manual
      refresh; gr.Timer polls project.json every 5 s
    - artifact tabs: s0 gallery, s2 scripts, check reports, s3 prompt JSON,
      s4 images + validation, s5/s6 text, s7/s8 video players

Execution model: every run action spawns ONE daemon background thread; a
per-project guard rejects concurrent runs and the poller disables buttons of
a busy project. All stage execution goes through mangacopy.stages.run_stage.

Engineering note (state-machine trap): dryrun must NEVER go through
stages.run_stage() — run_stage marks the stage completed and next_pending()
would then skip it in later auto chains. If a dryrun is ever added to the GUI
it must call the business module directly (see cli.py:_run_dryrun). The GUI
currently exposes no dryrun button for that reason.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from functools import partial
from pathlib import Path

import gradio as gr

from mangacopy import config, stages
from mangacopy.project import Project

STAGE_INFO = [
    ("s0", "源页导入", "参考漫画拷贝与 manifest（页数/灰度）"),
    ("s1", "版面/分镜理解", "整页版面理解 + 分镜裁切细粒度理解"),
    ("s2", "复刻脚本", "L0 设定冻结 → L1 总述 → L2 分页 → L3 分镜 + checker"),
    ("s3", "生图 prompt", "Wai 规则生图 prompt（含种子与尺寸分桶）"),
    ("s4", "复刻生图+校验", "ComfyUI 生图与 LLM 校验并行流水"),
    ("s4b", "文字排印", "对白/拟声词 PIL 排印到复刻图（有字版，无字版保留）"),
    ("s5", "视频脚本", "分段/镜头/声音设计（对白保留中文）"),
    ("s6", "H3 视频 prompt", "MiniMax-H3 prompt（2d→I2V / 3d,live→T2V）"),
    ("s7", "分段视频生成", "ComfyUI 逐段生成视频"),
    ("s8", "拼接成片", "ffmpeg 拼接 + 音频交叉淡化"),
]
STAGES = [s for s, _, _ in STAGE_INFO]

REPORTS = [
    ("s2_check/report.md", "S2 一致性检查"),
    ("s3_check/report.md", "S3 prompt 自检"),
    ("s6_h3/check_report.md", "S6 六项自检"),
]

STATUS_COLORS = {
    "pending": "gray",
    "in_progress": "orange",
    "completed": "green",
    "failed": "red",
}

# ---- run engine (singleton guard per project) ----------------------------------

_RUN = {
    "threads": {},      # pid -> threading.Thread
    "resume": None,     # threading.Event for confirm-mode auto chains
    "waiting": False,   # True while an auto chain waits for user "continue"
    "last": "",         # last one-line status for the status bar
}
_RUN_LOCK = threading.Lock()


def _is_busy(pid: str) -> bool:
    t = _RUN["threads"].get(pid)
    return t is not None and t.is_alive()


def _run_worker(proj: Project, stage: str, resume: threading.Event) -> None:
    try:
        if stage == "auto" and resume.is_set():
            # no-confirm auto: DAG-parallel — after s2, [s3→s4→s4b] and
            # [s5→s6] run on separate DGX sessions while ComfyUI generates
            # images; s7 overlaps s4b (user 2026-09-27 directive)
            from mangacopy import scheduler
            results = scheduler.run_dag(proj)
            bad = [s for s, v in results.items() if v != "completed"]
            _RUN["last"] = ("auto：全部阶段完成" if not bad
                            else f"auto：未完成 {bad}（详见日志）")
        elif stage == "auto":
            while True:
                nxt = stages.next_pending(proj)
                if nxt is None:
                    _RUN["last"] = "auto：全部阶段完成"
                    break
                if not stages.run_stage(proj, nxt):
                    _RUN["last"] = f"阶段 {nxt} 失败（详见日志）"
                    break
                _RUN["waiting"] = True
                resume.wait()
                _RUN["waiting"] = False
        else:
            ok = stages.run_stage(proj, stage)
            _RUN["last"] = f"阶段 {stage} {'完成' if ok else '失败（详见日志）'}"
    except Exception as exc:  # noqa: BLE001 - never crash the UI silently
        _RUN["last"] = f"运行异常：{type(exc).__name__}: {exc}"
    finally:
        _RUN["waiting"] = False
        with _RUN_LOCK:
            _RUN["threads"].pop(proj.id, None)


def _start_run(pid: str, stage: str, confirm: bool) -> str:
    proj = _load_project(pid)
    if proj is None:
        raise gr.Error("请先选择项目")
    with _RUN_LOCK:
        if _is_busy(pid):
            raise gr.Error("该项目已有运行中的任务，请等待完成或稍后刷新")
        resume = threading.Event()
        if stage != "auto" or not confirm:
            resume.set()
        _RUN["resume"] = resume
        _RUN["waiting"] = False
        _RUN["last"] = f"已启动：{'auto 链' if stage == 'auto' else stage}"
        th = threading.Thread(target=_run_worker, args=(proj, stage, resume), daemon=True)
        _RUN["threads"][pid] = th
        th.start()
    return _RUN["last"]


# ---- helpers --------------------------------------------------------------------


def _list_projects() -> list:
    if not config.PROJECTS_DIR.is_dir():
        return []
    return sorted(p.name for p in config.PROJECTS_DIR.iterdir()
                  if (p / "project.json").is_file())


def _load_project(pid: str):
    if not pid:
        return None
    d = config.PROJECTS_DIR / pid
    if not (d / "project.json").is_file():
        return None
    try:
        return Project.load(d)
    except Exception:  # noqa: BLE001 - unreadable project should not kill the UI
        return None


def _item_counts(proj: Project, stage: str) -> dict:
    counts: dict[str, int] = {}
    for it in proj.state["stages"].get(stage, {}).get("items", {}).values():
        st = it.get("status", "?")
        counts[st] = counts.get(st, 0) + 1
    return counts


def _badge(status: str) -> str:
    color = STATUS_COLORS.get(status, "gray")
    return f'<span style="color:{color};font-weight:bold">{status}</span>'


def _stage_md(pid: str, stage: str, label: str, desc: str) -> str:
    proj = _load_project(pid)
    if proj is None:
        return f"**{stage} · {label}**\n（未选择项目）"
    st = proj.state["stages"].get(stage, {})
    status = st.get("status", "pending")
    lines = [f"**{stage} · {label}**　{_badge(status)}", desc]
    counts = _item_counts(proj, stage)
    if counts:
        lines.append("items：" + "，".join(f"{k}={v}" for k, v in sorted(counts.items())))
    note = st.get("note")
    if note:
        lines.append(f"note：{note}")
    return "\n\n".join(lines)


def _log_tail(pid: str, stage: str, n: int = 50) -> str:
    proj = _load_project(pid)
    if proj is None:
        return "（未选择项目）"
    f = proj.dir / "logs" / f"{stage}.log"
    if not f.is_file():
        return f"（{stage} 暂无日志）"
    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:]) or "（日志为空）"


def _rel_files(proj: Project, subdir: str, pattern: str = "*") -> list:
    base = proj.dir / subdir
    if not base.is_dir():
        return []
    return sorted(str(p.relative_to(proj.dir)) for p in base.rglob(pattern) if p.is_file())


def _render_text_file(proj: Project, rel: str) -> str:
    p = proj.dir / rel
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"（读取失败：{exc}）"
    if p.suffix == ".md":
        return text or "（空文件）"
    if p.suffix == ".json":
        try:
            return f"```json\n{json.dumps(json.loads(text), ensure_ascii=False, indent=2)}\n```"
        except json.JSONDecodeError:
            pass
    return f"```\n{text}\n```"


# ---- UI callbacks ----------------------------------------------------------------


def refresh_ui(pid):
    """Poller / manual refresh: re-read project.json and repaint status."""
    ids = _list_projects()
    if pid not in ids:
        pid = ids[0] if ids else None
    updates = [gr.update(choices=ids, value=pid)]
    proj = _load_project(pid)
    for stage, label, desc in STAGE_INFO:
        updates.append(_stage_md(pid, stage, label, desc))
    status = _RUN["last"] or "（空闲）"
    if pid and _is_busy(pid):
        status = f"运行中…（{status}）"
    updates.append(status)
    updates.append(gr.update(interactive=_RUN["waiting"]))
    busy = bool(pid and _is_busy(pid))
    for _ in STAGES:
        updates.append(gr.update(interactive=not busy))   # run buttons
    for _ in STAGES:
        updates.append(gr.update(interactive=not busy))   # retry buttons
    updates.append(gr.update(interactive=not busy))       # auto button
    if proj is None and not ids:
        updates[1] = "（暂无项目，请新建）"
    return updates


def on_run_stage(pid, stage):
    return _start_run(pid, stage, confirm=False)


def on_auto(pid, confirm):
    return _start_run(pid, "auto", confirm=bool(confirm))


def on_continue():
    if _RUN["waiting"] and _RUN["resume"] is not None:
        _RUN["resume"].set()
        return "已继续，等待下一阶段…"
    return "（当前没有等待确认的任务）"


def on_create(ref_text, slug, style, params_text, _pid):
    ref = Path(ref_text).expanduser() if ref_text else None
    if ref is None or not ref.exists():
        return ("新建失败：ref 路径不存在，请检查。", gr.update())
    if not slug or not slug.strip():
        return ("新建失败：slug 不能为空。", gr.update())
    params = {}
    if params_text and params_text.strip():
        try:
            params = json.loads(params_text)
        except json.JSONDecodeError as exc:
            return (f"新建失败：params JSON 不合法：{exc}", gr.update())
        if not isinstance(params, dict):
            return ("新建失败：params 必须是 JSON 对象。", gr.update())
    params = {"style": style, **params}
    try:
        proj = Project.create(str(ref), slug.strip(), params)
    except Exception as exc:  # noqa: BLE001
        return (f"新建失败：{type(exc).__name__}: {exc}", gr.update())
    ids = _list_projects()
    msg = f"已创建项目 {proj.id}（目录 {proj.dir}）。请运行 s0 导入源页。"
    return msg, gr.update(choices=ids, value=proj.id)


def on_log(pid, stage):
    return _log_tail(pid, stage)


# ---- artifact tab callbacks ------------------------------------------------------


def on_load_source(pid):
    proj = _load_project(pid)
    if proj is None:
        return [], "（未选择项目）"
    src = proj.out_dir("s0")
    imgs = sorted(src.glob("*.png")) + sorted(src.glob("*.jpg")) + sorted(src.glob("*.jpeg"))
    if not imgs:
        return [], "（暂无源页产物，请先运行 s0）"
    manifest = {}
    mf = src / "manifest.json"
    if mf.is_file():
        for p in json.loads(mf.read_text(encoding="utf-8"))["pages"]:
            manifest[p["file"]] = p
    gallery = []
    for im in imgs:
        meta = manifest.get(im.name, {})
        cap = f"{im.name}  {meta.get('w', '?')}x{meta.get('h', '?')}"
        if meta.get("gray"):
            cap += "  灰度"
        gallery.append((str(im), cap))
    info = f"共 {len(gallery)} 页"
    return gallery, info


def on_load_file_list(pid, subdir, pattern, selection, as_json=False):
    """Shared loader for dropdown+content tabs. Returns (dropdown update, md)."""
    proj = _load_project(pid)
    if proj is None:
        return gr.update(choices=[], value=None), "（未选择项目）"
    files = _rel_files(proj, subdir, pattern)
    if not files:
        placeholder = f"（{subdir}/ 下暂无产物）"
        return gr.update(choices=[], value=None), placeholder
    sel = selection if selection in files else files[0]
    return gr.update(choices=files, value=sel), _render_text_file(proj, sel)


def on_load_reports(pid, selection):
    proj = _load_project(pid)
    if proj is None:
        return gr.update(choices=[], value=None), "（未选择项目）"
    avail = [(rel, label) for rel, label in REPORTS if (proj.dir / rel).is_file()]
    if not avail:
        return gr.update(choices=[], value=None), "（暂无检查报告产物）"
    files = [rel for rel, _ in avail]
    sel = selection if selection in files else files[0]
    return gr.update(choices=files, value=sel), _render_text_file(proj, sel)


def on_load_s4(pid, val_selection):
    proj = _load_project(pid)
    if proj is None:
        return [], gr.update(choices=[], value=None), "（未选择项目）"
    imgs = sorted(proj.out_dir("s4").glob("*.png"))
    gallery = [(str(im), im.name) for im in imgs]
    vals = _rel_files(proj, "s4_validate", "*.json")
    if not gallery and not vals:
        return [], gr.update(choices=[], value=None), "（暂无 s4 产物）"
    sel = val_selection if val_selection in vals else (vals[0] if vals else None)
    json_md = _render_text_file(proj, sel) if sel else "（暂无校验记录）"
    return gallery, gr.update(choices=vals, value=sel), json_md


def on_load_s4b(pid):
    proj = _load_project(pid)
    if proj is None:
        return [], []
    typeset = [(str(im), im.name) for im in sorted(proj.out_dir("s4b").glob("*.png"))]
    textless = [(str(im), im.name) for im in sorted(proj.out_dir("s4").glob("*.png"))]
    return typeset, textless


def on_load_video(pid, subdir, selection):
    proj = _load_project(pid)
    if proj is None:
        return gr.update(choices=[], value=None), None, "（未选择项目）"
    vids = _rel_files(proj, subdir, "*.mp4")
    if not vids:
        return gr.update(choices=[], value=None), None, f"（{subdir}/ 下暂无视频产物）"
    sel = selection if selection in vids else vids[0]
    return gr.update(choices=vids, value=sel), str(proj.dir / sel), f"共 {len(vids)} 个视频"


# ---- app assembly ----------------------------------------------------------------


def build_app() -> gr.Blocks:
    ids = _list_projects()
    with gr.Blocks(title="MangaCopy 控制台") as demo:
        gr.Markdown("# MangaCopy 控制台\n漫画 → 脚本 → 复刻生图 → 复刻生视频流水线（项目状态见各阶段卡片）")
        with gr.Row():
            project_dd = gr.Dropdown(choices=ids, value=ids[0] if ids else None,
                                     label="项目", interactive=True)
            refresh_btn = gr.Button("刷新项目列表", size="sm")

        with gr.Accordion("新建项目", open=False):
            with gr.Row():
                new_ref = gr.Textbox(label="ref 路径（zip 或文件夹）", scale=2)
                new_slug = gr.Textbox(label="slug（项目短名）")
                new_style = gr.Radio(choices=["2d", "3d", "live"], value="2d", label="style")
            new_params = gr.Textbox(label='params JSON（可选，覆盖默认参数，如 {"megapixels":0.5}）')
            create_btn = gr.Button("创建项目", variant="primary")
            new_status = gr.Markdown()

        with gr.Row():
            auto_btn = gr.Button("Auto-run：运行所有未完成阶段", variant="primary")
            confirm_cb = gr.Checkbox(value=True, label="Confirm 模式（每阶段完成后需点“继续”才推进）")
            continue_btn = gr.Button("继续（confirm 确认）", interactive=False)
        run_status = gr.Markdown("（空闲）")

        stage_mds, run_btns, retry_btns, log_btns = {}, {}, {}, {}
        rows = [STAGE_INFO[i:i + 5] for i in range(0, len(STAGE_INFO), 5)]
        for row in rows:
            with gr.Row():
                for stage, label, desc in row:
                    with gr.Column():
                        stage_mds[stage] = gr.Markdown(
                            _stage_md(None, stage, label, desc))
                        with gr.Row():
                            run_btns[stage] = gr.Button("运行", size="sm")
                            retry_btns[stage] = gr.Button("重试失败项", size="sm")
                            log_btns[stage] = gr.Button("日志", size="sm")
        log_view = gr.Textbox(label="日志尾部（末 50 行）", lines=14, max_lines=20)

        with gr.Tabs():
            with gr.Tab("s0 源页"):
                s0_btn = gr.Button("加载源页")
                s0_info = gr.Markdown()
                s0_gallery = gr.Gallery(label="参考漫画页", columns=4, height="auto")
            with gr.Tab("s2 复刻脚本"):
                with gr.Row():
                    s2_dd = gr.Dropdown(label="文件（s2_zero/ 下）")
                    s2_btn = gr.Button("加载")
                s2_md = gr.Markdown("（选择项目后点“加载”）")
            with gr.Tab("检查报告"):
                with gr.Row():
                    rep_dd = gr.Dropdown(label="报告文件")
                    rep_btn = gr.Button("加载")
                rep_md = gr.Markdown("（选择项目后点“加载”）")
            with gr.Tab("s3 生图 Prompt"):
                with gr.Row():
                    s3_dd = gr.Dropdown(label="s3_prompts/*.json")
                    s3_btn = gr.Button("加载")
                s3_md = gr.Markdown("（选择项目后点“加载”）")
            with gr.Tab("s4 复刻图+校验"):
                s4_btn = gr.Button("加载")
                with gr.Row():
                    s4_gallery = gr.Gallery(label="复刻生成图", columns=4, height="auto")
                    with gr.Column():
                        s4_val_dd = gr.Dropdown(label="校验记录（s4_validate/）")
                        s4_val_md = gr.Markdown()
            with gr.Tab("s4b 有字版"):
                s4b_btn = gr.Button("加载")
                with gr.Row():
                    s4b_gallery = gr.Gallery(label="排印成品（s4_text/）", columns=4, height="auto")
                    s4b_src_gallery = gr.Gallery(label="无字底图（s4_images/）", columns=4, height="auto")
            with gr.Tab("s5 视频脚本"):
                with gr.Row():
                    s5_dd = gr.Dropdown(label="文件（s5_video/ 下）")
                    s5_btn = gr.Button("加载")
                s5_md = gr.Markdown("（选择项目后点“加载”）")
            with gr.Tab("s6 H3 Prompt"):
                with gr.Row():
                    s6_dd = gr.Dropdown(label="文件（s6_h3/ 下）")
                    s6_btn = gr.Button("加载")
                s6_md = gr.Markdown("（选择项目后点“加载”）")
            with gr.Tab("s7 分段视频"):
                with gr.Row():
                    s7_dd = gr.Dropdown(label="s7_videos/*.mp4")
                    s7_btn = gr.Button("加载")
                s7_info = gr.Markdown()
                s7_video = gr.Video(label="分段视频")
            with gr.Tab("s8 成片"):
                with gr.Row():
                    s8_dd = gr.Dropdown(label="s8_final/*.mp4")
                    s8_btn = gr.Button("加载")
                s8_info = gr.Markdown()
                s8_video = gr.Video(label="成片")

        timer = gr.Timer(5.0)

        # ---- wiring ----
        refresh_outputs = (
            [project_dd]
            + [stage_mds[s] for s in STAGES]
            + [run_status, continue_btn]
            + [run_btns[s] for s in STAGES]
            + [retry_btns[s] for s in STAGES]
            + [auto_btn]
        )
        refresh_btn.click(refresh_ui, inputs=[project_dd], outputs=refresh_outputs)
        project_dd.change(refresh_ui, inputs=[project_dd], outputs=refresh_outputs)
        timer.tick(refresh_ui, inputs=[project_dd], outputs=refresh_outputs)

        for s in STAGES:
            run_btns[s].click(partial(on_run_stage, stage=s), inputs=[project_dd],
                              outputs=[run_status]).then(
                refresh_ui, inputs=[project_dd], outputs=refresh_outputs)
            retry_btns[s].click(partial(on_run_stage, stage=s), inputs=[project_dd],
                                outputs=[run_status]).then(
                refresh_ui, inputs=[project_dd], outputs=refresh_outputs)
            log_btns[s].click(partial(on_log, stage=s), inputs=[project_dd],
                             outputs=[log_view])

        auto_btn.click(on_auto, inputs=[project_dd, confirm_cb],
                       outputs=[run_status]).then(
            refresh_ui, inputs=[project_dd], outputs=refresh_outputs)
        continue_btn.click(on_continue, inputs=None, outputs=[run_status]).then(
            refresh_ui, inputs=[project_dd], outputs=refresh_outputs)
        create_btn.click(on_create,
                         inputs=[new_ref, new_slug, new_style, new_params, project_dd],
                         outputs=[new_status, project_dd]).then(
                refresh_ui, inputs=[project_dd], outputs=refresh_outputs)

        s0_btn.click(on_load_source, inputs=[project_dd], outputs=[s0_gallery, s0_info])
        s2_btn.click(partial(on_load_file_list, subdir="s2_zero", pattern="*"),
                     inputs=[project_dd, s2_dd], outputs=[s2_dd, s2_md])
        rep_btn.click(on_load_reports, inputs=[project_dd, rep_dd],
                     outputs=[rep_dd, rep_md])
        s3_btn.click(partial(on_load_file_list, subdir="s3_prompts", pattern="*.json"),
                     inputs=[project_dd, s3_dd], outputs=[s3_dd, s3_md])
        s4_btn.click(on_load_s4, inputs=[project_dd, s4_val_dd],
                     outputs=[s4_gallery, s4_val_dd, s4_val_md])
        s4b_btn.click(on_load_s4b, inputs=[project_dd],
                      outputs=[s4b_gallery, s4b_src_gallery])
        s5_btn.click(partial(on_load_file_list, subdir="s5_video", pattern="*"),
                     inputs=[project_dd, s5_dd], outputs=[s5_dd, s5_md])
        s6_btn.click(partial(on_load_file_list, subdir="s6_h3", pattern="*"),
                     inputs=[project_dd, s6_dd], outputs=[s6_dd, s6_md])
        s7_btn.click(partial(on_load_video, subdir="s7_videos"),
                     inputs=[project_dd, s7_dd], outputs=[s7_dd, s7_video, s7_info])
        s8_btn.click(partial(on_load_video, subdir="s8_final"),
                     inputs=[project_dd, s8_dd], outputs=[s8_dd, s8_video, s8_info])

    return demo


USAGE = """\
MangaCopy GUI 已启动。
  - 顶栏选择/刷新/新建项目（ref 路径不存在会在表单下方报错，不会创建）。
  - 阶段卡片：运行 / 重试失败项（重跑该阶段，模块自动跳过已完成单元）/ 查看日志尾部 50 行。
  - Auto-run：沿 next_pending() 链推进；Confirm 模式开启时每阶段完成后需点“继续”。
  - 状态每 5 s 自动轮询 project.json，也可手动刷新。
  - 产物浏览各标签页：加载后展示源页/脚本/prompt/生成图/视频与检查报告。
"""


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="gui.py", description="MangaCopy Gradio GUI")
    parser.add_argument("--port", type=int, default=7860, help="监听端口（默认 7860）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--share", action="store_true", help="创建公网分享链接")
    args = parser.parse_args(argv)

    demo = build_app()
    print(USAGE, flush=True)
    demo.launch(server_name=args.host, server_port=args.port, share=args.share)


if __name__ == "__main__":
    main()
