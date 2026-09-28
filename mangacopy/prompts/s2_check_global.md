<!-- S2 检查-全局：跨页连续性与设定层一致性检查。
     输出 JSON：{"issues": [{"file", "problem", "fix"}]}，无问题输出 {"issues": []}。
     字段：<<SETTINGS_JSON>>（L0 设定 JSON）、<<OVERVIEW>>（L1 全文）、<<PAGES_DIGEST>>（各页 L2 摘要拼接） -->

【L0 冻结设定（JSON）】
<<SETTINGS_JSON>>

【L1 总体复述】
<<OVERVIEW>>

【各页脉络摘要（L2）】
<<PAGES_DIGEST>>

你是质检编辑。做全局一致性检查，逐项核对：

1. 页间连续性：时间线、空间转移、人物在场状态在相邻页之间是否连续合理（依据各页 L2 摘要与 L1 复述）。
2. clothing_states 区间无缝：每名角色的衣着状态区间并集覆盖全部涉及页码、相邻区间不重叠不冲突、区间切换与剧情吻合。
3. L1 复述与各页 L2 摘要之间无矛盾。
4. 人物卡完整性与自洽：L1/L2 中出现的角色都在 L0 中有卡；外貌描述自洽无冲突。

输出 JSON（字段名用英文）：

{"issues": [{"file": "问题文件相对路径（如 00_settings.json、01_overview.md、02_pages/page_02.md）", "problem": "问题描述（具体到页码/人物/区间）", "fix": "建议修法"}]}

无问题输出 {"issues": []}。只输出 JSON，不要输出任何其他文字。
