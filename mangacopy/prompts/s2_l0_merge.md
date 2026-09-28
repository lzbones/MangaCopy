<!-- S2 L0 设定层-最终合并：把各分块抽取结果合并为全话统一的冻结设定。
     输出 JSON：与单块结构相同（characters/environments/style_notes），clothing_states 为全话状态机。
     字段：<<CHUNKS_JSON>>（各块抽取结果 JSON 数组）、<<ALL_PAGES>>（全部页码范围，如 1-13） -->

以下是对漫画第 <<ALL_PAGES>> 页分块抽取的角色/服装/环境要素（JSON 数组，每块覆盖一段页码）：

<<CHUNKS_JSON>>

请将它们合并为一份全话统一的设定 JSON（字段名用英文，结构与每块相同）：

{
  "characters": [
    {
      "name": "...", "aliases": ["..."], "gender": "male|female|unknown",
      "appearance_cn": "...", "danbooru_tags": "...", "anchor": "",
      "clothing_states": [{"pages": "...", "state_cn": "...", "danbooru_tags": "..."}]
    }
  ],
  "environments": [{"name": "...", "desc_cn": "...", "danbooru_tags": "..."}],
  "style_notes": "..."
}

要求：
0. **输出最外层必须是 JSON 对象 `{}`，严禁用数组 `[]` 或任何嵌套包裹**；只输出 JSON，不要任何其他文字。
1. 跨块出现的同一角色必须合并为一条：aliases 取并集，外貌描述取最完整者，danbooru_tags 去重合并。**判断"同一角色"以外貌描述为准——名字写法不同（如"银发持剑者"与"银发剑士"）但外貌明显一致的是同一人物，必须合并**；同一人物的不同视角/姿态/损伤状态也必须合并为一条。
2. **合并后角色总数应明显少于输入各块角色数之和**：只有主要角色与重要配角保留独立条目；仅出现一次且无剧情作用的背景/路人角色合并为一条"背景路人"或删除。同名同貌的多条目出现说明你没有做足合并。
3. clothing_states 是重点：把各块的衣着状态按页码拼接成全话状态机，区间按页码升序排列、彼此不重叠不冲突、无缝衔接，并集必须覆盖第 <<ALL_PAGES>> 页的全部页码。同一状态在相邻块重复出现时合并区间。
4. environments 同名场景合并，描述取并集。
5. style_notes 汇总全话画风与氛围。
