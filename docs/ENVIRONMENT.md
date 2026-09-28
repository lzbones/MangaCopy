# MangaCopy 环境档案：算力条件、AI 服务与实测特性

> 版本：1.0（2026-09-28）
> 用途：供后续开发接手者全面了解本项目的硬件、服务、性能与运维约束。
> 关联文档：[程序架构](ARCHITECTURE.md) · [测试报告](TESTING.md)

## 1. 硬件清单

### 1.1 两台 DGX Spark（spark01 / spark02）——LLM 推理

| 项 | 值 |
|---|---|
| 芯片 | NVIDIA GB10（Grace Blackwell 统一内存架构） |
| 显存/统一内存 | ~128GB（vLLM EngineCore 常驻 ~84GB 加载模型权重） |
| 驱动 | NVIDIA-SMI 580.173.02, CUDA 13.0 |
| 显示 | `Not Supported` 出现在 nvidia-smi 部分字段是**正常现象**（GB10 特性） |
| 上跑服务 | VLLM::EngineCore（每台各一个实例）+ 桌面环境（Xorg/gnome/sunshine 串流） |

**关键算力约束（用户明确指示）**：
- **每台 DGX 同一时间只能扛 1 个并发 LLM 请求**——硬件并行能力弱，全局并发连接硬上限 = 2
- 4 并发连接实测：DGX 58 分钟零产出（互相拖死）

### 1.2 RTX PRO 6000（ComfyUI 宿主）——图片/视频生成

| 项 | 值 |
|---|---|
| GPU | NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition（94.9GB VRAM） |
| ComfyUI | 0.37.2，PyTorch 2.11.0+cu130，Python 3.12.13 |
| 队列 | FIFO 串行处理；图片与视频任务在同一队列交替 |
| 模型 | `waiIllustriousSDXL_v170.safetensors`（另有 11 个 ckpt 可选）、MiniMaxH3（i2v/t2v workflow）、`RealESRGAN_x4plus_anime_6B.pth` |

### 1.3 本机（Mac，流水线宿主）

| 项 | 值 |
|---|---|
| Python | `~/.ai-env/bin/python`（3.12.13）——**必须用这个**，系统 python3 缺依赖 |
| 已装包 | requests 2.32.5、pillow 12.1.1、gradio 6.28.0、imageio-ffmpeg |
| ffmpeg | **无系统级安装**；正确取法 `import imageio_ffmpeg; imageio_ffmpeg.get_ffmpeg_exe()` |
| 其他 | 用户另跑 qoder（AI 编程工具），会竞争 spark 算力 |

## 2. AI 服务端点

### 2.1 spark（LLM，经 litellm 代理）

```
base_url:  http://166.111.50.17:4000/v1
api_key:   sk-qingxu-litellm-thicv639
model:     spark
```

- **OpenAI 兼容**接口，支持 vision（base64 image_url）与 stream（SSE）
- **会话亲和**：请求体顶层字段 `metadata.session_id`（**不是 HTTP header**）——同 session 路由同一台 DGX，不同 session 分散两台；不能写死固定字符串（否则全部粘一台）
- 后端 vLLM 0.27.1，模型为**推理模型**（流式返回中 `reasoning_content` 与 `content` 分开）

### 2.2 ComfyUI（图片/视频生成）

```
host:  192.168.50.254:8188
REST:  POST /prompt（提交）、GET /history/{id}（轮询）、GET /view（下载）、POST /upload/image（上传首帧）
```

- Wai 生图 workflow：`Api/API - Wai.json`（hd 档：RealESRGAN x4 + 二过精修）、`Api/API - Wai NoUpScaling.json`（standard 档，日常默认）
- MiniMaxH3 视频 workflow：`Api/API - video_minimax_h3_i2v.json` / `..._t2v.json`
- 生图模板注入节点：3（正向）/4（负向）/5（seed）/6（尺寸）/8（prefix）/12（hd 放大目标=桶×1.5，仅 hd 模板有）
- 视频模板注入节点：105:104（prompt）、105:111（时长秒）、105:15（seed）、92（prefix）、114（首帧，仅 i2v）、119（i2v megapixels）或 115（t2v aspect+megapixels）

## 3. spark 实测特性（开发接手必读）

### 3.1 性能画像（n=389+ 成功调用实测）

| 指标 | 值 | 说明 |
|---|---|---|
| TTFT（首 token） | **≈ 0s** | prefill 无压力；10K token 输入仅比 18 token 基线多 ~3s |
| decode 速度 | **~10 tok/s** | **真正的瓶颈**——GB10 显存带宽限制 |
| reasoning 占比 | 60-80% 字符量 | 推理模型思考开销大；视觉校验调用 reasoning 达 6.6K 字符 |
| 单调用时长公式 | ≈ (推理+输出 token) ÷ 10 | 40-90 分钟的大调用 = 数万 token 生成 |

### 3.2 延迟分布（按调用类别，单位秒）

| 类别 | 次数 | 中位 | P90 | 最大 |
|---|---|---|---|---|
| S1 整页版面（视觉） | 15 | 153 | 203 | 207 |
| S1 分镜细节（视觉） | 66 | 166 | 498 | 903 |
| S2 L0 分块抽取 | 11 | 232 | 1588 | 2392 |
| S2 L0 树形归并 | 14 | 397 | 1557 | 1615 |
| S2 L2 分页 | 22 | 79 | 116 | 1319 |
| S2 L3 分镜 | 101 | 106 | 262 | 2115 |
| S2 检查 | 44+9 | 380/397 | 899/1384 | 1650 |
| S5 段脚本 | 20 | 370 | 555 | 1219 |
| S6 段生成/修复 | 30 | 417 | 909 | 2075 |

**时段规律**：慢峰周期性出现（每 6-8 小时一轮，单轮 1-3 小时），期间大输入调用延迟放大 3-8 倍；**最佳窗口为凌晨 04:00-07:00**（中位 90-107s）。

### 3.3 故障模式目录（全部被重试/检查点机制吸收）

| 故障 | 表象 | 根因 | 客户端对策 |
|---|---|---|---|
| ReadTimeout | 静默超时 | 慢峰期 prefill/生成停滞 | 60/180s 退避 ×3 重试 |
| json 空流 | `response_format=json_object` 下流结束但零内容 | vLLM guided decoding 间歇故障 | 去 response_format 免费重试一次 |
| 非 json 空流 | 普通（非 json）请求也零内容 | "空流 regime"（分钟级窗口，交替健康） | 纳入退避重试 |
| ~15min 断流 | `ChunkedEncodingError: Response ended prematurely` | litellm/GB10 长流切断 | 中途断流重试 |
| content=null（非流式时代） | HTTP 200 但 content 为 null | vLLM 约束解码失败 | 已被流式改造覆盖 |
| EngineCore 软死锁 | GPU-util 0% + 空流，进程存活 | vLLM EngineCore IPC 失联 | **服务端重启 vLLM**（用户操作） |
| max_tokens 陷阱 | 8-token 探测返回空 content | 推理模型 reasoning 吃光预算 | 探测 max_tokens ≥256 |

### 3.4 非流式 vs 流式（重要教训）

**非流式请求使 read-timeout 变成总时长上限**——GB10 上大调用生成 30-90 分钟，非流式永远超时。**必须用 `stream: true`**：生成期间字节持续流出，超时只约束"字节间静默"。这是本项目 llm.py 的核心设计。

## 4. PRO 6000 实测特性

| 任务 | 耗时 | 条件 |
|---|---|---|
| 生图 standard（NoUpScaling） | **~5.1s/张** | 1024×1408；54 张无间隙流水实测 |
| 生图 hd（RealESRGAN+二过） | ~101s/张 | 同尺寸 |
| 视频 i2v/t2v | **~90s/段** | 0.4MP、5s 段、0.7MP 15s 段 400-700s（经验值） |
| 视频与图片共队 | FIFO 交替 | 无隔离，互相排队 |

## 5. 运维约束与纪律（用户明确指示，必须遵守）

1. **资源放行唯一依据 = 用户明确信号**——端点慢响应 ≠ 空闲；严禁 worker 挂探测自动发起资源消耗任务
2. **严禁脱离式后台脚本**（nohup 孤儿进程）执行消耗 LLM/ComfyUI 的工作——暂停令无法触及，本项目历史上 4 次违规教训
3. **异常处理流程**：报告异常（现象+定位+可选方案）→ 用户确认 → 共同讨论设计 → 实施
4. **产物落盘**：严禁 /tmp（重启丢失）；测试项目→`data/test_projects/`、脚本→`tests/`、日志→`tests/logs/`
5. **Mac 防休眠**：长任务用 `caffeinate -is <command>` 包裹
6. **Python 一律 `~/.ai-env/bin/python`**；ffmpeg 用 `imageio_ffmpeg.get_ffmpeg_exe()`

## 6. 架构级教训（为什么系统长这样）

| 教训 | 体现 |
|---|---|
| 检查点是一切 | item 级断点（页/块/分镜/段）+ DAG cycle 重试——端点反复故障零任务损失 |
| 严格成功判据 | "≥1 成功即阶段成功"判据曾使 7 个段永久搁浅（S5 搁浅→S6 死锁） |
| 拆小而非等长 | 大任务靠拆解（树形归并/双池/流水消费），不靠加大超时 |
| 双池优于串行闭环 | 生成与校验各占独立资源（GPU/spark），队列桥接 + 拒绝回流 |
| 槽配对保证双机 | 并发请求配对不同 session → 分落两台 DGX；按单元预分配会碰撞 |
| 阶段屏障变流水 | S7 不等 S6 整阶段（逐段消费）、S4b 不等 S4 整阶段（增量排印终态面板） |

## 7. 项目结构速查

```
MangaCopy/
├── Api/                    # 端点配置 + ComfyUI workflow 模板（只读）
├── MiniMaxH3/skills/       # H3 prompt 写法规范（只读，h3-prompt-writing 核心）
├── Ref/                    # 测试素材：第187话 13 页
├── mangacopy/              # 核心包（15 模块 5671 行 + 20 模板）
├── data/projects/          # 正式项目
├── data/test_projects/     # 测试项目（含全量彩排 20260927_full13）
├── tests/ + tests/logs/    # 测试脚本与日志
├── cli.py / gui.py         # 前端
└── docs/                   # 本文档集
```
