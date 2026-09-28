<!-- S2 L0 设定层-最终合并：把各分块抽取结果合并为全话统一的冻结设定。
     输出 JSON：与单块结构相同（characters/environments/style_notes），clothing_states 为全话状态机。
     字段：<<CHUNKS_JSON>>（各块抽取结果 JSON 数组）、<<ALL_PAGES>>（全部页码范围，如 1-13） -->

以下是对漫画第 <<ALL_PAGES>> 页分块抽取的角色/服装/环境要素（JSON 数组，每块覆盖一段页码）：

<<CHUNKS_JSON>>

请将它们合并为一份全话统一的设定 JSON（字段名用英文，结构与每块相同）：

{
  "characters": [
    {
      "name": "...", "aliases": ["..."], "gender": "female|male",
      "anchor": {
        "character_name": "已知知名动漫角色名",
        "anime_origin": "出品动漫作品全称与简称",
        "anchor_danbooru": "danbooru 角色标签"
      },
      "appearance_cn": "【核心外貌完整复述】：生理性别（女/男）、脸型骨架、眼眸颜色、发色与具体发型细节、身材骨架体型完整复述，严禁偷懒省略！",
      "danbooru_tags": "以 '1girl' 或 '1boy' 开头的外貌标签串...",
      "clothing_states": [{"pages": "...", "state_cn": "...", "danbooru_tags": "..."}]
    }
  ],
  "environments": [{"name": "...", "desc_cn": "...", "danbooru_tags": "..."}],
  "style_notes": "..."
}

要求：
0. **输出最外层必须是 JSON 对象 `{}`，严禁用数组 `[]` 或任何嵌套包裹**；只输出 JSON，不要任何其他文字。
1. **角色归并与锚定守恒**：跨块出现的同一角色必须合并为一条；判断“同一角色”以外貌和剧情为准。合并后必须**保留最精准的已知知名动漫角色锚定（含动漫作品出处 anime_origin）**。
2. **性别与外貌绝对锁死**：gender 必须明确为 female 或 male，禁止 unknown！appearance_cn 必须完整复述生理性别、面容五官、发型发色与体型，不得偷懒缩写；danbooru_tags 首词必须为 1girl 或 1boy。
3. **大幅收拢无名路人**：只有主要角色与重要配角保留独立条目；仅出现一次且无剧情作用的背景/路人角色合并为一条"背景路人"或删除。合并后角色总数应明显少于输入各块角色数之和。
4. **clothing_states 全话状态机**：把各块的衣着状态按页码拼接成全话状态机，区间按页码升序排列、彼此不重叠不冲突、无缝衔接，并集必须覆盖第 <<ALL_PAGES>> 页的全部页码。
5. environments 同名场景合并，描述取并集；style_notes 汇总全话画风与氛围。
