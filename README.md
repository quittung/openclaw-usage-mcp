# openclaw-usage-mcp

MCP server that reads local [OpenClaw](https://github.com/openClaw) session files for usage and cost data. No network calls or authentication required — it scans the JSONL session files directly from disk.

```
AI Agent  ──stdio──▶  MCP Server  ──reads──▶  ~/.openclaw/agents/*/sessions/*.jsonl
```

## Tools

| Tool | Description |
|------|-------------|
| `get_usage` | Token counts and costs for a date range, broken down by period and model |
| `list_sessions` | Sessions with usage breakdown, grouped by period (top-N or all) |
| `get_session_logs` | Per-message logs for a session: timestamps, token counts, costs, roles |

## Setup

### 1. Install dependencies

```bash
pip install mcp
```

### 2. Configure

The server reads these environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENCLAW_AGENTS_DIR` | `~/.openclaw/agents/` | Path to the OpenClaw agents directory containing session files |

### 3. Register with your MCP client

**mcporter** (recommended with OpenClaw):

```bash
mcporter config add usage \
  --command python3 \
  --arg /path/to/server.py \
  --scope home
```

**Claude Code** (`~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "openclaw-usage": {
      "command": "python3",
      "args": ["/path/to/server.py"]
    }
  }
}
```

## How it works

The server scans JSONL session files from the OpenClaw agents directory:

- **Active sessions:** `~/.openclaw/agents/*/sessions/*.jsonl`
- **Archived (reset) sessions:** `~/.openclaw/agents/*/sessions/*.jsonl.reset.*`

Each session file contains JSON lines with `type: "session"` headers, `type: "message"` entries (with usage/cost data on assistant messages), and `type: "model_change"` entries for model switches.

The server aggregates token counts and costs per model, per session, and per time period — returning the same structured output format the tools have always used.

Archived `.reset.*` files are included in scans, so usage from reset sessions is no longer invisible.

## Running standalone

```bash
python3 server.py
```

Communicates over stdio using the MCP protocol.

## Previous architecture

Earlier versions used the gateway's WebSocket RPC API with Ed25519 device
authentication. That code (including `device_auth.py`) is preserved at tag
[`v0.1.0-gateway-api`](../../tree/v0.1.0-gateway-api).
