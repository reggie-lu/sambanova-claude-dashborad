from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request


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
    for known in RATES.get("claude", {}):
        if known != "_default" and (model == known or model.startswith(known)):
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


def sambanova_run_cost(run: dict[str, Any]) -> float:
    model = run.get("model") or "unknown"
    rate = rate_for("sambanova", model)
    # opencode reports `input_tokens` as the fresh/uncached prompt tokens and
    # `cache_read_tokens` as context re-served from cache — these are additive,
    # not overlapping. Price cache reads at the discounted `cached_input` rate,
    # exactly like Claude Code sessions. Older records used the legacy
    # `cache_read_input_tokens`/`cached_input_tokens` keys, which were a subset
    # of input_tokens; support both without double-charging.
    input_tokens = int(run.get("input_tokens") or 0)
    cache_read = int(run.get("cache_read_tokens") or 0)
    legacy_cached = int(run.get("cached_input_tokens") or run.get("cache_read_input_tokens") or 0)
    if cache_read:
        billable_input_tokens = input_tokens
        cached_tokens = cache_read
    else:
        # legacy subset semantics
        cached_tokens = legacy_cached
        billable_input_tokens = max(0, input_tokens - cached_tokens)
    output_tokens = int(run.get("output_tokens") or 0)
    cached_rate = rate.get("cached_input", rate.get("input", 0.0))
    return (
        cached_tokens / 1_000_000 * cached_rate
        + billable_input_tokens / 1_000_000 * rate.get("input", 0.0)
        + output_tokens / 1_000_000 * rate.get("output", 0.0)
    )


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
        + cache_write_5m / 1_000_000 * input_rate * 1.25
        + cache_write_1h / 1_000_000 * input_rate * 2.0
        + unclassified_cache_write / 1_000_000 * input_rate * 1.25
        + cache_read / 1_000_000 * input_rate * 0.1
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
    if not CLAUDE_PROJECTS_DIR.exists():
        return []

    for path in CLAUDE_PROJECTS_DIR.rglob("*.jsonl"):
        session_id = path.stem if path.parent.name != "subagents" else path.parent.parent.name
        session = sessions.setdefault(
            session_id,
            {
                "id": session_id,
                "cwd": "",
                "started_at": "",
                "updated_at": "",
                "claude": empty_tokens(),
                "claude_cost": 0.0,
                "models": defaultdict(int),
                "agents": defaultdict(lambda: empty_tokens()),
                "sambanova_mentions": [],
                "events": [],
            },
        )
        try:
            lines = path.read_text(errors="ignore").splitlines()
        except OSError:
            continue

        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            timestamp = event.get("timestamp")
            if timestamp:
                if not session["started_at"] or iso_to_epoch(timestamp) < iso_to_epoch(
                    session["started_at"]
                ):
                    session["started_at"] = timestamp
                if iso_to_epoch(timestamp) > iso_to_epoch(session["updated_at"]):
                    session["updated_at"] = timestamp
            if event.get("cwd") and not session["cwd"]:
                session["cwd"] = event.get("cwd")

            message = event.get("message") or {}
            usage = message.get("usage") or {}
            model = message.get("model")
            if usage and model and is_claude_model(model):
                normalized_model = normalize_claude_model(model)
                event_cost = claude_usage_cost(normalized_model, usage)
                add_tokens(session["claude"], usage)
                session["claude_cost"] += event_cost
                session["models"][normalized_model] += 1
                agent = event.get("attributionAgent") or event.get("agentId") or "Claude Code"
                add_tokens(session["agents"][agent], usage)
                session["events"].append(
                    {
                        "timestamp": timestamp,
                        "provider": "claude",
                        "model": normalized_model,
                        "agent": agent,
                        "cost": event_cost,
                        "input_tokens": int(usage.get("input_tokens") or 0),
                        "output_tokens": int(usage.get("output_tokens") or 0),
                        "cache_tokens": int(usage.get("cache_creation_input_tokens") or 0)
                        + int(usage.get("cache_read_input_tokens") or 0),
                    }
                )

            text = content_text(message.get("content"))
            if any(marker in text for marker in ("samba-claude", "SAMBANOVA", "/code", "opencode")):
                model_match = re.search(r"\b(MiniMax-[\w.]+|DeepSeek-[\w.-]+|gpt-oss-\d+b)\b", text)
                session["sambanova_mentions"].append(
                    {
                        "timestamp": timestamp,
                        "model": model_match.group(1) if model_match else "unknown",
                        "source": "claude-transcript",
                    }
                )

    result = []
    for session in sessions.values():
        session["models"] = dict(session["models"])
        session["agents"] = dict(session["agents"])
        session["events"] = sorted(
            session["events"], key=lambda item: iso_to_epoch(item.get("timestamp"))
        )[-60:]
        result.append(session)
    return sorted(result, key=lambda item: iso_to_epoch(item.get("updated_at")), reverse=True)


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
        input_tokens = int(run.get("input_tokens") or 0)
        output_tokens = int(run.get("output_tokens") or 0)
        cache_read = int(run.get("cache_read_tokens") or 0)
        model = run.get("model") or "unknown"
        if run.get("status") == "running":
            started_epoch = iso_to_epoch(run.get("started_at"))
            if started_epoch and now_epoch - started_epoch > 30 * 60:
                run["status"] = "stale"
        run["cost"] = sambanova_run_cost(run)
        run["rates"] = rate_for("sambanova", model)
        # Total mirrors Claude Code: fresh input + cache reads + output.
        run["total_tokens"] = input_tokens + cache_read + output_tokens
    return sorted(
        runs,
        key=lambda item: iso_to_epoch(item.get("finished_at") or item.get("started_at")),
        reverse=True,
    )


def summarize() -> dict[str, Any]:
    claude_sessions = scan_claude_sessions()
    samba_runs = scan_sambanova_runs()
    attach_sambanova_runs_to_sessions(claude_sessions, samba_runs)

    claude_tokens = empty_tokens()
    claude_cost = 0.0
    for session in claude_sessions:
        for key in claude_tokens:
            claude_tokens[key] += session["claude"].get(key, 0)
        claude_cost += session.get("claude_cost", 0.0)

    samba_tokens = {"input": 0, "output": 0, "cache_read": 0, "total": 0}
    samba_cost = 0.0
    for run in samba_runs:
        samba_tokens["input"] += int(run.get("input_tokens") or 0)
        samba_tokens["output"] += int(run.get("output_tokens") or 0)
        samba_tokens["cache_read"] += int(run.get("cache_read_tokens") or 0)
        samba_tokens["total"] += int(run.get("total_tokens") or 0)
        samba_cost += float(run.get("cost") or 0.0)

    dominant_claude_model = "claude-opus-4-7"
    model_counts: defaultdict[str, int] = defaultdict(int)
    for session in claude_sessions:
        for model, count in session.get("models", {}).items():
            model_counts[model] += count
    if model_counts:
        dominant_claude_model = max(model_counts.items(), key=lambda item: item[1])[0]

    all_claude_cost = token_cost(
        "claude",
        dominant_claude_model,
        claude_tokens["input"] + claude_tokens["cache_creation"] + claude_tokens["cache_read"]
        + samba_tokens["input"] + samba_tokens["cache_read"],
        claude_tokens["output"] + samba_tokens["output"],
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
        },
        "sessions": claude_sessions[:25],
        "sambanova_runs": samba_runs[:50],
        "timeline": timeline,
        "rates": RATES,
    }


def attach_sambanova_runs_to_sessions(
    claude_sessions: list[dict[str, Any]], samba_runs: list[dict[str, Any]]
) -> None:
    for session in claude_sessions:
        session["sambanova"] = {"input": 0, "output": 0, "cache_read": 0, "total": 0, "cost": 0.0}
        session["hybrid_cost"] = float(session.get("claude_cost") or 0.0)
        session["matched_sambanova_runs"] = []

    for run in samba_runs:
        match = best_session_for_run(claude_sessions, run)
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
        }
        match["matched_sambanova_runs"].append(run_summary)
        match["sambanova"]["input"] += run_summary["input_tokens"]
        match["sambanova"]["output"] += run_summary["output_tokens"]
        match["sambanova"]["cache_read"] += run_summary["cache_read_tokens"]
        match["sambanova"]["total"] += run_summary["tokens"]
        match["sambanova"]["cost"] += run_summary["cost"]
        match["hybrid_cost"] = float(match.get("claude_cost") or 0.0) + match["sambanova"]["cost"]


def best_session_for_run(
    claude_sessions: list[dict[str, Any]], run: dict[str, Any]
) -> dict[str, Any] | None:
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
        elif in_window:
            score = 1
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
        input_tokens = int(run.get("input_tokens") or 0)
        output_tokens = int(run.get("output_tokens") or 0)
        token_total = input_tokens + output_tokens
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
                "all_claude_cost": token_cost(
                    "claude", dominant_claude_model, input_tokens, output_tokens
                ),
            }
        )

    return sorted(items, key=lambda item: iso_to_epoch(item.get("start")))[-120:]


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/metrics")
def metrics():
    return jsonify(summarize())


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
