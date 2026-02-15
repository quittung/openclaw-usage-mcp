# openclaw-usage-mcp

MCP server that exposes read-only usage and cost data from an [OpenClaw](https://github.com/openClaw) gateway. Lets AI agents query API usage without seeing gateway credentials.

```
AI Agent  ──stdio──▶  MCP Server  ──websocket──▶  OpenClaw Gateway
                      (holds token)
```

## Tools

| Tool | Description |
|------|-------------|
| `get_usage_summary` | Token counts and costs for a date range, with per-session and per-model breakdown |
| `get_usage_timeseries` | Usage over time for a specific session |
| `get_usage_logs` | Per-request logs for a session |

## Setup

### 1. Install dependencies

```bash
pip install mcp websockets
```

### 2. Configure

The server reads two environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENCLAW_GATEWAY_URL` | `ws://localhost:18789` | WebSocket URL of the OpenClaw gateway |
| `OPENCLAW_TOKEN` | *(required)* | Gateway operator token |

### 3. Register with your MCP client

**mcporter** (recommended with OpenClaw):

```bash
mcporter config add usage \
  --command python3 \
  --arg /path/to/server.py \
  --env OPENCLAW_GATEWAY_URL=ws://localhost:18789 \
  --env OPENCLAW_TOKEN=your-token-here \
  --scope home
```

**Claude Code** (`~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "openclaw-usage": {
      "command": "python3",
      "args": ["/path/to/server.py"],
      "env": {
        "OPENCLAW_GATEWAY_URL": "ws://localhost:18789",
        "OPENCLAW_TOKEN": "your-token-here"
      }
    }
  }
}
```

The token stays in the MCP server config — the AI agent only sees the tool interfaces, never the credentials.

## Running standalone

```bash
OPENCLAW_TOKEN=your-token python3 server.py
```

Communicates over stdio using the MCP protocol.

## How it works (and why)

OpenClaw's gateway exposes a WebSocket RPC protocol — the same one all first-party clients (CLI, web UI, mobile apps) use. This server connects as a `cli` client with an operator token and calls the `usage.cost` and `sessions.usage` RPC methods.

There are simpler alternatives (`openclaw sessions --json`, the HTTP `/tools/invoke` endpoint), but they only expose token counts. The WebSocket RPC is the only way to get dollar cost breakdowns, per-model usage, daily aggregations, and the other rich data the tools here return.

That said, OpenClaw moves fast — this approach may well be superseded by a proper REST API or dedicated MCP integration before long.

## Limitations

- **Broad scopes**: We request `operator.admin`, `operator.approvals`, and `operator.pairing` scopes even though we only need usage read access. Narrower scopes were rejected during testing. Worth revisiting if OpenClaw adds a dedicated `usage.read` scope.
- **No WebSocket keepalive**: The connection stays open between tool calls with no periodic ping. Long-lived MCP server processes may hit silent connection staleness. The auto-reconnect handles it reactively (retries once on failure), but a proactive ping would be more robust.
