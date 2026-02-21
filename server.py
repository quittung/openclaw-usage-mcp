#!/usr/bin/env python3
"""MCP server that proxies read-only usage queries to an OpenClaw gateway."""

import asyncio
import json
import os
import uuid
from datetime import date, timedelta
from pathlib import Path

import websockets
from mcp.server.fastmcp import FastMCP

import device_auth

# ---------------------------------------------------------------------------
# OpenClaw WebSocket client
# ---------------------------------------------------------------------------

class OpenClawClient:
    def __init__(self, url: str, credentials_path: Path):
        self.url = url
        self.credentials_path = credentials_path
        self._creds: device_auth.DeviceCredentials | None = None
        self._ws = None
        self._pending: dict[str, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def _connect(self):
        self._ws = await device_auth.connect(self.url, self._creds)
        # Persist in case token was rotated during handshake
        device_auth.save_credentials(self._creds, self.credentials_path)
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
            if self._creds is None:
                self._creds = await device_auth.bootstrap(self.url, self.credentials_path)
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
# Period bucketing helpers
# ---------------------------------------------------------------------------

def _compute_buckets(start: date, end: date, period: str) -> list[tuple[date, date]]:
    """Return (bucket_start, bucket_end) pairs covering [start, end]."""
    buckets: list[tuple[date, date]] = []
    if period == "day":
        d = start
        while d <= end:
            buckets.append((d, d))
            d += timedelta(days=1)
    elif period == "week":
        d = start - timedelta(days=start.weekday())  # Monday of start's week
        while d <= end:
            b_start = max(d, start)
            b_end = min(d + timedelta(days=6), end)
            buckets.append((b_start, b_end))
            d += timedelta(days=7)
    elif period == "month":
        year, month = start.year, start.month
        while True:
            b_start = max(date(year, month, 1), start)
            if month == 12:
                next_first = date(year + 1, 1, 1)
            else:
                next_first = date(year, month + 1, 1)
            b_end = min(next_first - timedelta(days=1), end)
            if b_start > end:
                break
            buckets.append((b_start, b_end))
            year, month = next_first.year, next_first.month
    else:
        raise ValueError(f"Unknown period {period!r}. Use 'day', 'week', or 'month'.")
    return buckets


def _period_label(b_start: date, b_end: date, period: str) -> str:
    if period == "day":
        return b_start.strftime("%A, %b %-d %Y")
    elif period == "week":
        return f"Week of {b_start.strftime('%b %-d %Y')} – {b_end.strftime('%b %-d %Y')}"
    elif period == "month":
        return b_start.strftime("%B %Y")
    return f"{b_start} – {b_end}"


def _extract_model_usage(entry: dict) -> tuple[dict, dict, float]:
    """Extract (tokens_dict, cost_by_type_dict, total_cost) from a byModel entry.

    byModel entries have a nested 'totals' object with fields:
      input, output, cacheRead, cacheWrite,
      inputCost, outputCost, cacheReadCost, cacheWriteCost, totalCost
    """
    totals = entry.get("totals", entry)  # fall back to flat entry if no 'totals'
    tokens = {}
    for key in ("input", "output", "cacheRead", "cacheWrite"):
        val = totals.get(key)
        if val:
            tokens[key] = val
    cost_by_type = {}
    for key, field in [("input", "inputCost"), ("output", "outputCost"),
                        ("cacheRead", "cacheReadCost"), ("cacheWrite", "cacheWriteCost")]:
        val = totals.get(field)
        if val:
            cost_by_type[key] = val
    total_cost = totals.get("totalCost") or totals.get("cost") or 0.0
    return tokens, cost_by_type, total_cost


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("openclaw-usage", log_level="WARNING")

_client: OpenClawClient | None = None

def _get_client() -> OpenClawClient:
    global _client
    if _client is None:
        url = os.environ.get("OPENCLAW_GATEWAY_URL", "ws://localhost:18789")
        _client = OpenClawClient(url, device_auth.CREDENTIALS_PATH)
    return _client


@mcp.tool()
async def get_usage(
    start_date: str = "",
    end_date: str = "",
    period: str = "day",
) -> str:
    """Get usage & cost broken down by period with per-model detail.

    Makes one API call per period bucket to get full per-model token-type breakdown
    (input, output, cache read, cache write) and cost for each period.

    Args:
        start_date: Start date (YYYY-MM-DD). Defaults to 7 days ago.
        end_date: End date (YYYY-MM-DD). Defaults to today.
        period: Aggregation granularity — "day" (default), "week", or "month".
    """
    today = date.today()
    start = date.fromisoformat(start_date) if start_date else today - timedelta(days=6)
    end = date.fromisoformat(end_date) if end_date else today

    buckets = _compute_buckets(start, end, period)
    client = _get_client()

    result_periods = []
    for b_start, b_end in buckets:
        data = await client.request("sessions.usage", {
            "startDate": b_start.isoformat(),
            "endDate": b_end.isoformat(),
            "limit": 500,
        })
        by_model = data.get("aggregates", {}).get("byModel", [])

        totals_tokens: dict[str, int] = {}
        totals_cost_by_type: dict[str, float] = {}
        totals_cost = 0.0
        models_out = []

        for m in by_model:
            tokens, cost_by_type, cost = _extract_model_usage(m)
            totals_cost += cost
            for k, v in tokens.items():
                totals_tokens[k] = totals_tokens.get(k, 0) + v
            for k, v in cost_by_type.items():
                totals_cost_by_type[k] = totals_cost_by_type.get(k, 0.0) + v
            if tokens or cost:  # skip zero-usage entries
                models_out.append({
                    "model": m.get("model") or m.get("modelId", "unknown"),
                    "tokens": tokens,
                    "cost": {**{k: round(v, 6) for k, v in cost_by_type.items()},
                             "total": round(cost, 6)},
                })

        result_periods.append({
            "period": _period_label(b_start, b_end, period),
            "totals": {
                "tokens": totals_tokens,
                "cost": {**{k: round(v, 6) for k, v in totals_cost_by_type.items()},
                         "total": round(totals_cost, 6)},
            },
            "by_model": models_out,
        })

    return json.dumps(result_periods, indent=2)


@mcp.tool()
async def get_usage_timeseries(key: str) -> str:
    """Get usage over time for a specific session.

    Args:
        key: The session key (visible in gateway UI or get_usage model entries).
    """
    client = _get_client()
    data = await client.request("sessions.usage.timeseries", {"key": key})
    return json.dumps(data, indent=2)


@mcp.tool()
async def get_usage_logs(key: str, limit: int = 50) -> str:
    """Get per-request logs for a session: timestamps, token counts, costs, roles.
    Message content is excluded.

    Args:
        key: The session key (visible in gateway UI or get_usage model entries).
        limit: Max log entries to return (default 50).
    """
    client = _get_client()
    data = await client.request("sessions.usage.logs", {"key": key, "limit": limit})

    # Strip any message content fields — we only want metadata
    _CONTENT_KEYS = frozenset({"content", "messages", "text", "input", "output",
                                "prompt", "completion", "body"})
    logs = data.get("logs", data) if isinstance(data, dict) else data
    clean = [
        {k: v for k, v in entry.items() if k not in _CONTENT_KEYS}
        for entry in (logs if isinstance(logs, list) else [])
    ]
    return json.dumps(clean, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
