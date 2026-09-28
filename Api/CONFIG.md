# VideoAutoGenerator — 运行配置说明

> **用途**：记录本系统当前使用的 LLM、ComfyUI、视频生成等关键配置，供排查与调整参考。
> **注意**：实际生效的持久化配置在 `core/settings.json`；build/render 脚本内的参数会覆盖部分默认值。

---

## 1. LLM 配置（litellm 代理）

| 项 | 值 |
|---|---|
| base_url | `http://166.111.50.17:4000/v1` |
| api_key | `sk-qingxu-litellm-thicv639` |
| model | `spark` |
| temperature | `0.7` |
| max_tokens | `8000` |
| timeout | `300s` |

**部署拓扑**：spark 模型部署在 **两台 DGX 硬件上，经 litellm 自动路由/负载均衡**。通过请求体顶层字段 `metadata.session_id`（UUID）实现会话亲和——同一 session 的所有请求路由到同一台机器；不同 session 分散到两台。

- **session_id 传递方式**：`extra_body={"metadata": {"session_id": <uuid>}}`（不是 HTTP header）
- **规则**：每个会话用独立 UUID，不能写死固定字符串，否则全部流量粘到一台

## 2. ComfyUI / GPU 配置

| 项 | 值 |
|---|---|
| host | `192.168.50.254` |
| port | `8188` |
| timeout（poll） | `1200s` |

**GPU**：NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition (94.9 GB VRAM)

> **超时说明**：视频单镜生成约 400-700s，`comfyui_timeout=1200` 留足余量。之前用 900/600 会误报"超时失败"。

## 3. 视频生成参数

| 项 | 值 |
|---|---|
| aspect_ratio | `16:9 (Widescreen)` |
| megapixels（build脚本） | `0.7` |
| sampling_steps | `20` |
| total_duration | `30s`（默认，可按项目覆盖） |
| max_shot_duration | `12s` |
| min_shot_duration | `1.5s` |
| audio_crossfade | `0.3~0.6s` |

**分辨率映射表（用户常用档位）：**

| megapixels | 分辨率 |
|---|---|
| **0.4 MP** | 864×480 (约480P) |
| **0.7 MP** | 1152×640 (约720P) |
| **1.3 MP** | 1376×768 (约1080P) |
| **1.5 MP** | 1504×832 |

> build 脚本当前用 `megapixels=0.8`；render 脚本从项目配置读取 megapixels/steps/aspect_ratio（不写死）。

## 4. ffmpeg

- **本机无系统级 ffmpeg**。正确取法：`import imageio_ffmpeg; imageio_ffmpeg.get_ffmpeg_exe()`。
- `VideoAssembler(ffmpeg_bin=<该路径>)`。

## 5. 一致性保障机制（提示词注入）

每个镜头 H3 提示词开头强制注入：
1. **风格守卫**：写实=real 35mm film / NOT 3D CGI；2D=NOT realistic live-action；3D=stylized 3D CGI
2. **身份锁定**：洛淮南=MALE、白早=FEMALE，真人演员锚定（REAL HUMAN ACTORS）
3. **角色数量锁定**：EXACTLY TWO people, each appears ONCE
4. **逐部位裸体状态描述**（CLOTHING STATE）：锁骨/乳房/小肚子/阴部/屁股/大腿的裸露或覆盖状态，用露骨解剖词明示

## 6. LLM 审查机制

- 检查清单：`data/prompt_review_checklist.md`（13个检查点）
- 审查脚本：`scripts/review_prompts.py`——spark 按清单逐条审查，输出 verdict + fix_suggestions
- 修复脚本：`scripts/fix_review_issues.py`

## 7. 常用脚本

| 脚本 | 用途 |
|---|---|
| `build_bai_zao_version.py <A1/B1/C1/D1>` | 从 storyboards.md 导入分镜并生成 H3 prompt |
| `build_strip_clothes.py <style>` | 撕衣强暴场景三风格 prompt |
| `build_rape_variant.py <R1-R8>` | 凌辱强奸变体 prompt |
| `render_bai_zao_version.py <proj_id>` | GPU 渲染 + 拼接成片 |
| `review_prompts.py <proj_id>` | LLM 逐条审查提示词质量 |
| `fix_review_issues.py <proj_id>` | 自动修复审查发现的问题 |

> **session_id**：build/review 脚本每次运行生成独立 UUID，分散到两台 spark。
