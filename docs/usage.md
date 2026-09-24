# pi-qoder-adapter 使用文档

一个只监听本机回环地址的 OpenAI 兼容适配器，把 **Pi**（coding agent）桥接到 **Qoder Agent SDK**。
Pi 把它当作一个普通的 OpenAI provider 使用；它在背后通过 Qoder SDK 启动会话，
把 Pi 的工具以 MCP 工具的形式暴露给 Qoder 模型，并把模型的输出转换回 OpenAI
chat completion（含 SSE 流式）格式。

## 环境要求

- Python ≥ 3.12（推荐用 [uv](https://docs.astral.sh/uv/) 管理）
- 依赖：`fastapi`、`uvicorn`、`qoder-agent-sdk`
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
cd pi-qoder-adpter
uv sync            # 或者: python -m venv .venv && .venv/bin/pip install -e .
```

之后可以用 `uv run pi-qoder-adapter ...`，或直接调用 `.venv/bin/pi-qoder-adapter`
（Windows 为 `.venv\Scripts\pi-qoder-adapter.exe`），也可以 `python main.py ...`。

## 命令一览

```text
pi-qoder-adapter serve          启动只监听本机的 OpenAI 兼容接口
pi-qoder-adapter print-config   打印要合并进 Pi models.json 的片段
pi-qoder-adapter probe          验证 Qoder 工具可以挂起并交还给调用方
```

### 1. `serve` — 启动适配器

```bash
uv run pi-qoder-adapter serve --host 127.0.0.1 --port 8765
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | `127.0.0.1` | 只允许 `127.0.0.1` / `localhost` / `::1`，其他值直接拒绝启动 |
| `--port` | `8765` | 监听端口 |

启动后提供以下 HTTP 接口：

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

### 2. `print-config` — 生成 Pi 配置片段

```bash
uv run pi-qoder-adapter print-config --port 8765
```

输出一个 JSON 片段，把其中的 `providers.qoder` 合并进 Pi 的 `models.json`
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
      "models": [ { "id": "auto", ... }, { "id": "ultimate", ... }, ... ]
    }
  }
}
```

同时在该 provider 或对应模型上配置 Pi 侧的 `apiKey`，值为你的 Qoder PAT
（Pi 会以 `Authorization: Bearer` 形式发给适配器）。

> 片段里**不包含** PAT，需要自己填写。

### 3. `probe` — 连通性自检

```bash
uv run pi-qoder-adapter probe --model efficient --wait-seconds 60
```

在临时目录里跑一次真实的 Qoder 会话，注册一个 `ping` MCP 工具并等待模型调用它，
用来验证「Qoder 工具可以挂起并把控制权交还给调用方」。需要已设置
`QODER_PERSONAL_ACCESS_TOKEN`。退出码 0 表示成功。

## 默认模型

未从 Qoder 服务刷新时，内置目录包含 5 个模型 id（均按 200k 上下文 / 32k 输出、
零成本声明）：

| id | 说明 |
|----|------|
| `auto` | Qoder Auto |
| `ultimate` | Qoder Ultimate |
| `performance` | Qoder Performance |
| `efficient` | Qoder Efficient |
| `lite` | Qoder Lite |

启动 `serve` 时若设置了 PAT，会尝试从 Qoder 拉取真实模型目录（结果缓存 10 分钟）；
`/v1/models` 带 Bearer token 也会触发刷新。请求了未知模型 id 时，若目录已从服务
加载过，会返回 400 并列出可用 id。

## 在 Pi 中使用（端到端流程）

1. 设置环境变量 `QODER_PERSONAL_ACCESS_TOKEN`。
2. 启动适配器：`uv run pi-qoder-adapter serve`（保持前台运行）。
3. `uv run pi-qoder-adapter print-config` 的输出合并进 Pi 的 `models.json`，
   并把 PAT 配置为 Pi 侧的 apiKey。
4. 在 Pi 里选择 `qoder` provider 下的模型（如 `qoder:efficient`）正常对话。
   Pi 的工具（读写文件、执行命令等）会通过适配器暴露给 Qoder 模型，由 Pi 实际执行。

## 安全说明

- 适配器**强制只绑定回环地址**，不会暴露到局域网/公网。
- 日志与错误信息中出现的 token 会被替换为 `[redacted]`。
- 每个 Qoder 会话在独立的临时工作目录中运行；空闲会话由后台任务定期关闭。

## 常见问题

| 现象 | 原因 / 处理 |
|------|-------------|
| `401 Missing bearer token` | 请求未带 `Authorization: Bearer <PAT>`；检查 Pi 的 apiKey 配置 |
| `serve` 启动即退出并提示「拒绝监听」 | `--host` 不是回环地址，只能用 127.0.0.1/localhost/::1 |
| `400 未知模型 xxx` | 模型 id 不在目录中；先 GET `/v1/models`（带 token）刷新，或使用返回的 id |
| 请求超时 | PAT 无效或 Qoder 服务不可达；先跑 `probe` 验证凭据与网络 |
