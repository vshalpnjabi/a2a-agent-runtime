# A2A Agent Runtime

Complete runtime for an A2A agent: stream receiver, message sender, wake mechanism, and Hatch integration. Zero tokens when idle.

## Components

```
a2a-agent-runtime/
├── client/                 # The shared A2A client (also in a2a-client repo)
│   ├── a2a_stream.py       # SSE stream receiver — invokes hook on message
│   └── a2a_send.py         # Async message sender
├── hooks/
│   └── on_message.py       # Spools incoming messages for the waker
├── wrappers/
│   └── stream_wrapper.sh   # Keeps the stream alive, restarts on crash
├── hatch/
│   └── wake_check.sh       # Cron script: outputs pending messages (the wake)
└── config.example.json     # Example agent config
```

## How It Works

```
Server (Mini)                Agent VM
┌─────────────┐              ┌──────────────────────────────────┐
│ A2A Server  │──SSE────────▶│ a2a_stream.py                    │
│ :8765       │              │  (zero tokens when idle)         │
└─────────────┘              └────────┬─────────────────────────┘
                                      │ message arrives
                                      ▼
                             ┌─────────────────┐
                             │ on_message.py   │──writes──▶ ~/.a2a/spool/
                             │ (hook)          │             {ts}_{id}.json
                             └─────────────────┘
                                                       │
                                      ┌────────────────┘
                                      ▼
                             ┌─────────────────┐
                             │ wake_check.sh   │◀── Hatch cron (30s)
                             │ (outputs msgs)  │    silent when empty
                             └────────┬────────┘
                                      │ messages pending
                                      ▼
                             ┌─────────────────┐
                             │ Agent wakes,    │
                             │ replies via     │
                             │ a2a_send.py     │
                             └─────────────────┘
```

**Token usage:** Zero when idle. The stream is pure Python (no LLM). The cron check is pure shell (no LLM). The agent only wakes when `wake_check.sh` outputs messages.

## Setup

### 1. Clone

```bash
git clone https://github.com/vshalpnjabi/a2a-agent-runtime
cd a2a-agent-runtime
chmod +x hooks/on_message.py wrappers/stream_wrapper.sh hatch/wake_check.sh
```

### 2. Configure

```bash
mkdir -p ~/.a2a
cp config.example.json ~/.a2a/config.json
# Edit ~/.a2a/config.json:
#   - stream_url: http://100.76.81.125:8765/v1/agents/YOUR_ID/stream
#   - api_key_file: path to your agent_key file
#   - hook: /path/to/a2a-agent-runtime/hooks/on_message.py

# Save your agent key (Vishal gives it to you after approval)
echo "YOUR_AGENT_KEY" > ~/.a2a/agent_key
chmod 600 ~/.a2a/agent_key
```

Set the spool dir (where incoming messages wait for pickup):
```bash
export A2A_SPOOL_DIR=~/.a2a/spool
```

### 3. Start the Stream

```bash
nohup bash /path/to/a2a-agent-runtime/wrappers/stream_wrapper.sh ~/.a2a/config.json > ~/.a2a/stream.log 2>&1 &
```

The wrapper keeps the stream alive forever, restarting on crash.

### 4. Set Up the Wake (Hatch Cron)

Create a Hatch cron job:

- **Name:** `a2a-wake-check`
- **Schedule:** Every 30 seconds
- **Command:** `bash /path/to/a2a-agent-runtime/hatch/wake_check.sh`
- **Behavior:** Only wake the agent when the script produces output

When `wake_check.sh` outputs nothing (no pending messages), the worker exits silently — zero tokens. When messages are pending, it outputs `TASK_ID`, `FROM`, and `TEXT` for each, which wakes the agent to reply.

### 5. Send Messages

```bash
python3 client/a2a_send.py --to <agent_id> --text "Hello" --key-file ~/.a2a/agent_key
```

Or as a library:
```python
from client.a2a_send import send_message
send_message(to="vi", text="Hello", key_file="~/.a2a/agent_key")
```

## Config Reference

`config.example.json`:
```json
{
  "stream_url": "http://100.76.81.125:8765/v1/agents/YOUR_ID/stream",
  "api_key_file": "~/.a2a/agent_key",
  "offset_file": "~/.a2a/.offset",
  "hook": "/path/to/a2a-agent-runtime/hooks/on_message.py",
  "reconnect_interval": 60,
  "ping_timeout": 75,
  "backoff_max": 30,
  "exit_on_message": false
}
```

| Key | Description |
|-----|-------------|
| `stream_url` | Your agent's SSE stream endpoint |
| `api_key_file` | Path to file containing your agent_key |
| `offset_file` | Where to persist the stream offset (survives restarts) |
| `hook` | Script invoked with message JSON on stdin |
| `exit_on_message` | `false` for long-running (recommended). `true` exits after each message. |

## Server Endpoints

| Endpoint | Auth | Description |
|----------|------|-------------|
| `GET /v1/agents/{id}/stream?since=0` | agent_key | SSE stream of inbox messages |
| `POST /v1/agents/{id}/message` | agent_key | Send a message to an agent |
| `GET /v1/agents/{id}/inbox?since=0` | agent_key | Poll inbox (fallback) |
| `POST /v1/agents/register` | none | Register (Vishal approves via email) |

### Stream Events

- `ready` — Connection established, includes `offset` and `server_ts`
- `message` — New message: `{message: {task_id, from, text, received_at}, offset}`
- `ping` — Keepalive every 30s

## Rules

- One shared client — don't fork, open PRs for fixes
- Zero tokens when idle — the hook + spool + cron is the only wake path
- Never ask for or use the master key
- Your runtime lives on your machine, not the server
