#!/usr/bin/env python3
"""MCP server that proxies read-only usage queries to an OpenClaw gateway."""

import asyncio
import json
import os
import uuid
from datetime import date
from urllib.parse import urlparse

import websockets
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# OpenClaw WebSocket client
# ---------------------------------------------------------------------------

class OpenClawClient:
    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
        self._ws = None
        self._pending: dict[str, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def _connect(self):
        parsed = urlparse(self.url)
        origin = f"http://{parsed.hostname}:{parsed.port or 80}"
        extra_headers = {"Origin": origin}
        self._ws = await websockets.connect(self.url, additional_headers=extra_headers)
        # Complete handshake before starting reader loop to avoid recv() races
        await self._auth_handshake()
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def _disconnect(self):
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def _ensure_connected(self):
        if self._ws is not None:
            return
        async with self._lock:
            if self._ws is not None:
                return
            await self._connect()

    async def _reader_loop(self):
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                if msg.get("type") == "res":
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                # events are silently discarded
        except websockets.ConnectionClosed:
            self._ws = None
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("WebSocket closed"))
            self._pending.clear()

    async def _auth_handshake(self):
        # Wait for connect.challenge event
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=5)
            msg = json.loads(raw)
            if msg.get("type") == "event" and msg.get("event") == "connect.challenge":
                break

        # Send connect request and read response directly (reader loop not yet running)
        req_id = str(uuid.uuid4())
        await self._ws.send(json.dumps({
            "type": "req", "id": req_id, "method": "connect",
            "params": {
                "minProtocol": 3, "maxProtocol": 3,
                "client": {"id": "webchat-ui", "version": "0.1", "platform": "linux", "mode": "webchat"},
                "role": "operator",
                "scopes": ["operator.admin", "operator.approvals", "operator.pairing"],
                "auth": {"token": self.token},
            },
        }))

        # Read until we get our connect response (skip interleaved events)
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=10)
            msg = json.loads(raw)
            if msg.get("type") == "res" and msg.get("id") == req_id:
                if not msg.get("ok"):
                    raise RuntimeError(f"Auth failed: {msg.get('error')}")
                return

    async def request(self, method: str, params: dict) -> dict:
        last_err = None
        for attempt in range(2):
            await self._ensure_connected()
            req_id = str(uuid.uuid4())
            fut = asyncio.get_event_loop().create_future()
            self._pending[req_id] = fut
            try:
                await self._ws.send(json.dumps({
                    "type": "req", "id": req_id, "method": method, "params": params,
                }))
                resp = await asyncio.wait_for(fut, timeout=30)
            except (ConnectionError, websockets.ConnectionClosed, asyncio.TimeoutError) as e:
                self._pending.pop(req_id, None)
                last_err = e
                await self._disconnect()
                continue
            if not resp.get("ok", False):
                raise RuntimeError(f"RPC {method} failed: {resp.get('error')}")
            return resp.get("payload", {})
        raise ConnectionError(f"Failed after reconnect: {last_err}")

    async def close(self):
        await self._disconnect()


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("openclaw-usage", log_level="WARNING")

_client: OpenClawClient | None = None

def _get_client() -> OpenClawClient:
    global _client
    if _client is None:
        url = os.environ.get("OPENCLAW_GATEWAY_URL", "ws://localhost:18789")
        token = os.environ.get("OPENCLAW_TOKEN")
        if not token:
            raise SystemExit("OPENCLAW_TOKEN env var is required")
        _client = OpenClawClient(url, token)
    return _client


@mcp.tool()
async def get_usage_summary(start_date: str = "", end_date: str = "") -> str:
    """Get usage & cost summary for a date range.

    Args:
        start_date: Start date (YYYY-MM-DD). Defaults to today.
        end_date: End date (YYYY-MM-DD). Defaults to today.
    """
    today = date.today().isoformat()
    start = start_date or today
    end = end_date or today
    client = _get_client()

    cost_data, sessions_data = await asyncio.gather(
        client.request("usage.cost", {"startDate": start, "endDate": end}),
        client.request("sessions.usage", {
            "startDate": start, "endDate": end,
            "limit": 200, "includeContextWeight": True,
        }),
    )

    return json.dumps({"cost": cost_data, "sessions": sessions_data}, indent=2)


@mcp.tool()
async def get_usage_timeseries(key: str) -> str:
    """Get usage over time for a specific session.

    Args:
        key: The session key (from get_usage_summary results).
    """
    client = _get_client()
    data = await client.request("sessions.usage.timeseries", {"key": key})
    return json.dumps(data, indent=2)


@mcp.tool()
async def get_usage_logs(key: str, limit: int = 100) -> str:
    """Get detailed per-request logs for a session.

    Args:
        key: The session key (from get_usage_summary results).
        limit: Max number of log entries to return (default 100).
    """
    client = _get_client()
    data = await client.request("sessions.usage.logs", {"key": key, "limit": limit})
    return json.dumps(data, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
