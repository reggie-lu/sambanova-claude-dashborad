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
