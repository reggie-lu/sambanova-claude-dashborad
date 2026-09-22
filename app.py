from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

from activity import claude_requests, opencode_steps, read_records


BASE_DIR = Path(__file__).resolve().parent
CLAUDE_PROJECTS_DIR = Path(
    os.environ.get("CLAUDE_PROJECTS_DIR", Path.home() / ".claude" / "projects")
)
SAMBANOVA_RUNS_PATH = Path(
    os.environ.get("SAMBANOVA_RUNS_PATH", BASE_DIR / "data" / "sambanova_runs.jsonl")
)
RATES_PATH = Path(os.environ.get("RATES_PATH", BASE_DIR / "rates.json"))

app = Flask(__name__)


def load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return fallback


RATES = load_json(RATES_PATH, {})


def iso_to_epoch(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def is_claude_model(model: str | None) -> bool:
    """True only for genuine Claude Code models.

    Transcripts also carry assistant events whose `model` is the SambaNova
    coding model (e.g. MiniMax-M2.7) when `/code` is used, plus `<synthetic>`
    placeholder events. Those must not be counted on the Claude side.
    """
    if not model:
        return False
    return model == "claude" or model.startswith("claude-")


def normalize_claude_model(model: str | None) -> str:
    if not model:
        return "unknown"
    for known in sorted(RATES.get("claude", {}), key=len, reverse=True):
        if known != "_default" and (model == known or model.startswith(known + "-")):
            return known
    return model


def rate_for(provider: str, model: str) -> dict[str, float]:
    provider_rates = RATES.get(provider, {})
    normalized = normalize_claude_model(model) if provider == "claude" else model
    if normalized in provider_rates:
        return provider_rates[normalized]
    return provider_rates.get("_default", {"input": 0.0, "output": 0.0})


def token_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    rate = rate_for(provider, model)
    return (input_tokens / 1_000_000 * rate.get("input", 0.0)) + (
        output_tokens / 1_000_000 * rate.get("output", 0.0)
    )


def sambanova_tokens(run: dict[str, Any]) -> dict[str, int]:
    """Normalize additive usage and the older cached-input-subset convention."""
    fresh = int(run.get("input_tokens") or 0)
    if "cache_read_tokens" in run:
        cached = int(run.get("cache_read_tokens") or 0)
    else:
        cached = int(run.get("cached_input_tokens") or run.get("cache_read_input_tokens") or 0)
        fresh = max(0, fresh - cached)
    written = int(run.get("cache_write_tokens") or 0)
    output = int(run.get("output_tokens") or 0)
    return {"input": fresh, "output": output, "cache_read": cached,
            "cache_creation": written, "total": fresh + written + cached + output}


def sambanova_run_cost(run: dict[str, Any]) -> float:
    rate = rate_for("sambanova", run.get("model") or "unknown")
    tokens = sambanova_tokens(run)
    return ((tokens["input"] + tokens["cache_creation"]) * rate["input"]
            + tokens["cache_read"] * rate.get("cached_input", rate["input"])
            + tokens["output"] * rate["output"]) / 1_000_000


def claude_usage_cost(model: str, usage: dict[str, Any]) -> float:
    rate = rate_for("claude", model)
    input_rate = rate.get("input", 0.0)
    output_rate = rate.get("output", 0.0)
    cache_creation = usage.get("cache_creation") or {}
    cache_write_5m = int(cache_creation.get("ephemeral_5m_input_tokens") or 0)
    cache_write_1h = int(cache_creation.get("ephemeral_1h_input_tokens") or 0)
    cache_creation_total = int(usage.get("cache_creation_input_tokens") or 0)
    unclassified_cache_write = max(0, cache_creation_total - cache_write_5m - cache_write_1h)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    return (
        int(usage.get("input_tokens") or 0) / 1_000_000 * input_rate
        + cache_write_5m / 1_000_000 * rate.get("cache_write_5m", input_rate * 1.25)
        + cache_write_1h / 1_000_000 * rate.get("cache_write_1h", input_rate * 2.0)
        + unclassified_cache_write / 1_000_000 * rate.get("cache_write_5m", input_rate * 1.25)
        + cache_read / 1_000_000 * rate.get("cached_input", input_rate * 0.1)
        + int(usage.get("output_tokens") or 0) / 1_000_000 * output_rate
    )


def empty_tokens() -> dict[str, int]:
    return {
        "input": 0,
        "output": 0,
        "cache_creation": 0,
        "cache_read": 0,
        "total": 0,
    }


def add_tokens(target: dict[str, int], usage: dict[str, Any]) -> None:
    target["input"] += int(usage.get("input_tokens") or 0)
    target["output"] += int(usage.get("output_tokens") or 0)
    target["cache_creation"] += int(usage.get("cache_creation_input_tokens") or 0)
    target["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
    target["total"] = (
        target["input"] + target["output"] + target["cache_creation"] + target["cache_read"]
    )


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    chunks.append(str(item.get("text", "")))
                elif item.get("type") == "tool_use":
                    chunks.append(str(item.get("name", "")))
                    chunks.append(json.dumps(item.get("input", {}), ensure_ascii=False))
            else:
                chunks.append(str(item))
        return "\n".join(chunks)
    return ""


def scan_claude_sessions() -> list[dict[str, Any]]:
    sessions: dict[str, dict[str, Any]] = {}
    records_by_session: defaultdict[str, list] = defaultdict(list)
    for path in CLAUDE_PROJECTS_DIR.rglob("*.jsonl"):
        session_id = path.stem if path.parent.name != "subagents" else path.parent.parent.name
        records_by_session[session_id].extend(read_records(path))
    for session_id, records in records_by_session.items():
        session = {
            "id": session_id, "cwd": "", "started_at": "", "updated_at": "",
            "claude": empty_tokens(), "claude_cost": 0.0,
            "models": defaultdict(int), "agents": defaultdict(empty_tokens),
            "sambanova_mentions": [], "events": [],
        }
        for record in records:
            timestamp = record.get("timestamp")
            if timestamp:
                if not session["started_at"] or iso_to_epoch(timestamp) < iso_to_epoch(session["started_at"]):
                    session["started_at"] = timestamp
                if iso_to_epoch(timestamp) > iso_to_epoch(session["updated_at"]):
                    session["updated_at"] = timestamp
            if record.get("cwd") and not session["cwd"]:
                session["cwd"] = record["cwd"]
            message = record.get("message") or {}
            if not isinstance(message, dict):
                continue
            text = content_text(message.get("content"))
            if any(marker in text for marker in ("samba-claude", "/code", "opencode")):
                session["sambanova_mentions"].append({"timestamp": timestamp})
        for item in claude_requests(records):
            usage = item.pop("usage")
            model = normalize_claude_model(item["model"])
            cost = claude_usage_cost(model, usage)
            add_tokens(session["claude"], usage)
            add_tokens(session["agents"][item["agent"]], usage)
            session["claude_cost"] += cost
            session["models"][model] += 1
            session["events"].append({
                **item, "model": model, "provider": "claude", "cost": cost,
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
                "cache_write_tokens": int(usage.get("cache_creation_input_tokens") or 0),
                "cache_tokens": int(usage.get("cache_read_input_tokens") or 0)
                    + int(usage.get("cache_creation_input_tokens") or 0),
                "coding": any(tool["coding"] for tool in item["tools"]),
                "rates": rate_for("claude", model),
                "rate_fallback": model not in RATES.get("claude", {}),
            })
        session["events"].sort(key=lambda item: iso_to_epoch(item.get("timestamp")))
        session["models"] = dict(session["models"])
        session["agents"] = dict(session["agents"])
        sessions[session_id] = session
    return sorted(sessions.values(), key=lambda item: iso_to_epoch(item.get("updated_at")), reverse=True)


def scan_sambanova_runs() -> list[dict[str, Any]]:
    by_id = {}
    anonymous_runs = []
    if not SAMBANOVA_RUNS_PATH.exists():
        return []
    try:
        lines = SAMBANOVA_RUNS_PATH.read_text(errors="ignore").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            run = json.loads(line)
        except json.JSONDecodeError:
            continue
        run_id = run.get("id")
        if run_id:
            current = by_id.get(run_id, {})
            current.update(run)
            by_id[run_id] = current
        else:
            anonymous_runs.append(run)
    runs = list(by_id.values()) + anonymous_runs
    now_epoch = datetime.now(timezone.utc).timestamp()
    for run in runs:
        steps = opencode_steps(run)
        for step in steps:
            if step["usage"] is not None:
                step.update(step["usage"])
                step["cost"] = sambanova_run_cost(step)
        run["steps"] = steps
        run["tool_count"] = sum(len(step["tools"]) for step in steps)
        run["detail_status"] = "available" if steps else "unavailable"
        # Running records contain zero placeholders; completed step usage is more useful.
        measured = [step for step in steps if step["usage"] is not None]
        if measured and (run.get("status") == "running" or run.get("estimated")):
            for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
                run[key] = sum(step[key] for step in measured)
            run["estimated"] = False
            run["usage_source"] = "completed log steps"
        tokens = sambanova_tokens(run)
        run["token_breakdown"] = tokens
        run["total_tokens"] = tokens["total"]
        model = run.get("model") or "unknown"
        if run.get("status") == "running":
            started_epoch = iso_to_epoch(run.get("started_at"))
            if started_epoch and now_epoch - started_epoch > 30 * 60:
                # A long-running process is not necessarily stale.
                try:
                    if not run.get("pid"):
                        raise ProcessLookupError
                    os.kill(int(run["pid"]), 0)
                except (ProcessLookupError, ValueError):
                    run["status"] = "stale"
                except PermissionError:
                    pass
        run["cost"] = sambanova_run_cost(run)
        run["rates"] = rate_for("sambanova", model)
        run["rate_fallback"] = model not in RATES.get("sambanova", {})
    return sorted(
        runs,
        key=lambda item: iso_to_epoch(item.get("finished_at") or item.get("started_at")),
        reverse=True,
    )


def estimate_coding(session: dict[str, Any], model: str) -> dict[str, Any]:
    """Reprice complete coding-tool requests, without adding hypothetical usage to actuals."""
    rate = rate_for("sambanova", model)
    result = {"model": model, "request_count": 0, "tool_count": 0,
              "cost": 0.0, "cache_reuse_cost": 0.0, "claude_cost": 0.0,
              "savings": 0.0, "tokens": empty_tokens(), "events": [],
              "eligible": not bool(session.get("matched_sambanova_runs"))}
    if result["eligible"]:
        for event in session["events"]:
            if not event["coding"]:
                continue
            fresh = event["input_tokens"] + event["cache_write_tokens"]
            cached = event["cache_read_tokens"]
            output = event["output_tokens"]
            cost = ((fresh + cached) * rate["input"] + output * rate["output"]) / 1_000_000
            reuse = (fresh * rate["input"] + cached * rate.get("cached_input", rate["input"])
                     + output * rate["output"]) / 1_000_000
            result["events"].append({**event, "source_model": event["model"], "model": model,
                                     "cost": cost, "cache_reuse_cost": reuse,
                                     "claude_cost": event["cost"]})
            result["request_count"] += 1
            result["tool_count"] += sum(tool["coding"] for tool in event["tools"])
            result["cost"] += cost
            result["cache_reuse_cost"] += reuse
            result["claude_cost"] += event["cost"]
            for key, value in (("input", fresh), ("cache_read", cached), ("output", output)):
                result["tokens"][key] += value
        result["tokens"]["total"] = sum(result["tokens"][key] for key in ("input", "output", "cache_read"))
        result["savings"] = result["claude_cost"] - result["cost"]
    result["projected_cost"] = session.get("hybrid_cost", session["claude_cost"]) - result["savings"]
    return result


def offloaded_claude_cost(tokens: dict[str, int], model: str) -> float:
    """Reprice SambaNova usage on Claude, preserving recorded cache categories."""
    return claude_usage_cost(model, {
        "input_tokens": tokens["input"], "output_tokens": tokens["output"],
        "cache_read_input_tokens": tokens["cache_read"],
        "cache_creation_input_tokens": tokens["cache_creation"],
    })


def session_cost_comparison(session: dict[str, Any], fallback_model: str) -> dict[str, Any]:
    """Compare the recorded combined cost against the same usage entirely on Claude."""
    models = session.get("models", {})
    model = max(models, key=models.get) if models else fallback_model
    offloaded_cost = offloaded_claude_cost(session["sambanova"], model)
    all_claude_cost = session["claude_cost"] + offloaded_cost
    combined_cost = session["hybrid_cost"]
    difference = all_claude_cost - combined_cost
    runs = session["matched_sambanova_runs"]
    return {
        "model": model, "model_basis": "session" if models else "fallback",
        "rate_fallback": normalize_claude_model(model) not in RATES.get("claude", {}),
        "all_claude_cost": all_claude_cost, "combined_cost": combined_cost,
        "offloaded_claude_cost": offloaded_cost, "savings": difference,
        "savings_pct": difference / all_claude_cost * 100 if all_claude_cost else None,
        "all_claude_premium_pct": difference / combined_cost * 100 if combined_cost else None,
        "has_offload": bool(runs),
        "incomplete": any(run["status"] in {"running", "stale"} for run in runs),
        "estimated_tokens": any(run.get("estimated") for run in runs),
    }


def summarize(estimate_model: str = "MiniMax-M2.7") -> dict[str, Any]:
    claude_sessions = scan_claude_sessions()
    samba_runs = scan_sambanova_runs()
    attach_sambanova_runs_to_sessions(claude_sessions, samba_runs)
    for session in claude_sessions:
        session["sambanova_estimate"] = estimate_coding(session, estimate_model)

    claude_tokens = empty_tokens()
    claude_cost = 0.0
    for session in claude_sessions:
        for key in claude_tokens:
            claude_tokens[key] += session["claude"].get(key, 0)
        claude_cost += session.get("claude_cost", 0.0)

    samba_tokens = empty_tokens()
    samba_cost = 0.0
    for run in samba_runs:
        for key in samba_tokens:
            samba_tokens[key] += run["token_breakdown"][key]
        samba_cost += float(run.get("cost") or 0.0)

    dominant_claude_model = "claude-opus-4-7"
    model_counts: defaultdict[str, int] = defaultdict(int)
    for session in claude_sessions:
        for model, count in session.get("models", {}).items():
            model_counts[model] += count
    if model_counts:
        dominant_claude_model = max(model_counts.items(), key=lambda item: item[1])[0]

    for session in claude_sessions:
        session["cost_comparison"] = session_cost_comparison(session, dominant_claude_model)
    sessions_by_id = {session["id"]: session for session in claude_sessions}
    for run in samba_runs:
        matched = sessions_by_id.get(run.get("matched_claude_session_id"))
        run["comparison_model"] = (
            matched["cost_comparison"]["model"] if matched else dominant_claude_model
        )
    # Each offloaded run uses its own session's Claude model; existing Claude cost is unchanged.
    all_claude_cost = claude_cost + sum(
        offloaded_claude_cost(run["token_breakdown"], run["comparison_model"])
        for run in samba_runs
    )
    hybrid_cost = claude_cost + samba_cost
    savings = all_claude_cost - hybrid_cost
    savings_pct = (savings / all_claude_cost * 100) if all_claude_cost > 0 else 0.0
    timeline = build_timeline(claude_sessions, samba_runs, dominant_claude_model)

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "paths": {
            "claude_projects_dir": str(CLAUDE_PROJECTS_DIR),
            "sambanova_runs_path": str(SAMBANOVA_RUNS_PATH),
            "rates_path": str(RATES_PATH),
        },
        "estimation": {
            "model": estimate_model,
            "eligible_requests": sum(s["sambanova_estimate"]["request_count"] for s in claude_sessions),
            "cost": sum(s["sambanova_estimate"]["cost"] for s in claude_sessions),
            "savings": sum(s["sambanova_estimate"]["savings"] for s in claude_sessions),
            "projected_cost": hybrid_cost - sum(s["sambanova_estimate"]["savings"] for s in claude_sessions),
        },
        "totals": {
            "claude_tokens": claude_tokens,
            "sambanova_tokens": samba_tokens,
            "claude_cost": claude_cost,
            "sambanova_cost": samba_cost,
            "hybrid_cost": hybrid_cost,
            "all_claude_cost": all_claude_cost,
            "savings": savings,
            "savings_pct": savings_pct,
            "dominant_claude_model": dominant_claude_model,
            "comparison_models": sorted({run["comparison_model"] for run in samba_runs}),
        },
        "sessions": [s for s in claude_sessions if s["events"] or s["matched_sambanova_runs"]][:25],
        "sambanova_runs": samba_runs[:50],
        "timeline": timeline,
        "rates": RATES,
    }


def attach_sambanova_runs_to_sessions(
    claude_sessions: list[dict[str, Any]], samba_runs: list[dict[str, Any]]
) -> None:
    for session in claude_sessions:
        session["sambanova"] = {**empty_tokens(), "cost": 0.0}
        session["hybrid_cost"] = float(session.get("claude_cost") or 0.0)
        session["matched_sambanova_runs"] = []

    for run in samba_runs:
        match = best_session_for_run(claude_sessions, run)
        run["matched_claude_session_id"] = match["id"] if match else None
        if not match:
            continue
        run_summary = {
            "id": run.get("id"),
            "status": run.get("status") or "finished",
            "tool": run.get("tool") or "opencode",
            "model": run.get("model") or "unknown",
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "tokens": int(run.get("total_tokens") or 0),
            "input_tokens": int(run.get("input_tokens") or 0),
            "output_tokens": int(run.get("output_tokens") or 0),
            "cache_read_tokens": int(run.get("cache_read_tokens") or 0),
            "cost": float(run.get("cost") or 0.0),
            "pid": run.get("pid"),
            "log_path": run.get("log_path"),
            "steps": run["steps"], "tool_count": run["tool_count"],
            "detail_status": run["detail_status"], "estimated": run.get("estimated", False),
            "rate_fallback": run["rate_fallback"], "rates": run["rates"],
            "token_breakdown": run["token_breakdown"],
        }
        match["matched_sambanova_runs"].append(run_summary)
        for key in empty_tokens():
            match["sambanova"][key] += run["token_breakdown"][key]
        match["sambanova"]["cost"] += run_summary["cost"]
        match["hybrid_cost"] = float(match.get("claude_cost") or 0.0) + match["sambanova"]["cost"]


def best_session_for_run(
    claude_sessions: list[dict[str, Any]], run: dict[str, Any]
) -> dict[str, Any] | None:
    explicit_id = run.get("claude_session_id")
    if explicit_id:
        return next((session for session in claude_sessions if session["id"] == explicit_id), None)
    run_time = iso_to_epoch(run.get("started_at") or run.get("finished_at"))
    run_cwd = str(run.get("cwd") or "")
    candidates = []
    for session in claude_sessions:
        session_start = iso_to_epoch(session.get("started_at"))
        session_end = iso_to_epoch(session.get("updated_at"))
        if not session_start or not session_end:
            continue
        time_margin = 15 * 60
        in_window = session_start - time_margin <= run_time <= session_end + time_margin
        cwd_match = bool(
            run_cwd
            and session.get("cwd")
            and (run_cwd.startswith(session["cwd"]) or session["cwd"].startswith(run_cwd))
        )
        mention_match = any(
            abs(iso_to_epoch(item.get("timestamp")) - run_time) <= time_margin
            for item in session.get("sambanova_mentions", [])
        )
        if cwd_match and in_window:
            score = 3
        elif mention_match and in_window:
            score = 2
        else:
            score = 0
        if score:
            candidates.append((score, abs(session_end - run_time), session))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][2]


def build_timeline(
    claude_sessions: list[dict[str, Any]],
    samba_runs: list[dict[str, Any]],
    dominant_claude_model: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for session in claude_sessions:
        for event in session.get("events", []):
            timestamp = event.get("timestamp")
            if not timestamp:
                continue
            token_total = (
                int(event.get("input_tokens") or 0)
                + int(event.get("output_tokens") or 0)
                + int(event.get("cache_tokens") or 0)
            )
            items.append(
                {
                    "id": f"{session['id']}:{timestamp}:{event.get('model')}",
                    "provider": "claude",
                    "lane": "Claude Code",
                    "label": event.get("agent") or "Claude Code",
                    "model": event.get("model") or "unknown",
                    "session": session["id"],
                    "start": timestamp,
                    "end": timestamp,
                    "tokens": token_total,
                    "cost": float(event.get("cost") or 0.0),
                    "all_claude_cost": float(event.get("cost") or 0.0),
                }
            )

    for run in samba_runs:
        start = run.get("started_at") or run.get("finished_at")
        end = run.get("finished_at") or start
        if not start:
            continue
        tokens = run["token_breakdown"]
        token_total = tokens["total"]
        items.append(
            {
                "id": run.get("id") or f"samba:{start}",
                "provider": "sambanova",
                "lane": "SambaNova Coding Tool",
                "label": run.get("tool") or "opencode",
                "model": run.get("model") or "unknown",
                "session": run.get("session") or "",
                "start": start,
                "end": end,
                "status": run.get("status") or "finished",
                "tokens": token_total,
                "cost": float(run.get("cost") or 0.0),
                "all_claude_cost": offloaded_claude_cost(
                    tokens, run.get("comparison_model", dominant_claude_model)
                ),
            }
        )

    return sorted(items, key=lambda item: iso_to_epoch(item.get("start")))[-120:]


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/metrics")
def metrics():
    model = request.args.get("estimate_model", "MiniMax-M2.7")
    if model.startswith("_") or model not in RATES.get("sambanova", {}):
        return jsonify({"error": "Unknown SambaNova estimate model"}), 400
    return jsonify(summarize(model))


@app.post("/api/sambanova-runs")
def add_sambanova_run():
    payload = request.get_json(force=True)
    payload.setdefault("started_at", datetime.now(timezone.utc).isoformat())
    payload.setdefault("finished_at", datetime.now(timezone.utc).isoformat())
    SAMBANOVA_RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SAMBANOVA_RUNS_PATH.open("a") as handle:
        handle.write(json.dumps(payload) + "\n")
    return jsonify({"ok": True, "run": payload}), 201


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes"}
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5055")),
        debug=debug,
        use_reloader=debug,
    )
