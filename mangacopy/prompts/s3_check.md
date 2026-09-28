<!-- S3 出场人物识别回退：L3 "## 出场人物" 节确定性解析失败时，用 LLM 判断画面实际出场人物。
     输出 JSON：{"characters": ["<人物卡 name 原样>", ...]}。
     字段：<<PANEL_KEY>>、<<CHARACTER_CARDS>>（人物卡摘要）、<<PANEL_SCRIPT>>（L3 全文） -->

【人物卡（L0 冻结设定）】
<<CHARACTER_CARDS>>

【分镜 <<PANEL_KEY>> 的复刻脚本（L3，中文）】
<<PANEL_SCRIPT>>

你是漫画分镜分析师。请判断本分镜画面中实际出场的人物：只能从上方人物卡中选择；仅在对话或旁白中被提及但画面未露面者不算出场。人名必须与人物卡的 name 完全一致。

只输出 JSON（不要任何其他文字）：
{"characters": ["<name>", ...]}

画面中无人物卡所列人物时输出 {"characters": []}。
