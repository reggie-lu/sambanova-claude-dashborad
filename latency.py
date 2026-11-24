"""Client-side latency breakdown of Claude Code sessions, from local transcripts only.

Claude Code logs when each API request is sent (`api-request`), when each response
content block finishes (assistant records), how long thinking and tool-input blocks
streamed, when each tool result arrived, who approved it, and each turn's duration.
That is enough to split wall-clock time into model wait, generation, tools, Claude
Code overhead, and idle time. Tool calls cleared by auto mode's safety classifier
are kept apart ("auto_check"), since the classifier is itself a model request. Server-side queue, prefill, and network time are not
logged, so time to first token here is what the client observed: all three combined,
unless the local timing proxy (proxy.py) recorded the request, which adds the split.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from timing import timestamp_ms

# Higher priority wins where intervals overlap, so every instant counts once.
# "auto_check": a tool call cleared by auto mode's safety classifier. The classifier is
# an extra model request Claude Code does not log, so without the timing proxy the
# check and the command's own run are one interval; with it, the check is timed exactly
# and the rest of the interval stays "tool".
CATEGORIES = ["generation", "first_token", "approval", "auto_check", "tool", "overhead", "idle"]
PRIORITY = {name: index for index, name in enumerate(CATEGORIES)}


def count(value: Any) -> int:
    """A non-negative token count from untrusted log data; anything else is 0."""
    if isinstance(value, bool):
        return 0
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, number)


def duration(value: Any) -> float | None:
    """A non-negative finite duration in ms, or None (bools and NaN are not durations)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 and value == value and value != float("inf") else None


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def clean(records: list[Any]) -> list[dict[str, Any]]:
    """Only records with a string type can be interpreted; drop anything else up front."""
    return [record for record in records if isinstance(record, dict) and isinstance(record.get("type"), str)]


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
        streamed = duration(block["record"].get(key))
        if streamed is not None:
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
        if duration(value) is None or duration(sent) is None or value < sent:
            return None
        return value - sent

    split: dict[str, Any] = {
        # Claude Code logs the request just after handing it off; tiny negatives are clock noise.
        "client_ms": max(0.0, proxy["received_ms"] - request_ms)
        if request_ms is not None and duration(proxy.get("received_ms")) is not None else None,
        "upstream_headers_ms": since("headers_ms"),
        "upstream_ttft_ms": since("first_token_ms"),
        "upstream_total_ms": since("end_ms"),
    }
    metrics = proxy.get("server_metrics") if isinstance(proxy.get("server_metrics"), dict) else {}
    for key, names in SERVER_SECONDS.items():
        value = next((metrics[name] for name in names if duration(metrics.get(name)) is not None), None)
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
            for record in records if record.get("type") == "api-request" and isinstance(record.get("id"), str)}
    responses: dict[str, dict[str, Any]] = {}
    for record in records:
        message = record.get("message")
        if record.get("type") != "assistant" or not isinstance(message, dict):
            continue
        model = str(message.get("model") or "")
        if not isinstance(message.get("id"), str) or not model or model.startswith("<"):
            continue
        ref = record.get("requestRef")
        item = responses.setdefault(message["id"], {
            "id": message["id"], "model": model, "blocks": [], "usage": {}, "seen": set(),
            "request_ms": sent.get(ref) if isinstance(ref, str) else None,
        })
        if isinstance(message.get("usage"), dict) and message["usage"]:
            item["usage"] = message["usage"]
        item["stop_reason"] = message.get("stop_reason") or item.get("stop_reason")
        content = message.get("content")
        for index, block in enumerate(content if isinstance(content, list) else []):
            if not isinstance(block, dict):
                continue
            block_id = block.get("id") if isinstance(block.get("id"), str) else None
            key = block_id or (str(record.get("uuid") or record.get("timestamp")), index, str(block.get("type")))
            if key in item["seen"]:
                continue
            item["seen"].add(key)
            item["blocks"].append({"type": block.get("type"), "id": block_id,
                                   "name": block.get("name") if isinstance(block.get("name"), str) else None,
                                   "record": record})
    requests = []
    for item in responses.values():
        if not item["blocks"]:
            continue
        timed = [block for block in item["blocks"] if timestamp_ms(block["record"].get("timestamp")) is not None]
        if not timed:
            continue
        timed.sort(key=lambda block: timestamp_ms(block["record"].get("timestamp")))
        start, measured = first_token_ms(timed[0])
        end = timestamp_ms(timed[-1]["record"].get("timestamp"))
        request = item["request_ms"]
        ttft = start - request if start is not None and request is not None else None
        if ttft is not None and ttft < 0:
            # Streamed longer than the request existed: clocks or durations disagree, so trust neither.
            ttft, measured, start = None, False, max(request, start)
        generation = end - start if end is not None and start is not None else None
        output = count(item["usage"].get("output_tokens"))
        requests.append({
            "id": item["id"], "model": item["model"], "stop_reason": item.get("stop_reason"),
            "request_ms": request, "first_token_ms": start, "end_ms": end,
            "ttft_ms": ttft,
            "ttft_measured": measured,
            "generation_ms": generation if generation is None or generation >= 0 else None,
            "output_tokens": output,
            "input_tokens": count(item["usage"].get("input_tokens")),
            "cache_read_tokens": count(item["usage"].get("cache_read_input_tokens")),
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
            if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                results[block["tool_use_id"]] = {
                    "end_ms": timestamp_ms(record.get("timestamp")),
                    "approval": record.get("permissionDecision") if isinstance(record.get("permissionDecision"), dict) else {},
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
            end, length = timestamp_ms(record.get("timestamp")), duration(record.get("durationMs"))
            if end is not None and length is not None:
                turns.append((end - length, end))
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
    # Any assistant record (a response or an API error) after a request means it was answered.
    last_assistant = max((value for record in records if record.get("type") == "assistant"
                          if (value := timestamp_ms(record.get("timestamp"))) is not None), default=None)
    sent = [value for record in records if record.get("type") == "api-request"
            if (value := timestamp_ms(record.get("timestamp"))) is not None]
    intervals: list[tuple] = []
    if sent and (last_assistant is None or max(sent) > last_assistant) and max(sent) <= now_ms:
        intervals.append((max(sent), now_ms, "first_token", "waiting for model"))
    # A tool without a result is running only if the model hasn't responded since it was called.
    latest = max((request for request in requests if request["end_ms"] is not None),
                 key=lambda request: request["end_ms"], default=None)
    if latest is not None and latest["end_ms"] == last_assistant:
        for tool_id, name, started in latest["tool_uses"]:
            if tool_id not in results and started is not None and started <= now_ms:
                intervals.append((started, now_ms, "tool", name or "tool"))
    prompts = [value for record in records if is_prompt(record)
               if (value := timestamp_ms(record.get("timestamp"))) is not None]
    last_turn_end = max((end for _, end in turns), default=0)
    if prompts and last_turn_end < max(prompts) <= now_ms:
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


def side_requests(records: list[dict[str, Any]],
                  proxy: dict[str, dict[str, Any]] | None) -> list[tuple[float, float]]:
    """Proxied requests with no response in this transcript (the classifier and other
    side calls), as (start, end), limited to this session when the proxy saw its ID."""
    if not proxy:
        return []
    logged = {message["id"] for record in records if record.get("type") == "assistant"
              if isinstance(message := record.get("message"), dict) and isinstance(message.get("id"), str)}
    sessions = {record["sessionId"] for record in records if isinstance(record.get("sessionId"), str)}
    calls = []
    for message_id, request in proxy.items():
        if message_id in logged or not isinstance(request, dict):
            continue
        owner = request.get("claude_session_id")
        if owner and sessions and owner not in sessions:
            continue
        start, end = duration(request.get("received_ms")), duration(request.get("end_ms"))
        if start is not None and end is not None and end >= start:
            calls.append((start, end))
    return sorted(calls)


def check_summary(tools: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Auto-checked tool calls next to calls of the same tools that skipped the check:
    the gap between the two is roughly what the safety check costs per call."""
    checked = [tool for tool in tools if tool["approval"] == "auto-mode check"]
    if not checked:
        return None
    names = {tool["name"] for tool in checked}
    unchecked = [tool for tool in tools if tool["name"] in names and tool["approval"] == "rule"]

    def p50(items: list[dict[str, Any]], key: str = "duration_ms") -> float | None:
        return percentile([item[key] for item in items if item.get(key) is not None], 0.5)

    measured = [tool for tool in checked if tool.get("check_ms") is not None]
    return {"checked": len(checked), "checked_p50_ms": p50(checked),
            "unchecked": len(unchecked), "unchecked_p50_ms": p50(unchecked),
            "measured": len(measured), "check_p50_ms": p50(measured, "check_ms"),
            "check_total_ms": sum(tool["check_ms"] for tool in measured)}


def stream_latency(records: list[dict[str, Any]], label: str,
                   proxy: dict[str, dict[str, Any]] | None = None,
                   now_ms: float | None = None) -> dict[str, Any]:
    records = clean(records)
    requests = stream_requests(records, proxy)
    results = tool_results(records)
    turns = turn_intervals(records)
    times = [value for record in records if (value := timestamp_ms(record.get("timestamp"))) is not None]
    span = (min(times), max(times)) if times else None
    live = bool(span and now_ms is not None and 0 <= now_ms - span[1] < LIVE_WINDOW_MS)
    intervals: list[tuple] = [(start, end, "overhead") for start, end in turns]
    if live:
        intervals += open_intervals(records, requests, results, turns, now_ms)
        span = (span[0], now_ms)
    tools: list[dict[str, Any]] = []
    seen_tools: set[str] = set()
    side_calls = [(start, end) for start, end in side_requests(records, proxy)
                  if span and start < span[1] and end > span[0]]
    for request in requests:
        request["tool_uses"] = [use for use in request["tool_uses"]
                                if use[0] not in seen_tools and not seen_tools.add(use[0])]
        if request["request_ms"] is not None and request["first_token_ms"] is not None:
            intervals.append((request["request_ms"], request["first_token_ms"], "first_token", request["model"]))
        if request["first_token_ms"] is not None and request["end_ms"] is not None:
            intervals.append((request["first_token_ms"], request["end_ms"], "generation", request["model"]))
        for tool_id, name, started in request["tool_uses"]:
            result = results.get(tool_id, {})
            ended = result.get("end_ms")
            duration = ended - started if ended is not None and started is not None and ended >= started else None
            approved = approval_kind(result.get("approval", {}))
            tool = {"name": name or "tool", "duration_ms": duration, "approval": approved,
                    "error": result.get("error", False), "check_ms": None}
            tools.append(tool)
            if duration is None:
                continue
            if approved == "you":
                intervals.append((started, ended, "approval", tool["name"]))
            elif approved != "auto-mode check":
                intervals.append((started, ended, "tool", tool["name"]))
            else:
                checks = [(max(start, started), min(end, ended)) for start, end in side_calls
                          if start < ended and end > started]
                if checks:  # timed by the proxy: the check itself, then the command
                    intervals.append((started, ended, "tool", tool["name"]))
                    intervals += [(start, end, "auto_check", "auto-mode check") for start, end in checks]
                    tool["check_ms"] = partition([(start, end, "auto_check") for start, end in checks],
                                                 (started, ended))["auto_check"]
                else:
                    intervals.append((started, ended, "auto_check", tool["name"]))
        request["tools"] = [tool["name"] for tool in tools[-len(request["tool_uses"]):]] if request["tool_uses"] else []
        request["tool_ms"] = sum(tool["duration_ms"] or 0 for tool in tools[-len(request["tool_uses"]):]) if request["tool_uses"] else 0
        del request["tool_uses"]
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
        "auto_check": check_summary(tools),
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
