# openclaw-usage-mcp

MCP server that exposes read-only usage and cost data from an [OpenClaw](https://github.com/openClaw) gateway. Lets AI agents query API usage without seeing gateway credentials.

```
AI Agent  ──stdio──▶  MCP Server  ──websocket──▶  OpenClaw Gateway
                      (own device identity, operator.read scope)
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
pip install cryptography mcp websockets
```

### 2. Configure

The server reads these environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENCLAW_GATEWAY_URL` | `ws://localhost:18789` | WebSocket URL of the OpenClaw gateway |
| `OPENCLAW_USAGE_MCP_CREDENTIALS` | `~/.config/openclaw-usage-mcp/device.json` | Path to store the device credentials |
| `OPENCLAW_GATEWAY_TOKEN` | *(reads openclaw.json)* | Override the gateway shared auth token used for first-run pairing |

No token configuration is required for normal use. On first run the server reads
`gateway.auth.token` from `~/.openclaw/openclaw.json` to authenticate the initial
pairing handshake, then stores its own device token for all subsequent connections.

### 3. Register with your MCP client

**mcporter** (recommended with OpenClaw):

```bash
mcporter config add usage \
  --command python3 \
  --arg /path/to/server.py \
  --env OPENCLAW_GATEWAY_URL=ws://localhost:18789 \
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
        "OPENCLAW_GATEWAY_URL": "ws://localhost:18789"
      }
    }
  }
}
```

## First-run pairing

On first run the server generates an Ed25519 device identity and pairs with
the gateway automatically. No manual steps needed:

```
[device_auth] No credentials at ~/.config/openclaw-usage-mcp/device.json. Pairing with gateway...
[device_auth] Paired successfully. Device ID: b071fc07...
```

Credentials are stored at `~/.config/openclaw-usage-mcp/device.json` and
reused on all subsequent runs. The device appears in `openclaw devices list`
as `gateway-client` with `operator.read` scope only.

## Running standalone

```bash
python3 server.py
```

Communicates over stdio using the MCP protocol.

## How it works (and why)

OpenClaw's gateway exposes a WebSocket RPC protocol — the same one all
first-party clients (CLI, web UI, mobile apps) use. This server pairs as a
named device (`gateway-client/backend`) with `operator.read` scope only, then
calls the `usage.cost` and `sessions.usage` RPC methods.

There are simpler alternatives (`openclaw sessions --json`, the HTTP
`/tools/invoke` endpoint), but they only expose token counts. The WebSocket
RPC is the only way to get dollar cost breakdowns, per-model usage, daily
aggregations, and the other rich data the tools here return.

That said, OpenClaw moves fast — this approach may well be superseded by a
proper REST API or dedicated MCP integration before long.

## Protocol notes (undocumented as of 2026.2.x)

These details were reverse-engineered from the gateway source since they are
not in the official docs. They apply to the WebSocket connect handshake.

**Two auth layers, both required on every connect:**

1. *Auth token* — Either the gateway's shared auth token (`gateway.auth.token`
   in openclaw.json) for first-time pairing, or a device-specific token issued
   after pairing. Provided in `params.auth.token`.

2. *Ed25519 device signature* — Required for all connections, including local
   loopback (only the nonce is skipped for loopback, not the signature). The
   payload signed is:
   ```
   v1|{deviceId}|{clientId}|{clientMode}|{role}|{scopes,joined}|{signedAtMs}|{authToken}
   ```
   The auth token is baked into the signed payload, binding the keypair to the
   credential. A fresh timestamp is used on every connect, so there's no static
   value to replay.

**First-time pairing flow (loopback only):**

For loopback connections from an unknown device, the gateway auto-pairs
silently and inline during the connect handler — it creates and immediately
approves the pairing request before responding. The `hello-ok` response
includes the issued device token in `payload.auth.deviceToken`. There is no
pending-approval queue and no race window.

**Client ID registry:**

`clientId` must be one of the gateway's registered values. The relevant ones
for programmatic clients are:
- `gateway-client` — backend/programmatic clients (mode: `backend`)
- `cli` — the OpenClaw CLI (mode: `cli`)
- `openclaw-control-ui` — the web control UI (mode: `ui` or `webchat`)

Using an unregistered clientId fails schema validation at connect time.

**Device ID derivation:**

```
deviceId = SHA-256(raw_ed25519_public_key_bytes).hex()
```

## Limitations

- **Scopes**: The device is paired with `operator.read` only — the minimum required for usage/cost RPC calls.
- **No WebSocket keepalive**: The connection stays open between tool calls with no periodic ping. Long-lived MCP server processes may hit silent connection staleness. The auto-reconnect handles it reactively (retries once on failure), but a proactive ping would be more robust.
