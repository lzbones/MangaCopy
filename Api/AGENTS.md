# VideoAutoGenerator — 工程约定与已知环境事实

本文件记录本仓库专属的工程约定、已做的代码修改，以及运行流水线时必须遵守的环境约束。跨项目通用的机器级事实见全局记忆 `~/.qoder-cn/memory/feedback-llm-media-environment.md`。

## LLM 端点（litellm 代理）在本项目的处理

- **不要依赖 `response_format: json_object`**：本仓库的 `pipeline/llm_service.py::_call_llm` 已加入回退——当端点拒绝 `json_object` 时自动删除该参数重试，并靠 `extract_json_from_response` 解析纯文本。这是有意为之，勿删。
- **大输出会截断**：一次让 LLM 生成多镜头的大 JSON（stage2/3）经常停在 JSON 中间。规避方式：
  1. **逐镜调用**——每次只传一个 shot 给 `format_h3_prompts` / `expand_independent_subscripts`，成功率远高于全量。
  2. 对顽固镜头改用确定性编译兜底：`PromptCompiler.compile_h3_prompt(...)`（秒级、可靠），不要死磕多次重试。

## 已做的代码修改（本会话）

- `core/settings.json`：`llm_max_tokens` 4096 → **8000**，`llm_timeout` 120 → **300**。原值导致 stage1/2/3 输出截断。
- `pipeline/llm_service.py::_call_llm`：增加 `json_object` 被拒时的回退（见上）。
- `pipeline/llm_service.py::expand_independent_subscripts`：修正对 `validate_anchor_coverage` 的调用——该函数是 lambda，签名 `(text, chars, char_anchors, env_anchors, scene="")`，必须用位置参数传，不能用 `characters_present=`/`character_anchors=` 等关键字。

## ffmpeg

- **本机无系统级 ffmpeg**。脚本里不要假设 `~/.ai-env/bin/ffmpeg` 存在。
- 正确取法：`import imageio_ffmpeg; imageio_ffmpeg.get_ffmpeg_exe()`（已安装）。`VideoAssembler(ffmpeg_bin=<该路径>)`。

## 编排脚本规范

- **断点续跑**：每个阶段落盘到 `project.json`，渲染时跳过已完成镜头（检查 `status=="completed"` 且视频文件存在）。
- **无缓冲日志**：后台长任务用 `python -u` 启动，否则 stdout 被块缓冲、看不到进度。
- **关键 ID 从磁盘读取**：如 `ls data/projects/*proj_<前缀>*` 取真实项目目录名，不要手敲完整 ID（易错）。
- **预检**：跑长任务前先确认 LLM 连通性、ComfyUI 队列空闲、ffmpeg 可用。

## LLM 性能基准

- spark 模型部署在**两台 DGX 上，经 litellm 自动路由/负载均衡**。prefill / decode 速率与耗时预期见 [`LLM_PERFORMANCE_BENCHMARK.md`](LLM_PERFORMANCE_BENCHMARK.md)。做视频 prompt、分镜生成等任务前先读，以便预估耗时；因多节点路由导致延迟波动大，按上限留余量。

## 常用分辨率映射（用户指定）

| megapixels | 分辨率 |
|---|---|
| **0.4 MP** | 864×480 (约480P) |
| **0.7 MP** | 1152×640 (约720P) |
| **1.3 MP** | 1376×768 (约1080P) |
| **1.5 MP** | 1504×832 |

> 用户常用这几档。需要调分辨率时，直接让用户从这组里选一个即可（默认推荐 2.0MP / 1920×1152）。

## 本场景产物

- 白早×洛淮南洞穴夺玺视频：`data/projects/proj_1790277498_proj_18002/`（成片 `final_master.mp4`，42s）。
