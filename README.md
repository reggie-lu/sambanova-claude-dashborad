# SambaNova Cost Lens

Small Flask dashboard for comparing Claude model usage, SambaNova coding-tool offload,
and Claude Code running directly on SambaNova models.

## Run

```bash
cd /Users/bowenl/work/sambanova-claude-dashborad
python app.py
```

Open <http://127.0.0.1:5055>.

## Dashboard experience

The overview explains two setups side by side: **Full SambaNova** (Claude Code's
agent framework with SambaNova model inference) and **Coding offload** (Claude
orchestration with SambaNova coding-tool runs). Each has a workflow diagram and
a setup guide with copyable commands; the guide does not change your configuration.

Recent sessions carry setup badges and can be filtered by setup, including Claude-only
and mixed-provider sessions. The filter applies to the latest 10; the saved-session
picker still accesses the full archive. Cost cards and proportional comparison bars
distinguish recorded token cost from the same-token all-Claude estimate. Extra cost
is labeled explicitly when the comparison is unfavorable.

Timelines stay visible. Expand **Explore tokens & requests** for token categories,
session identity, endpoint evidence, and individual model/tool activity. Expanded
details, setup selection, and activity-list scroll positions survive live refresh.
The layout adapts to narrow screens and supports keyboard controls and reduced motion.

The dashboard polls:

- `~/.claude/projects/**/*.jsonl` for Claude Code sessions and recorded model token usage.
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

## Claude Code with a SambaNova model

Sessions run with `ANTHROPIC_BASE_URL=https://api.sambanova.ai` and
`ANTHROPIC_MODEL=MiniMax-M3` are read directly from Claude Code's normal JSONL logs.
No `/code` invocation or extra tracking wrapper is required. The dashboard identifies
the provider from the endpoint recorded inside the Claude Code session, before
considering the model name:

- `ANTHROPIC_BASE_URL` with hostname `sambanova.ai` or a subdomain: all model
  requests, including subagents and custom model aliases, count as SambaNova usage.
- An unset or other endpoint: model requests count on the Claude side. Actual
  SambaNova `/code` runs remain separate SambaNova coding-tool usage.
- Older requests with no endpoint record retain model-based inference, labeled in
  the session card. The dashboard's own environment never relabels historical usage.

Install the lightweight endpoint recorder into your Claude Code user settings:

```bash
.venv/bin/python provider_tracking.py --install
```

This adds `SessionStart` and `UserPromptSubmit` command hooks, preserving existing
hooks/settings and saving a timestamped settings backup. Start or resume Claude Code
after installation to ensure the hooks are loaded. The recorder checks the environment
of that Claude Code process, so exports in a separate terminal work without restarting
the dashboard. It records only session ID, observation time, hook event, provider,
and endpoint hostname in `data/claude_providers.jsonl`; no API keys, prompts, URL
credentials, paths, or query parameters are saved. Endpoint observations are applied
only to subsequent requests in that session, including its subagents. Resuming with
a different endpoint preserves the provider of previously recorded requests.

Unknown model aliases are still counted when the endpoint is known; their costs
use visibly labeled fallback rates until the model is added to `rates.json`.
For local gateways, the hostname must identify SambaNova for automatic SambaNova
attribution. Other gateways use the Claude-side convention above.

The backend reads `CLAUDE_PROVIDER_LOG` to override the metadata path. For that setup,
install with `provider_tracking.py --install --output /path/to/claude_providers.jsonl`
so the hook and dashboard use the same file. Hooks are documented in the
[Claude Code hooks reference](https://code.claude.com/docs/en/hooks).

Main-agent and subagent requests appear under **SambaNova activity**, labeled
**Claude Code → SambaNova**. Streaming fragments with one message ID count once,
using the latest usage snapshot and merging tool calls. Direct usage contributes
to SambaNova totals and the all-Claude comparison, never to recorded Claude cost.
The raw usage remains available on each request in the API response.

SambaNova's [Messages usage schema](https://github.com/sambanova/sambanova-python/blob/main/src/sambanova/types/message_create_response.py)
defines `input_tokens` as the total prompt, so cache reads and writes are separated
from that total rather than added again. Output is used as reported, without adding
thinking tokens a second time. This differs from native Claude's additive input
categories and from the existing OpenCode step format.

A SambaNova-only session has a purple-only bar. Direct-model timing uses observed
transcript spans; it is not prefill, decoding, or request latency. Consecutive requests
are grouped by model within each agent; main-agent model switches split spans at the
first logged response for the new model. Subagent spans use their first and last
recorded responses. Purple retains priority when spans overlap. These observations
do not establish exact provider switch times or whether a session is still running.

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

## Session timing

Each session shows one combined timeline. Purple covers the union of SambaNova
`/code` run spans; orange covers the remaining Claude session span (first to last
logged event). Purple takes priority during overlap, so every instant is attributed
only once, including overlapping SambaNova runs. Gray marks gaps outside recorded
intervals. The legend shows these non-overlapping attributed durations, while run
notes retain each run's full elapsed duration.

This attribution rule does not prove that Claude was idle during offload; both
providers may have activity during the same period. Spans include waiting and idle
gaps. Finished runs use launch-to-finish time, active runs use elapsed time so far,
and stale/missing-finish runs use the last observed event and are labeled incomplete.
Unknown durations remain unavailable rather than zero. The bar includes runs that
extend beyond the last Claude event.

SambaNova tool `state.time.start/end` values also provide execution durations.
The purple row shows cumulative tool execution and timing coverage; each tool in
the activity list shows its own duration. These sums may include overlapping calls.
Neither session spans nor `/code` response spans represent prefill, decoding, or
exclusive model computation time.

## Logs from another VM

Open **Remote logs → Working on another machine?** in the dashboard for copyable
SSH commands. Install the endpoint hook on the machine that runs Claude Code,
restart Claude Code there, then use `scripts/export_logs.py` (Python 3.10+, no
third-party dependencies) to export `~/.claude/projects` and endpoint history.
For coding offload, supply `--runs /actual/path/sambanova_runs.jsonl`; the helper
copies referenced detail logs and makes their paths portable. The VM must already
have produced this tracking summary; the exporter does not install offload tracking.

Copy the exported directory using rsync into `data/imports/my-vm/`, then start:

```bash
COST_LENS_LOG_DIR="$PWD/data/imports/my-vm" HOST=127.0.0.1 PORT=5055 .venv/bin/python app.py
```

`COST_LENS_LOG_DIR` selects one imported archive and takes precedence over the
individual `CLAUDE_PROJECTS_DIR`, `CLAUDE_PROVIDER_LOG`, and `SAMBANOVA_RUNS_PATH`
settings. Local and imported sessions are not merged. Imported running offloads
are treated as snapshots: their VM PIDs are never checked locally and timing stops
at the last observed step. The page displays the export time and export warnings.
Re-export and rsync with the same options to update; previous transcripts remain
archived, and repeated copies replace files without duplicating requests. Use a
different directory for each VM. The UI polls local files, not the VM.
