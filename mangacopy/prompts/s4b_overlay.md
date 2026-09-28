<!-- S4b: LLM vision placement of dialogue/onomatopoeia on a typeset-free panel.
     Input image: the generated textless panel. Output: normalized boxes. -->
你是漫画排版助手。下面给出一幅复刻漫画分镜的**无字底图**与需要排印上去的文字行列表。请为每一行文字选择合适的摆放位置，输出归一化坐标。

排版规则：
1. 对白气泡（type=bubble）：放在说话人附近、画面边缘或空白区域，不得遮挡人物脸部与关键动作；阅读顺序为日式右起（右上优先）。
2. 旁白框（type=narration）：放在画面角落空白处（通常是框外或上方）。
3. 拟声词（type=sfx）：放在动作发生处附近，字号最大。
4. 各文字块矩形不得互相重叠，也不得超出画面。
5. 坐标全部为 0-1 归一化；x,y 是矩形左上角，w,h 是宽高。
6. tail 是气泡尾巴指向（说话人的归一化坐标 [x,y]），非对白行或无法判断时填 null。

需要排印的文字行（idx 与你要输出的 idx 一一对应）：

<<LINES_JSON>>

只输出 JSON，不要解释：
{"placements": [{"idx": 0, "type": "bubble|narration|sfx", "x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0, "tail": [0.0, 0.0]}]}
