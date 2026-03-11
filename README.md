# AnyClaw

By wrapping up claude code(claude -p) and standing on the shoulders of giants, you can use any model with [cc-switch](https://github.com/farion1231/cc-switch).


# Claude CLI — Local API Wrapper

> Wrap any LLM client around `claude -p`

---

## Architecture

```mermaid
flowchart TD
    A["🦞  Any LLM Client
    ─────────────────────────────
    CherryStudio · OpenAI SDK
    curl · Your App"]

    B["🖥️  Local API Server
    ─────────────────────────────
    http://127.0.0.1:8080
    server.py"]

    C["⚡  claude -p
    ─────────────────────────────
    Claude Code CLI"]

    D["🔀  cc-switch
    ─────────────────────────────
    Model Switcher"]

    E["☁️  Anthropic API
    ─────────────────────────────
    api.anthropic.com"]

    A -->|"POST /v1/chat/completions
    POST /v1/messages
    OpenAI · Anthropic format"| B

    B -->|"bash: source venv/bin/activate
    && claude -p '...'
    + HTTP_PROXY injected"| C

    D -.->|"switch active model
    no restart needed"| C

    C -->|"HTTPS
    via proxy 127.0.0.1:7890"| E
```

---

## Request Flow

```mermaid
sequenceDiagram
    participant Client as 🦞 LLM Client
    participant API    as 🖥️ Local API Server
    participant Shell  as 🐚 bash subprocess
    participant CLI    as ⚡ claude -p
    participant Remote as ☁️ Anthropic API

    Client->>API: POST /v1/chat/completions<br/>(stream: true/false)
    API->>API: Parse format (OpenAI / Anthropic)
    API->>API: Build plain-text prompt
    API->>API: Acquire Semaphore slot
    API->>Shell: source venv/bin/activate<br/>&& claude -p '...'
    Shell->>CLI: exec with proxy env vars
    CLI->>Remote: HTTPS request
    Remote-->>CLI: response
    CLI-->>Shell: stdout
    Shell-->>API: captured output
    API->>API: Release Semaphore slot
    alt stream = true
        API-->>Client: SSE chunks (text/event-stream)
    else stream = false
        API-->>Client: JSON response
    end
```

---

## Concurrency Model

```mermaid
flowchart LR
    R1[Request 1] --> T1[Thread 1]
    R2[Request 2] --> T2[Thread 2]
    R3[Request 3] --> T3[Thread 3]
    R4[Request 4] --> T4[Thread 4]
    RN[Request N] --> TN[Thread N]

    subgraph HTTP ["HTTP Layer — ThreadingMixIn (unlimited threads)"]
        T1
        T2
        T3
        T4
        TN
    end

    subgraph SEM ["Semaphore(N) — max N claude processes"]
        T1 --> P1[claude -p]
        T2 --> P2[claude -p]
        T3 --> WAIT1[queued ...]
        T4 --> WAIT2[queued ...]
        TN --> WAITN[queued ...]
    end

    P1 --> ANT[Anthropic API]
    P2 --> ANT
```

---

## Model Switching with `cc-switch`

```mermaid
flowchart LR
    CC["cc-switch"] -->|select| M1["claude-opus-4"]
    CC -->|select| M2["claude-sonnet-4  ✅ active"]
    CC -->|select| M3["claude-3-5-sonnet"]
    CC -->|select| M4["claude-haiku-4-5"]

    M2 -->|used by all subsequent| CLI["claude -p '...'"]
```

> Switch once → every following `claude -p` call uses the new model automatically. No server restart required.

---


# Claude Local API Server

A local API server that wraps the `claude -p` CLI, exposing both **OpenAI-compatible** and **Anthropic-compatible** HTTP endpoints. Designed to work with clients like [CherryStudio](https://github.com/kangfenmao/cherry-studio), `curl`, or any OpenAI SDK.

## Features

- **OpenAI format** — `POST /v1/chat/completions` (streaming + non-streaming)
- **Anthropic format** — `POST /v1/messages` (streaming + non-streaming)
- **Model list** — `GET /v1/models`
- **Stats** — `GET /v1/stats`
- **Health check** — `GET /health`
- Concurrent requests via `ThreadingMixIn` + `Semaphore` queue
- Automatic proxy injection (`http_proxy`, `https_proxy`, `all_proxy`)
- Optional virtualenv activation before each `claude` call
- Graceful forced exit on `Ctrl+C` / `SIGTERM`
- Zero third-party dependencies (Python stdlib only)

## Requirements

- Python 3.10+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated

```bash
npm install -g @anthropic-ai/claude-code
claude --version   # verify
```

## Usage

```bash
# Default: 127.0.0.1:8080, proxy=127.0.0.1:7890, venv=nano_env
python server.py

# Custom options
python server.py --host 0.0.0.0 --port 11434 --workers 4 --timeout 180

# Disable proxy
python server.py --proxy ""

# Disable virtualenv
python server.py --venv ""

# Custom virtualenv path
python server.py --venv /home/user/myenv
```

### All Arguments

| Argument | Default | Description |
|----------|---------|-------------|
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8080` | Bind port |
| `--timeout` | `120` | Per-request claude timeout (seconds) |
| `--workers` | `8` | Max concurrent claude processes |
| `--proxy` | `http://127.0.0.1:7890` | Proxy URL, empty string to disable |
| `--venv` | `nano_env` | Virtualenv path, empty string to disable |

## Endpoints

### `POST /v1/chat/completions` — OpenAI format

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-local",
    "stream": false,
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user",   "content": "What is recursion?"}
    ]
  }'
```

### `POST /v1/messages` — Anthropic format

```bash
curl http://127.0.0.1:8080/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-local",
    "system": "You are a helpful assistant.",
    "messages": [{"role": "user", "content": "What is recursion?"}]
  }'
```

### `GET /v1/models`

```bash
curl http://127.0.0.1:8080/v1/models
```

### `GET /v1/stats`

```bash
curl http://127.0.0.1:8080/v1/stats
# {"max_workers": 8, "total": 42, "active": 2, "errors": 0, "queued": 1}
```

### `GET /health`

```bash
curl http://127.0.0.1:8080/health
```

## CherryStudio Setup

1. Go to **Settings → Model Providers → Add**
2. Set **Type** to `OpenAI Compatible`
3. Set **API URL** to `http://127.0.0.1:8080`
4. Set **API Key** to any string (e.g. `sk-local`) — not validated
5. Click **Get Models** or manually enter `claude-local`
6. Click **Test Connection** — should show ✅

## Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-local",
    base_url="http://127.0.0.1:8080/v1"
)

resp = client.chat.completions.create(
    model="claude-local",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(resp.choices[0].message.content)
```

## Architecture

```
HTTP Request
    │
    ▼
ThreadedHTTPServer  (one thread per request)
    │
    ▼
Semaphore  (queue when > max workers)
    │
    ▼
/bin/bash -c "source <venv>/bin/activate && claude -p '<prompt>'"
             + proxy env vars injected
```

## License

MIT
