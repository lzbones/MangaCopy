# MangaCopy 设计文档

> 版本：0.1.0（基础层）　日期：2026-09-26　状态：基础层已实现并通过自验

---

## ① 项目定位与范围

MangaCopy 是一套**个人留存用途**的"漫画 → 脚本 → 复刻生图 → 复刻生视频"流水线：以一部参考漫画（当前样本：Ref/第187话，13 页，1500×2357，黑白）为输入，经多模态理解与脚本化改写后，先用 Wai（Illustrious SDXL 系）生成复刻静态图，再用 MiniMax-H3 生成带音频的复刻视频，最终拼接成片。

- **范围**：单话漫画 → 单部成片视频；人物一致性靠"冻结卡 + 全量重复外貌标签 + 首帧锚定"保障，不训练 LoRA、不做人脸参考图控制。
- **非范围**：批量话数处理、模型训练、发布与分发。
- **运行形态**：对话驱动 + CLI（阶段化断点续跑），GUI 采用 Gradio（决策⑥-1）。
- **实现分层**：基础层（config / llm / templates / comfy / project / stages / ingest，本文档交付）+ 业务层（S1–S8 各阶段模块，由后续实现基于第⑦节接口契约开发）。

---

## ② 环境事实表

以下事实均经前期调研**实际验证**（LLM 真实调用、ComfyUI 健康检查、依赖版本核对），来源为三项前期调研结论，可直接信任。

| 项 | 值 | 备注 |
|---|---|---|
| 解释器 | `~/.ai-env/bin/python`（3.12.13） | 已装 requests 2.32.5、pillow 12.1.1；系统 python3 缺依赖，禁用 |
| LLM 端点 | `http://166.111.50.17:4000/v1` | OpenAI 兼容（litellm 代理） |
| LLM api_key / model | `sk-qingxu-litellm-thicv639` / `spark` | spark 部署于两台 DGX，litellm 负载均衡 |
| LLM vision | 原生支持 | base64 `image_url` 格式（OpenAI vision content 数组） |
| 会话亲和 | 请求体顶层 `metadata.session_id` | **会话池模式（2026-09-27 用户指示）**：`llm.new_session_pool()` 默认 2 个 UUID（每 DGX 一个），每个独立单元（页/块/分镜/段）固定领一个——单元内粘同一台机器，单元间轮询分散两台，LLM 吞吐翻倍 |
| DAG 并行调度 | **`mangacopy/scheduler.py`（2026-09-27）**：真实依赖 `s7←{s4,s6}`、s5/s6 不依赖 s3/s4；s2 后 **[s3→s4→s4b]∥[s5→s6]**（两台 DGX+PRO6000 三线齐开），s7（GPU 生视频）∥ s4b（spark 排印）；依赖失败→下游跳过（不写 project.json，可重跑补救）；CLI auto（无 --confirm/--opts）与 GUI auto 默认走 DAG | 并发写安全：project.py 全局 RLock |
| 超时退避与并行度 | **全局 LLM 并发硬上限 = 2**（`config.LLM_MAX_CONCURRENT`，用户 2026-09-27：两台 DGX 各只扛一个并发，`llm.chat` 内信号量强制，跨所有阶段/分支/线程生效；实测 8 线程峰值并发=2）；**LLM 超时终版统一 1200s/20 分钟**（用户裁定：静默超 20 分钟即快速失败——问题在任务体量应拆解而非等待；流式生成中的长调用总长不受此限，实测 2392s 成功案例）；ReadTimeout 重试间隔 60/180s；各阶段线程并发默认 2；S4 ComfyUI inflight=3（GPU 与 spark 无关） | 2026-09-27 实测：4 并发连接时 DGX 58 分钟零产出（硬件并行弱），降为 2 后恢复正常 |
| L0 树形归并 | **单次 7 路大合并（30-60K token 输入）当日三次饿死端点 prefill → 改两两树形归并**（7 块→4 节点→2→1，单次输入仅 2 份设定 JSON，约为原 1/3）；每节点检查点 `l0_mr_<cid 列表>`（命名=全序 chunk-id 列表，min/max 会碰撞：{0,1,6} 与 {0..6} 同名）；**层内按提交序收集结果保证树结构与检查点名跨次运行确定**；中间层只做结构校验（覆盖各自页码跨度），终态层做全页覆盖校验+确定性修复 | 2026-09-27 用户指示"从任务拆解编排想办法"；单测：随机完成序×3 次结构一致 |
| 已知坑 1 | 端点可能拒绝 `response_format={"type":"json_object"}` | 被拒时自动去参重试一次，并对纯文本做鲁棒 JSON 抽取 |
| 已知坑 2 | 大 JSON 输出易截断 | 超时 300 s、max_tokens 8000；大任务逐条调用防截断 |
| ComfyUI | `http://192.168.50.254:8188`（0.37.2） | RTX PRO 6000 Blackwell Max-Q（94.9 GB VRAM） |
| ComfyUI API | 提交 `POST /prompt`；轮询 `GET /history/{id}`；下载 `GET /view`；上传 `POST /upload/image` | 生图模板 `Api/API - Wai NoUpScaling.json`（standard，默认）与 `Api/API - Wai.json`（hd，按需）；视频模板 `Api/API - video_minimax_h3_i2v.json` / `..._t2v.json` |
| 生图模板要点 | 两模板共通注入节点：1 CheckpointLoader、3/4 正负向 prompt、5 KSampler(steps=30, cfg=7, euler/normal)、6 EmptyLatentImage、8 SaveImage；hd 版额外含 10 RealESRGAN_x4plus_anime_6B + 12 ImageScale(桶尺寸×1.5) + 14 二过 KSampler(denoise=0.5, steps=20) | 负面词继承 `Api/settings_for_Wai.json` 的 `api_neg_prompt` |
| 生图质量档位 | **standard（默认，~5s/张，无放大）**；hd（~15s/张，RealESRGAN x4 + 二过精修，仅用户需要高清时用）——用户 2026-09-26 指定；选择方式：`proj.params["image_quality"]`（创建项目时 `--params '{"image_quality":"hd"}'`）或运行时 `opts["image_quality"]` 覆盖 | `config.IMAGE_WORKFLOWS` 维护映射 |
| S4b 文字排印 | **生图模型不写字**（用户 2026-09-26 要求）：S4 无字底图永不改动；对白/拟声词取自 S1 逐字捕获（`s1_understand/page_XX.json`），LLM 视觉排版（`prompts/s4b_overlay.md`，归一化坐标，失败回退右上起确定性堆叠），PIL+PingFang 绘制（bubble=白底黑边圆角气泡+可选尾巴 / narration=方框 / sfx=大号描边字），输出 `s4_text/pXXX_YY.png` 有字版与 `s4_images/` 无字版**并存**；无对白分镜标记 lines=0 跳过 | 字体 `config.TEXT_FONT_PATH`（PingFang）；零 ComfyUI 调用；opts：panels/concurrency/no_llm |
| 视频单镜耗时 | 约 400–700 s | 轮询超时取 1200 s 留余量 |
| ffmpeg | 本机无系统级安装 | 用 `imageio_ffmpeg.get_ffmpeg_exe()` 获取（已验证可用） |
| 产物落盘位置 | **严禁放 /tmp**（用户 2026-09-27 要求：根目录不方便且重启丢失） | 测试项目一律建在 `data/test_projects/`（monkeypatch `config.PROJECTS_DIR` 指向它）；测试驱动脚本放 `tests/`，日志放 `tests/logs/` |
| GUI 依赖 | gradio 6.28.0（已装入 ~/.ai-env） | |
| 目录约束 | `Api/`、`MiniMaxH3/`、`Ref/` 只读 | 项目非 git 仓库，不做版本管理 |

---

## ③ 总体架构

三层结构：前端层驱动核心层的状态机，核心层调用服务层的两类生成服务；所有断点状态落盘在 `project.json`。

```
+------------------------------------------------------------------+
|  前端层                                                            |
|    对话驱动 / CLI（mangacopy.stages.run_stage 逐段驱动）           |
|    Gradio GUI（项目创建、进度查看、断点续跑）                        |
+------------------------------------------------------------------+
                 |  Project (data/projects/<id>/project.json)
                 v
+------------------------------------------------------------------+
|  核心层  S0..S8 状态机 + 断点续跑                                   |
|  s0 ingest -> s1 understand -> s2 zero_script -> s3 image_prompt  |
|     -> s4 image_gen(+validate) -> s5 video_script -> s6 h3_prompt |
|     -> s7 video_gen -> s8 assemble                                |
|  基础模块: config / llm / templates / comfy / project / stages     |
+------------------------------------------------------------------+
        |  HTTP (requests)                 |  HTTP (requests)
        v                                  v
+---------------------------+   +-----------------------------------+
|  服务层: LLM               |   |  服务层: ComfyUI 0.37.2            |
|  spark via litellm        |   |  生图: Wai (Illustrious SDXL)      |
|  两台 DGX, session 亲和    |   |       + RealESRGAN x4 anime 二过   |
|  vision(base64) / 文本     |   |  生视频: MiniMax-H3 (i2v / t2v)    |
+---------------------------+   +-----------------------------------+
```

数据流主线：`source/*.png` →（S1 版面/分镜理解）→（S2 复刻脚本）→（S3 生图 prompt）→（S4 复刻图 + 校验循环）→（S5 视频脚本）→（S6 H3 prompt）→（S7 分段视频）→（S8 拼接成片）。

---

## ④ 项目产物目录结构

每个项目位于 `data/projects/<YYYYMMDD>_<slug>/`（重名自动追加 `-2`/`-3`）：

```
<project>/
├── project.json        # id / created / params / stages{status, items} —— 断点续跑的
│                       #   唯一事实源；原子写（tmp + rename）
├── source/             # S0：参考漫画页拷贝（规范化 0001.png...）+ manifest.json
├── logs/               # 各阶段日志 s0.log ... s8.log（追加模式）
├── s1_understand/      # S1：整页版面理解 + 分镜裁切细粒度理解产物
├── s2_zero/            # S2：复刻脚本
│   ├── 02_pages/       #   L2 分页脉络（带前后页摘要）
│   └── 03_panels/      #   L3 分镜固定字段模板
├── s2_check/           # S2 checker：一致性全面检查与自动修订记录
├── s3_prompts/         # S3：Wai 生图 prompt（每分镜一个）
├── s3_check/           # S3 prompt 规则自检记录
├── s4_images/          # S4：复刻生成图（最终过审版本）
├── s4_validate/        # S4：LLM 校验结论（含换种子重生成历史）
├── s5_video/           # S5：视频脚本（分段/镜头/声音设计）
├── s6_h3/              # S6：MiniMax-H3 视频 prompt
├── s7_videos/          # S7：分段视频文件
└── s8_final/           # S8：拼接成片
```

`mangacopy/prompts/` 存放各阶段提示词模板（`<<FIELD>>` 占位符语法）；`Api/` 为 ComfyUI 工作流模板与 Wai 设置（只读）。

---

## ⑤ 各阶段算法要点

### S0 ingest（已实现）
- 输入支持 zip（解压到临时目录后取**最深层含编号图片的目录**）与文件夹两种形态，拷入 `source/`。
- 页码识别：文件名词干最后一个数字组，兼容 `0001.png` / `1.png` / `01.png` / `page_0003.jpg` 等；自然排序；缺号仅告警不中断；拷贝时统一规范化为 4 位零填充名。
- 每页用 PIL 读宽高；灰度判定：转 HSV 后饱和度均值（归一化 0–1）< 0.01 判为黑白。产物 `source/manifest.json`：`{"count": N, "pages": [{"index", "file", "w", "h", "gray"}]}`。
- 幂等：重跑先清空旧编号图与 manifest 再重建。

### S1 understand（两轮法）
- **轮 1（整页）**：对每页整图做版面理解——分镜边界（行列分割）、气泡/拟声词归属、**阅读顺序按日式漫画右起**排序，输出页级版面结构。
- **轮 2（分镜裁切）**：按轮 1 边界裁切各分镜，逐个细粒度理解（人物、动作、对白、背景、镜头），控制单次请求的图片数与输出长度以防截断。

### S2 zero_script（自顶向下约束传播）
层级化生成，上层产物冻结后作为下层约束，避免一次性长文生成的漂移：
- **L0 全局设定**：人物卡（外貌锚标签）、服装状态机（何页换装/破损）、环境卡——一次生成后**冻结**。
- **L1 总体复述**：整话剧情梗概与场次划分。
- **L2 分页脉络**：逐页剧情脉络，每页生成时携带前后页摘要（滑动窗口）保持衔接。
- **L3 分镜脚本**：固定字段模板（人物/服装状态/动作/构图/背景/对白/情绪），从 L0–L2 逐层注入。
- **checker**：全部生成后由独立 LLM 调用做全面一致性检查（人物、服装状态机、时间线、对白语言），发现问题进入自动修订循环，产出写 `s2_check/`。

### S3 image_prompt（Wai 规则）
- 结构：**质量头**（masterpiece, best quality 等）+ **人物锚 tag** + 每条 prompt **全量重复**该人物外貌标签（不依赖上下文记忆）+ 当镜衣着 + 动作/构图/背景。
- 负面词继承 `Api/settings_for_Wai.json` 的 `api_neg_prompt`（每次现读，用户改配置即生效）。
- 尺寸分桶：`vertical 1024×1408` / `horizontal 1408×1024` / `square 1216×1216`。
- 种子确定性：每分镜种子记入产物，复现实验可重放。
- 工作流参数写入节点：正向 3、负向 4、seed 5、尺寸 6、`filename_prefix` 8。

### S4 image_gen 与校验并行
- 生图请求吃 ComfyUI 队列，LLM 校验吃 spark——两类服务**并行流水**，互不占对方的等待时间。
- 校验：vision 调用比对生成图与 S3 要求（人物/服装/构图/明显缺陷），不合格**换种子重生成**，循环上限可配；全部记录留 `s4_validate/`。

### S5 video_script
- 风格参数（`params.style`）：`2d` / `3d` / `live`，决定 S7 的生成模式（决策⑥-2）。
- 分段约束：每段 ≤ `max_shot_seconds`（默认 15.0 s）、单镜 ≤ 3 个主要角色。
- 内容改写 + 镜头设计（景别/视角/运镜）+ 声音设计（环境音/非剧情音乐）。
- **对白保留中文**（决策⑥-4），仅在做 H3 prompt 时按规则转写。

### S6 h3_prompt
- 模式规则：`2d` → I2VA，首帧 = S4 生成图；`3d` / `live` → T2V。
- 固定字段顺序：`integrated_multimodal_description` → `overall_soundscape` → `non_diegetic_music`。
- 时间轴标注：`[Shot N] At MM:SS.mmm`；对白格式 `<d>[Chinese] 中文原文</d>`。
- 运镜描述 = 类型 + 幅度 + 速度（如 "slow push-in, small amplitude"）。
- 一致性：每段重复身份锁与服装描述（防多段生成遗忘）。
- 输出前过**六项自检**（字段完整/时间轴单调/对白闭合/身份锁/服装一致/风格守卫）。

### S7 video_gen（ComfyUI 逐段）
- i2v：先 `comfy.upload_image()` 上传首帧，写入 LoadImage 节点（模板节点 `114` 的 `image`），`105:104` 节点 `first_frame` 链路保持；t2v：ResolutionSelector 节点（`115`）按 `video_aspect` + `megapixels` 设置。
- 时长：`105:111` PrimitiveFloat 节点值 = 段时长（≤ max_shot_seconds），`105:107` 表达式自动对齐 24 fps 帧数。
- prompt 写入 `105:104` 的 `prompt`；逐段 `submit` → `wait`（超时 1200 s）→ `fetch_outputs` 收回视频。
- 每段结果记 `set_item("s7", seg_key, ...)`，断点续跑跳过已完成段。

### S8 assemble
- ffmpeg 取自 `imageio_ffmpeg.get_ffmpeg_exe()`；分段视频按时间轴顺序拼接。
- 相邻段音频交叉淡化 0.3–0.6 s，避免爆音；产物写 `s8_final/`。

---

## ⑥ 已确认的设计决策记录

以下决策由用户 **2026-09-26 确认**，后续实现不得偏离：

| # | 决策 | 内容 |
|---|---|---|
| 1 | GUI 框架 | 采用 Gradio（对话驱动 + CLI 为主，GUI 为进度/续跑面板） |
| 2 | 视频模式随风格 | `style=2d` → H3 I2V（首帧锚定 S4 图）；`3d` / `live` → T2V |
| 3 | 生图尺寸分桶 | vertical 1024×1408 / horizontal 1408×1024 / square 1216×1216 |
| 4 | 对白语言 | 视频对白保留中文原文（`<d>[Chinese] …</d>`） |

---

## ⑦ 接口契约（基础层）

后续 S1–S8 业务模块**必须**按以下签名对接（`from mangacopy import …`）；实现侧不得改动签名。

### config
```python
ROOT / DATA_DIR / PROJECTS_DIR / API_DIR / PROMPTS_DIR   # pathlib.Path
LLM_BASE_URL / LLM_API_KEY / LLM_MODEL                    # str
LLM_TIMEOUT=300 / LLM_MAX_TOKENS=8000 / LLM_RETRY=3       # int
COMFY_HOST="192.168.50.254" / COMFY_PORT=8188 / COMFY_TIMEOUT=1200
IMAGE_SIZE_BUCKETS: dict[str, tuple[int, int]]            # {"vertical":(1024,1408), ...}
load_neg_prompt() -> str                                  # 每次现读 settings_for_Wai.json
```

### llm
```python
class LLMError(Exception)
new_session_id() -> str                                   # uuid4 hex；每次流水线运行取一个并全程传递
chat(messages: list, *, json_mode: bool = False,
      max_tokens: int | None = None, timeout: int | None = None,
      session_id: str | None = None) -> str
chat_vision(text_prompt: str, image_paths: list,
      *, json_mode=False, max_tokens=None, timeout=None,
      session_id=None) -> str                             # text 项在前，图片按序 data URL
extract_json(text: str) -> dict | list                    # 剥 ```json 围栏 + 首个配平 JSON；失败 raise LLMError
```

### templates
```python
render(name: str, **fields) -> str    # 读 prompts/{name}.md；<<KEY>> 替换；缺失占位符保留原样并 warning 到 stderr
```

### comfy
```python
class ComfyError(Exception)
health() -> dict                                  # GET /system_stats
queue_status() -> dict                            # GET /queue
load_workflow(name: str) -> dict                  # 读 Api/{name}.json，name 如 "API - Wai"
set_node(workflow: dict, node_id: str, **inputs)  # 原地改已有输入；节点或输入键缺失 raise KeyError
submit(workflow: dict) -> str                     # POST /prompt，返回 prompt_id
wait(prompt_id: str, timeout: int = None, poll: float = 5.0) -> dict
                                                  # 轮询 /history/{id}；error 状态 raise ComfyError（附节点错误摘要）
fetch_outputs(history_entry: dict, dest_dir: Path) -> list[Path]
                                                  # 下载 outputs 的 images/videos 到 dest_dir
upload_image(path: str | Path) -> str             # POST /upload/image (overwrite=true)，返回服务端文件名
```

### project
```python
class Project:
    dir: Path; id: str; params: dict; state: dict  # state 即 project.json 全量内存态
    @classmethod create(cls, ref_path: str, slug: str, params: dict | None = None) -> Project
    @classmethod load(cls, proj_dir) -> Project
    stage_status(stage: str) -> str
    set_stage(stage: str, status: str, note: str | None = None) -> None   # 立即写盘
    item_status(stage: str, key: str) -> str | None
    set_item(stage: str, key: str, status: str, data=None) -> None
                  # items[key] = {"status": status, "data": data}，立即写盘
    out_dir(stage: str) -> Path
                  # s0→source/ s1→s1_understand/ s2→s2_zero/ s2_check→s2_check/
                  # s3→s3_prompts/ s3_check→s3_check/ s4→s4_images/ s4_validate→s4_validate/
                  # s5→s5_video/ s6→s6_h3/ s7→s7_videos/ s8→s8_final/
    get_logger(stage: str) -> logging.Logger      # logs/{stage}.log 追加 + stderr；同名复用
    save() -> None                                # 原子写回 project.json
```
项目默认参数（可被 `create(params=...)` 覆盖）：`style="2d"`, `dialogue_lang="zh"`, `megapixels=0.7`, `video_aspect="16:9 (Widescreen)"`, `max_shot_seconds=15.0`；`ref_path` 恒为绝对路径。

### stages
```python
STAGE_ORDER = ["s0", ..., "s8"]
STAGE_MODULES = {"s0": "ingest", "s1": "understand", "s2": "zero_script",
                 "s3": "image_prompt", "s4": "image_gen", "s5": "video_script",
                 "s6": "h3_prompt", "s7": "video_gen", "s8": "assemble"}
run_stage(proj: Project, stage: str, **opts) -> bool
        # 延迟 import mangacopy.<module> 并调其 run(proj, **opts)；
        # 前置 set_stage(in_progress)，成功 completed / 异常或 False → failed(带 note)，返回 bool
next_pending(proj: Project) -> str | None
        # STAGE_ORDER 中首个未 completed 的阶段（支持断点续跑与失败重试）
```

### 业务模块约定（S1–S8 实现方）
每个阶段模块需暴露 `run(proj: Project, **opts) -> bool`；产物写 `proj.out_dir(<stage>)`，细粒度进度用 `proj.set_item` 记录以获得断点续跑能力；日志用 `proj.get_logger(<stage>)`。

---

## ⑧ 工程约束清单

1. **json_object 回退**：`llm.chat(json_mode=True)` 在端点拒绝 `response_format` 时自动去参重试一次；业务侧仍须对输出用 `extract_json` 鲁棒抽取，不假设纯 JSON 返回。
2. **大 JSON 防截断**：单次调用输出控制在 max_tokens=8000 内；超过的批量任务（如 L2 逐页、L3 逐镜）**逐条调用**，不一次性生成整话大 JSON。
3. **断点续跑**：所有进度经 `set_stage` / `set_item` 即时落盘；驱动侧循环 `next_pending` → `run_stage`，崩溃后从首个未完成单元恢复，已完成的单元（item 级）不重做。
4. **无缓冲日志**：流水线/驱动脚本以 `python -u` 运行，长任务日志实时可见。
5. **ffmpeg 获取**：一律 `imageio_ffmpeg.get_ffmpeg_exe()`（本机无系统 ffmpeg）。
6. **长任务三件套预检**：任何生图/生视频批量任务启动前检查（a）LLM 连通（`comfy` 之外 `llm.chat` 一次小请求），（b）ComfyUI 队列（`queue_status`），（c）ffmpeg 可用；任一失败提前终止，避免排队后段任务白跑。
7. **重试与退避**：LLM 超时/连接错误/5xx 按 `LLM_RETRY=3` 退避重试（5 s/15 s）；失败抛 `LLMError` 并附最后一次响应片段。
8. **会话亲和**：每次流水线运行生成一个 `session_id` 全程复用（路由同一台 DGX 保证上下文缓存命中），不同运行之间不共享。
9. **只读目录**：`Api/`、`MiniMaxH3/`、`Ref/` 不写入；负面词等配置通过现读文件生效而非复制快照。
10. **原子写**：`project.json` 经临时文件 + `os.replace` 原子更新，避免崩溃损坏状态。

---

## ⑨ 扩展点

| 扩展点 | 位置 | 方式 |
|---|---|---|
| 风格 | `params.style`（2d/3d/live） | S5/S6/S7 按 style 分支；新增风格只需扩展映射与 prompt 模板 |
| 生图模型 | `Api/` 新增工作流 + `comfy.load_workflow(name)` | 节点参数经 `set_node` 注入，模板与代码解耦 |
| 视频模型 | 同上（i2v/t2v 模板成对） | `STAGE_MODULES["s7"]` 模块内按模板名分发 |
| 分辨率档位 | `config.IMAGE_SIZE_BUCKETS` / `params.megapixels` | 增加桶项即可，S3/S7 读取配置不写死 |
| 提示词模板 | `mangacopy/prompts/*.md` | 只改模板不动代码；占位符缺失有 warning 不崩 |
| 阶段钩子 | `stages.run_stage` 前后 | 可插入通知/计量逻辑（预留：note 字段与 logger 均已结构化） |

---

## ⑩ 前端层（CLI + Gradio GUI）

> 版本：0.1.0（前端层）　交付物：项目根 `cli.py`、`gui.py`。二者只经 `mangacopy.project` / `mangacopy.stages` 驱动核心层，不 import 业务逻辑、不重复实现（dryrun 特例见 10.4）。

### 10.1 CLI 命令表

解释器一律 `~/.ai-env/bin/python`；退出码 0 成功 / 1 失败；输出实时 flush（阶段日志本身由 `proj.get_logger` 写 stderr，`python -u` 语义下无缓冲）。

| 命令 | 参数 | 行为 |
|---|---|---|
| `new` | `--ref PATH`（必填）`--slug NAME`（必填）`--style 2d\|3d\|live` `--params JSON` | `Project.create`（`--params` 覆盖 `--style` 与默认参数）→ `run_stage("s0")` → 打印项目 id、目录、manifest 摘要（页数/灰度数）；ref 路径不存在直接报错退出，不创建项目 |
| `list` | — | 枚举 `PROJECTS_DIR` 全部项目：id、创建时间、九阶段状态一行摘要（`C`=completed `.`=pending `>`=in_progress `!`=failed） |
| `status` | `--project ID` | 逐阶段状态 + note + item 统计（completed/failed/needs_review 计数）+ params + manifest 摘要 + `next_pending` 指针 |
| `run` | `--project ID`（必填）`--stage sN\|auto`（必填）`--confirm` `--opts JSON` `--dryrun` | 见 10.2 / 10.3 |

`--opts JSON` 原样透传给阶段模块的 `run(proj, **opts)`（如 `--opts '{"pages":[1,2]}'`）；未知阶段（如 `s9`）、非法 JSON、不存在的项目均给出友好错误（无 traceback），退出码 1。他人未交付的业务模块经 `run_stage` 的 ImportError 兜底转为 failed(note) + 友好提示，不抛 traceback。

示例：

```bash
PY=~/.ai-env/bin/python
$PY cli.py new --ref Ref/第187话 --slug ep187 --style 2d
$PY cli.py list
$PY cli.py status --project 20260926_ep187
$PY cli.py run --project 20260926_ep187 --stage s1 --opts '{"pages":[1,2]}'
$PY cli.py run --project 20260926_ep187 --stage auto --confirm
$PY cli.py run --project 20260926_ep187 --stage s4 --dryrun
```

### 10.2 auto 链与 confirm 语义

- `--stage auto`：循环 `next_pending(proj)` → `run_stage(proj, st)`，直到某阶段失败（打印 note 与 `logs/<st>.log` 路径，退出码 1）或全部 completed（退出码 0）。断点即 `project.json`，中断后重跑 auto 从首个未完成阶段恢复，item 级完成单元不重做。
- `--confirm`（仅作用于 auto 链）：每阶段成功后打印该阶段摘要，stdin 提示 `[Enter] 继续下一阶段 / [s] 跳出`；`s` 跳出为正常退出（退出码 0），已完成进度保留。

### 10.3 dryrun 与 run_stage 状态机陷阱（工程约束，必须遵守）

**陷阱**：`stages.run_stage` 成功后会把阶段标成 `completed`，而 `next_pending()` 按"首个未 completed"取阶段。若 dryrun 走 `run_stage`，一旦被标 completed，后续 auto 链会**跳过该阶段**——状态机被 dryrun 污染。

**约束**：dryrun（仅 s4/s7，模块自带 `opts["dryrun"]` 实现：只构建工作流并打日志，不 submit、不调 LLM、不改状态）**必须模块直调，绕过 run_stage**：

```python
from mangacopy import image_gen
image_gen.run(proj, dryrun=True)   # s7 同理 video_gen.run(proj, dryrun=True)
```

CLI 中 `--stage auto --dryrun` 与 `--stage s4/s7 之外的 --dryrun` 直接报错拒绝；dryrun 前后 `project.json` 的阶段状态与 items 保持原样（已用测试断言验证）。GUI 不提供 dryrun 按钮，即为规避同一陷阱。

### 10.4 GUI（`gui.py`，Gradio 6.28 单文件）

启动：`~/.ai-env/bin/python gui.py [--port 7860] [--share] [--host 127.0.0.1]`；默认只监听 `127.0.0.1`；`import gui` 无任何副作用（构建/启动均在 `build_app()` / `main()` 内）。

结构（自上而下）：

| 区块 | 内容 |
|---|---|
| 顶栏 | 项目下拉（枚举 `PROJECTS_DIR`）+ 刷新；新建表单（ref 路径、slug、style 单选、可选 params JSON），ref 路径不存在时表单下方报错、不创建项目 |
| 全局控制 | Auto-run 按钮；Confirm 开关（默认开）；"继续"按钮（confirm 等待中才可点）；手动刷新 |
| 阶段面板 | s0–s8 九张卡片：阶段名 + 中文说明 + 状态徽章（颜色区分）+ item 计数 + note；每卡三个按钮：运行 / 重试失败项（重跑该阶段，模块自动跳过已完成 item）/ 查看日志尾部（`logs/<st>.log` 末 50 行，底部共享日志框） |
| 产物标签页 | s0 源页画廊（manifest 尺寸/灰度标注）；s2 脚本文件选择器（markdown 渲染）；检查报告（s2_check / s3_check / s6_h3 三份 report.md，markdown 渲染）；s3 prompt JSON；s4 生成图画廊 + 校验记录 JSON；s5/s6 文本；s7 分段视频播放器；s8 成片播放器；无产物时显示占位提示 |

执行模型：

- 每次运行动作（单阶段 / auto）以 `threading.Thread(daemon=True)` 后台执行，UI 不阻塞；worker 内经 `stages.run_stage` 驱动。
- **单例守卫**：按项目 id 记录活跃线程，同一项目运行期间其全部运行/重试/auto 按钮被禁用并拒绝并发（`gr.Error` 提示）。
- **轮询刷新**：`gr.Timer(5.0)` 每 5 s 重读 `project.json` 重绘状态徽章/按钮可用性；手动刷新与项目切换走同一 `refresh_ui`。
- **Confirm 语义（GUI 版）**：auto worker 每完成一个阶段后在 `threading.Event` 上阻塞；Timer 检测到等待态即点亮"继续"按钮；点击后 Event 置位、链继续。Confirm 关闭时 Event 预置为 set，全链自动推进。
