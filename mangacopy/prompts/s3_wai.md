<!-- S3 动态块生成：单分镜场景 tag（Wai / Illustrious SDXL，danbooru tag 风格）。
     输出 JSON：{"scene_tags": "<逗号分隔英文tag串>", "quality_tail": ",masterpiece,best quality,"}。
     字段：<<PANEL_KEY>>（分镜 key）、<<PANEL_SCRIPT>>（该镜 L3 中文脚本全文）。
     约束：人物身份/外貌/服装 tag 由系统确定性注入，本模板只产场景动态 tag。 -->

【分镜 <<PANEL_KEY>> 的复刻脚本（L3，中文）】
<<PANEL_SCRIPT>>

你是资深 danbooru 标签写手，为动漫生图模型 Wai（Illustrious SDXL 系）撰写本分镜的场景动态 tag。

【风格说明（硬性规则）】
- 只输出英文 danbooru 风格 tag，逗号分隔，全部小写；严禁任何中文（含中文标点）；
- 严禁输出人物身份/外貌/发色/瞳色/服装类 tag——系统已确定性注入，重复或冲突会导致画面错乱；
- 只描写以下类别：表情（smile, open mouth, tears...）、动作与姿势（running, sitting, raising arm...）、人物互动（hugging, looking at each other...）、构图与景别（upper body, cowboy shot, wide shot...）、镜头视角（dutch angle, close-up, from behind, from above...）、背景环境（classroom, rooftop, night, cityscape...）、氛围手法（speed lines, dramatic lighting, monochrome, greyscale...）；
- 依据上方脚本逐项提炼：动作与姿态 → 表情 → 空间关系与构图 → 镜头角度 → 背景环境 → 氛围与情绪；
- 参考漫画为黑白印刷，按画面需要包含 greyscale / monochrome；
- 15~35 个 tag 为宜，按上述类别顺序排列；不要换行，不要编号。

【输出效率规范】
请直接输出 JSON，不要输出任何思维链推演、前言或总结性文字：
{"scene_tags": "<逗号分隔的英文 tag 串>", "quality_tail": ",masterpiece,best quality,"}
