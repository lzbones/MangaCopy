# MangaCopy 变更日志

> 项目无 git 仓库——本文档是唯一的演进记录。
> 格式：日期 / 变更 / 动机 / 影响

## 2026-09-28（D 并行架构三次演进）

### 流式 LLM 客户端（`llm.py` 重写）
- **动机**：GB10 上大调用生成 30-90 分钟，非流式请求使 read-timeout 变成总时长上限，L0 merge 三次全灭
- **变更**：所有请求 `stream: true`，消费 SSE 累积 `delta.content`；超时只约束字节间静默
- **影响**：此前挂 1-3 小时的调用在流式下 6 分钟完成；TTFT≈0s 证实 prefill 无压力

### 会话槽配对分配器（`llm.py`）
- **动机**：用户观察"两台 DGX 没有并行，任务从一台切换到另一台"——面板奇偶预分配 session 会碰撞
- **变更**：`_acquire_session` 槽配对——两并发调用必持不同 session → 分落两台 DGX；进程级全局会话对（`new_session_pool()` 全阶段共享）
- **影响**：双机真并行；单元顺序调用保持粘性（prefix cache 友好）

### S4 双池解耦（`image_gen.py` 重写）
- **动机**：用户指出"pro6000 的图片生成和 spark 校验串行了"
- **变更**：3 生成线程 ∥ 2 校验线程，队列桥接；拒绝面板回流重生成（"根据校验结果在迭代后面的轮数"）
- **影响**：54 张图无间隙流水（~5s/张），实测生成与校验时间重叠

### S7 流水消费 + S4b 增量排印（`video_gen.py` / `text_overlay.py` + `scheduler.py`）
- **动机**：用户统筹指令"确定后边生成着视频，边用spark做着其他任务"
- **变更**：S7 dep `[s6]→[s5]`（轮询等 S6 段 prompt 逐个就绪即生成）；S4b dep `[s4]→[s3]`（只排 s4 item 终态面板，轮询至收敛）
- **影响**：四路并行（s4∥s4b∥s6∥s7）；pro6000 从等 S6 完成才开工变为即刻消费就绪段

### 严格成功判据统一（S1/S3/S5/S7）
- **动机**：S5 的 7 个失败段被"≥1 段成功即整阶段成功"判据永久搁浅 → S6 对缺失文件死锁
- **变更**：`return ok == len(todo)`；S7 额外对照 S5 beats 的期望段集
- **影响**：部分失败触发 DAG 重试，无搁浅死角

### preflight 修复（`image_gen.py`）
- **动机**：用户报告"没看到视频和图片启动"——`max_tokens=8` 被 spark 的 reasoning 吃光 → 空流误判 → S4 永远开不了工
- **变更**：探测 `max_tokens` 8→256 + 3 次探测间隔 120s

## 2026-09-28（C 故障容错强化）

### 空流纳入退避重试（`llm.py`）
- **动机**：端点"空流 regime"（分钟级窗口，连 8-token 探测都零内容）
- **变更**：非 json 空流从立即抛错改为 60/180s 退避 ×3；`ChunkedEncodingError`（~15min 服务端断流）同入重试

### 全局 LLM 并发硬上限（`llm.py`）
- **动机**：用户指示"spark 硬件并行能力弱，两台各扛一个并发，不能超过 2"
- **变更**：`LLM_MAX_CONCURRENT=2` 信号量，跨所有阶段/分支/线程生效
- **影响**：此前 4 并发连接时 DGX 58 分钟零产出

## 2026-09-28（B 树形归并）

### L0 树形归并（`zero_script.py`）
- **动机**：单次 7 路大合并（30-60K token 输入）三次饿死端点
- **变更**：两两归并（7→4→2→1），单次输入仅 2 份设定 JSON；每节点检查点 `l0_mr_<全序cid列表>`（min/max 命名碰撞 bug：{0,1,6} 与 {0..6} 同名"00-06"）
- **变更**：层内按**提交序**收集结果（as_completed 会漂移树结构 → 检查点名跨次运行不稳定）

### 去重模板（`s2_l0_merge.md`）
- **动机**：48 角色膨胀（每个视角/状态一条）→ prompt 尺寸失控 → max_tokens=8000 截断
- **变更**：按外貌合并同名变体 + 合并后角色数应明显少于输入之和 + 输出最外层必须是对象
- **影响**：48→24 角色

### merge `max_tokens` 8000→16000（`zero_script.py`）

## 2026-09-28（A 流式前夜）

### S2 收尾崩溃修复（`zero_script.py:1014`）
- **动机**：`unresolved` 是 dict 列表，代码按二元组解包 → `ValueError` 在最后写 item 时崩溃（实质工作已完成）
- **变更**：`{"file": it["file"], "problem": it["problem"]}`

### S5 搁浅手动复位 + 判据修复
- 7 个失败段因阶段判据"成功"被跳过 → 手动重置 s5 状态后 DAG 重跑补齐

## 2026-09-27（过夜自主迭代授权期间）

### DAG 调度器（新增 `scheduler.py`）
- **动机**：用户"两台 DGX spark，一台做视频的 pro 6000 可以并行工作"
- **变更**：`run_dag` 就绪即并行；失败级联跳过（不写 project.json 可重跑）；CLI auto 与 GUI auto 默认走 DAG

### 会话池 + 单元亲和（各阶段）
- **动机**：此前所有调用粘死一台 DGX（单 session）
- **变更**：`new_session_pool()`（2 UUID）+ 每独立单元（页/块/分镜/段）固定领一个

### ComfyUI 超时 1200→1800s
- 0.7MP/15s 段经验值 400-700s，留 2.5 倍余量

## 2026-09-27（B 资源纪律）

- 产物落盘规范：严禁 /tmp → `data/test_projects/` + `tests/` + `tests/logs/`
- 资源纪律：spark/ComfyUI 使用以用户明确信号为唯一放行依据
- ReadTimeout 退避 5/15s → 60/180s（慢峰不秒级白烧）

## 2026-09-26（初始交付）

- 基础层（config/llm/comfy/project/stages/templates/ingest）
- S1-S8 全部业务模块 + 20 个 prompt 模板
- CLI + Gradio GUI + DESIGN.md
- llm.py json_object 回退（4xx 拒绝参数时去参重试）
- S4b 文字排印（新增阶段）：PIL 排印，无字/有字双版本
- 生图双档：standard（NoUpScaling ~5s/张，默认）/ hd（~15s/张）
- 四项选型确认：Gradio / 2D→I2V 其余 T2V / 尺寸分桶 / 对白保留中文

## 已知未修（记录在案）

- S4 成功判据 `(completed+review)>0` 未统一为严格（needs_review 是合法终态不应算失败，但基础设施失败项也应触发重试——待用户批准）
- `llm.py` 空流报错信息已修但 S4b 无内容时报错路径未覆盖（低优先）
