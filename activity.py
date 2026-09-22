"""Read local tool activity without executing commands from transcripts."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def read_records(path: Path) -> list[dict[str, Any]]:
    """Read JSONL, tolerating incomplete writes and non-JSON log lines."""
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def tool_detail(name: str, inputs: dict[str, Any]) -> str:
    """Return a short action description without including file contents."""
    value = next((inputs[key] for key in (
        "description", "file_path", "filePath", "path", "pattern", "command", "query"
    ) if inputs.get(key)), name)
    return str(value)[:240]


def coding_tool(name: str, inputs: dict[str, Any]) -> bool:
    """Conservatively identify file/code tools and development shell commands."""
    name = name.lower()
    if name in {"read", "write", "edit", "multiedit", "notebookedit", "glob", "grep",
                "apply_patch", "read_file", "write_file", "edit_file"}:
        return True
    if name not in {"bash", "shell", "exec_command"}:
        return False
    command = str(inputs.get("command") or inputs.get("cmd") or "")
    if re.search(r"samba[-_]claude|opencode|/code\b|code\.py|progress/", command, re.I):
        return False
    return bool(re.search(
        r"(?:^|[\s;&|/])(git|npm|npx|pnpm|yarn|bun|node|python[\d.]*|pytest|uv|"
        r"cargo|rustc|go|make|cmake|bazel|rg|grep|sed|cat|head|tail|ls|find|tsc|ruff)\b",
        command,
    ))


def claude_requests(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge content fragments by API message ID; usage is a snapshot, not a delta."""
    requests: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        message = record.get("message")
        if not isinstance(message, dict) or not str(message.get("model", "")).startswith("claude"):
            continue
        key = message.get("id") or record.get("uuid") or f"line-{index}"
        item = requests.setdefault(key, {
            "id": key, "model": message["model"], "timestamp": record.get("timestamp"),
            "agent": record.get("attributionAgent") or record.get("agentId") or "Claude Code",
            "usage": {}, "tools": {},
        })
        # Later fragments may contain updated output-token totals.
        if message.get("usage"):
            item["usage"] = message["usage"]
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name", "tool")
            inputs = block.get("input") or {}
            tool_id = block.get("id") or f"{name}:{json.dumps(inputs, sort_keys=True)}"
            item["tools"][tool_id] = {
                "id": tool_id, "name": name, "detail": tool_detail(name, inputs),
                "coding": coding_tool(name, inputs),
            }
    for item in requests.values():
        item["tools"] = list(item["tools"].values())
    return [item for item in requests.values() if item["usage"]]


def event_time(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
    return value if isinstance(value, str) else None


def opencode_steps(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Group OpenCode tool calls and usage by model step, without allocating tool costs."""
    log_path = run.get("log_path")
    records = read_records(Path(log_path)) if log_path else []
    steps: dict[str, dict[str, Any]] = {}
    current = ""
    for record in records:
        part = record.get("part") or {}
        if not isinstance(part, dict):
            continue
        kind = record.get("type")
        if kind not in {"step_start", "step_finish", "tool_use", "text"}:
            continue
        key = part.get("messageID")
        if not key:
            if kind == "step_start" or not current:
                current = part.get("id") or f"step-{len(steps)}"
            key = current
        current = key
        step = steps.setdefault(key, {
            "id": key, "timestamp": event_time(record.get("timestamp")),
            "model": run.get("model"), "tools": {}, "status": "running", "usage": None,
        })
        if kind == "tool_use":
            state = part.get("state") or {}
            tool_id = part.get("callID") or part.get("id") or f"tool-{len(step['tools'])}"
            name = part.get("tool", "tool")
            step["tools"][tool_id] = {
                "id": tool_id, "name": name,
                "detail": tool_detail(name, state.get("input") or {}),
                "status": state.get("status", "running"),
            }
        if kind == "step_finish":
            tokens = part.get("tokens")
            step["status"] = "completed"
            if isinstance(tokens, dict):
                cache = tokens.get("cache") or {}
                step["usage"] = {
                    "input_tokens": int(tokens.get("input") or 0),
                    # Match the local tracking plugin: reasoning is billed as output.
                    "output_tokens": int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0),
                    "reasoning_tokens": int(tokens.get("reasoning") or 0),
                    "cache_read_tokens": int(cache.get("read") or 0),
                    "cache_write_tokens": int(cache.get("write") or 0),
                }
    for step in steps.values():
        step["tools"] = list(step["tools"].values())
    return list(steps.values())
