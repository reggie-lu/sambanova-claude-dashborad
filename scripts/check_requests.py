"""Summarize timing-proxy records: main-loop requests vs side requests (auto-mode checks).

A side request is one whose response never appears in a Claude Code transcript.
Usage: python check_requests.py [proxy log] [--since ISO-time]
"""
import json, sys, glob, os
from collections import defaultdict

import argparse
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("log", nargs="?", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "proxy_requests.jsonl"))
parser.add_argument("--since", default="", help="only requests at or after this UTC time, e.g. 2026-11-24T12:45:19")
options = parser.parse_args()
log, since = options.log, options.since
logged = set()
for path in glob.glob(os.path.expanduser("~/.claude/projects/**/*.jsonl"), recursive=True):
    for line in open(path, errors="ignore"):
        if '"assistant"' in line:
            try:
                message = json.loads(line).get("message") or {}
            except ValueError:
                continue
            if isinstance(message, dict) and isinstance(message.get("id"), str):
                logged.add(message["id"])

def p50(values):
    values = sorted(v for v in values if v is not None)
    return values[len(values) // 2] if values else None

def fmt(ms):
    return "—" if ms is None else f"{ms / 1000:.1f}s"

groups = defaultdict(lambda: {"ttft": [], "total": [], "out": [], "errors": 0, "settings": defaultdict(int)})
for line in open(log):
    try:
        r = json.loads(line)
    except ValueError:
        continue
    if r.get("received_at", "") < since:
        continue
    kind = "main" if r.get("message_id") in logged else "side (check)"
    model = r.get("model") or r.get("request_model") or "?"
    g = groups[(kind, f"{model} (low effort)" if r.get("fast_check") else model)]
    sent = r.get("upstream_sent_ms")
    g["ttft"].append(r["first_token_ms"] - sent if r.get("first_token_ms") and sent else None)
    g["total"].append(r["end_ms"] - sent if r.get("end_ms") and sent else None)
    g["out"].append((r.get("usage") or {}).get("output_tokens"))
    g["errors"] += bool(r.get("error") or (r.get("status") or 200) >= 400)
    settings = r.get("request_settings") or {}
    g["settings"][json.dumps({key: settings.get(key) for key in ("max_tokens", "thinking", "output_config")
                              if settings.get(key) is not None}, sort_keys=True)] += 1

print(f"{'kind':14} {'model':28} {'n':>4} {'TTFT p50':>9} {'total p50':>10} {'out tok p50':>12} {'errors':>6}")
for (kind, model), g in sorted(groups.items()):
    print(f"{kind:14} {model:28} {len(g['total']):>4} {fmt(p50(g['ttft'])):>9} {fmt(p50(g['total'])):>10} {str(p50(g['out'])):>12} {g['errors']:>6}")
    for asked, count in sorted(g["settings"].items(), key=lambda item: -item[1]):
        if asked != "{}":
            print(f"{'':14}   asked for {asked} ×{count}")
