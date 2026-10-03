# MangaCopy 测试报告

> 更新：2026-09-28 21:00
> 关联文档：[需求](02_REQUIREMENTS.md) · [方案设计](03_DESIGN_PLAN.md)

## 1. 测试总览

| 测试 | 规模 | 结果 | 日志 |
|---|---|---|---|
| S0 解析 | 13 页 | ✅ 全灰度判定正确 | tests/logs/ |
| S1 理解 | 2 页（8 分镜）+ 全 13 页 | ✅ 对白中文逐字、右起编号 | tests/logs/ |
| S2 验收 | 2 页 50 断言 | ✅ **50/50 全过** | tests/logs/s1s2_acceptance3.log |
| S3 prompt | 桩 69 项 + 真实抽查 | ✅ | tests/logs/test_s3s4_*.log |
| S4 生图 | 双档真实验证 | ✅ standard 5.1s/张，hd 101s/张 | tests/logs/ |
| S4b 排印 | 2 面板 | ✅ LLM 排版 + PIL 绘制 | data/projects/20260926_s4real/ |
| S5/S6 | 2 段+六项自检 | ✅ 双 PASS | data/test_projects/mangacopy_s5s8_test/ |
| S7/S8 | I2V+T2V 各一段 | ✅ 9.92s 成片音画同步 | data/projects/20260926_s7s8real/ |
| **全量彩排** | 13 页 56 分镜 14 段 | 🟢 **进行中**（详见下） | tests/logs/full13_run15.log |

## 2. 全量彩排当前进度（run15，21:00 快照）

| 阶段 | 状态 | 产出 |
|---|---|---|
| S0-S3 | ✅ 全部完成 | 00_settings.json（24 角色 9 环境）、56 分镜脚本 |
| S4 | 🟢 in_progress | **54/54 图全部生成**（151 次含重试），校验追赶中（18 面板已过） |
| S4b | 🟢 in_progress | 增量排印已启动（只排校验终态面板） |
| S5 | ✅ 完成 | 7 段脚本 |
| S6 | 🟢 in_progress | 12/14 段 prompt 完成 |
| S7 | 🟢 流水消费中 | 等待 S6 尾段 prompt 就绪即生成视频 |
| S8 | 等待 S7 | — |

## 3. 真实验证发现与修复的问题（闭环迭代实录）

| # | 问题 | 根因 | 修复 |
|---|---|---|---|
| 1 | L0 大合并 3 次全灭 | GB10 上大输入非流式总时长超限 | 流式客户端 + 树形归并 |
| 2 | S2 收尾崩溃 `ValueError` | `unresolved` dict 按二元组解包 | 改为 `it["file"]` 取键 |
| 3 | S3 空流失败 | json_mode 下 vLLM 空回包 | llm.py 去参免费重试 |
| 4 | 4 并发连接 DGX 58 分钟零产出 | 硬件并行弱 | `LLM_MAX_CONCURRENT=2` 信号量 |
| 5 | preflight 永远拒绝 | `max_tokens=8` 被 reasoning 耗尽 | 改 256 + 3 次探测 |
| 6 | S5 7 段搁浅 | "≥1 段成功即整阶段成功"判据 | 严格判据（S1/S3/S5/S7 统一） |
| 7 | 命名碰撞 `{0,1,6}` vs `{0..6}` | min/max 命名 | 全序 cid 列表命名 |
| 8 | `as_completed` 树结构不确定 | 线程完成序漂移 | 提交序收集 |
| 9 | 服务端 ~15min 断流 | GB10/litellm 长流切断 | ChunkedEncodingError 纳入重试 |
| 10 | 生成与校验串行 | 每面板闭环流程 | **双池解耦**（3 生成∥2 校验队列桥接） |
| 11 | 会话不落两台 | 面板奇偶预分配碰撞 | **槽配对分配器**（并发必持不同 session） |
| 12 | S7 等 S6 整阶段、S4b 等 S4 整阶段 | 阶段屏障 | **流水消费 + 增量排印**（统筹优化） |

## 4. 单元测试汇总

| 模块 | 测试 | 结果 |
|---|---|---|
| llm.py | json 空流→去参重试；非 json 空流→退避重试；ChunkedEncoding 重试 | 3/3 PASS |
| llm.py 槽配对 | 同 session 并发→分持不同 session；顺序→粘性；全局池共享 | 3/3 PASS |
| llm.py 遥测 | ttft/reasoning/decode 输出 | PASS |
| scheduler.py | 分支并行（时间重叠）；失败级联；断点续跑 | 3/3 PASS |
| zero_script 树归并 | 7 块→6 次两两归并→唯一终态；检查点续跑 | 2/2 PASS |
| image_gen 双池 | 生成与校验时间重叠；拒绝回流重生成 | PASS |
| image_gen 空流 | 8 线程峰值并发=2 | PASS |
| preflight | max_tokens 8（复现）/256（修复） | 确认 |

## 5. 性能统计（实测，n=389+ 成功调用）

| 调用类别 | 中位s | P90s | 最大s |
|---|---|---|---|
| S1 视觉 | 153-166 | 203-498 | 903 |
| S2 L3 分镜 | 106 | 262 | 2115 |
| S2 L0 树归并 | 397 | 1557 | 1615 |
| S2 检查 | 380 | 899 | 1650 |
| S5 段脚本 | 370 | 555 | 1219 |
| S6 段生成/修复 | 417 | 909 | 2075 |
| 生图（pro6000） | 5.1s（standard） | — | 101s（hd） |
| 视频（pro6000） | ~90s/段 | — | — |

**端点特性**：TTFT≈0s（prefill 无压力）；decode ~10 tok/s 是瓶颈且 reasoning 占大头；慢峰周期性（每 6-8h 一轮，1-3h）；最佳窗口凌晨 04-07 时。

## 6. 遗留 UNRESOLVED

- S2 检查报告 8 条 UNRESOLVED（多为历史 S1 项，供人工复核）
- S6 seg_03_check 空错误（待下轮重试自然修复）
