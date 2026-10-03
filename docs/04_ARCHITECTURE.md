# MangaCopy 程序架构

> 版本：1.0（2026-09-28）
> 关联文档：[方案设计](03_DESIGN_PLAN.md) · [用户手册](08_USER_MANUAL.md)

## 1. 三层架构总览

```
前端层：  CLI（cli.py：new/list/status/run）
         GUI（gui.py：Gradio 6.28，十阶段卡片+产物画廊+后台执行）
         对话式（用户直接吩咐，lead 直接驱动）
─────────────────────────────────────────────
核心层：  mangacopy/ 包
         调度：scheduler.py（DAG）+ stages.py（阶段注册表/状态机）
         阶段：ingest → understand → zero_script → image_prompt
               → image_gen → text_overlay → video_script
               → h3_prompt → video_gen → assemble
─────────────────────────────────────────────
服务层：  llm.py → spark（litellm→两台 DGX Spark GB10，流式 SSE）
         comfy.py → ComfyUI（192.168.50.254:8188，Wai 生图 / MiniMaxH3 生视频）
```

## 2. DAG 依赖图（scheduler.py）

```
s0 → s1 → s2 ─┬─ s3 ─→ s4 ─→ (s4b)
              │    ↑(dep s3)
              └─ s5 ─→ s6
                        ↓ (s6 段产物流式就绪)
              s7 (dep s5，流水消费 s6 + s4 首帧)
              s8 ← s7
```

- s4b 依赖 s3（不等 s4 整阶段）——增量排印只消费 s4 item 终态面板
- s7 依赖 s5（不等 s6 整阶段）——轮询消费 s6 段 prompt，首帧等 s4
- 依赖失败 → 下游跳过（不写 project.json，可重跑补救）

## 3. 并发模型

| 资源 | 上限 | 机制 |
|---|---|---|
| spark LLM 连接 | **2**（每台 DGX 一个） | `llm.py` 信号量 + 会话槽配对分配器：两并发必持不同 session → 分落两台 |
| 会话池 | 进程级全局一对 UUID | `new_session_pool()` 全阶段共享；单元锁定分配（页/块/分镜/段粘同一台） |
| ComfyUI 生图在途 | 3 | 双池生成线程数 |
| S4 校验线程 | 2 | 双池校验线程数 = spark 槽位数 |
| 阶段级并行 | DAG 就绪即启动 | s4∥s4b∥s6∥s7 四路同时 |
| 线程安全 | project.py 全局 RLock | 并行阶段同时写 project.json |

## 4. 模块清单（mangacopy/，共 5671 行）

| 模块 | 职责 | 关键点 |
|---|---|---|
| config.py | 端点/超时/并发/尺寸桶/字体配置 | `LLM_MAX_CONCURRENT=2`；`LLM_TIMEOUT=1200s`；`COMFY_TIMEOUT=1800s` |
| llm.py | 流式 LLM 客户端 | 槽配对分配器；json/空流/断流三类免费或退避重试；`[llm] ttft/content/reasoning/decode` 遥测 |
| comfy.py | ComfyUI REST 客户端 | submit/wait/fetch/upload_image |
| project.py | 项目状态机 | project.json 原子写（RLock）；item 级断点 |
| scheduler.py | DAG 调度器 | 就绪即并行启动；失败级联跳过 |
| stages.py | 阶段注册表 | 延迟 import；严格成功判据统一 |
| ingest.py | S0 | 编号规范化/灰度判定 |
| understand.py | S1 | 两轮法，视觉，并发 2 |
| zero_script.py | S2 | L0 树形归并（全序 cid 命名，无碰撞）+ L2 串行链 + L3 并行 + 检查修订 |
| image_prompt.py | S3 | 混合式构建 + 确定性全面检查 |
| image_gen.py | S4 | **双池**（3 生成∥2 校验，队列桥接，拒绝回流） |
| text_overlay.py | S4b | PIL 排印；**增量**（只排 s4 终态面板，轮询至收敛） |
| video_script.py | S5 | 风格卡/节拍/段脚本 |
| h3_prompt.py | S6 | skill 三字段结构；六项自检双层 |
| video_gen.py | S7 | **流水消费**（等 S6 段就绪）+ 首帧等待 |
| assemble.py | S8 | 音画同步拼接 |
| templates.py | 模板渲染 | `<<FIELD>>` 占位符 |

## 5. 产物目录约定

```
data/projects/<id>/          正式项目
data/test_projects/          测试项目（严禁 /tmp）
tests/                       测试驱动脚本
tests/logs/                  测试日志
```

```
data/projects/<id>/
├── project.json      # 状态机（原子写，RLock）
├── source/           # S0 原稿 + manifest
├── s1_understand/    # 每页理解 JSON + crops/ 分镜裁块
├── s2_zero/          # 00_settings(.json/.md) 01_overview 02_pages 03_panels
├── s2_check/         # 检查修订报告
├── s3_prompts/       # Wai prompt（正/负/种子/尺寸桶）
├── s3_check/         # prompt 检查报告
├── s4_images/        # ★ 无字复刻图
├── s4_validate/      # 校验记录（每 attempt）
├── s4_text/          # ★ 有字排印版
├── s5_video/         # 风格卡/节拍/段脚本
├── s6_h3/            # H3 prompt + check_report
├── s7_videos/        # ★ 分段视频
├── s8_final/         # ★ 拼接成片
└── logs/             # 每阶段日志
```

## 6. 模板清单（mangacopy/prompts/，20 个）

S1×2、S2×7、S3×2、S4×1、S4b×1、S5×3、S6×2 + _example
