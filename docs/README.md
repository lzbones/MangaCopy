# MangaCopy 文档索引

> 更新：2026-09-28
> 项目：漫画 → 零号脚本 → 复刻图（无字/有字）→ 复刻视频 全自动流水线

## 文档关系图

```
                      ┌─────────────────┐
                      │ REQUIREMENTS.md │  需求（为什么做）
                      └────────┬────────┘
                               │ 回答
                               ▼
                      ┌─────────────────┐
        ┌─────────────│  DESIGN_PLAN.md  │  方案（怎么做）
        │             └────────┬────────┘
        │ 决策落地为               │ 实现为
        ▼                        ▼
┌───────────────┐       ┌─────────────────┐
│   PROMPTS.md  │       │ ARCHITECTURE.md  │  架构（代码怎么组织）
│ 模板（大脑）    │◄──────┤                 │
└───────┬───────┘  调用  └────────┬────────┘
        │                     验证于
        ▼                        ▼
┌───────────────┐       ┌─────────────────┐
│QUALITY_GUIDE  │       │   TESTING.md     │  测试（做得如何）
│（质量怎么看）   │       └────────┬────────┘
└───────────────┘                │ 运行于
                                 ▼
                      ┌─────────────────┐
                      │ ENVIRONMENT.md   │  环境（跑在什么上面）
                      └─────────────────┘

贯穿全局：
  COORDINATION.md（人机协作规则）── 任何角色必读
  USER_MANUAL.md（怎么用）────────── 使用者入口
  CHANGELOG.md（怎么演变的）──────── 无 git，唯一历史
  ROADMAP.md（接下来做什么）
  DESIGN.md / STATUS.md（历史积累 / 状态快照）
```

## 阅读路线（按角色）

| 你是 | 推荐顺序 | 目的 |
|---|---|---|
| **使用者**（跑复刻） | COORDINATION → USER_MANUAL → QUALITY_GUIDE | 会用 + 会看质量 + 懂规矩 |
| **开发接手者** | COORDINATION → ENVIRONMENT → ARCHITECTURE → DESIGN_PLAN → PROMPTS → TESTING → CHANGELOG | 懂规矩 → 懂硬件 → 懂代码 → 懂设计 → 懂模板 → 懂测试 → 懂历史 |
| **快速了解项目** | REQUIREMENTS → DESIGN_PLAN → STATUS → ROADMAP | 5 分钟知道这是什么、做到哪了 |

## 文档清单

| 文档 | 内容 | 读者 |
|---|---|---|
| [REQUIREMENTS.md](REQUIREMENTS.md) | 任务需求全集：原始 7 步 + 全部追加修订 + 质量红线 | 所有人 |
| [DESIGN_PLAN.md](DESIGN_PLAN.md) | 方案设计：核心决策、各阶段算法、容错体系、性能要点 | 开发接手者 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 程序架构：三层结构、DAG、并发模型、模块清单、目录约定 | 开发接手者 |
| [PROMPTS.md](PROMPTS.md) | 模板设计说明：20 个模板的结构、硬约束、连锁影响、调优经验 | 开发接手者 |
| [TESTING.md](TESTING.md) | 测试情况：全部验证结果、12 个闭环修复、性能统计 | 开发接手者 |
| [ENVIRONMENT.md](ENVIRONMENT.md) | 环境档案：硬件清单、AI 服务端点、spark/PRO6000 实测特性、故障模式目录 | **开发接手者必读** |
| [HARDWARE_API_GUIDE.md](HARDWARE_API_GUIDE.md) | **算力与 API 接口指南**：双 Spark 独立会话租赁、PRO 6000 ComfyUI REST 调用与可直接复用 Python 示例 | **开发者必读** |
| [BENCHMARK_OPT_FULL13.md](BENCHMARK_OPT_FULL13.md) | **基准测试报告**：方案 B 480P 全链路从头重新生成实测耗时与提速统计 | 所有人 |
| [COORDINATION.md](COORDINATION.md) | 协作规则：资源纪律、异常流程、环境约定 | **所有人必读** |
| [USER_MANUAL.md](USER_MANUAL.md) | 用户手册：快速开始、CLI/GUI 用法、产物位置、故障排查 | 使用者 |
| [QUALITY_GUIDE.md](QUALITY_GUIDE.md) | 质量评估：校验分数体系、已知偏移模式、审查要点、needs_review 处置 | 使用者 |
| [CHANGELOG.md](CHANGELOG.md) | 变更日志：全部演进记录（无 git，唯一历史） | 开发接手者 |
| [ROADMAP.md](ROADMAP.md) | 下一步计划：进行中、收官验证、短期优化、中长期规划 | 所有人 |
| [DESIGN.md](DESIGN.md) | 工程约束与技术细节（历史积累，随开发过程更新） | 开发接手者 |
| [STATUS.md](STATUS.md) | 项目状态总览（阶段性快照） | 所有人 |
