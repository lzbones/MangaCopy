# MangaCopy 用户手册

> 版本：1.0（2026-09-28）
> 关联文档：[环境档案](05_ENVIRONMENT.md) · [程序架构](04_ARCHITECTURE.md) · [需求](02_REQUIREMENTS.md)

## 1. 快速开始

### 1.1 环境准备（一次性）

```bash
# 所有命令统一用 ~/.ai-env 的 Python
alias mcpy="~/.ai-env/bin/python"
# 依赖已装好：gradio 6.28.0、imageio-ffmpeg（无系统 ffmpeg 需求）
```

### 1.2 创建项目并全流程复刻

```bash
cd /Users/qingxu/Documents/Software/AI/MangaCopy

# 创建项目（自动跑 S0 解析源稿）
mcpy cli.py new --ref /path/to/漫画文件夹 --slug my_manga
# 或 zip：--ref /path/to/漫画.zip
# 可选风格：--style 2d（默认）| 3d | live
# 高清生图：--params '{"image_quality":"hd"}'（默认 standard，5s/张）

# 全自动跑完整条流水线（DAG 并行调度）
mcpy cli.py run --project 20260928_my_manga --stage auto
# 或逐步确认模式（每阶段完成后等 Enter）
mcpy cli.py run --project 20260928_my_manga --stage auto --confirm
# 只跑某一阶段
mcpy cli.py run --project 20260928_my_manga --stage s2
# 带参数跑（如只处理部分页）
mcpy cli.py run --project 20260928_my_manga --stage s1 --opts '{"pages":[1,2]}'
```

### 1.3 图形界面

```bash
mcpy gui.py                # 默认 127.0.0.1:7860
mcpy gui.py --port 8080    # 指定端口
```

浏览器打开后：
- **顶栏**：选择/刷新/新建项目
- **十张阶段卡片**：状态徽章（灰/橙/绿/红）+ 运行/重试/查看日志按钮
- **Auto-run 按钮** + Confirm 开关（默认开：每阶段完成后点"继续"）
- **产物标签页**：源页画廊、零号脚本（markdown 渲染）、检查报告、prompt JSON、复刻图（无字/有字双画廊）、分段视频、成片播放器

## 2. 流水线阶段说明

| 阶段 | 名称 | 做什么 | 预计耗时（13 页） |
|---|---|---|---|
| s0 | 源页导入 | 解 zip/文件夹、编号规范化、灰度判定 | ~2s |
| s1 | 版面/分镜理解 | 整页版面→裁切分镜→细粒度理解（对白逐字） | ~50min |
| s2 | 复刻脚本 | L0 设定冻结→L1 总述→L2 分页→L3 分镜→全面检查 | ~4-8h |
| s3 | 生图 prompt | Wai tag 格式，外貌全量携带，确定性检查 | ~30min |
| s4 | 复刻生图+校验 | 双池并行（生成∝校验），拒绝回流重生成 | ~1-2h |
| s4b | 文字排印 | PIL 在复刻图上排版对白/拟声词 | ~30-60min |
| s5 | 视频脚本 | 风格卡/节拍分段/段脚本（逐秒镜头+声音） | ~30min |
| s6 | H3 视频 prompt | skill 三字段结构，身份锁，六项自检 | ~1h |
| s7 | 分段视频生成 | ComfyUI 逐段（流水消费，不等整阶段） | ~20-30min |
| s8 | 拼接成片 | 音画同步 xfade+acrossfade 0.45s | ~5min |

**并行说明**：s2 完成后 `[s3→s4→s4b]` 与 `[s5→s6]` 两分支并行（两台 DGX + PRO 6000 三线齐开）；s7 流水消费 s6 产出，s4b 增量消费 s4 校验终态面板——全程资源满载。

## 3. 产物位置

```
data/projects/<项目id>/
├── s2_zero/00_settings.md           # 人物卡（中文+danbooru tag+服装状态机）
├── s2_zero/01_overview.md            # 总体复述
├── s2_zero/02_pages/page_NN.md       # 分页脉络
├── s2_zero/03_panels/pNNN_NN.md     # 56 个分镜细粒度脚本
├── s2_check/report.md                # 一致性检查+修订报告
├── s4_images/pNNN_NN.png             # ★ 无字复刻图
├── s4_text/pNNN_NN.png               # ★ 有字排印版（气泡+拟声词）
├── s4_validate/pNNN_NN.json         # 每次校验的分数与问题
├── s7_videos/seg_NN.mp4             # 分段视频
└── s8_final/final.mp4                # ★ 拼接成片（含音频）
```

## 4. 常用操作

### 4.1 查看状态

```bash
mcpy cli.py list                              # 所有项目
mcpy cli.py status --project <id>            # 逐阶段+item 状态
```

### 4.2 断点续跑 / 重试

流水线全程幂等：**任何时刻中断（Ctrl-C、关机、端点故障），重新 `run --stage auto` 即从断点继续**，已完成单元零浪费。

### 4.3 单独重跑某个分镜/页/段

```bash
mcpy cli.py run --project <id> --stage s1 --opts '{"pages":[3]}'     # 只重跑第 3 页
mcpy cli.py run --project <id> --stage s4 --opts '{"panels":["p003_02"]}'  # 只重生成分镜
```

### 4.4 dryrun（只看注入值，不实际调用）

```bash
mcpy cli.py run --project <id> --stage s4 --dryrun
```

## 5. 配置调整

| 需求 | 方法 |
|---|---|
| 改负面 prompt | 编辑 `Api/settings_for_Wai.json` 的 `api_neg_prompt`（实时生效） |
| 高清生图 | 建项目时 `--params '{"image_quality":"hd"}'`（~15s/张） |
| 3D/真人视频风格 | 建项目时 `--style 3d` 或 `live`（自动走 T2V 模式） |
| 视频分辨率 | `--params '{"megapixels":1.3}'`（0.4=480P / 0.7=720P / 1.3=1080P） |
| LLM 并发上限 | `mangacopy/config.py` 的 `LLM_MAX_CONCURRENT`（默认 2=两台 DGX） |

## 6. 判断质量与人工介入

| 现象 | 位置 | 处置 |
|---|---|---|
| 分镜图不合格 | `s4_validate/pNNN_NN.json` 的 `final: needs_review` | 系统已保留 3 次尝试中最好的图；可 `--opts '{"panels":["pNNN_NN"]}'` 重跑 |
| 零号脚本遗留问题 | `s2_check/report.md` 的 `UNRESOLVED` | 人工复核后可改 `s2_zero/` 对应文件再重跑检查 |
| 视频段不合格 | `s6_h3/check_report.md` | 系统自动修 ≤2 轮；遗留可重跑 s6 |

## 7. 注意事项

1. **spark/ComfyUI 是共享资源**：跑之前确认你自己的其他任务（如 qoder）不冲突；端点故障时系统自动重试，无需干预，长时间故障（>1h）需服务端重启 vLLM
2. **Mac 休眠会中断流水线**：`cli.py run` 建议用 `caffeinate -is` 包裹（测试脚本已内置）
3. **项目目录名**：`<日期>_<slug>`，重名自动加 `-2`/`-3` 后缀
4. **对白保留中文**（汉化本原文）；视频生成时人物说中文

## 8. 故障排查

| 症状 | 原因 | 解决 |
|---|---|---|
| s2 卡在 L0 很久 | 端点慢峰（正常现象） | 等待或稍后重跑（检查点保留） |
| s4 反复拒绝某面板 | 生图与脚本严重不符 | `needs_review` 后人工定夺，或检查 `s3_prompts/` 对应 JSON |
| 视频段首帧等待 30 分钟后降级 | s4 该面板未完成 | 正常降级 T2VA；后续重跑 s7 会用 I2V 补 |
| `no s3 prompt files` | s3 尚未跑或失败 | 先 `run --stage s3` |
