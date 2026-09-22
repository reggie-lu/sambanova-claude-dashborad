"""Capture endpoint routing inside Claude Code's process environment, without secrets."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from timing import timestamp_ms

PROVIDER_LOG = Path(os.environ.get(
    "CLAUDE_PROVIDER_LOG", Path(__file__).resolve().parent / "data/claude_providers.jsonl"
))


def endpoint_identity(environment: dict[str, str]) -> dict[str, str]:
    """Classify the hostname, never credentials, paths, query strings, or model names."""
    base_url = environment.get("ANTHROPIC_BASE_URL", "").strip()
    if not base_url:
        return {"provider": "claude", "endpoint_host": "api.anthropic.com",
                "endpoint_source": "default"}
    try:
        parsed = urlsplit(base_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not host:
            return {}
    except ValueError:
        return {}
    samba = host == "sambanova.ai" or host.endswith(".sambanova.ai")
    return {"provider": "sambanova" if samba else "claude", "endpoint_host": host,
            "endpoint_source": "ANTHROPIC_BASE_URL"}


def record_endpoint(payload: dict[str, Any], environment: dict[str, str], path: Path) -> None:
    """Append only a session ID, time, hook name, and sanitized endpoint identity."""
    session_id = payload.get("session_id")
    identity = endpoint_identity(environment)
    if not isinstance(session_id, str) or not session_id or not identity:
        return
    record = {"session_id": session_id, "recorded_at": datetime.now(timezone.utc).isoformat(),
              "hook_event": payload.get("hook_event_name"), **identity}
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(record) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, data)
    finally:
        os.close(descriptor)


def load_provider_history(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Load endpoint observations by session; tolerate incomplete hook writes."""
    history: defaultdict[str, list] = defaultdict(list)
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("provider") not in {"claude", "sambanova"}:
            continue
        when = timestamp_ms(record.get("recorded_at"))
        if when is not None and isinstance(record.get("session_id"), str):
            history[record["session_id"]].append({**record, "recorded_ms": when})
    for records in history.values():
        records.sort(key=lambda record: record["recorded_ms"])
    return dict(history)


def observed_provider(timestamp: Any, history: list[dict[str, Any]]) -> dict[str, Any]:
    """Use the last endpoint observation before a request; never rewrite older usage."""
    when = timestamp_ms(timestamp)
    if when is not None:
        for record in reversed(history):
            if record["recorded_ms"] <= when:
                return {"provider": record["provider"], "provider_basis": "session-endpoint",
                        "endpoint_host": record.get("endpoint_host", "")}
    return {}


def install_hooks(settings_path: Path, output_path: Path) -> bool:
    """Add endpoint capture hooks while preserving all existing Claude settings."""
    original = settings_path.read_bytes() if settings_path.exists() else b'{}'
    settings = json.loads(original)
    command = shlex.join([sys.executable, str(Path(__file__).resolve()),
                          "--record", "--output", str(output_path.resolve())])
    hooks = settings.setdefault("hooks", {})
    changed = False
    for event in ("SessionStart", "UserPromptSubmit"):
        entries = hooks.setdefault(event, [])
        if any(hook.get("command") == command for entry in entries
               for hook in entry.get("hooks", [])):
            continue
        entries.append({"hooks": [{"type": "command", "command": command, "timeout": 5}]})
        changed = True
    if changed:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        if settings_path.exists():
            suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            shutil.copy2(settings_path, settings_path.with_name(settings_path.name + f'.cost-lens-{suffix}.bak'))
        descriptor, temporary = tempfile.mkstemp(dir=settings_path.parent, prefix='.cost-lens-')
        try:
            with os.fdopen(descriptor, 'w') as handle:
                json.dump(settings, handle, indent=2)
                handle.write('\n')
            if settings_path.exists():
                os.chmod(temporary, settings_path.stat().st_mode & 0o777)
            os.replace(temporary, settings_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return changed


def main() -> None:
    """Install hooks or capture one observation without disrupting Claude Code."""
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--record', action='store_true')
    action.add_argument('--install', action='store_true')
    parser.add_argument('--output', type=Path, default=PROVIDER_LOG)
    parser.add_argument('--settings', type=Path, default=Path.home() / '.claude/settings.json')
    args = parser.parse_args()
    if args.install:
        changed = install_hooks(args.settings, args.output)
        print('Endpoint capture hooks installed.' if changed else 'Endpoint capture hooks already installed.')
        return
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict):
            record_endpoint(payload, os.environ, args.output)
    except (OSError, ValueError, TypeError):
        print('Cost Lens: endpoint observation could not be recorded.', file=sys.stderr)


if __name__ == '__main__':
    main()
