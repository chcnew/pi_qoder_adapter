# pi-qoder-adapter

> 把 **Pi**（coding agent）桥接到 **Qoder Agent SDK** 的本机回环 OpenAI 兼容适配器。

Pi 把本适配器当作一个普通的 OpenAI provider 使用；适配器在背后通过 Qoder SDK
启动会话，把 Pi 的工具以 **MCP 工具**的形式暴露给 Qoder 模型，并把模型输出转换回
OpenAI Chat Completions（含 SSE 流式）格式。最终由 Pi 真正执行文件读写、命令运行等工具。

- 只监听 `127.0.0.1` / `localhost` / `::1`，不暴露到局域网或公网
- 完整支持流式与非流式 `chat.completions`
- 支持工具调用（tool calls）的双向往返与多轮会话复用
- 零成本模型声明，兼容 Pi 的 `models.json`

更面向使用者的逐步说明见 [`docs/usage.md`](docs/usage.md)。

---

## 目录

- [工作原理](#工作原理)
- [环境要求](#环境要求)
- [安装](#安装)
- [快速开始](#快速开始)
- [命令一览](#命令一览)
- [HTTP 接口](#http-接口)
- [模型目录](#模型目录)
- [在 Pi 中使用](#在-pi-中使用)
- [安全说明](#安全说明)
- [项目结构](#项目结构)
- [常见问题](#常见问题)
- [开发](#开发)
- [许可证](#许可证)

---

## 工作原理

```text
┌────────┐   ① OpenAI Chat Completions（含 tools）   ┌──────────────────────┐
│        │ ─────────────────────────────────────────▶ │  pi-qoder-adapter    │
│   Pi   │                                          │  (127.0.0.1:8765)    │
│        │ ◀───────────────────────────────────────── │                      │
└────────┘   ④ assistant 文本 / tool_calls / SSE     └──────────┬───────────┘
     ▲                                                          │ ② Qoder SDK
     │ ⑤ Pi 执行工具并把结果回传                                 ▼
     │                                              ┌──────────────────────┐
     └───────────────────────────────────────────── │  Qoder Agent SDK     │
                                                    │  （qodercli 会话）    │
                                                    └──────────────────────┘
```

1. Pi 向适配器发送一次 `POST /v1/chat/completions`，携带对话历史与可用工具。
2. 适配器按「会话键」复用或新建一个 Qoder 会话，并把 Pi 的工具注册成 MCP 工具
   （`mcp__pi__<tool>`），同时屏蔽 Qoder 内置工具，确保动作都由 Pi 执行。
3. Qoder 模型发出工具调用时，适配器把它转成 OpenAI 的 `tool_calls`，并以
   `finish_reason="tool_calls"` 返回给 Pi。
4. Pi 实际执行工具，再把 `role="tool"` 的结果发回适配器。
5. 适配器把结果交还给正在挂起的 Qoder 会话，模型继续推理，循环直到本轮结束。

### 会话与增量

- 每个 Pi 会话（token + model + system + 首条 user 消息计算出的哈希）对应一个**长生命周期**
  Qoder 会话，避免每轮都重建进程。
- 适配器只把 Pi transcript 中**尚未被 Qoder 看到**的增量转成下一轮 query（用户消息或工具结果）；
  当历史对不上、工具集变化或出错时，会退回**重放整段 transcript**（reseed）。
- 空闲会话由后台任务定期回收（默认 30 分钟），会话在独立临时工作目录中运行，关闭时清理。

## 环境要求

- Python ≥ 3.12（推荐用 [uv](https://docs.astral.sh/uv/) 管理）
- 依赖：`fastapi`、`uvicorn[standard]`、`qoder-agent-sdk`
- 一个 **Qoder Personal Access Token（PAT）**，通过环境变量提供：

  ```bash
  # Linux / macOS / WSL
  export QODER_PERSONAL_ACCESS_TOKEN="你的PAT"
  
  # Windows PowerShell
  $env:QODER_PERSONAL_ACCESS_TOKEN = "你的PAT"
  ```

  > PAT 本身不由适配器保管：每次请求由调用方（Pi）通过 `Authorization: Bearer <token>`
  > 头传入。环境变量主要用于启动时刷新模型目录和 `probe` 命令。

## 安装

```bash
git clone <repo-url> pi-qoder-adpter
cd pi-qoder-adpter
uv sync            # 或者: python -m venv .venv && .venv/bin/pip install -e .
```

安装后可用以下任意方式启动：

- `uv run pi-qoder-adapter ...`
- `.venv/bin/pi-qoder-adapter ...`（Windows 为 `.venv\Scripts\pi-qoder-adapter.exe`）
- `python main.py ...`

## 快速开始

```bash
# 1) 提供 PAT
export QODER_PERSONAL_ACCESS_TOKEN="你的PAT"

# 2) 启动适配器（保持前台运行）
uv run pi-qoder-adapter serve --host 127.0.0.1 --port 8765

# 3) 另开一个终端：生成 Pi 配置片段，合并进 Pi 的 models.json
uv run pi-qoder-adapter print-config --port 8765

# 4) （可选）自检 Qoder 工具挂起与交还是否正常
uv run pi-qoder-adapter probe --model efficient --wait-seconds 60
```

## 命令一览

```text
pi-qoder-adapter serve          启动只监听本机的 OpenAI 兼容接口
pi-qoder-adapter print-config   打印要合并进 Pi models.json 的片段
pi-qoder-adapter probe          验证 Qoder 工具可以挂起并交还给调用方
```

### `serve` — 启动适配器

```bash
uv run pi-qoder-adapter serve --host 127.0.0.1 --port 8765
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | `127.0.0.1` | 只允许 `127.0.0.1` / `localhost` / `::1`，其他值直接拒绝启动 |
| `--port` | `8765` | 监听端口 |

### `print-config` — 生成 Pi 配置片段

```bash
uv run pi-qoder-adapter print-config --port 8765
```

输出一个可合并进 Pi `models.json` 的 JSON 片段，详见[在 Pi 中使用](#在-pi-中使用)。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--port` | `8765` | 生成的 `baseUrl` 端口，需与 `serve` 一致 |

### `probe` — 连通性自检

```bash
uv run pi-qoder-adapter probe --model efficient --wait-seconds 60
```

在临时目录里跑一次真实的 Qoder 会话，注册一个 `ping` MCP 工具并等待模型调用它，
用来验证「Qoder 工具可以挂起并把控制权交还给调用方」。需要已设置
`QODER_PERSONAL_ACCESS_TOKEN`。退出码 `0` 表示成功。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--model` | `efficient` | 用于探测的模型 id |
| `--wait-seconds` | `60` | 工具挂起并等待交还的秒数 |

## HTTP 接口

| 接口 | 方法 | 鉴权 | 说明 |
|------|------|------|------|
| `/healthz` | GET | 无 | 健康检查，返回 `{"status":"ok"}` |
| `/v1/models` | GET | 可选 Bearer | 模型列表；带 token 时会尝试从 Qoder 服务刷新目录 |
| `/v1/chat/completions` | POST | **必须** Bearer | OpenAI 兼容对话接口，支持 `stream: true/false` |

示例请求：

```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $QODER_PERSONAL_ACCESS_TOKEN" \
  -d '{
    "model": "efficient",
    "messages": [{"role": "user", "content": "hello"}],
    "stream": true
  }'
```

错误响应遵循 OpenAI 结构：

```json
{ "error": { "message": "...", "type": "authentication_error" } }
```

## 模型目录

未从 Qoder 服务刷新时，内置目录包含 5 个模型 id（均按 200k 上下文 / 32k 输出、
零成本声明）：

| id | 说明 |
|----|------|
| `auto` | Qoder Auto |
| `ultimate` | Qoder Ultimate |
| `performance` | Qoder Performance |
| `efficient` | Qoder Efficient |
| `lite` | Qoder Lite |

- 启动 `serve` 时若设置了 PAT，会尝试从 Qoder 拉取真实模型目录（结果缓存 10 分钟）。
- `/v1/models` 携带 Bearer token 也会触发刷新。
- 模型 id 支持大小写/标点无关的别名匹配（例如显示名）。
- 请求了未知模型 id 时，若目录已从服务加载过，会返回 `400` 并列出可用 id。

## 在 Pi 中使用

1. 设置环境变量 `QODER_PERSONAL_ACCESS_TOKEN`。
2. 启动适配器：`uv run pi-qoder-adapter serve`（保持前台运行）。
3. 把 `uv run pi-qoder-adapter print-config` 的输出合并进 Pi 的 `models.json`
   （通常位于 `~/.pi/agent/models.json`）：

   ```json
   {
     "providers": {
       "qoder": {
         "name": "Qoder",
         "baseUrl": "http://127.0.0.1:8765/v1",
         "api": "openai-completions",
         "compat": {
           "supportsDeveloperRole": true,
           "supportsReasoningEffort": false,
           "supportsUsageInStreaming": true,
           "maxTokensField": "max_tokens"
         },
         "models": [ { "id": "auto", "...": "..." } ]
       }
     }
   }
   ```

   同时在 provider 或对应模型上配置 Pi 侧的 `apiKey`，值为你的 Qoder PAT
   （Pi 会以 `Authorization: Bearer` 形式发给适配器）。

   > 片段里**不包含** PAT，需要自己填写。

4. 在 Pi 里选择 `qoder` provider 下的模型（如 `qoder:efficient`）正常对话。
   Pi 的工具会通过适配器暴露给 Qoder 模型，由 Pi 实际执行。

## 安全说明

- 适配器**强制只绑定回环地址**，无法监听 `0.0.0.0` 等对外地址。
- 日志与错误信息中出现的 token 会被替换为 `[redacted]`。
- 每个 Qoder 会话在独立的临时工作目录中运行；空闲会话由后台任务定期关闭并清理目录。
- 适配器不持久化任何 PAT 或对话内容。

## 项目结构

```text
.
├── main.py                       # 便捷入口：调用 pi_qoder_adapter.cli:main
├── pyproject.toml                # 项目元数据与依赖
├── docs/
│   └── usage.md                  # 面向使用者的详细使用文档
└── src/pi_qoder_adapter/
    ├── cli.py                    # 命令行：serve / print-config / probe
    ├── server.py                 # FastAPI 应用、路由、SSE 生产消费
    ├── bridge.py                 # 会话管理：一轮对话 ↔ 一个 Qoder 会话
    ├── qoder_runtime.py          # Qoder SDK 会话、MCP 工具注册与工具挂起
    ├── transcript.py             # Pi transcript 归一化与增量/reseed 判定
    ├── events.py                 # 内部语义事件类型（Semantic）
    ├── openai_sse.py             # 语义事件 → OpenAI chunk / completion 编码
    ├── catalog.py                # 内置模型目录与 Pi 配置片段
    └── probe.py                  # 工具挂起/交还的自检探针
```

## 常见问题

| 现象 | 原因 / 处理 |
|------|-------------|
| `401 Missing bearer token` | 请求未带 `Authorization: Bearer <PAT>`；检查 Pi 的 apiKey 配置 |
| `serve` 启动即退出并提示「拒绝监听」 | `--host` 不是回环地址，只能用 `127.0.0.1`/`localhost`/`::1` |
| `400 未知模型 xxx` | 模型 id 不在目录中；先带 token GET `/v1/models` 刷新，或使用返回的 id |
| 请求超时 | PAT 无效或 Qoder 服务不可达；先跑 `probe` 验证凭据与网络 |
| 工具调用没有真正执行 | 确认 Pi 已把工具结果以 `role="tool"` 回传；必要时重跑 `probe` |

## 开发

```bash
uv sync                       # 安装依赖
uv run pi-qoder-adapter --help
```

模块职责与调用关系见上文[项目结构](#项目结构)与[工作原理](#工作原理)。
提交前建议至少执行一次 `probe`，确认工具往返链路未被破坏。

## 许可证

[MIT](LICENSE)
