<!-- S2 L0 设定层-分块抽取：从一段页码范围的 S1 理解结果中抽取角色/服装/环境要素。
     输出 JSON：characters[]（含 clothing_states 页区间）、environments[]、style_notes。
     字段：<<PAGES_JSON>>（本块页面的 S1 精简 JSON）、<<PAGE_RANGE>>（本块页码范围，如 1-4）、<<EXISTING_ROSTER_MD>>（前序页码已确立的核心角色设定） -->

【前序已登记核心角色名单（跨页对齐基准）】
<<EXISTING_ROSTER_MD>>

以下是漫画第 <<PAGE_RANGE>> 页的多模态理解结果（JSON）：

<<PAGES_JSON>>

请从中抽取这几页的全局设定要素，输出 JSON（字段名用英文）：

{
  "characters": [
    {
      "name": "角色名（统一的角色代号或正式名，如'银发主角'、'黑裙组长'）",
      "aliases": ["本块中出现的其他称谓或别名"],
      "gender": "female|male",
      "anchor": {
        "character_name": "外貌/气质最相似的已知知名动漫角色名（如'阿尔托莉雅·潘德拉贡 / Saber'、'绫波丽'、'2B'、'奇犽·揍敌客'）",
        "anime_origin": "该知名动漫角色的出品作品全称与常用简称（如'Fate/stay night (FATE系列)'、'新世纪福音战士 (EVA)'、'尼尔：机械纪元 (NieR:Automata)'、'全职猎人 (Hunter x Hunter)'）",
        "anchor_danbooru": "该已知角色的官方 danbooru 核心标签（如 'saber (fate)' 或 'ayanami rei'）"
      },
      "appearance_cn": "【核心外貌完整复述】：必须完整复述生理性别（女性/男性，严禁模糊）、面容脸型骨架、眼眸颜色与眼神、发色与具体发型（长短、刘海分缝、发梢走势、标志性发饰）、体型身段骨架（修长/娇小/健美/高大）与整体气质。不能怕啰嗦，严禁偷懒缩写！不含易变服装。",
      "danbooru_tags": "英文小写 danbooru 外貌标签串。首词必须是 '1girl' 或 '1boy'，接着是固定外貌标签（发型、发色、瞳色、体型，逗号分隔，如 '1girl, silver hair, short hair, bangs, sharp pale eyes, slender'；不含服装）",
      "clothing_states": [
        {
          "pages": "该衣着状态覆盖的页码区间，如 '1-3'，单页写 '4'",
          "state_cn": "中文衣着描述（款式、颜色、材质、武器）",
          "danbooru_tags": "英文衣着标签串（如 'white battle uniform, v-neck, short skirt, black thighhighs, spear'）"
        }
      ]
    }
  ],
  "environments": [
    {
      "name": "场景名",
      "desc_cn": "中文场景描述",
      "danbooru_tags": "英文场景标签串（如 'grand cathedral, stone pillars, checkered floor'）"
    }
  ],
  "style_notes": "本块画风与氛围要点（黑白热血少年漫、速度线、网点、高对比度等）"
}

要求：
0. **输出最外层必须是 JSON 对象 `{}`，严禁使用数组 `[]` 或任何外层包裹嵌套**；只输出 JSON，不要任何其他文字。
1. **已知动漫角色锚定（强制）**：每个核心角色必须锁定一个 LLM 知识库中已存在的经典知名动漫角色，必须同时提供角色的中英文名和出品动漫作品全称/简称（anime_origin）。
2. **跨页人物对齐与守恒（极其重要）**：
   - 若上方【前序已登记核心角色名单】中已有该角色（外貌相似、特征吻合或剧情承接），**必须严格沿用已有角色的 name、gender、anchor（含作品出处）和 appearance_cn**，严禁另起新名、严禁擅自更换锚定动漫人物、严禁颠倒性别！
   - 只需在 clothing_states 中补充记录该角色在本块页码范围（<<PAGE_RANGE>>）内的衣着变化。
   - 只有在本块中全新登场的非路人重要角色，才创建新角色条目并进行经典动漫角色锚定。
3. **性别绝对锁死（强制）**：gender 必须明确为 female 或 male，严禁写 unknown！danbooru_tags 第一词必须是 1girl 或 1boy，物理切断性别漂移。
4. **特征不可省略（强制）**：appearance_cn 中必须完整复述生理性别、面部骨架、五官、发型细节与身材体态，不得偷懒缩写。
5. **大幅收拢无名路人**：无剧情台词、仅作为背景板出现的路人严禁单独建卡。
6. 同一角色跨页出现必须合并为一条；clothing_states 记录随页码变化的衣着状态，区间覆盖 <<PAGE_RANGE>>。
7. 只输出 JSON，不要输出任何其他文字。
