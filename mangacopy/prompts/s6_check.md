<!-- S6 自检：逐段 LLM 六项自检（与代码确定性检查互补）。
     输出 JSON：{"pass": true|false, "problems": ["问题描述", ...]}。
     字段：<<SEG_KEY>>、<<MODE>>、<<DURATION>>、<<PROMPT>>（被检 H3 prompt 全文）、<<DIALOGUE_LINES>>（中文对白清单）、<<CHARACTER_TAGS>>（相关角色英文 tag） -->

你是 MiniMax-H3 prompt 质检员。逐项检查下述 <<SEG_KEY>> 的 H3 prompt（模式 <<MODE>>，目标时长 <<DURATION>> 秒）：

六项检查（每项不通过都必须在 problems 中具体说明）：
1. 单段时长 ≤ 15 秒：prompt 描述的内容量与镜头切换在 <<DURATION>> 秒内可完成，无超时内容。
2. 单镜角色 ≤ 3：每个 [Shot N] 中有动作或对白的主要角色不超过 3 个；若超过，须指出具体镜头。
3. I2VA 首行句正确：模式为 i2va 时，第一行是否为 "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced."；模式为 t2va 时不得出现该句。
4. 时间戳递增在时长内：[Shot N] At MM:SS.mmm 严格递增，且全部大于 0、小于 <<DURATION>>；[Shot 1] 无时间戳。
5. 对白中文逐字：下方对白清单中的每一行是否都逐字出现在 <d>[Chinese] ...</d> 中（一字不改、不翻译、不增删标点）；<d> 标签是否全部闭合。
6. 身份锁与衣着每段出现：每个镜头是否重复角色名+英文外貌+英文衣着+"identity lock, no identity swap, no changing face or hairstyle"，衣着与清单一致，角色数量已锁定。

【被检 H3 prompt】
<<PROMPT>>

【本段中文对白清单】
<<DIALOGUE_LINES>>

【本段相关角色英文 tag（衣着基准）】
<<CHARACTER_TAGS>>

输出 JSON：
{"pass": true, "problems": []}
或
{"pass": false, "problems": ["第 X 项：具体问题与位置"]}
只输出 JSON。
