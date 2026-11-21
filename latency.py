"""Client-side latency breakdown of Claude Code sessions, from local transcripts only.

Claude Code logs when each API request is sent (`api-request`), when each response
content block finishes (assistant records), how long thinking and tool-input blocks
streamed, when each tool result arrived, who approved it, and each turn's duration.
That is enough to split wall-clock time into model wait, generation, tools, Claude
Code overhead, and idle time. Server-side queue, prefill, and network time are not
logged, so time to first token here is what the client observed: all three combined.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from timing import timestamp_ms

# Higher priority wins where intervals overlap, so every instant counts once.
CATEGORIES = ["generation", "first_token", "approval", "tool", "overhead", "idle"]
PRIORITY = {name: index for index, name in enumerate(CATEGORIES)}


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def is_prompt(record: dict[str, Any]) -> bool:
    """A typed user message, not a tool result or injected meta content."""
    message = record.get("message")
    if record.get("type") != "user" or record.get("isMeta") or not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, list):
        return not any(isinstance(block, dict) and block.get("type") == "tool_result"
                       for block in content)
    return isinstance(content, str) and bool(content.strip())


def first_token_ms(block: dict[str, Any]) -> tuple[float | None, bool]:
    """Start of a response's first block; False marks an upper bound, not a measurement."""
    finished = timestamp_ms(block["record"].get("timestamp"))
    if finished is None:
        return None, False
    for key in ("thinkingDurationMs", "toolInputStreamMs"):
        streamed = block["record"].get(key)
        if isinstance(streamed, (int, float)) and streamed >= 0:
            return finished - streamed, True
    return finished, False


def stream_requests(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per model response, linked to the request that produced it."""
    sent = {record["id"]: timestamp_ms(record.get("timestamp"))
            for record in records if record.get("type") == "api-request" and record.get("id")}
    responses: dict[str, dict[str, Any]] = {}
    for record in records:
        message = record.get("message")
        if record.get("type") != "assistant" or not isinstance(message, dict):
            continue
        model = str(message.get("model") or "")
        if not message.get("id") or not model or model.startswith("<"):
            continue
        item = responses.setdefault(message["id"], {
            "id": message["id"], "model": model, "blocks": [], "usage": {},
            "request_ms": sent.get(record.get("requestRef")),
        })
        if message.get("usage"):
            item["usage"] = message["usage"]
        item["stop_reason"] = message.get("stop_reason") or item.get("stop_reason")
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict):
                item["blocks"].append({"type": block.get("type"), "id": block.get("id"),
                                       "name": block.get("name"), "record": record})
    requests = []
    for item in responses.values():
        if not item["blocks"]:
            continue
        start, measured = first_token_ms(item["blocks"][0])
        ends = [value for block in item["blocks"]
                if (value := timestamp_ms(block["record"].get("timestamp"))) is not None]
        end = max(ends) if ends else None
        request = item["request_ms"]
        ttft = start - request if start is not None and request is not None else None
        generation = end - start if end is not None and start is not None else None
        output = int(item["usage"].get("output_tokens") or 0)
        requests.append({
            "id": item["id"], "model": item["model"], "stop_reason": item.get("stop_reason"),
            "request_ms": request, "first_token_ms": start, "end_ms": end,
            "ttft_ms": ttft if ttft is None or ttft >= 0 else None,
            "ttft_measured": measured,
            "generation_ms": generation if generation is None or generation >= 0 else None,
            "output_tokens": output,
            "input_tokens": int(item["usage"].get("input_tokens") or 0),
            "cache_read_tokens": int(item["usage"].get("cache_read_input_tokens") or 0),
            "tool_uses": [(block["id"], block["name"], timestamp_ms(block["record"].get("timestamp")))
                          for block in item["blocks"] if block["type"] == "tool_use" and block["id"]],
        })
    for request in requests:
        # Decode speed needs a measured first token and enough span to be meaningful.
        seconds = (request["generation_ms"] or 0) / 1000
        request["tokens_per_s"] = (request["output_tokens"] / seconds
                                   if request["ttft_measured"] and seconds >= 0.5
                                   and request["output_tokens"] else None)
    return sorted(requests, key=lambda item: item["request_ms"] or item["end_ms"] or 0)


def tool_results(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    results = {}
    for record in records:
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id"):
                results[block["tool_use_id"]] = {
                    "end_ms": timestamp_ms(record.get("timestamp")),
                    "approval": record.get("permissionDecision") or {},
                    "error": bool(block.get("is_error")),
                }
    return results


def approval_kind(decision: dict[str, Any]) -> str:
    source = str(decision.get("source") or "")
    if source.startswith("user"):
        return "you"
    if decision.get("reasonType") == "classifier":
        return "auto-mode check"
    return "rule" if decision else "unknown"


def turn_intervals(records: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """Claude Code's own turn durations; prompts to last response where those are absent."""
    turns = []
    for record in records:
        if record.get("type") == "system" and record.get("subtype") == "turn_duration":
            end, duration = timestamp_ms(record.get("timestamp")), record.get("durationMs")
            if end is not None and isinstance(duration, (int, float)) and duration >= 0:
                turns.append((end - duration, end))
    if turns:
        return turns
    prompts = sorted(value for record in records if is_prompt(record)
                     if (value := timestamp_ms(record.get("timestamp"))) is not None)
    activity = sorted(value for record in records if record.get("type") in {"assistant", "user"}
                      if (value := timestamp_ms(record.get("timestamp"))) is not None)
    for index, start in enumerate(prompts):
        limit = prompts[index + 1] if index + 1 < len(prompts) else float("inf")
        inside = [value for value in activity if start <= value < limit]
        if inside:
            turns.append((start, max(inside)))
    return turns


def partition(intervals: list[tuple[float, float, str]], span: tuple[float, float]) -> dict[str, float]:
    """Sweep once over all boundaries; gaps are overhead inside turns, idle outside."""
    totals = dict.fromkeys(CATEGORIES, 0.0)
    events = []
    for start, end, category in intervals:
        start, end = max(start, span[0]), min(end, span[1])
        if end > start:
            events += [(start, 1, category), (end, -1, category)]
    events.sort(key=lambda event: (event[0], event[1]))
    active: defaultdict[str, int] = defaultdict(int)
    cursor = span[0]
    for time, delta, category in events + [(span[1], 0, "")]:
        if time > cursor:
            live = [name for name, count in active.items() if count > 0]
            winner = min(live, key=PRIORITY.__getitem__) if live else "idle"
            totals[winner] += time - cursor
            cursor = time
        if category:
            active[category] += delta
    return totals


def stream_latency(records: list[dict[str, Any]], label: str) -> dict[str, Any]:
    requests = stream_requests(records)
    results = tool_results(records)
    turns = turn_intervals(records)
    intervals: list[tuple[float, float, str]] = [(start, end, "overhead") for start, end in turns]
    tools: list[dict[str, Any]] = []
    for request in requests:
        if request["request_ms"] is not None and request["first_token_ms"] is not None:
            intervals.append((request["request_ms"], request["first_token_ms"], "first_token"))
        if request["first_token_ms"] is not None and request["end_ms"] is not None:
            intervals.append((request["first_token_ms"], request["end_ms"], "generation"))
        for tool_id, name, started in request["tool_uses"]:
            result = results.get(tool_id, {})
            ended = result.get("end_ms")
            duration = ended - started if ended is not None and started is not None and ended >= started else None
            approved = approval_kind(result.get("approval", {}))
            tools.append({"name": name or "tool", "duration_ms": duration, "approval": approved,
                          "error": result.get("error", False)})
            if duration is not None:
                intervals.append((started, ended, "approval" if approved == "you" else "tool"))
        request["tools"] = [tool["name"] for tool in tools[-len(request["tool_uses"]):]] if request["tool_uses"] else []
        request["tool_ms"] = sum(tool["duration_ms"] or 0 for tool in tools[-len(request["tool_uses"]):]) if request["tool_uses"] else 0
        del request["tool_uses"]
    times = [value for record in records if (value := timestamp_ms(record.get("timestamp"))) is not None]
    span = (min(times), max(times)) if times else None
    by_tool: defaultdict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "duration_ms": 0.0, "durations": []})
    for tool in tools:
        entry = by_tool[tool["name"]]
        entry["count"] += 1
        if tool["duration_ms"] is not None:
            entry["duration_ms"] += tool["duration_ms"]
            entry["durations"].append(tool["duration_ms"])
    tool_summary = sorted(({"name": name, "count": entry["count"], "duration_ms": entry["duration_ms"],
                            "p50_ms": percentile(entry["durations"], 0.5),
                            "max_ms": max(entry["durations"], default=None)}
                           for name, entry in by_tool.items()), key=lambda item: -item["duration_ms"])
    measured_ttft = [request["ttft_ms"] for request in requests
                     if request["ttft_measured"] and request["ttft_ms"] is not None]
    speeds = [request["tokens_per_s"] for request in requests if request["tokens_per_s"]]
    models: defaultdict[str, int] = defaultdict(int)
    for request in requests:
        models[request["model"]] += 1
    return {
        "label": label,
        "span_ms": span[1] - span[0] if span else None,
        "breakdown_ms": partition(intervals, span) if span else dict.fromkeys(CATEGORIES, 0.0),
        "request_count": len(requests),
        "timed_request_count": sum(1 for request in requests if request["request_ms"] is not None),
        "models": dict(models),
        "ttft_p50_ms": percentile(measured_ttft, 0.5),
        "ttft_p90_ms": percentile(measured_ttft, 0.9),
        "ttft_samples": len(measured_ttft),
        "tokens_per_s_p50": percentile(speeds, 0.5),
        "tool_count": len(tools),
        "tool_summary": tool_summary[:12],
        "approvals": {kind: sum(1 for tool in tools if tool["approval"] == kind)
                      for kind in ("you", "auto-mode check", "rule", "unknown")},
        "tool_errors": sum(1 for tool in tools if tool["error"]),
        "requests": requests[-60:],
        "_samples": [(request["model"], request["ttft_ms"] if request["ttft_measured"] else None,
                      request["tokens_per_s"]) for request in requests],
    }


def session_latency(streams: list[tuple[str, list[dict[str, Any]]]]) -> dict[str, Any] | None:
    """Main thread is the session's critical path; subagents are reported alongside it."""
    results = [stream_latency(records, label) for label, records in streams if records]
    results = [result for result in results if result["request_count"]]
    if not results:
        return None
    main = next((result for result in results if result["label"] == "Main agent"), results[0])
    return {"main": main, "subagents": [result for result in results if result is not main],
            "server_side": None}


def strip_samples(latency: dict[str, Any] | None) -> None:
    """Raw sample lists feed cross-session stats only; keep them out of the API payload."""
    for stream in ([latency["main"], *latency["subagents"]] if latency else []):
        stream.pop("_samples", None)
