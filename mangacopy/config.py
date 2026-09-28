"""Global configuration: paths, LLM endpoint, ComfyUI endpoint, image buckets."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PROJECTS_DIR = DATA_DIR / "projects"
API_DIR = ROOT / "Api"
PROMPTS_DIR = ROOT / "mangacopy" / "prompts"

# LLM (OpenAI-compatible endpoint, spark via litellm routing to two DGX hosts)
LLM_BASE_URL = "http://166.111.50.17:4000/v1"
LLM_API_KEY = "sk-qingxu-litellm-thicv639"
LLM_MODEL = "spark"
LLM_TIMEOUT = 1200  # 用户 2026-09-27 终版裁定：上限 20 分钟——静默超 1200s 即快速失败，问题在任务体量
                   #（应拆解编排）而非等待；流式生成中的长调用总长不受此限（实测 2392s 成功案例）
LLM_SESSION_POOL = 2  # 两台 DGX：每个独立单元（页/块/分镜/段）分配一个 UUID，
                      # 单元内粘同一台机器，单元间轮询分散到两台（2026-09-27 用户指示）
LLM_MAX_CONCURRENT = 2  # 用户 2026-09-27：spark 硬件并行能力弱，两台各扛一个并发，
                         # 全局 LLM 并发连接硬上限=2，llm.chat 内信号量强制执行
LLM_MAX_TOKENS = 8000
LLM_RETRY = 3

# ComfyUI
COMFY_HOST = "192.168.50.254"
COMFY_PORT = 8188
COMFY_TIMEOUT = 1800  # 2026-09-27 由 1200 上调：0.7MP/15s 段经验值 400-700s，留足余量

# Image generation size buckets: (width, height)
IMAGE_SIZE_BUCKETS = {
    "vertical": (1024, 1408),
    "horizontal": (1408, 1024),
    "square": (1216, 1216),
}


# Image generation workflow tiers (user 2026-09-26): "standard" = no
# upscaling (~5s/img, default), "hd" = RealESRGAN x4 + 2nd-pass refine
# (~15s/img, only when the user wants high resolution).
IMAGE_WORKFLOWS = {
    "standard": "API - Wai NoUpScaling",
    "hd": "API - Wai",
}
IMAGE_QUALITY_DEFAULT = "standard"

# Text overlay (S4b): CJK font + sizing for typesetting dialogue/onomatopoeia
# onto the textless panels (user 2026-09-26: never let the image model render
# text; PIL typesetting only, both textless and typeset copies are kept).
TEXT_FONT_PATH = "/System/Library/Fonts/PingFang.ttc"
TEXT_FONT_INDEX = 0
TEXT_BASE_SIZE_RATIO = 0.026  # base font size as fraction of image height


def load_neg_prompt() -> str:
    """Read api_neg_prompt from Api/settings_for_Wai.json on every call,
    so user edits to that file take effect without restart."""
    settings = json.loads(
        (API_DIR / "settings_for_Wai.json").read_text(encoding="utf-8")
    )
    return settings["api_neg_prompt"]
