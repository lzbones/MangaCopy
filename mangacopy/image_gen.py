"""S4 image_gen: Wai image generation with in-flight cap and parallel LLM validation.

Per panel (checkpoint item "<panel>" under stage "s4"):
- build the workflow and inject: positive -> node 3, negative -> node 4,
  seed = prompt seed + attempt*7919 -> node 5, bucket size (batch_size=1)
  -> node 6, filename_prefix "<project_id>/<panel>" -> node 8; node 12
  (upscale target = bucket * 1.5) exists only in the "hd" template.
  Workflow selection (user 2026-09-26): "standard" = API - Wai NoUpScaling
  (~5s/img, DEFAULT), "hd" = API - Wai with RealESRGAN + 2nd pass
  (~15s/img); chosen via proj.params["image_quality"] or opts["image_quality"]
  (opts override params);
- submit -> wait (COMFY_TIMEOUT) -> fetch under an in-flight semaphore
  (opts["inflight"], default 2, ThreadPoolExecutor + Semaphore); the finished
  image is saved as s4_images/pXXX_YY.png (multiple outputs -> first + warning);
- validate immediately after each image completes (don't wait for the batch):
  vision LLM call via prompts/s4_validate.md against the panel's L3 script;
  pass && score >= 7 -> completed, otherwise retry with the next seed for up
  to 2 regenerations (3 attempts total); exhausted -> needs_review, which does
  not block the rest of the stage.

Per-attempt records (pass/issues/score/seed/prompt_id) go to
s4_validate/pXXX_YY.json. Infrastructure errors (ComfyUI unreachable, submit/
wait/ fetch failure) abort that panel as failed without burning attempts.
Panels whose item is completed and whose image exists are skipped (idempotent
resume). opts["dryrun"]=True only builds the workflow and logs the injected
values — no submit, no validation, no preflight.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import comfy, config, llm, templates
from .project import Project

PANEL_KEY_RE = re.compile(r"^p(\d{3})_(\d{2})$")
SEED_STEP = 7919          # seed + attempt * SEED_STEP per retry
MAX_ATTEMPTS = 3          # initial + <= 2 regenerations
PASS_SCORE = 7

_STATE_LOCK = threading.Lock()


def _set_item(proj: Project, key: str, status: str, data=None) -> None:
    with _STATE_LOCK:
        proj.set_item("s4", key, status, data)


def _atomic_write_json(path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _panel_sort_key(p: dict):
    m = PANEL_KEY_RE.match(str(p.get("panel", "")))
    return (int(m.group(1)), int(m.group(2))) if m else (9999, 99)


# ---- workflow construction -------------------------------------------------

def _build_workflow(prompt: dict, seed: int, proj_id: str, wf_name: str) -> dict:
    """Selected workflow template with per-panel injections (nodes 3/4/5/6/8;
    node 12 upscale target only in the "hd" template)."""
    wf = comfy.load_workflow(wf_name)
    w, h = int(prompt["width"]), int(prompt["height"])
    comfy.set_node(wf, "3", text=prompt["positive"])
    comfy.set_node(wf, "4", text=prompt["negative"])
    comfy.set_node(wf, "5", seed=int(seed))
    comfy.set_node(wf, "6", width=w, height=h, batch_size=1)
    if "12" in wf:  # hd template only: upscale target = bucket * 1.5
        comfy.set_node(wf, "12", width=int(round(w * 1.5)), height=int(round(h * 1.5)))
    comfy.set_node(wf, "8", filename_prefix=f"{proj_id}/{prompt['panel']}")
    return wf


# ---- LLM validation ----------------------------------------------------------

def _validate_panel(proj: Project, prompt: dict, img_path: Path, session_id: str, log) -> dict:
    """Vision check of the generated image against the L3 script.
    LLM failures degrade to a not-passed verdict (issue 'validator_error'),
    so the panel regenerates or ends needs_review rather than crashing."""
    key = prompt["panel"]
    try:
        md = (proj.out_dir("s2") / "03_panels" / f"{key}.md").read_text(encoding="utf-8")
    except OSError:
        md = "（L3 脚本缺失）"
        log.warning(f"{key}: L3 script missing, validating against prompt only")
    text = templates.render("s4_validate", PANEL_KEY=key, PANEL_SCRIPT=md)
    try:
        raw = llm.chat_vision(text, [img_path], json_mode=True, session_id=session_id)
        data = llm.extract_json(raw)
    except llm.LLMError as exc:
        log.warning(f"{key}: validator LLM failed: {exc}")
        return {"pass": False, "issues": [f"validator_error: {exc}"], "score": 0}
    if not isinstance(data, dict):
        data = {}
    ok = bool(data.get("pass"))
    try:
        score = int(data.get("score"))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(10, score))
    issues = data.get("issues") or []
    if isinstance(issues, str):
        issues = [issues]
    issues = [str(i) for i in issues if str(i).strip()]
    return {"pass": ok, "issues": issues, "score": score}


# ---- dual-pool pipeline (2026-09-28 user directive) ---------------------------
# Image generation (PRO 6000) and LLM validation (spark) run as two SEPARATE
# parallel pools bridged by queues — NOT one serial generate→validate loop per
# panel. Generators keep the ComfyUI queue fed; validators check finished
# images; rejections re-enter the generation queue for the next round
# ("根据校验结果再迭代后面的轮数"). Per-panel semantics preserved:
# seed = base + attempt*SEED_STEP, MAX_ATTEMPTS total, infrastructure failures
# abort the panel without burning attempts.

_GEN_POOL = 3   # generator threads == ComfyUI in-flight cap
_VAL_POOL = 2   # validator threads == spark connection slots


def _generate_one(proj: Project, prompt: dict, attempt: int,
                  sem: threading.Semaphore, wf_name: str, log):
    """Generate one panel image (blocking). Returns (img_path, prompt_id) or
    (None, None) after marking the panel failed (infrastructure error)."""
    key = prompt["panel"]
    img_dir = proj.out_dir("s4")
    img_path = img_dir / f"{key}.png"
    seed = int(prompt["seed"]) + attempt * SEED_STEP
    wf = _build_workflow(prompt, seed, proj.id, wf_name)
    tmp_dir = img_dir / f".tmp_{key}_{attempt}"
    try:
        with sem:  # in-flight ComfyUI generation cap
            pid = comfy.submit(wf)
            log.info(f"{key}: attempt {attempt} seed {seed} submitted ({pid})")
            entry = comfy.wait(pid, timeout=config.COMFY_TIMEOUT)
            paths = comfy.fetch_outputs(entry, tmp_dir)
    except comfy.ComfyError as exc:
        log.error(f"{key}: ComfyUI failure, panel aborted: {exc}")
        _atomic_write_json(proj.out_dir("s4_validate") / f"{key}.json",
                           {"panel": key, "final": "failed", "error": str(exc),
                            "attempts": []})
        _set_item(proj, key, "failed", {"error": str(exc)})
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, None
    if not paths:
        _atomic_write_json(proj.out_dir("s4_validate") / f"{key}.json",
                           {"panel": key, "final": "failed",
                            "error": "no outputs", "attempts": []})
        _set_item(proj, key, "failed", {"error": "no outputs"})
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, None
    if len(paths) > 1:
        log.warning(f"{key}: {len(paths)} outputs returned, taking the first")
    if img_path.exists():
        img_path.unlink()
    os.replace(paths[0], img_path)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    log.info(f"{key}: image saved -> {img_path.name}")
    return img_path, pid


# ---- preflight ----------------------------------------------------------------

def _preflight(session_id: str, log) -> bool:
    """LLM connectivity + ComfyUI queue check before a real generation batch.
    The endpoint has minutes-scale "empty-stream" windows (2026-09-28), so the
    LLM is probed up to 3 times with 120 s gaps before giving up (each probe
    already retries internally inside llm.chat). ffmpeg is not an S4
    dependency (images only), so it is not checked here."""
    for attempt in range(3):
        try:
            # max_tokens must exceed the reasoning budget: spark is a thinking
            # model and an 8-token probe gets eaten by reasoning_content alone
            # (bug confirmed 2026-09-28: content='' finish=length → the stage
            # refused to start on a perfectly healthy endpoint)
            llm.chat([{"role": "user", "content": "reply with: ok"}],
                     max_tokens=256, session_id=session_id, timeout=60)
            break
        except llm.LLMError as exc:
            log.warning(f"preflight probe {attempt + 1}/3 failed: {str(exc)[:140]}")
            if attempt < 2:
                time.sleep(120)
    else:
        log.error("preflight: LLM unavailable after 3 probes (empty-stream regime?)")
        return False
    try:
        q = comfy.queue_status()
        running = len(q.get("queue_running") or [])
        pending = len(q.get("queue_pending") or [])
        log.info(f"preflight: comfy queue running={running} pending={pending}")
    except comfy.ComfyError as exc:
        log.error(f"preflight: ComfyUI unavailable: {exc}")
        return False
    return True


# ---- stage entry ---------------------------------------------------------------

def run(proj: Project, **opts) -> bool:
    log = proj.get_logger("s4")
    t0 = time.time()
    out_dir = proj.out_dir("s3")
    files = sorted(out_dir.glob("*.json"))
    if not files:
        log.error(f"no s3 prompt files under {out_dir} (run s3 first)")
        return False
    prompts = []
    for f in files:
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
            for k in ("panel", "positive", "seed", "width", "height"):
                if k not in p:
                    raise KeyError(k)
            p["negative"] = p.get("negative") or config.load_neg_prompt()
            prompts.append(p)
        except Exception as exc:  # noqa: BLE001
            log.error(f"{f.name}: invalid s3 prompt ({exc}), skipped")
            _set_item(proj, f.stem, "failed", {"error": f"invalid prompt file: {exc}"})
    prompts.sort(key=_panel_sort_key)

    sel = opts.get("panels")
    if sel is not None:
        sel = [str(k) for k in sel]
        prompts = [p for p in prompts if p["panel"] in sel]
    if not prompts:
        log.error("no usable s3 prompts")
        return False

    dryrun = bool(opts.get("dryrun"))
    inflight = max(1, int(opts.get("inflight", 3)))
    quality = str(opts.get("image_quality")
                  or proj.params.get("image_quality")
                  or config.IMAGE_QUALITY_DEFAULT)
    wf_name = config.IMAGE_WORKFLOWS.get(quality,
                                         config.IMAGE_WORKFLOWS[config.IMAGE_QUALITY_DEFAULT])
    img_dir = proj.out_dir("s4")

    todo, skipped = [], []
    for p in prompts:
        key = p["panel"]
        if (proj.item_status("s4", key) == "completed"
                and (img_dir / f"{key}.png").exists()):
            skipped.append(key)
        else:
            todo.append(p)
    if skipped:
        log.info(f"skip completed panels: {skipped}")
    log.info(f"s4 start: {len(todo)} to generate, {len(skipped)} skipped, "
             f"inflight={inflight}, quality={quality} ({wf_name}), dryrun={dryrun}")

    sessions = llm.new_session_pool()  # one per DGX; per-panel affinity
    if not dryrun and todo and not _preflight(sessions[0], log):
        return False

    results: dict = {}
    if todo:
        sem = threading.Semaphore(inflight)
        if dryrun:
            for p in todo:
                seed = int(p["seed"])
                wf = _build_workflow(p, seed, proj.id, wf_name)
                n12 = (f"node12={wf['12']['inputs']['width']}x{wf['12']['inputs']['height']}"
                       if "12" in wf else "node12=absent(no-upscale)")
                log.info(f"[dryrun] {p['panel']}: node5.seed={seed} "
                         f"node6={wf['6']['inputs']['width']}x{wf['6']['inputs']['height']} "
                         f"batch_size={wf['6']['inputs']['batch_size']} {n12} "
                         f"node8.prefix={wf['8']['inputs']['filename_prefix']}")
            results = {p["panel"]: "dryrun" for p in todo}
        else:
            # ---- dual-pool orchestration (user 2026-09-28): generators and
            # validators as separate parallel pools bridged by queues ----
            gen_q: "queue.Queue" = queue.Queue()
            val_q: "queue.Queue" = queue.Queue()
            lock = threading.Lock()
            pending = {p["panel"] for p in todo}
            attempts_by_key: dict = {p["panel"]: [] for p in todo}

            def resolve(key: str, status: str) -> None:
                results[key] = status
                with lock:
                    pending.discard(key)
                    empty = not pending
                if empty:  # poison both pools
                    for _ in range(_GEN_POOL):
                        gen_q.put(None)
                    for _ in range(_VAL_POOL):
                        val_q.put(None)

            def generator() -> None:
                while True:
                    item = gen_q.get()
                    if item is None:
                        return
                    prompt, attempt = item
                    try:
                        img_path, pid = _generate_one(proj, prompt, attempt,
                                                     sem, wf_name, log)
                    except Exception as exc:  # noqa: BLE001 - never strand a panel
                        log.error(f"{prompt['panel']}: generator error: "
                                  f"{type(exc).__name__}: {exc}")
                        img_path = None
                    if img_path is None:
                        resolve(prompt["panel"], "failed")
                    else:
                        val_q.put((prompt, attempt, img_path, pid))

            def validator(session_id: str) -> None:
                while True:
                    item = val_q.get()
                    if item is None:
                        return
                    prompt, attempt, img_path, pid = item
                    key = prompt["panel"]
                    seed = int(prompt["seed"]) + attempt * SEED_STEP
                    try:
                        verdict = _validate_panel(proj, prompt, img_path,
                                                   session_id, log)
                    except Exception as exc:  # noqa: BLE001 - never strand a panel
                        log.error(f"{key}: validator error: {type(exc).__name__}: {exc}")
                        resolve(key, "failed")
                        continue
                    attempts_by_key[key].append(
                        {"attempt": attempt, "seed": seed, "prompt_id": pid,
                         "pass": verdict["pass"], "issues": verdict["issues"],
                         "score": verdict["score"]})
                    if verdict["pass"] and verdict["score"] >= PASS_SCORE:
                        log.info(f"{key}: attempt {attempt} accepted "
                                 f"(score={verdict['score']})")
                        _atomic_write_json(proj.out_dir("s4_validate") / f"{key}.json", {
                            "panel": key, "final": "completed",
                            "image": f"s4_images/{key}.png",
                            "attempts": attempts_by_key[key]})
                        _set_item(proj, key, "completed",
                                  {"attempts": len(attempts_by_key[key]),
                                   "score": verdict["score"], "seed": seed,
                                   "image": f"s4_images/{key}.png"})
                        resolve(key, "completed")
                    elif attempt + 1 < MAX_ATTEMPTS:
                        log.info(f"{key}: attempt {attempt} rejected "
                                 f"(score={verdict['score']}) — requeued for "
                                 f"round {attempt + 2}: {verdict['issues']}")
                        gen_q.put((prompt, attempt + 1))
                    else:
                        log.warning(f"{key}: all {MAX_ATTEMPTS} attempts "
                                    f"rejected -> needs_review")
                        _atomic_write_json(proj.out_dir("s4_validate") / f"{key}.json", {
                            "panel": key, "final": "needs_review",
                            "image": f"s4_images/{key}.png",
                            "attempts": attempts_by_key[key]})
                        _set_item(proj, key, "needs_review",
                                  {"attempts": len(attempts_by_key[key]),
                                   "issues": verdict["issues"],
                                   "image": f"s4_images/{key}.png"})
                        resolve(key, "needs_review")

            threads = [threading.Thread(target=generator, daemon=True)
                       for _ in range(_GEN_POOL)]
            threads += [threading.Thread(target=validator,
                                          args=(sessions[i % len(sessions)],),
                                          daemon=True)
                        for i in range(_VAL_POOL)]
            for p in todo:
                gen_q.put((p, 0))
            for t in threads:
                t.start()
            for t in threads:
                t.join()

    n_completed = sum(1 for r in results.values() if r == "completed")
    n_review = sum(1 for r in results.values() if r == "needs_review")
    n_failed = sum(1 for r in results.values() if r == "failed")
    n_dry = sum(1 for r in results.values() if r == "dryrun")
    log.info(f"s4 summary: completed {n_completed} / needs_review {n_review} / "
             f"failed {n_failed} / dryrun {n_dry}, skipped {len(skipped)}, "
             f"{time.time() - t0:.1f}s")
    if dryrun:
        return all(r == "dryrun" for r in results.values())
    if not todo:
        return True
    return (n_completed + n_review) > 0
