"""Client-side latency breakdown of Claude Code sessions, from local transcripts only.

Claude Code logs when each API request is sent (`api-request`), when each response
content block finishes (assistant records), how long thinking and tool-input blocks
streamed, when each tool result arrived, who approved it, and each turn's duration.
That is enough to split wall-clock time into model wait, generation, tools, Claude
Code overhead, and idle time. Server-side queue, prefill, and network time are not
logged, so time to first token here is what the client observed: all three combined,
unless the local timing proxy (proxy.py) recorded the request, which adds the split.
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


SERVER_SECONDS = {  # provider-reported timing, in seconds, by the names SambaNova documents
    "server_ttft_ms": ("time_to_first_token",),
    "queue_ms": ("queue_time",),
    "server_total_ms": ("total_latency", "model_execution_time"),
}


def server_split(proxy: dict[str, Any], request_ms: float | None) -> dict[str, Any]:
    """Proxy timestamps plus provider metrics → where first-token time was spent."""
    sent = proxy.get("upstream_sent_ms")

    def since(key: str) -> float | None:
        value = proxy.get(key)
        return value - sent if isinstance(value, (int, float)) and isinstance(sent, (int, float)) else None

    split: dict[str, Any] = {
        # Claude Code logs the request just after handing it off; tiny negatives are clock noise.
        "client_ms": max(0.0, proxy["received_ms"] - request_ms)
        if request_ms is not None and isinstance(proxy.get("received_ms"), (int, float)) else None,
        "upstream_headers_ms": since("headers_ms"),
        "upstream_ttft_ms": since("first_token_ms"),
        "upstream_total_ms": since("end_ms"),
    }
    metrics = proxy.get("server_metrics") or {}
    for key, names in SERVER_SECONDS.items():
        value = next((metrics[name] for name in names if isinstance(metrics.get(name), (int, float))), None)
        split[key] = value * 1000 if value is not None else None
    if split["upstream_ttft_ms"] is not None and split["server_ttft_ms"] is not None:
        split["network_ms"] = max(0.0, split["upstream_ttft_ms"] - split["server_ttft_ms"])
    else:
        split["network_ms"] = None
    split["metrics"] = {key: value for key, value in metrics.items() if not key.startswith("header:")}
    split["headers"] = {key[7:]: value for key, value in metrics.items() if key.startswith("header:")}
    return split


def stream_requests(records: list[dict[str, Any]],
                    proxy: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
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
            "server": server_split(proxy[item["id"]], request) if proxy and item["id"] in proxy else None,
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


LIVE_WINDOW_MS = 5 * 60 * 1000  # a stream with activity this recent is drawn up to now
MAX_SEGMENTS = 4000


def sweep(intervals: list[tuple], span: tuple[float, float]) -> tuple[dict[str, float], list[list]]:
    """Sweep once over all boundaries; gaps are overhead inside turns, idle outside.

    Returns per-category totals and the time-ordered segments behind them, each
    labeled with the most recently started interval of the winning category.
    """
    totals = dict.fromkeys(CATEGORIES, 0.0)
    segments: list[list] = []
    events = []
    for interval in intervals:
        start, end, category = max(interval[0], span[0]), min(interval[1], span[1]), interval[2]
        label = interval[3] if len(interval) > 3 else ""
        if end > start:
            events += [(start, 1, category, label), (end, -1, category, label)]
    events.sort(key=lambda event: (event[0], event[1]))
    active: defaultdict[str, list[str]] = defaultdict(list)
    cursor = span[0]
    for time, delta, category, label in events + [(span[1], 0, "", "")]:
        if time > cursor:
            live = [name for name, labels in active.items() if labels]
            winner = min(live, key=PRIORITY.__getitem__) if live else "idle"
            winner_label = active[winner][-1] if live else ""
            totals[winner] += time - cursor
            if segments and segments[-1][2] == winner and segments[-1][3] == winner_label \
                    and segments[-1][1] == cursor:
                segments[-1][1] = time
            else:
                segments.append([cursor, time, winner, winner_label])
            cursor = time
        if category:
            if delta > 0:
                active[category].append(label)
            elif label in active[category]:
                active[category].remove(label)
    return totals, segments


def partition(intervals: list[tuple], span: tuple[float, float]) -> dict[str, float]:
    return sweep(intervals, span)[0]


def timeline(segments: list[list], span: tuple[float, float], live: bool) -> dict[str, Any]:
    """Compact segments: [offset ms, duration ms, category index, label index]."""
    labels: dict[str, int] = {}
    rows = [[round(start - span[0]), round(end - start), PRIORITY[category],
             labels.setdefault(label, len(labels))]
            for start, end, category, label in segments[-MAX_SEGMENTS:]]
    return {"start_ms": span[0], "end_ms": span[1], "live": live, "categories": CATEGORIES,
            "labels": list(labels), "segments": rows, "truncated": len(segments) > MAX_SEGMENTS}


def open_intervals(records: list[dict[str, Any]], requests: list[dict[str, Any]],
                   results: dict[str, dict[str, Any]], turns: list[tuple[float, float]],
                   now_ms: float) -> list[tuple]:
    """What a live stream is doing right now: an unanswered request, running tools, the open turn."""
    answered = {record.get("requestRef") for record in records if record.get("type") == "assistant"}
    last_response = max((request["end_ms"] for request in requests if request["end_ms"]), default=0)
    pending = [timestamp_ms(record.get("timestamp")) for record in records
               if record.get("type") == "api-request" and record.get("id") not in answered]
    pending = [value for value in pending if value is not None and value >= last_response]
    intervals: list[tuple] = []
    if pending:
        intervals.append((max(pending), now_ms, "first_token", "waiting for model"))
    for record in records:
        message = record.get("message")
        if record.get("type") != "assistant" or not isinstance(message, dict):
            continue
        for block in message.get("content") if isinstance(message.get("content"), list) else []:
            started = timestamp_ms(record.get("timestamp"))
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") not in results \
                    and started is not None and now_ms - started < 6 * 3600 * 1000:
                intervals.append((started, now_ms, "tool", block.get("name") or "tool"))
    prompts = [value for record in records if is_prompt(record)
               if (value := timestamp_ms(record.get("timestamp"))) is not None]
    last_turn_end = max((end for _, end in turns), default=0)
    if prompts and max(prompts) > last_turn_end:
        intervals.append((max(prompts), now_ms, "overhead", ""))
    return intervals


def server_summary(requests: list[dict[str, Any]]) -> dict[str, Any] | None:
    splits = [request["server"] for request in requests if request.get("server")]
    if not splits:
        return None
    keys = ("client_ms", "upstream_headers_ms", "upstream_ttft_ms", "upstream_total_ms",
            "server_ttft_ms", "queue_ms", "server_total_ms", "network_ms")
    summary = {f"{key}_p50": percentile([split[key] for split in splits if split.get(key) is not None], 0.5)
               for key in keys}
    summary["matched"] = len(splits)
    summary["metric_names"] = sorted({name for split in splits for name in split["metrics"]})
    summary["header_names"] = sorted({name for split in splits for name in split["headers"]})
    return summary


def stream_latency(records: list[dict[str, Any]], label: str,
                   proxy: dict[str, dict[str, Any]] | None = None,
                   now_ms: float | None = None) -> dict[str, Any]:
    requests = stream_requests(records, proxy)
    results = tool_results(records)
    turns = turn_intervals(records)
    intervals: list[tuple] = [(start, end, "overhead") for start, end in turns]
    tools: list[dict[str, Any]] = []
    for request in requests:
        if request["request_ms"] is not None and request["first_token_ms"] is not None:
            intervals.append((request["request_ms"], request["first_token_ms"], "first_token", request["model"]))
        if request["first_token_ms"] is not None and request["end_ms"] is not None:
            intervals.append((request["first_token_ms"], request["end_ms"], "generation", request["model"]))
        for tool_id, name, started in request["tool_uses"]:
            result = results.get(tool_id, {})
            ended = result.get("end_ms")
            duration = ended - started if ended is not None and started is not None and ended >= started else None
            approved = approval_kind(result.get("approval", {}))
            tools.append({"name": name or "tool", "duration_ms": duration, "approval": approved,
                          "error": result.get("error", False)})
            if duration is not None:
                intervals.append((started, ended, "approval" if approved == "you" else "tool", name or "tool"))
        request["tools"] = [tool["name"] for tool in tools[-len(request["tool_uses"]):]] if request["tool_uses"] else []
        request["tool_ms"] = sum(tool["duration_ms"] or 0 for tool in tools[-len(request["tool_uses"]):]) if request["tool_uses"] else 0
        del request["tool_uses"]
    times = [value for record in records if (value := timestamp_ms(record.get("timestamp"))) is not None]
    span = (min(times), max(times)) if times else None
    live = bool(span and now_ms is not None and 0 <= now_ms - span[1] < LIVE_WINDOW_MS)
    if live:
        intervals += open_intervals(records, requests, results, turns, now_ms)
        span = (span[0], now_ms)
    totals, segments = sweep(intervals, span) if span else (dict.fromkeys(CATEGORIES, 0.0), [])
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
        "breakdown_ms": totals,
        "timeline": timeline(segments, span, live) if span else None,
        "current": segments[-1][2] if live and segments else None,
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
        "server_side": server_summary(requests),
        "requests": requests[-60:],
        "_samples": [(request["model"], request["ttft_ms"] if request["ttft_measured"] else None,
                      request["tokens_per_s"]) for request in requests],
    }


def session_latency(streams: list[tuple[str, list[dict[str, Any]]]],
                    proxy: dict[str, dict[str, Any]] | None = None,
                    now_ms: float | None = None) -> dict[str, Any] | None:
    """Main thread is the session's critical path; subagents are reported alongside it."""
    results = [stream_latency(records, label, proxy, now_ms) for label, records in streams if records]
    results = [result for result in results if result["request_count"]]
    if not results:
        return None
    main = next((result for result in results if result["label"] == "Main agent"), results[0])
    return {"main": main, "subagents": [result for result in results if result is not main]}


def strip_samples(latency: dict[str, Any] | None) -> None:
    """Raw sample lists feed cross-session stats only; keep them out of the API payload."""
    for stream in ([latency["main"], *latency["subagents"]] if latency else []):
        stream.pop("_samples", None)
