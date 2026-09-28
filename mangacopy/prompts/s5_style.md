<!-- S5 风格卡：按 params.style 生成全片视频风格基准卡，写 s5_video/00_style.md。
     输出 JSON：{"content": "<markdown 正文>"}。
     字段：<<STYLE>>（风格参数 2d/3d/live）、<<STYLE_LABEL_CN>>（中文风格名）、<<STYLE_GUARD_CN>>（中文风格守卫句）、<<STYLE_GUARD_NOT>>（英文 NOT 反例守卫，逗号分隔）、<<STYLE_GUARD_EN>>（英文风格锚句，逐字符保留）、<<SETTINGS_DIGEST>>（L0 设定摘要：人物/环境/风格注记） -->

【全话设定（L0）】
<<SETTINGS_DIGEST>>

【风格参数】
style = <<STYLE>>（<<STYLE_LABEL_CN>>）

【风格守卫（固定，逐字符保留，不得改写）】
中文守卫：<<STYLE_GUARD_CN>>
英文守卫：<<STYLE_GUARD_NOT>>
英文风格锚句：<<STYLE_GUARD_EN>>

你是资深动画导演兼视觉开发指导。基于给定设定与风格参数，撰写本片的全片视频风格基准卡（中文为主，含英文锚句），输出 JSON：

{"content": "<markdown 正文>"}

正文必须严格使用以下固定标题（## 级，顺序不变，逐项填写，不得增删改标题）：

## 风格定位
风格名称与整体质感定位；第二行必须原样给出中文守卫句：<<STYLE_GUARD_CN>>

## English Style Anchor
供视频生成 prompt 使用的英文风格锚点；第一行必须原样给出：<<STYLE_GUARD_EN>>
随后用 2-3 句英文展开：媒介质感（赛璐璐上色/CGI 渲染/真人实拍）、线条与轮廓、色彩体系、光影处理、镜头质感。不得出现 <<STYLE_GUARD_NOT>> 中的任何反例风格。

## 色彩与光影
主色调、明度对比、光源类型与氛围（结合 style_notes 与夜景/日景设定）。

## 人物渲染基准
人物在镜头中的统一处理方式（轮廓、发丝、衣褶、面部细节级别），确保跨镜一致性。

## 全片一致性守卫
列出每段视频脚本与 prompt 都必须重复的要素：英文风格锚句、身份锁、衣着锚定；再次原样列出英文 NOT 守卫：<<STYLE_GUARD_NOT>>

要求：中文部分使用中文；英文锚句逐字符保留守卫原文；只输出 JSON。
