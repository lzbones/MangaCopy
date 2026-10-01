# DGX Spark 与 RTX PRO 6000 (ComfyUI) 接口规范与工程实战指南

> **适用范围**：智能网联计算系统、多模态内容生成（MangaCopy）及类似异构算力流水线  
> **文档维护**：MangaCopy 工程组  
> **版本**：v1.0 (2026-10-01)  

---

## 摘要

本指南系统梳理了 MangaCopy 系统中两台 DGX Spark（`166.111.50.17:4000`，挂载 `21251` 与 `21252` 实例）与一台 NVIDIA RTX PRO 6000 96GB（`192.168.50.254:8188`，ComfyUI）的底层通信协议、负载均衡机理、API 调用规范及工程实战范式。针对实际部署中出现的“单卡满载假死/单卡空载”、“流式推理超时”、“ComfyUI 节点参数动态重写”以及“双池并发解耦”等关键问题，提供了机理剖析与即插即用的模块化 Python 示例代码，配备 Vim 编辑与调试说明，便于后续同类软件系统的复用与扩展。

---

## 目录

- [一、算力拓扑与网络架构](#一算力拓扑与网络架构)
- [二、DGX Spark (LLM / VLM) 开发调用指南](#二dgx-spark-llm--vlm-开发调用指南)
  - [1. 负载均衡与独立会话池（Session Leasing）机理](#1-负载均衡与独立会话池session-leasing机理)
  - [2. 严格并发控制纪律（硬上限 2）](#2-严格并发控制纪律硬上限-2)
  - [3. 流式 SSE 通信与 Reasoning 模型解码规范](#3-流式-sse-通信与-reasoning-模型解码规范)
  - [4. 多模态视觉质检接口（Vision LLM）](#4-多模态视觉质检接口vision-llm)
  - [5. 可直接复用的 Spark 客户端完整实现](#5-可直接复用的-spark-客户端完整实现)
- [三、RTX PRO 6000 (ComfyUI) 开发调用指南](#三rtx-pro-6000-comfyui-开发调用指南)
  - [1. ComfyUI REST API 端点规范](#1-comfyui-rest-api-端点规范)
  - [2. 工作流 JSON 节点动态注入逻辑](#2-工作流-json-节点动态注入逻辑)
  - [3. 轮询等待与结果文件原子下载](#3-轮询等待与结果文件原子下载)
  - [4. 可直接复用的 ComfyUI 客户端完整实现](#4-可直接复用的-comfyui-客户端完整实现)
- [四、双池解耦协同流水线工程范式（Dual-Pool Pipeline）](#四双池解耦协同流水线工程范式dual-pool-pipeline)
  - [1. 生产者-消费者双队列架构原理](#1-生产者-消费者双队列架构原理)
  - [2. 端到端双池协同实战脚本](#2-端到端双池协同实战脚本)
- [五、Vim 开发环境与运维调试指南](#五vim-开发环境与运维调试指南)

---

## 一、算力拓扑与网络架构

系统由两类异构算力节点构成，拓扑如下：

```
                           ┌────────────────────────────────────────────────────────┐
                           │            本地工作站 (Mac / ~/.ai-env)                │
                           │   调度中心 / 遥测监控 / PIL 排版 / FFmpeg 终片合成     │
                           └───────────────┬────────────────────────┬───────────────┘
                                           │                        │
                      HTTP REST (OpenAI-compatible)         HTTP REST (ComfyUI)
                      端口 4000 (LiteLLM Proxy)             端口 8188 (ComfyUI)
                                           │                        │
                                           ▼                        ▼
              ┌────────────────────────────────────────┐   ┌───────────────────────────────┐
              │      LiteLLM 动态网关 (166.111.50.17)   │   │   NVIDIA RTX PRO 6000 96GB    │
              │       路由策略：least-busy 负载均衡     │   │      (192.168.50.254:8188)    │
              └───────────┬────────────────┬───────────┘   │  - SDXL 图像生成 (NoUpScaling)│
                          │                │               │  - MiniMax-H3 480P 视频生成   │
                          ▼                ▼               └───────────────────────────────┘
           ┌──────────────────────┐ ┌──────────────────────┐
           │   DGX Spark 01       │ │   DGX Spark 02       │
           │  (166.111.50.17:21251)│ │  (166.111.50.17:21252)│
           │  Qwen3.8-27B-NVFP4   │ │  Qwen3.8-27B-NVFP4   │
           │  (vLLM EngineCore)   │ │  (vLLM EngineCore)   │
           └──────────────────────┘ └──────────────────────┘
```

---

## 二、DGX Spark (LLM / VLM) 开发调用指南

### 1. 负载均衡与独立会话池（Session Leasing）机理

- **底层端点配置**：
  - 网关地址：`http://166.111.50.17:4000/v1`
  - 认证密钥：`Bearer sk-qingxu-litellm-thicv639`
  - 模型标识：`spark`
  - 物理后端：`http://166.111.50.17:21251/v1` (Spark 01) 与 `http://166.111.50.17:21252/v1` (Spark 02)。
- **物理机理与会话亲和**：
  - LiteLLM 网关依据请求体顶层的 `metadata.session_id` 进行会话路由与前缀缓存（Prefix Cache）优化；
  - **严重陷阱**：若客户端所有请求使用同一个固定 `session_id` 或不传 `session_id`，网关会将请求全部打到单一物理实例（如 `21251`），引发自回归深度解码长耗时占满（表现为该实例假死），而另一台物理实例（`21252`）处于 100% 空载状态；
  - **解决方案**：在客户端初始化一个全局双会话池（`session_pool = [str(uuid.uuid4()), str(uuid.uuid4())]`）。并发线程分别租赁不同的 `session_id`，网关即可通过 `least-busy` 策略将两路并发精准分发至 `21251` 与 `21252`，使两台机器均达到 100% 满负荷。

### 2. 严格并发控制纪律（硬上限 2）

- DGX Spark 采用 Grace Blackwell (GB10) 架构，当前后端运行 vLLM EngineCore，显存带宽属于计算密集型。
- **并发阈值**：**每台 DGX 同一时间仅能承载 1 个并发推理请求**。全局并发硬上限严格锁定为 **2**（`threading.Semaphore(2)`）。实测若超过 2 并发，两台机器将发生请求严重抢占，排队等待超过 50 分钟零产出。

### 3. 流式 SSE 通信与 Reasoning 模型解码规范

- **流式接收（`stream: true`）**：
  - 后端大模型生成单次可达数千至上万 token，总耗时可能达数分钟。必须使用流式 SSE（Server-Sent Events）消费，将请求超时（`timeout`）约束在“两个连续数据块之间的静默间隔”（如 60s），而非约束总端到端耗时。
- **Thinking / Reasoning 结构分离**：
  - Qwen3.8 推理模型在流式输出中包含 `delta.reasoning_content`（思考过程）与 `delta.content`（最终输出内容）。
  - **`max_tokens` 设定陷阱**：模型会优先消耗数千 token 进行内部思考。探活或简单请求的 `max_tokens` 必须设定在 $\ge 256$；若误设为 8 或 16，`reasoning_content` 会瞬间耗尽配额导致 `content` 返回空字符串，引发“空流”误报。复杂文本/剧本生成建议设置为 $8000 \sim 16000$。

### 4. 多模态视觉质检接口（Vision LLM）

- 质检接口支持直接传入图片。图片需先在本地以 PIL 或 I/O 读取，并转换为 Base64 编码字符串，按 OpenAI 视觉格式传入：
  ```json
  {
    "type": "image_url",
    "image_url": {
      "url": "data:image/png;base64,<BASE64_DATA>"
    }
  }
  ```

---

### 5. 可直接复用的 Spark 客户端完整实现

以下代码为生产验证通过的高可用客户端模板，可直接保存为 `spark_client.py` 使用：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DGX Spark 专用高性能客户端模块.

特性：
1. 信号量全局硬并发约束（上限 2）；
2. 独立 Session ID 租约池，保证两台 DGX 100% 负载均衡；
3. SSE 流式消费，兼容 reasoning_content 与 content 字段；
4. 自动指数退避重试（针对网络抖动与间歇性空流）。
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SparkClient")

class SparkClient:
    def __init__(
        self,
        base_url: str = "http://166.111.50.17:4000/v1",
        api_key: str = "sk-qingxu-litellm-thicv639",
        model: str = "spark",
        max_concurrent: int = 2,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.sem = threading.Semaphore(max_concurrent)
        # 初始化双会话池，确保两路并发分落两台物理实例
        self.session_pool = [str(uuid.uuid4()), str(uuid.uuid4())]
        self._session_idx = 0
        self._lock = threading.Lock()

    def lease_session(self) -> str:
        """从会话池轮询租赁会话 ID."""
        with self._lock:
            s_id = self.session_pool[self._session_idx % len(self.session_pool)]
            self._session_idx += 1
            return s_id

    def chat(
        self,
        messages: List[Dict[str, Any]],
        session_id: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        json_mode: bool = False,
        timeout: int = 120,
        max_retries: int = 3,
    ) -> str:
        """执行流式聊天补全."""
        if session_id is None:
            session_id = self.lease_session()

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "metadata": {"session_id": session_id},
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        url = f"{self.base_url}/chat/completions"

        for attempt in range(max_retries):
            with self.sem:
                t0 = time.time()
                try:
                    resp = requests.post(url, headers=headers, json=payload, stream=True, timeout=timeout)
                    if resp.status_code != 200:
                        err_text = resp.text[:200]
                        logger.warning(f"Spark 返回错误 HTTP {resp.status_code}: {err_text}")
                        time.sleep(2 ** attempt * 5)
                        continue

                    collected_content = []
                    collected_reasoning = []
                    for line in resp.iter_lines(decode_unicode=True):
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            choice = chunk.get("choices", [{}])[0]
                            delta = choice.get("delta", {})
                            if "reasoning_content" in delta and delta["reasoning_content"]:
                                collected_reasoning.append(delta["reasoning_content"])
                            if "content" in delta and delta["content"]:
                                collected_content.append(delta["content"])
                        except Exception:
                            continue

                    result_text = "".join(collected_content).strip()
                    dur = time.time() - t0
                    if not result_text and json_mode and attempt == 0:
                        # 降级：部分情况 vLLM guided decoding 导致空流，去掉 json_object 兜底重试
                        logger.warning("触发 json_mode 空流降级重试...")
                        payload.pop("response_format", None)
                        continue

                    if not result_text:
                        logger.warning(f"Spark 返回空流 (attempt {attempt + 1}/{max_retries})，退避重试...")
                        time.sleep(10)
                        continue

                    logger.info(f"[Spark OK] session={session_id[:8]} 用时={dur:.1f}s 产出={len(result_text)}字")
                    return result_text

                except Exception as exc:
                    logger.error(f"Spark 连接异常 (attempt {attempt + 1}/{max_retries}): {exc}")
                    time.sleep(15)

        raise RuntimeError(f"Spark 在重试 {max_retries} 次后仍未能完成响应")

    def chat_vision(
        self,
        prompt: str,
        image_paths: List[Path],
        session_id: Optional[str] = None,
        max_tokens: int = 2048,
        json_mode: bool = False,
    ) -> str:
        """多模态视觉打分质检."""
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img_p in image_paths:
            if not img_p.exists():
                raise FileNotFoundError(f"质检图片不存在: {img_p}")
            data = base64.b64encode(img_p.read_bytes()).decode("utf-8")
            ext = img_p.suffix.lower().lstrip(".") or "png"
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/{ext};base64,{data}"}
            })

        messages = [{"role": "user", "content": content}]
        return self.chat(messages, session_id=session_id, max_tokens=max_tokens, json_mode=json_mode)
```

---

## 三、RTX PRO 6000 (ComfyUI) 开发调用指南

### 1. ComfyUI REST API 端点规范

ComfyUI 原生提供简单高效的异步 REST 接口：

| HTTP 方法 | 路径端点 | 请求参数 / Body | 功能与返回说明 |
| :--- | :--- | :--- | :--- |
| `POST` | `/prompt` | `{"prompt": <WORKFLOW_DICT>, "client_id": "..."}` | 提交计算图任务；返回 `{"prompt_id": "uuid"}` |
| `GET` | `/queue` | 无 | 查询队列；返回 `{"queue_running": [...], "queue_pending": [...]}` |
| `GET` | `/history/{prompt_id}`| URL 路径传 prompt_id | 查询指定任务输出；执行中返回 `{}`，完成返回节点输出明细 |
| `GET` | `/view` | Query: `filename`, `subfolder`, `type=output` | 流式下载生成的图片 PNG 或视频 MP4 二进制数据 |
| `POST` | `/upload/image` | Multipart-form: `image` (文件), `overwrite=true`| 上传参考首帧至 ComfyUI 输入目录（用于 I2V 视频） |

### 2. 工作流 JSON 节点动态注入逻辑

通过 ComfyUI 导出的 API 格式 JSON 包含若干节点字典（键为节点 ID 字符串）。调用时需动态定位并重写输入字段：

1. **SDXL 480P 无上采样绘图工作流（标准绘图）**：
   - 节点 `3`（CLIPTextEncode）：正向提示词 `inputs.text`；
   - 节点 `4`（CLIPTextEncode）：负向提示词 `inputs.text`；
   - 节点 `5`（KSampler）：随机种子 `inputs.seed`；
   - 节点 `6`（EmptyLatentImage）：尺寸 `inputs.width`、`inputs.height`、`inputs.batch_size=1`；
   - 节点 `8`（SaveImage）：导出路径前缀 `inputs.filename_prefix`；
   - 节点 `12`（若为 HD 放大模板）：放大目标宽高 `inputs.width`、`inputs.height`。
2. **MiniMax-H3 480P 视频生成工作流**：
   - 节点 `105:104`：提示词文本；
   - 节点 `105:111`：生成视频时长（秒，如 10.0）；
   - 节点 `105:15`：随机种子；
   - 节点 `92`：导出前缀；
   - 节点 `114`（I2V 模式）：首帧图像文件名 `inputs.image`。

### 3. 轮询等待与结果文件原子下载

提交后需进入轮询循环（间隔 1~2s 查询 `/history/{prompt_id}`）。获取到 outputs 后，定位 `images` 或 `videos` 列表，调取 `/view` 接口拉取二进制字节，并以 `.tmp_<key>` 临时目录做原子写入后安全移动（`os.replace`），杜绝半写入污染。

---

### 4. 可直接复用的 ComfyUI 客户端完整实现

以下代码为生产验证通过的 ComfyUI 客户端封装，可直接保存为 `comfy_client.py` 使用：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RTX PRO 6000 ComfyUI 专用异步驱动模块.

特性：
1. 节点参数安全注入辅助函数（set_node）；
2. 任务提交、状态轮询与超时熔断；
3. 产出文件安全下载与原子落盘。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ComfyClient")

class ComfyClient:
    def __init__(self, host: str = "192.168.50.254", port: int = 8188):
        self.base_url = f"http://{host}:{port}"

    def set_node(self, workflow: Dict[str, Any], node_id: str, **kwargs) -> None:
        """安全修改工作流指定节点的 inputs 参数."""
        if node_id not in workflow:
            raise KeyError(f"工作流中未找到节点 ID: {node_id}")
        inputs = workflow[node_id].setdefault("inputs", {})
        inputs.update(kwargs)

    def submit(self, workflow: Dict[str, Any]) -> str:
        """提交计算图，返回 prompt_id."""
        url = f"{self.base_url}/prompt"
        payload = json.dumps({"prompt": workflow}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            prompt_id = data.get("prompt_id")
            if not prompt_id:
                raise RuntimeError(f"ComfyUI 未返回 prompt_id: {data}")
            return prompt_id

    def wait(self, prompt_id: str, timeout: int = 300, poll_interval: float = 1.5) -> Dict[str, Any]:
        """轮询等待执行完毕，返回 history 节点明细."""
        url = f"{self.base_url}/history/{prompt_id}"
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    if prompt_id in data:
                        logger.info(f"ComfyUI 任务完成: {prompt_id} (耗时 {time.time() - t0:.1f}s)")
                        return data[prompt_id]
            except Exception:
                pass
            time.sleep(poll_interval)
        raise TimeoutError(f"ComfyUI 任务超时 ({timeout}s): {prompt_id}")

    def download_outputs(self, history_entry: Dict[str, Any], dest_dir: Path) -> List[Path]:
        """从 history 条目中解析所有输出文件并下载到目标目录."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        outputs = history_entry.get("outputs", {})
        saved_paths: List[Path] = []

        for node_id, node_out in outputs.items():
            # 兼容图片和视频字段
            items = node_out.get("images", []) + node_out.get("videos", []) + node_out.get("gifs", [])
            for item in items:
                fn = item.get("filename")
                subfolder = item.get("subfolder", "")
                f_type = item.get("type", "output")
                if not fn:
                    continue

                qs = urllib.parse.urlencode({"filename": fn, "subfolder": subfolder, "type": f_type})
                dl_url = f"{self.base_url}/view?{qs}"
                local_path = dest_dir / fn
                with urllib.request.urlopen(dl_url, timeout=60) as resp, open(local_path, "wb") as out_f:
                    shutil.copyfileobj(resp, out_f)
                saved_paths.append(local_path)
                logger.info(f"文件下载成功: {local_path.name} ({local_path.stat().st_size} bytes)")

        return saved_paths

    def upload_image(self, image_path: Path) -> str:
        """上传首帧参考图（适用于 I2V 视频）."""
        import requests
        url = f"{self.base_url}/upload/image"
        with open(image_path, "rb") as fh:
            files = {"image": (image_path.name, fh, "image/png")}
            data = {"overwrite": "true"}
            resp = requests.post(url, files=files, data=data, timeout=30)
            if resp.status_code != 200:
                raise RuntimeError(f"首帧图片上传失败: {resp.text}")
            return resp.json().get("name", image_path.name)
```

---

## 四、双池解耦协同流水线工程范式（Dual-Pool Pipeline）

### 1. 生产者-消费者双队列架构原理

在传统的“生图 $\rightarrow$ 质检 $\rightarrow$ 生图”串行逻辑中，GPU（PRO 6000）在 LLM 质检期间完全闲置，而 Spark 在 GPU 绘图期间完全闲置。

**双池解耦架构**通过队列连接两个异步线程池：
1. **生成池（PRO 6000）**：不断从 `gen_q` 取任务绘图，完成后将图像路径投入 `val_q`；
2. **质检池（两台 Spark）**：两个独立会话线程从 `val_q` 取图像进行打分；若评分 $\ge 7$ 则判定通过；若未通过且未满 3 轮，则通过 `seed + attempt * 7919` 扰动种子将画格重新推入 `gen_q`；
3. **效果**：PRO 6000 持续全速出图（单张 5s），两台 Spark 全速并行评分打满 100%，两者重叠并行，总体用时缩短 50% 以上。

```
                     ┌──────────────┐
                     │   gen_q      │◄────────── (打回重绘，seed扰动)
                     └──────┬───────┘                    ▲
                            │ 取画格任务                 │
                            ▼                            │
                   ┌─────────────────┐                   │
                   │ PRO 6000 生成池 │                   │ 评分 < 7
                   │ (单图 5.0 秒)   │                   │ (最多 3 轮)
                   └────────┬────────┘                   │
                            │ 投递成品图                 │
                            ▼                            │
                     ┌──────────────┐                    │
                     │   val_q      │                    │
                     └──────┬───────┘                    │
                            │ 双通道并行取图             │
                            ▼                            │
                   ┌─────────────────┐                   │
                   │ Spark 01 & 02   ├───────────────────┘
                   │ 双并发质检池    │
                   └────────┬────────┘
                            │ 评分 >= 7 / 达到上限
                            ▼
                     [ 最终收敛落盘 ]
```

---

### 2. 端到端双池协同实战脚本

可直接保存为 `dual_pool_demo.py` 并在 `~/.ai-env` 环境下运行验证：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双池解耦协同流水线实战范式.

结合 SparkClient 与 ComfyClient，实现 PRO 6000 极速初生与双 Spark 并发质检闭环。
"""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from spark_client import SparkClient
from comfy_client import ComfyClient

def run_dual_pool(tasks: list[dict], workflow_template: dict, output_dir: Path):
    spark = SparkClient()
    comfy = ComfyClient()
    output_dir.mkdir(parents=True, exist_ok=True)

    gen_q: queue.Queue = queue.Queue()
    val_q: queue.Queue = queue.Queue()
    pending = {t["id"] for t in tasks}
    lock = threading.Lock()
    MAX_ATTEMPTS = 3

    # 初始化生成任务
    for t in tasks:
        gen_q.put((t, 0))

    def resolve(task_id: str, success: bool):
        with lock:
            pending.discard(task_id)
            if not pending:
                # 终止毒丸
                for _ in range(3):
                    gen_q.put(None)
                for _ in range(2):
                    val_q.put(None)

    def generator_worker():
        while True:
            item = gen_q.get()
            if item is None:
                break
            task, attempt = item
            tid = task["id"]
            seed = task["base_seed"] + attempt * 7919

            # 拷贝工作流并注入参数
            wf = json.loads(json.dumps(workflow_template))
            comfy.set_node(wf, "3", text=task["positive"])
            comfy.set_node(wf, "4", text=task["negative"])
            comfy.set_node(wf, "5", seed=seed)
            comfy.set_node(wf, "6", width=task["width"], height=task["height"])
            comfy.set_node(wf, "8", filename_prefix=f"demo/{tid}")

            try:
                pid = comfy.submit(wf)
                entry = comfy.wait(pid, timeout=120)
                tmp_dir = output_dir / f".tmp_{tid}_{attempt}"
                files = comfy.download_outputs(entry, tmp_dir)
                if not files:
                    resolve(tid, False)
                    continue
                final_img = output_dir / f"{tid}.png"
                if final_img.exists():
                    final_img.unlink()
                files[0].replace(final_img)
                tmp_dir.rmdir()
                val_q.put((task, attempt, final_img))
            except Exception as e:
                resolve(tid, False)

    def validator_worker(session_id: str):
        while True:
            item = val_q.get()
            if item is None:
                break
            task, attempt, img_path = item
            tid = task["id"]

            prompt = (
                f"请核对生成的漫画分镜【{tid}】是否符合文学脚本：{task['script']}。\n"
                f"请以 JSON 格式返回：{{\"pass\": true/false, \"score\": 0~10, \"reason\": \"简述理由\"}}"
            )
            try:
                raw_json = spark.chat_vision(prompt, [img_path], session_id=session_id, json_mode=True)
                data = json.loads(raw_json)
                score = int(data.get("score", 0))
                ok = bool(data.get("pass", False)) and score >= 7
            except Exception:
                ok, score = False, 0

            if ok:
                resolve(tid, True)
            elif attempt + 1 < MAX_ATTEMPTS:
                gen_q.put((task, attempt + 1))
            else:
                # 用尽尝试次数，保留当前图像锁定
                resolve(tid, True)

    threads = [threading.Thread(target=generator_worker, daemon=True) for _ in range(2)]
    threads += [threading.Thread(target=validator_worker, args=(spark.session_pool[i],), daemon=True) for i in range(2)]

    for t in threads:
        t.start()
    for t in threads:
        t.join()
```

---

## 五、Vim 开发环境与运维调试指南

许副研究员常用 Vim 进行代码编写与远程调试，以下为推荐的高效配置与排查指令：

### 1. Vim 针对本模块的推荐快捷指令

在编辑 `spark_client.py` 或 `comfy_client.py` 时，将以下设置添加至 `~/.vimrc` 或在命令模式中执行：

```vim
" 开启 Python 语法折叠与智能缩进
setlocal foldmethod=indent
setlocal tabstop=4 shiftwidth=4 expandtab
setlocal number relativenumber

" 快捷键：一键调用 ~/.ai-env 执行当前脚本并查看输出
nnoremap <buffer> <leader>r :w<CR>:!~/.ai-env/bin/python %<CR>

" 快捷键：在 Vim 内格式化选中区域的 JSON 字符串
vnoremap <leader>j :!jq .<CR>
```

### 2. 远程终端即时联调指令（单行命令）

- **测试两台 Spark 物理端点是否分别畅通**：
  ```bash
  ~/.ai-env/bin/python -c 'import requests; [print(p, requests.post(f"http://166.111.50.17:{p}/v1/chat/completions", headers={"Authorization":"Bearer thicv639"}, json={"model":"Qwen3.8-27B-abliterated-NVFP4","messages":[{"role":"user","content":"hi"}],"max_tokens":10}).status_code) for p in [21251, 21252]]'
  ```
- **检查 ComfyUI 当前运行队列与等待队列**：
  ```bash
  curl -s http://192.168.50.254:8188/queue | jq '{running: (.queue_running | length), pending: (.queue_pending | length)}'
  ```
