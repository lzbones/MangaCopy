# MangaCopy Prompt 模板设计说明

> 版本：1.0（2026-09-28）
> 模板位置：`mangacopy/prompts/`（20 个 .md 文件）
> 关联文档：[方案设计](03_DESIGN_PLAN.md) · [程序架构](04_ARCHITECTURE.md)

## 1. 模板机制

- **占位符语法**：`<<FIELD>>`（双尖括号大写字段名），由 `templates.render(name, **fields)` 替换；缺失字段保留原样并 warning 到 stderr。**不用** Jinja/`{}`——模板正文含大量 JSON 花括号，避免转义地狱。
- **输出约定**：所有模板要求模型输出 JSON（字段名英文），经 `llm.extract_json` 鲁棒解析（剥 ```json 围栏、找首个配平对象）。
- **修改模板不影响已产出**：模板在调用时实时渲染，改完即生效，无需重跑已完成 item。

## 2. 模板分层设计逻辑（零号脚本的自顶向下约束）

```
s2_l0_chunk（分块抽取）
    ↓ 输入 S1 理解 JSON，抽取人物/服装/环境
s2_l0_merge（树形两两归并）
    ↓ 中间层只校验结构，终态层做全页覆盖校验+确定性修复
s2_l1_overview（L1 总述）
    ↓ 输入 L0 digest + 各页 summary
s2_l2_pages（L2 分页，串行链）
    ↓ 页 N 上下文含第 N-1 页摘要——保跨页连续性
s2_l3_panels（L3 分镜，固定十标题）
    ↓ 上下文 = 相关角色卡 + 所在页 L2 全文 + S1 detail
s2_check_page / s2_check_global（全面检查）
    ↓ 逐页+全局，问题清单驱动自动修订
```

## 3. 关键模板的硬约束（修改前必读）

### 3.1 s2_l3_panels.md —— 十个固定标题

```
## 分镜编号与位置 / ## 出场人物 / ## 动作与姿态 / ## 空间关系与构图
/ ## 镜头角度 / ## 背景环境 / ## 对白原文 / ## 拟声词 / ## 氛围与情绪 / ## 备注
```

- **顺序不变、不得增删**——`zero_script._content_validator(_L3_HEADERS)` 确定性校验
- 出场人物必须引用人物卡 `clothing_states` 的当页状态（衣着一致性约束的落点）
- 对白原文必须与 S1 的 `detail.dialogue` 逐字一致（检查器核对）

### 3.2 s2_l0_merge.md —— 去重与外层约束

- **输出最外层必须是 JSON 对象 `{}`**（严禁数组包裹——实测模型会犯）
- **按外貌合并同名变体**（"银发持剑者"与"银发剑士"是同一人物必须合并）
- **合并后角色数应明显少于输入之和**（实测 48→24；不去重会让下游 prompt 膨胀）
- clothing_states 区间按页码升序、不重叠、无缝、并集覆盖全部页码

### 3.3 s6_h3.md —— H3 三字段结构（逐字符硬规则）

```
[I2VA 模式] 首行固定句 + 空行
integrated_multimodal_description: [Shot 1] ...（英文正文）
overall_soundscape: ...
non_diegetic_music: ... / N/A
```

- 首行句以 `MiniMaxH3/skills/h3-prompt-writing/references/base-en.txt` 官方原文**逐字符**为准
- `[Shot 1]` 无时间戳；后续 `[Shot N] At MM:SS.mmm,` 严格递增
- 对白 `<d>[Chinese] 中文原文</d>` 逐字不改写
- 身份锁短语 `identity lock, no identity swap, no changing face or hairstyle` 每镜重复
- 六项自检（`s6_check.md`）：单段≤15s / 单镜角色≤3 / 首行句正确 / 时间戳递增 / 对白逐字 / 身份锁在场

### 3.4 s4_validate.md —— 校验输出

```json
{"pass": true, "issues": ["具体问题描述"], "score": 7}
```

- pass 且 score≥7 才 completed；score 0-10
- issues 要具体到"缺什么/错什么"（示例："白色尖耳兽数量不符：脚本要求 2 只，图中仅 1 只"）

### 3.5 s4b_overlay.md —— 排版坐标

```json
{"placements": [{"idx": 0, "type": "bubble|narration|sfx", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1, "tail": [0.5, 0.5]}]}
```

- 坐标 0-1 归一化；不遮脸不遮关键动作；块间不重叠
- idx 与输入 LINES_JSON 的 idx 一一对应

## 4. 模板修改的连锁影响

| 改动 | 影响范围 | 注意 |
|---|---|---|
| L3 十标题 | `_content_validator(_L3_HEADERS)` 硬编码同步改 | 漏改 → 校验全挂 |
| L0 输出 schema | `_validate_settings` / `_coerce_settings` / `_settings_repair_fn` | 结构变更需同步 |
| H3 字段名 | 六项自检的正则校验 | 字段名是逐字符比对 |
| Wai tag 生成（s3_wai.md） | 只影响新生成的 prompt，已生成的不变 | 场景 tag 风格可自由调 |
| 对白语言 | s5/s6 模板中 `<d>[Chinese]` 标签 + 校验器 | 改语言需全链同步 |

## 5. 实测调优经验

1. **错误清单重试要截断**：`_json_call` 重试时错误列表截断到 10 条——65 条错误清单曾把模型打崩
2. **"直接输出"优于解释**：模板要求"只输出 JSON，不要任何其他文字"显著降低空流率
3. **去重要显式指令**：L0 merge 不写"按外貌合并同名变体"时模型保留全部 48 条
4. **思考预算**：spark 是推理模型，max_tokens 必须 ≥256 才能有 content 产出（reasoning 吃预算）
5. **视觉模板的图片说明要具体**：S1 版面模板中"bbox 是粗略值、务必覆盖含对白的区域"显著改善裁切质量
