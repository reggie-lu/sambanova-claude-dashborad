# SambaNova Cost Lens

Small Flask dashboard for comparing Claude Code orchestration against SambaNova coding-tool offload.

## Run

```bash
cd /Users/bowenl/work/sambanova-claude-dashborad
python app.py
```

Open <http://127.0.0.1:5055>.

The dashboard polls:

- `~/.claude/projects/**/*.jsonl` for Claude Code sessions and exact Claude token usage.
- `data/sambanova_runs.jsonl` for tracked SambaNova runs.

The installed `samba-claude` plugin entrypoint has also been instrumented locally, so normal Claude CLI use of the SambaNova `/code` tool writes to the same `data/sambanova_runs.jsonl` file automatically.

In Claude CLI, use the upstream plugin syntax:

```text
/code MiniMax-M2.7 /Users/bowenl/work/sambanova-claude-dashborad "Review this project and suggest improvements"
/code MiniMax-M2.7 /Users/bowenl/work/sambanova-claude-dashborad "Continue the previous task" --session <id>
```

## Track a SambaNova coding run

The wrapper is optional and mainly useful for direct terminal testing:

```bash
python scripts/run_samba_tracked.py opencode MiniMax-M2.7 /Users/bowenl/work/sambanova-claude-dashborad \
  "Review this demo and report token usage" -- --format json
```

The wrapper invokes the installed `samba-claude` plugin script, captures stdout/stderr, parses token usage when the tool prints it, and appends a JSONL record. If usage is not printed, it marks the run as estimated.

## Manual ingest

You can also post usage from any Claude CLI hook or shell script:

```bash
curl -X POST http://127.0.0.1:5055/api/sambanova-runs \
  -H 'content-type: application/json' \
  -d '{"tool":"opencode","model":"MiniMax-M2.7","cwd":"/repo","input_tokens":12000,"output_tokens":4000}'
```

## Pricing

Rates live in `rates.json` as USD per 1M input/output tokens. Claude defaults use public Anthropic API pricing. SambaNova rates are intentionally editable because account/model pricing can change.

## Activity and estimates

- SambaNova model requests appear directly in the activity column, newest first,
  matching Claude's request list. Requests from all matched runs are combined;
  each shows model, time, tokens, cost, and tool calls from the tracked `log_path`
  (OpenCode JSONL). Multiple tools share a request's cost. Missing detailed logs
  show a separate aggregate-only notice and are excluded from request counts.
- Claude transcript fragments with the same API message ID are merged before counting
  usage. Input, output, cache reads, and cache writes are shown separately.
- Claude-only sessions get a SambaNova estimate for requests containing file tools
  or recognized development shell commands. Delegation and SambaNova launch commands
  are excluded. The entire qualifying request is priced once; other requests stay
  on Claude. This is a heuristic, not a semantic coding classifier.
- Session-level SambaNova estimates use MiniMax-M2.7 by default. The no-cache estimate prices
  all source prompt tokens as fresh input. A second scenario assumes the same cache
  hits; source cache writes use the SambaNova input rate. Actual tokenization, context,
  output, and task quality may differ. Estimates never increase recorded token totals.
- Pricing is in USD per million tokens, verified against the linked provider pages on
  2026-09-21. Unknown models use a visibly labeled fallback. These are standard API
  token costs, not subscription bills or negotiated rates; extra tool fees and premium
  service modifiers are not included.
- Manual run ingestion can include `claude_session_id` for exact session matching.
  Without it, runs are matched using time plus working-directory or transcript evidence.

Run accounting checks with `.venv/bin/python -m unittest discover -s tests -v`.

## Per-session all-Claude comparison

Each session has a fourth cost card: **If all usage ran on Claude**. It preserves
recorded Claude cost and reprices the SambaNova usage on the session's most frequent
Claude model. Fresh input, output, cache reads, and cache writes stay separate;
SambaNova cache writes are assumed to use Claude's 5-minute cache duration. If the
session has no Claude model, the dashboard labels the overall model fallback.

- Estimated savings = all-Claude estimate − combined recorded cost.
- Percent saved = savings / all-Claude estimate × 100.
- All-Claude percent more = savings / combined recorded cost × 100.

Zero denominators show no percentage; higher combined costs show an extra-cost
message. Comparisons assume the same tokens and cache hits, not identical actual
usage or task quality across models. Global comparisons use the same per-session
models, with the overall model used only for unmatched runs.

The local `static/sambanova-icon.png` is the official icon downloaded from
https://sambanova.ai/hubfs/sambanova-favicon.png, used in the header and browser tab.

## Browsing saved sessions

The dashboard shows the latest 10 sessions with recorded usage. Below them,
**Browse saved sessions** lists every available session by date, folder, and ID,
including sessions older than the first 10. Selecting one displays its complete
cost and activity cards below the picker; automatic refresh preserves the selection.
Choose the placeholder to clear it. Sessions are read from the existing local logs,
not deleted when they leave the latest 10.

`GET /api/metrics` returns 10 recent sessions plus a lightweight `session_index`.
`GET /api/metrics?session_id=<id>` additionally returns the selected session, without
changing the recent list or the global totals. A missing ID sets
`selected_session_missing` and returns no selected session.

The Claude icon in the cost cards is served locally from `static/claude-icon.png`,
obtained from the official claude.com favicon:
https://assets.claude.com/95a868946ac8a31e5ff832e2899f294aa368b836.png?w=32&h=32
