#!/usr/bin/env python3
"""MCP server that reads OpenClaw session files locally for usage/cost data."""

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Session scanner — reads JSONL session files directly
# ---------------------------------------------------------------------------

AGENTS_DIR = Path(os.environ.get("OPENCLAW_AGENTS_DIR", Path.home() / ".openclaw" / "agents"))


def _session_files(agents_dir: Path) -> list[Path]:
    """Find all session JSONL files (active and archived/reset)."""
    files = []
    for sessions_dir in agents_dir.glob("*/sessions"):
        for f in sessions_dir.iterdir():
            if f.name.endswith(".jsonl") or ".jsonl.reset." in f.name:
                files.append(f)
    return files


def _parse_session_key(path: Path) -> str:
    """Derive a session key from the file path.

    Format: agent:<agent_id>:<session_uuid>
    For archived files like abc.jsonl.reset.2026-..., strip the .reset.* suffix.
    """
    agent_id = path.parent.parent.name
    name = path.name
    # Strip .reset.* suffix for archived files
    idx = name.find(".jsonl.reset.")
    if idx >= 0:
        session_id = name[:idx]
    elif name.endswith(".jsonl"):
        session_id = name[:-6]
    else:
        session_id = name
    return f"agent:{agent_id}:{session_id}"


def _file_session_id(path: Path) -> str:
    """Extract just the session UUID from a file path."""
    name = path.name
    idx = name.find(".jsonl.reset.")
    if idx >= 0:
        return name[:idx]
    if name.endswith(".jsonl"):
        return name[:-6]
    return name


def _parse_session_file(path: Path) -> dict | None:
    """Parse a session JSONL file and return aggregated usage data.

    Returns a dict with: key, session_id, agent_id, start_time, model,
    messages (count), tokens, cost, model_usage (per-model breakdown),
    log_entries (list of per-message metadata).
    Returns None if the file can't be parsed or has no session header.
    """
    tokens: dict[str, int] = {}
    cost_by_type: dict[str, float] = {}
    total_cost = 0.0
    message_count = 0
    model_usage: dict[str, dict] = {}  # model -> {tokens: {}, cost_by_type: {}, total_cost: float, messages: int}
    log_entries: list[dict] = []
    session_id = None
    start_time = None
    current_model = None
    agent_id = path.parent.parent.name

    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = entry.get("type")

                if etype == "session":
                    session_id = entry.get("id")
                    ts_str = entry.get("timestamp")
                    if ts_str:
                        try:
                            start_time = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        except ValueError:
                            pass

                elif etype == "model_change":
                    current_model = entry.get("modelId")

                elif etype == "message":
                    msg = entry.get("message", {})
                    role = msg.get("role")
                    ts_str = entry.get("timestamp") or msg.get("timestamp")
                    msg_ts = None
                    if isinstance(ts_str, str):
                        try:
                            msg_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        except ValueError:
                            pass
                    elif isinstance(ts_str, (int, float)):
                        msg_ts = datetime.fromtimestamp(ts_str / 1000, tz=timezone.utc)

                    if role == "assistant":
                        usage = msg.get("usage", {})
                        model = msg.get("model") or current_model or "unknown"
                        msg_cost = usage.get("cost", {})
                        message_count += 1

                        # Accumulate tokens
                        for key in ("input", "output", "cacheRead", "cacheWrite"):
                            val = usage.get(key, 0)
                            if val:
                                tokens[key] = tokens.get(key, 0) + val

                        # Accumulate cost
                        for key in ("input", "output", "cacheRead", "cacheWrite"):
                            val = msg_cost.get(key, 0)
                            if val:
                                cost_by_type[key] = cost_by_type.get(key, 0.0) + val
                        entry_total_cost = msg_cost.get("total", 0.0)
                        total_cost += entry_total_cost

                        # Per-model accumulation
                        if model not in model_usage:
                            model_usage[model] = {"tokens": {}, "cost_by_type": {}, "total_cost": 0.0, "messages": 0}
                        mu = model_usage[model]
                        mu["messages"] += 1
                        for key in ("input", "output", "cacheRead", "cacheWrite"):
                            val = usage.get(key, 0)
                            if val:
                                mu["tokens"][key] = mu["tokens"].get(key, 0) + val
                        for key in ("input", "output", "cacheRead", "cacheWrite"):
                            val = msg_cost.get(key, 0)
                            if val:
                                mu["cost_by_type"][key] = mu["cost_by_type"].get(key, 0.0) + val
                        mu["total_cost"] += entry_total_cost

                        # Log entry for get_session_logs
                        log_entry: dict = {"timestamp": ts_str, "role": role, "model": model}
                        if usage:
                            log_tokens = {}
                            for key in ("input", "output", "cacheRead", "cacheWrite", "totalTokens"):
                                val = usage.get(key, 0)
                                if val:
                                    log_tokens[key] = val
                            if log_tokens:
                                log_entry["tokens"] = log_tokens
                            if msg_cost:
                                log_entry["cost"] = msg_cost
                        log_entries.append(log_entry)

                    elif role == "user":
                        log_entries.append({"timestamp": ts_str, "role": role})

    except (OSError, UnicodeDecodeError):
        return None

    if session_id is None:
        # Not a valid session file
        return None

    key = f"agent:{agent_id}:{session_id}"
    return {
        "key": key,
        "session_id": session_id,
        "agent_id": agent_id,
        "start_time": start_time,
        "model": next(iter(model_usage), "unknown"),  # first model used
        "messages": message_count,
        "tokens": tokens,
        "cost_by_type": cost_by_type,
        "total_cost": total_cost,
        "model_usage": model_usage,
        "log_entries": log_entries,
    }


def _scan_sessions(start: date, end: date) -> list[dict]:
    """Scan session files and return parsed sessions within the date range."""
    agents_dir = AGENTS_DIR
    if not agents_dir.exists():
        return []

    # Convert date range to datetime range (UTC)
    range_start = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    range_end = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)

    files = _session_files(agents_dir)
    sessions = []

    for path in files:
        # Quick pre-filter: skip files last modified before the start of the range
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime.date() < start:
            continue

        parsed = _parse_session_file(path)
        if parsed is None:
            continue

        # Filter by session start time
        st = parsed.get("start_time")
        if st is None:
            continue
        if st < range_start or st > range_end:
            continue

        sessions.append(parsed)

    return sessions


def _find_session_file(key: str) -> Path | None:
    """Find a session file by its key (agent:<agent_id>:<session_id>)."""
    parts = key.split(":")
    if len(parts) != 3 or parts[0] != "agent":
        return None
    agent_id, session_id = parts[1], parts[2]

    sessions_dir = AGENTS_DIR / agent_id / "sessions"
    if not sessions_dir.exists():
        return None

    # Check active file first, then archived
    active = sessions_dir / f"{session_id}.jsonl"
    if active.exists():
        return active

    # Look for archived files
    for f in sessions_dir.iterdir():
        if f.name.startswith(f"{session_id}.jsonl.reset."):
            return f

    return None


# ---------------------------------------------------------------------------
# Period bucketing helpers (kept from original)
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


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

def _build_session_entry(session: dict) -> dict:
    """Build a compact session summary from a parsed session dict."""
    tokens = session["tokens"]
    cost_by_type = session["cost_by_type"]
    total_cost = session["total_cost"]

    models_out = []
    for model, mu in session["model_usage"].items():
        m_tokens = mu["tokens"]
        m_cost_by_type = mu["cost_by_type"]
        m_cost = mu["total_cost"]
        if m_tokens or m_cost:
            models_out.append({
                "model": model,
                "tokens": m_tokens,
                "cost": {**{k: round(v, 6) for k, v in m_cost_by_type.items()},
                         "total": round(m_cost, 6)},
            })

    entry: dict = {
        "key": session["key"],
        "model": session["model"],
        "channel": "",
    }
    if session["messages"]:
        entry["messages"] = session["messages"]
    entry["tokens"] = tokens
    entry["cost"] = {**{k: round(v, 6) for k, v in cost_by_type.items()},
                     "total": round(total_cost, 6)}
    if len(models_out) > 1:
        entry["by_model"] = models_out
    return entry


def _aggregate_sessions(sessions: list[dict]) -> dict:
    """Aggregate a list of session entries into a single 'other' summary."""
    agg_tokens: dict[str, int] = {}
    agg_cost_by_type: dict[str, float] = {}
    agg_cost = 0.0
    agg_messages = 0
    for s in sessions:
        for k, v in s.get("tokens", {}).items():
            agg_tokens[k] = agg_tokens.get(k, 0) + v
        cost = s.get("cost", {})
        for k in ("input", "output", "cacheRead", "cacheWrite"):
            if k in cost:
                agg_cost_by_type[k] = agg_cost_by_type.get(k, 0.0) + cost[k]
        agg_cost += cost.get("total", 0.0)
        agg_messages += s.get("messages", 0)
    entry: dict = {"key": "(other)", "sessions": len(sessions)}
    if agg_messages:
        entry["messages"] = agg_messages
    entry["tokens"] = agg_tokens
    entry["cost"] = {**{k: round(v, 6) for k, v in agg_cost_by_type.items()},
                     "total": round(agg_cost, 6)}
    return entry


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("openclaw-usage", log_level="WARNING")


@mcp.tool()
async def get_usage(
    start_date: str = "",
    end_date: str = "",
    period: str = "day",
) -> str:
    """Get usage & cost broken down by period with per-model detail.

    Scans local session files for per-model token-type breakdown
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
    all_sessions = _scan_sessions(start, end)

    result_periods = []
    for b_start, b_end in buckets:
        range_start = datetime(b_start.year, b_start.month, b_start.day, tzinfo=timezone.utc)
        range_end = datetime(b_end.year, b_end.month, b_end.day, 23, 59, 59, tzinfo=timezone.utc)

        # Filter sessions for this bucket
        bucket_sessions = [
            s for s in all_sessions
            if s["start_time"] and range_start <= s["start_time"] <= range_end
        ]

        # Aggregate by model across all sessions in this bucket
        by_model: dict[str, dict] = {}
        for s in bucket_sessions:
            for model, mu in s["model_usage"].items():
                if model not in by_model:
                    by_model[model] = {"tokens": {}, "cost_by_type": {}, "total_cost": 0.0}
                bm = by_model[model]
                for k, v in mu["tokens"].items():
                    bm["tokens"][k] = bm["tokens"].get(k, 0) + v
                for k, v in mu["cost_by_type"].items():
                    bm["cost_by_type"][k] = bm["cost_by_type"].get(k, 0.0) + v
                bm["total_cost"] += mu["total_cost"]

        totals_tokens: dict[str, int] = {}
        totals_cost_by_type: dict[str, float] = {}
        totals_cost = 0.0
        models_out = []

        for model, bm in by_model.items():
            tokens = bm["tokens"]
            cost_by_type = bm["cost_by_type"]
            cost = bm["total_cost"]
            totals_cost += cost
            for k, v in tokens.items():
                totals_tokens[k] = totals_tokens.get(k, 0) + v
            for k, v in cost_by_type.items():
                totals_cost_by_type[k] = totals_cost_by_type.get(k, 0.0) + v
            if tokens or cost:
                models_out.append({
                    "model": model,
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
async def list_sessions(
    start_date: str = "",
    end_date: str = "",
    period: str = "day",
    top_n: int = 5,
    all_sessions: bool = False,
) -> str:
    """List sessions with usage breakdown, grouped by period.

    By default shows the top N sessions by cost per period, with remaining
    sessions aggregated into an "other" entry. Use all_sessions=true to
    output every session chronologically instead.

    Session keys returned here can be passed to get_session_logs for
    per-request detail.

    Args:
        start_date: Start date (YYYY-MM-DD). Defaults to 7 days ago.
        end_date: End date (YYYY-MM-DD). Defaults to today.
        period: Aggregation granularity — "day" (default), "week", or "month".
        top_n: Number of heaviest sessions to show per period (default 5). Ignored when all_sessions is true.
        all_sessions: If true, show all sessions chronologically without aggregation.
    """
    today = date.today()
    start = date.fromisoformat(start_date) if start_date else today - timedelta(days=6)
    end = date.fromisoformat(end_date) if end_date else today

    buckets = _compute_buckets(start, end, period)
    scanned = _scan_sessions(start, end)

    result_periods = []
    for b_start, b_end in buckets:
        range_start = datetime(b_start.year, b_start.month, b_start.day, tzinfo=timezone.utc)
        range_end = datetime(b_end.year, b_end.month, b_end.day, 23, 59, 59, tzinfo=timezone.utc)

        bucket_sessions = [
            s for s in scanned
            if s["start_time"] and range_start <= s["start_time"] <= range_end
        ]

        entries = [_build_session_entry(s) for s in bucket_sessions]
        # Drop zero-usage sessions
        entries = [e for e in entries if e.get("cost", {}).get("total", 0)]

        if all_sessions:
            sessions_out = entries
        else:
            entries.sort(key=lambda e: e.get("cost", {}).get("total", 0), reverse=True)
            top = entries[:top_n]
            rest = entries[top_n:]
            sessions_out = top
            if rest:
                sessions_out.append(_aggregate_sessions(rest))

        result_periods.append({
            "period": _period_label(b_start, b_end, period),
            "sessions": sessions_out,
        })

    return json.dumps(result_periods, indent=2)


@mcp.tool()
async def get_session_logs(key: str, limit: int = 50) -> str:
    """Get per-request logs for a session: timestamps, token counts, costs, roles.
    Message content is excluded.

    Use list_sessions to find session keys.

    Args:
        key: The session key (from list_sessions output).
        limit: Max log entries to return (default 50). Returns most recent entries.
    """
    path = _find_session_file(key)
    if path is None:
        return json.dumps({"error": f"Session not found: {key}"})

    parsed = _parse_session_file(path)
    if parsed is None:
        return json.dumps({"error": f"Could not parse session file for: {key}"})

    entries = parsed["log_entries"]
    # Return most recent `limit` entries
    if len(entries) > limit:
        entries = entries[-limit:]

    return json.dumps(entries, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
