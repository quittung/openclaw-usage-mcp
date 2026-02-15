# Known Issues & Limitations

## Broad scopes

We request `operator.admin`, `operator.approvals`, and `operator.pairing` scopes even though we only need read access to usage data. Narrower scopes were rejected during testing. Worth revisiting if OpenClaw introduces a dedicated `usage.read` scope.

## No WebSocket keepalive

The connection stays open between tool calls with no periodic ping. Long-lived MCP server processes may hit silent connection staleness. The auto-reconnect handles this reactively (retries once on failure), but a proactive ping would be more robust.
