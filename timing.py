"""Wall-clock timing derived from local session and tool logs."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


def timestamp_ms(value: Any) -> float | None:
    """Parse ISO timestamps or OpenCode's Unix milliseconds; reject invalid values."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            result = float(value)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            result = parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp() * 1000
        else:
            return None
        return result if math.isfinite(result) else None
    except (ValueError, OverflowError, OSError):
        return None


def timing_interval(start: Any, end: Any) -> dict[str, float | None]:
    """Preserve zero-duration intervals and report incomplete timing as unknown."""
    start_ms, end_ms = timestamp_ms(start), timestamp_ms(end)
    if start_ms is not None and end_ms is not None and end_ms < start_ms:
        end_ms = None
    duration = end_ms - start_ms if start_ms is not None and end_ms is not None else None
    return {"start_ms": start_ms, "end_ms": end_ms, "duration_ms": duration}


def run_timing(run: dict[str, Any], now_ms: float) -> dict[str, Any]:
    """Measure a tracked /code run and cumulative timed tool execution within it."""
    status = run.get("status", "finished")
    end = run.get("finished_at")
    end_kind = "finished"
    steps = run.get("steps", [])
    if run.get("source") == "claude-code":
        end = run.get("observed_until")
        end_kind = "observed"
    elif end is None or end == "":
        end_kind = "elapsed" if status == "running" else "last_observed"
        if status == "running":
            end = now_ms
        else:
            observed = [timestamp_ms(step.get("finished_at") or step.get("timestamp"))
                        for step in steps]
            end = max((value for value in observed if value is not None), default=None)
    tools = {tool["id"]: tool for step in steps for tool in step.get("tools", [])}
    durations = [tool["timing"]["duration_ms"] for tool in tools.values()
                 if tool.get("timing", {}).get("duration_ms") is not None]
    return {
        **timing_interval(run.get("started_at"), end),
        "id": run.get("id"), "model": run.get("model", "unknown"),
        "status": status, "end_kind": end_kind,
        "tool_count": len(tools), "timed_tool_count": len(durations),
        "tool_duration_ms": sum(durations) if durations else None,
    }


def attributed_timeline(
    claude: dict[str, Any], runs: list[dict[str, Any]],
    axis_start: float | None, axis_end: float | None,
) -> dict[str, Any]:
    """Partition wall time once, assigning every offload overlap to SambaNova."""
    intervals = [("claude", claude), *(("sambanova", run) for run in runs)]
    known = [(provider, item) for provider, item in intervals if item["duration_ms"] is not None]
    totals = {"claude": 0.0, "sambanova": 0.0, "unknown": 0.0}
    segments = []
    if axis_start is not None and axis_end is not None:
        boundaries = sorted({axis_start, axis_end, *(value for _, item in known
                            for value in (item["start_ms"], item["end_ms"]))})
        for start, end in zip(boundaries, boundaries[1:]):
            active = {provider for provider, item in known
                      if item["start_ms"] <= start and item["end_ms"] >= end}
            provider = "sambanova" if "sambanova" in active else "claude" if "claude" in active else "unknown"
            duration = end - start
            totals[provider] += duration
            if segments and segments[-1]["provider"] == provider:
                segments[-1]["end_ms"] = end
                segments[-1]["duration_ms"] += duration
            else:
                segments.append({"provider": provider, "start_ms": start,
                                 "end_ms": end, "duration_ms": duration})
    return {"segments": segments, "duration_ms": totals, "has_timing": bool(known)}


def session_timing(session: dict[str, Any], now_ms: float) -> dict[str, Any]:
    """Keep session span and offload intervals on one axis without adding overlaps."""
    direct_only = bool(session.get("direct_sambanova_events")) and not session.get("events")
    claude = timing_interval(None, None) if direct_only else timing_interval(
        session.get("started_at"), session.get("updated_at"))
    runs = [run_timing(run, now_ms) for run in session.get("matched_sambanova_runs", [])]
    bounds = [value for item in [claude, *runs] for key in ("start_ms", "end_ms")
              if (value := item[key]) is not None]
    completed = [run for run in runs if run["end_kind"] == "finished"
                 and run["duration_ms"] is not None]
    axis_start, axis_end = (min(bounds), max(bounds)) if bounds else (None, None)
    return {
        "attribution": attributed_timeline(claude, runs, axis_start, axis_end),
        "claude": claude, "sambanova_runs": runs,
        "axis_start_ms": axis_start,
        "axis_end_ms": axis_end,
        "completed_run_count": len(completed),
        "completed_response_ms": sum(run["duration_ms"] for run in completed) if completed else None,
    }
