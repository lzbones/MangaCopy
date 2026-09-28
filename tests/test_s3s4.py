#!/Users/qingxu/.ai-env/bin/python
"""S3/S4 integration test (fixture fabricated under /tmp, NO ComfyUI
submission — s4 runs in dryrun only).

TEST_MODE=real  -> real LLM calls for the s3 dynamic block (~2 calls)
TEST_MODE=mock  -> llm.chat stubbed (endpoint outage fallback, 0 real calls)"""

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path("/Users/qingxu/Documents/Software/AI/MangaCopy")
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from mangacopy import config, image_gen, image_prompt, llm, stages  # noqa: E402
from mangacopy.project import DEFAULT_PARAMS, SUBDIRS, Project  # noqa: E402

PROJ_DIR = Path("/tmp/s3s4test")
REF_PAGE = ROOT / "Ref" / "第187话" / "0001.png"
MODE = os.environ.get("TEST_MODE", "mock")

RESULTS = []
LLM_CALLS = {"n": 0}

_MOCK_SCENE = {
    "scene_tags": ("tears, angry, pointing, standing, leaning forward, wind, "
                   "cowboy shot, from above, rooftop, dusk, school building, "
                   "dramatic lighting, speed lines, greyscale, monochrome"),
    "quality_tail": ",masterpiece,best quality,",
}


def _mock_chat(messages, *, json_mode=False, max_tokens=None, timeout=None, session_id=None):
    LLM_CALLS["n"] += 1
    return json.dumps(_MOCK_SCENE, ensure_ascii=False)


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def tokens(s):
    return [t.strip() for t in str(s).split(",") if t.strip()]


# ---------------------------------------------------------------- fixture ----
def build_fixture():
    if PROJ_DIR.exists():
        shutil.rmtree(PROJ_DIR)
    for sub in SUBDIRS:
        (PROJ_DIR / sub).mkdir(parents=True, exist_ok=True)
    state = {
        "id": "s3s4test",
        "created": "2026-09-26T00:00:00",
        "params": {"ref_path": str(REF_PAGE), **DEFAULT_PARAMS},
        "stages": {s: {"status": "pending", "items": {}} for s in stages.STAGE_ORDER},
    }
    (PROJ_DIR / "project.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    settings = {
        "characters": [
            {
                "name": "苏雪",
                "aliases": ["小雪", "Xue"],
                "gender": "female",
                "appearance_cn": "银白色长直发，异色瞳（左眼红色、右眼蓝色），身材纤细",
                "danbooru_tags": "silver hair, very long hair, straight hair, heterochromia, red eyes, blue eyes, slender",
                "anchor": "",
                "clothing_states": [
                    {"pages": "1-6", "state_cn": "白色连衣裙",
                     "danbooru_tags": "white dress, thighhighs"},
                    {"pages": "7-13", "state_cn": "深蓝色水手服",
                     "danbooru_tags": "serafuku, blue skirt, pleated skirt"},
                    {"pages": "1-3", "state_cn": "红色外套加身（内着白色连衣裙）",
                     "danbooru_tags": "red coat, white dress, thighhighs"},
                ],
            },
            {
                "name": "林岳",
                "aliases": ["阿岳"],
                "gender": "male",
                "appearance_cn": "黑色短发，金色眼瞳，身材高大",
                "danbooru_tags": "black hair, short hair, gold eyes, tall",
                "anchor": "astolfo \\(fate\\)",
                "clothing_states": [
                    {"pages": "1-4", "state_cn": "黑色学生制服",
                     "danbooru_tags": "black gakuran, school uniform"},
                    {"pages": "5-13", "state_cn": "白色T恤便装",
                     "danbooru_tags": "white t-shirt, jeans"},
                ],
            },
        ]
    }
    (PROJ_DIR / "s2_zero" / "00_settings.json").write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")

    md1 = """## 分镜编号与位置
分镜 p001_01，第 1 页第 1 镜，形状 vertical，位于页面右上部，右起第一格。

## 出场人物
- 苏雪（小雪）：当页衣着为红色外套加身（内着白色连衣裙）；银白长直发、异色瞳（左红右蓝）；表情愤怒，眉头紧锁，眼中含泪。
- 林岳（阿岳）：当页衣着为黑色学生制服；黑色短发、金色眼瞳；表情紧张，额头冒汗，不敢直视对方。

## 动作与姿态
苏雪右手攥紧信纸指向林岳，身体前倾；林岳背靠天台围栏，双手抬起做安抚姿势，重心后仰。

## 空间关系与构图
两人对立于天台，苏雪占画面左侧三分之二，林岳被逼至右侧围栏角落；前景虚化出栏杆斜线。

## 镜头角度
中景，平视，略微仰角强调苏雪的压迫感。

## 背景环境
傍晚的学校天台，远处教学楼剪影与积雨云，强风吹起发丝与外套下摆。

## 对白原文
苏雪：你到底瞒了我什么？
林岳：……对不起。

## 拟声词
哗——（风声）

## 氛围与情绪
对峙的紧张与伤感并存，冷色调疏离感。

## 备注
衣着状态：苏雪为红色外套状态，林岳为黑色学生制服状态。
"""
    md2 = """## 分镜编号与位置
分镜 p001_02，第 1 页第 2 镜，形状 horizontal，页面底部横贯整页。

## 出场人物
- 苏雪（小雪）：当页衣着为红色外套加身（内着白色连衣裙）；银白长直发向后飘扬；表情决绝，含泪奔跑。

## 动作与姿态
苏雪全力奔跑冲出天台门，回头一瞥，动势强烈。

## 空间关系与构图
苏雪位于画面中心偏左，走廊纵深向后延伸，速度线填充背景。

## 镜头角度
全景，平视追拍视角。

## 背景环境
傍晚的走廊，逆光，地面拉出长影。

## 对白原文
苏雪（内心）：再见了。

## 拟声词
哒哒哒（脚步声）

## 氛围与情绪
离别与决意，速度感与落寞并存。

## 备注
衣着状态：苏雪为红色外套状态。
"""
    (PROJ_DIR / "s2_zero" / "03_panels" / "p001_01.md").write_text(md1, encoding="utf-8")
    (PROJ_DIR / "s2_zero" / "03_panels" / "p001_02.md").write_text(md2, encoding="utf-8")

    crops = PROJ_DIR / "s1_understand" / "crops"
    crops.mkdir(parents=True, exist_ok=True)
    with Image.open(REF_PAGE) as im:  # Ref/ 只读：仅打开读取
        im.crop((900, 0, 1500, 1800)).save(crops / "p001_01.png")    # 600x1800 -> vertical
        im.crop((0, 1857, 1500, 2357)).save(crops / "p001_02.png")   # 1500x500 -> horizontal
    return settings


def main():
    settings = build_fixture()
    proj = Project.load(PROJ_DIR)

    # ---- S3 ------------------------------------------------------------
    if MODE == "mock":
        llm.chat = _mock_chat
        print(f"TEST_MODE={MODE}: llm.chat stubbed")
    else:
        print(f"TEST_MODE={MODE}: real LLM calls")
    ok = stages.run_stage(proj, "s3")
    check("run_stage s3 ok", ok)
    p1 = json.loads((PROJ_DIR / "s3_prompts" / "p001_01.json").read_text(encoding="utf-8"))
    p2 = json.loads((PROJ_DIR / "s3_prompts" / "p001_02.json").read_text(encoding="utf-8"))
    neg = config.load_neg_prompt()
    for p, key in ((p1, "p001_01"), (p2, "p001_02")):
        check(f"{key}: schema keys", set(p) >= {"panel", "page", "order", "positive",
                                                "negative", "anchor", "characters",
                                                "size_bucket", "width", "height", "seed"})
        check(f"{key}: negative == api_neg_prompt", p["negative"] == neg)
        check(f"{key}: seed md5-deterministic",
              p["seed"] == int(hashlib.md5(f"s3s4test:{key}".encode()).hexdigest()[:8], 16),
              f"seed={p['seed']}")
        check(f"{key}: positive no CJK", not image_prompt._has_cjk(p["positive"]))
        check(f"{key}: scene_tags non-empty from LLM", bool(p["scene_tags"]))
        check(f"{key}: scene tags all inside positive",
              all(t in p["positive"] for t in tokens(p["scene_tags"])))
        toks_low = [t.lower() for t in tokens(p["positive"])]
        check(f"{key}: quality tail last tokens", toks_low[-2:] == ["masterpiece", "best quality"],
              ", ".join(toks_low[-2:]))
        check(f"{key}: negative tokens absent from positive",
              "worst quality" not in p["positive"].lower())

    check("p1: characters=[苏雪,林岳]", [c["name"] for c in p1["characters"]] == ["苏雪", "林岳"])
    check("p1: count_tag", p1["count_tag"] == "1girl, 1boy", p1["count_tag"])
    check("p1: anchor list", p1["anchor"] == ["astolfo \\(fate\\)"], str(p1["anchor"]))
    check("p1: bucket vertical 1024x1408",
          p1["size_bucket"] == "vertical" and (p1["width"], p1["height"]) == (1024, 1408),
          p1["size_bucket"])
    check("p1: page/order", p1["page"] == 1 and p1["order"] == 1)
    low1 = p1["positive"].lower()
    req1 = tokens("silver hair, very long hair, straight hair, heterochromia, red eyes, blue eyes, "
                  "slender, red coat, white dress, thighhighs, "
                  "black hair, short hair, gold eyes, tall, black gakuran, school uniform")
    miss1 = [t for t in req1 if t not in low1]
    check("p1: all appearance+clothing tokens present", not miss1, f"missing={miss1}")
    check("p1: anchor prefixed before 林岳 tags",
          "astolfo \\(fate\\), black hair, short hair" in p1["positive"])
    check("p1: other-page clothing absent", "serafuku" not in low1 and "jeans" not in low1)
    check("p1: 苏雪 has no anchor", p1["characters"][0]["anchor"] == "")
    check("p1: multi-interval clothing picks LATTER (1-3 red coat)",
          p1["characters"][0]["clothing_state_cn"] == "红色外套加身（内着白色连衣裙）",
          p1["characters"][0]["clothing_state_cn"])

    check("p2: characters=[苏雪]", [c["name"] for c in p2["characters"]] == ["苏雪"])
    check("p2: count_tag 1girl", p2["count_tag"] == "1girl")
    check("p2: anchor empty", p2["anchor"] == [])
    check("p2: bucket horizontal 1408x1024",
          p2["size_bucket"] == "horizontal" and (p2["width"], p2["height"]) == (1408, 1024),
          p2["size_bucket"])
    check("p2: order 2", p2["order"] == 2)
    low2 = p2["positive"].lower()
    miss2 = [t for t in tokens("silver hair, very long hair, straight hair, heterochromia, "
                               "red eyes, blue eyes, slender, red coat, white dress, thighhighs")
             if t not in low2]
    check("p2: 苏雪 tokens present", not miss2, f"missing={miss2}")
    check("p2: 林岳 absent", "gakuran" not in low2 and "gold eyes" not in low2 and "1boy" not in low2)

    report = (PROJ_DIR / "s3_check" / "report.md").read_text(encoding="utf-8")
    check("report.md written", "S3 Prompt 全面检查报告" in report)
    check("report: both panels", "p001_01" in report and "p001_02" in report)
    check("report: no panel UNRESOLVED",
          "UNRESOLVED 0" in report and "— UNRESOLVED" not in report)
    items3 = proj.state["stages"]["s3"]["items"]
    check("items: p001_01/p001_02 completed",
          items3["p001_01"]["status"] == "completed" and items3["p001_02"]["status"] == "completed")
    check("items: check completed", items3["check"]["status"] == "completed",
          json.dumps(items3["check"], ensure_ascii=False))

    # ---- deterministic unit checks (no LLM) -----------------------------
    check("_strip_weight: (red eyes:1.2)", image_prompt._strip_weight("(red eyes:1.2)") == "red eyes")
    check("_strip_weight: escaped parens", image_prompt._strip_weight("astolfo \\(fate\\)") == "astolfo (fate)")
    check("_strip_weight: plain parens", image_prompt._strip_weight("(re:zero)") == "re:zero")
    check("_strip_weight: plain", image_prompt._strip_weight("school uniform") == "school uniform")
    charA = settings["characters"][0]
    check("_clothing: page1 -> latter hit (red coat)",
          image_prompt._clothing_for_page(charA, 1)[1] == "red coat, white dress, thighhighs")
    check("_clothing: page5 -> white dress",
          image_prompt._clothing_for_page(charA, 5)[1] == "white dress, thighhighs")
    check("_clothing: page9 -> serafuku",
          image_prompt._clothing_for_page(charA, 9)[1] == "serafuku, blue skirt, pleated skirt")

    fake = json.loads(json.dumps(p1))
    fake["positive"] = fake["positive"].replace("gold eyes, ", "").replace("silver hair", "银白长发")
    issues, _counts, _warn = image_prompt._check_one(
        fake, settings, PROJ_DIR / "s1_understand" / "crops")
    joined = "; ".join(issues)
    check("checker: flags missing token", "①" in joined and "gold eyes" in joined, joined[:120])
    check("checker: flags CJK residue", "④" in joined and "银白长发" in joined)
    fake2 = json.loads(json.dumps(p1))
    fake2["characters"][1]["anchor"] = "someone else"
    check("checker: anchor conflict across panels",
          "林岳" in image_prompt._anchor_conflicts([p1, fake2]))

    # repair restores the tampered prompt deterministically
    log = proj.get_logger("s3")
    by_name = {c["name"]: c for c in settings["characters"]}
    image_prompt._repair_one(fake, by_name, PROJ_DIR / "s1_understand" / "crops", log)
    issues2, _c2, _w2 = image_prompt._check_one(
        fake, settings, PROJ_DIR / "s1_understand" / "crops")
    check("repair: rebuild fixes missing+CJK", not issues2, "; ".join(issues2))

    # ---- S4 dryrun -------------------------------------------------------
    n_img_before = len(list((PROJ_DIR / "s4_images").glob("*.png")))
    ok4 = stages.run_stage(proj, "s4", dryrun=True)
    check("run_stage s4 dryrun ok", ok4)
    log4 = (PROJ_DIR / "logs" / "s4.log").read_text(encoding="utf-8")
    seed1 = p1["seed"]
    check("s4 log: seeds injected",
          f"node5.seed={seed1}" in log4 and f"node5.seed={p2['seed']}" in log4)
    check("s4 log: node6 1024x1408", "node6=1024x1408" in log4)
    check("s4 log: node6 1408x1024", "node6=1408x1024" in log4)
    check("s4 log: node12 1.5x upscale", "node12=1536x2112" in log4 and "node12=2112x1536" in log4)
    check("s4 log: filename_prefix", "s3s4test/p001_01" in log4 and "s3s4test/p001_02" in log4)
    check("s4 log: positive logged", p1["positive"][:40] in log4)
    check("no ComfyUI submission", "submitted" not in log4 and "prompt_id" not in log4)
    check("s4_images untouched by dryrun",
          len(list((PROJ_DIR / "s4_images").glob("*.png"))) == n_img_before)
    items4 = proj.state["stages"]["s4"]["items"]
    check("s4: no items mutated by dryrun", not items4)

    # direct workflow build assertions (no network)
    wf = image_gen._build_workflow(p1, seed1 + 7919, "s3s4test")
    check("wf: node3 positive", wf["3"]["inputs"]["text"] == p1["positive"])
    check("wf: node4 negative", wf["4"]["inputs"]["text"] == p1["negative"])
    check("wf: node5 seed=seed+7919", wf["5"]["inputs"]["seed"] == seed1 + 7919)
    check("wf: node6 bucket+batch", wf["6"]["inputs"] == {"width": 1024, "height": 1408, "batch_size": 1})
    check("wf: node12 1.5x", (wf["12"]["inputs"]["width"], wf["12"]["inputs"]["height"]) == (1536, 2112))
    check("wf: node8 prefix", wf["8"]["inputs"]["filename_prefix"] == "s3s4test/p001_01")
    wf2 = image_gen._build_workflow(p2, p2["seed"], "s3s4test")
    check("wf2: node6 horizontal", (wf2["6"]["inputs"]["width"], wf2["6"]["inputs"]["height"]) == (1408, 1024))
    check("wf2: node12 2112x1536", (wf2["12"]["inputs"]["width"], wf2["12"]["inputs"]["height"]) == (2112, 1536))
    check("wf: template untouched nodes (5 steps/cfg, 14 denoise)",
          wf["5"]["inputs"]["steps"] == 30 and wf["5"]["inputs"]["cfg"] == 7
          and wf["14"]["inputs"]["denoise"] == 0.5
          and wf["1"]["inputs"]["ckpt_name"] == "waiIllustriousSDXL_v170.safetensors")

    # ---- summary ---------------------------------------------------------
    n_fail = sum(1 for _n, ok_, _d in RESULTS if not ok_)
    print(f"\n===== TEST_MODE={MODE} llm_calls={LLM_CALLS['n']}: "
          f"{len(RESULTS)} checks, {n_fail} failed =====")
    if n_fail == 0:
        print("positive(p001_01) head:", p1["positive"][:300])
        print("positive(p001_02) head:", p2["positive"][:300])
        print("--- s3_check/report.md ---")
        print((PROJ_DIR / "s3_check" / "report.md").read_text(encoding="utf-8"))
        print("cleaning test project ...")
        shutil.rmtree(PROJ_DIR)
        print("removed", PROJ_DIR)
    else:
        print("KEEPING test project for inspection:", PROJ_DIR)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
