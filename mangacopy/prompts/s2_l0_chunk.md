<!-- S2 L0 设定层-分块抽取：从一段页码范围的 S1 理解结果中抽取角色/服装/环境要素。
     输出 JSON：characters[]（含 clothing_states 页区间）、environments[]、style_notes。
     字段：<<PAGES_JSON>>（本块页面的 S1 精简 JSON）、<<PAGE_RANGE>>（本块页码范围，如 1-4） -->

以下是漫画第 <<PAGE_RANGE>> 页的多模态理解结果（JSON）：

<<PAGES_JSON>>

请从中抽取这几页的全局设定要素，输出 JSON（字段名用英文）：

{
  "characters": [
    {
      "name": "角色名（作品中可辨认的正式名，无则以一致的代号命名）",
      "aliases": ["别名/代号/称谓"],
      "gender": "male|female|unknown",
      "appearance_cn": "中文外貌描述（发型发色、瞳色、体型、五官特征；不含服装）",
      "danbooru_tags": "英文 danbooru 外貌标签串（逗号分隔，如 'long hair, blue eyes, large breasts'；只写固有外貌，不含服装、不含角色名）",
      "anchor": "外貌最相似的知名动漫角色 danbooru 标签（如 'hatsune miku'；无明显相似则空字符串）",
      "clothing_states": [
        {
          "pages": "该衣着状态覆盖的页码区间，如 '1-3'，单页写 '4'",
          "state_cn": "中文衣着描述",
          "danbooru_tags": "英文衣着标签串（如 'school uniform, pleated skirt'）"
        }
      ]
    }
  ],
  "environments": [
    {
      "name": "场景名",
      "desc_cn": "中文场景描述",
      "danbooru_tags": "英文场景标签串（如 'classroom, desk, blackboard'）"
    }
  ],
  "style_notes": "本块画风与氛围要点（黑白少年漫、速度线、网点、分镜密度等）"
}

要求：
1. 同一角色跨页出现必须合并为一条（aliases 收拢各页的别名与代号）。
2. clothing_states 按页码追踪衣着变化（换装/破损/穿脱），区间必须无缝覆盖本块全部页码（<<PAGE_RANGE>>）。
3. danbooru_tags 用英文小写 danbooru 惯用标签；外貌标签与服装标签严格分开，不要混写。
4. 只输出 JSON，不要输出任何其他文字。
