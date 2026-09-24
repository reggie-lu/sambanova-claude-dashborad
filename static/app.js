const money = (value) => `$${Number(value || 0).toFixed(4)}`;
const ints = (value) => Number(value || 0).toLocaleString();
const setText = (id, value) => { document.getElementById(id).textContent = value; };
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
}[char]));
let selectedSessionId = "";
let sessionIndexSignature = "";
const scrollPositions = new Map();
const disclosureStates = new Map();
const renderedSessions = new Map();
let latestSessions = [];
let setupChoice = "direct";

function sessionMode(session) {
  const runs = session.matched_sambanova_runs || [];
  const direct = runs.some((run) => run.source === "claude-code");
  const claude = session.events?.length > 0;
  if (direct && claude) return {key: "mixed", label: "Mixed providers", description: "This session includes both Claude and SambaNova model requests."};
  if (direct) return {key: "direct", label: "01 · Full SambaNova", description: "Claude Code framework · SambaNova model inference"};
  if (runs.length) return {key: "hybrid", label: "02 · Coding offload", description: "Claude orchestration + SambaNova coding tool"};
  return {key: "claude", label: "Claude only", description: "No recorded SambaNova usage in this session"};
}

const setupCommands = {
  direct: 'export ANTHROPIC_BASE_URL="https://api.sambanova.ai"\nexport ANTHROPIC_API_KEY="YOUR_SAMBANOVA_API_KEY"\nexport ANTHROPIC_MODEL="MiniMax-M3"\nclaude',
  hybrid: 'claude\n\n# Inside Claude Code, with the samba-claude plugin installed:\n/code MiniMax-M3 /path/to/project "Implement the next coding task"',
};
const returnToClaudeCommands = 'unset ANTHROPIC_BASE_URL\nunset ANTHROPIC_API_KEY\nunset ANTHROPIC_AUTH_TOKEN\nunset ANTHROPIC_MODEL';

function showSetup(mode, reveal = false) {
  setupChoice = mode;
  const direct = mode === "direct";
  document.getElementById("guideDirect").setAttribute("aria-pressed", String(direct));
  document.getElementById("guideHybrid").setAttribute("aria-pressed", String(!direct));
  document.getElementById("setupContent").innerHTML = `<div class="guide-grid"><div>
    <span class="mode-badge ${mode}">${direct ? "01 · Full SambaNova" : "02 · Coding offload"}</span>
    <h3>${direct ? "Change the model provider." : "Delegate the coding work."}</h3>
    <p>${direct ? "In a new terminal, point Claude Code at SambaNova and choose your model. Use your SambaNova API key in the placeholder." : "Start Claude Code with your usual Claude authentication and provider. Use the SambaNova coding plugin when you want to delegate a coding task."}</p>
    ${direct ? "" : '<p>Add the marketplace with <code>/plugin marketplace add sambanova/sambanova-plugin-cc</code>, then follow the <a href="https://github.com/sambanova/sambanova-plugin-cc#installation" target="_blank" rel="noreferrer">repository setup guide ↗</a>.</p>'}
    <p class="guide-result">${direct ? "What you’ll see: every model request on the SambaNova side, with a purple-only timeline." : "What you’ll see: Claude requests in orange, SambaNova coding work in purple, and the combined cost of both."}</p>
    ${direct ? "" : '<p class="method-note">Switching from option 01? Use a terminal without the SambaNova ANTHROPIC_BASE_URL, API key and model overrides, then authenticate with Claude normally.</p>'}
    <a class="text-link" href="https://cloud.sambanova.ai/plans/pricing" target="_blank" rel="noreferrer">View SambaNova plans & rates ↗</a>
  </div><div class="code-panel"><div class="code-heading"><span>${direct ? "Terminal" : "Claude Code + /code plugin"}</span><button type="button" id="copySetup" class="copy-button">Copy commands</button></div><pre><code>${escapeHtml(setupCommands[mode])}</code></pre><p id="copyStatus" class="copy-feedback" role="status"></p></div></div>
  ${direct ? `<details class="return-guide"><summary>Switch back to Claude</summary><p>Exit Claude Code, run these commands in the same terminal, then run <code>claude</code> again with your usual Claude login.</p><div class="code-panel"><div class="code-heading"><span>Terminal · clear SambaNova overrides</span><button type="button" id="copyReturnToClaude" class="copy-button">Copy reset commands</button></div><pre><code>${escapeHtml(returnToClaudeCommands)}</code></pre><p id="copyReturnStatus" class="copy-feedback" role="status"></p></div></details>` : ""}`;
  document.getElementById("copySetup").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(setupCommands[setupChoice]);
      setText("copyStatus", "Copied. Replace placeholders before running.");
    } catch { setText("copyStatus", "Copy unavailable. Select and copy the commands above."); }
  });
  if (direct) document.getElementById("copyReturnToClaude").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(returnToClaudeCommands);
      setText("copyReturnStatus", "Copied. Run in your terminal, then start Claude Code again.");
    } catch { setText("copyReturnStatus", "Copy unavailable. Select and copy the commands above."); }
  });
  if (reveal) {
    const guide = document.getElementById("setupGuide");
    guide.open = true;
    guide.scrollIntoView?.({behavior: "smooth", block: "nearest"});
  }
}

function renderRecentSessions() {
  const filter = document.getElementById("sessionFilter").value || "all";
  const visible = latestSessions.filter((session) => filter === "all" || sessionMode(session).key === filter);
  renderSessions(visible);
  setText("filterStatus", `${visible.length} of ${latestSessions.length} recent sessions shown`);
}

function durationText(milliseconds) {
  if (milliseconds == null || !Number.isFinite(milliseconds) || milliseconds < 0) return "Not recorded";
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  const seconds = Math.round(milliseconds / 1000);
  if (seconds < 60) return `${seconds} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  const hours = Math.floor(seconds / 3600);
  return `${hours}h ${Math.floor((seconds % 3600) / 60)}m ${seconds % 60}s`;
}

function timeLabel(milliseconds) {
  return milliseconds == null ? "Not recorded" : new Date(milliseconds).toLocaleString();
}

function timingBox(session, disclosureKey = session.id || "timing") {
  const timing = session.timing;
  if (!timing?.attribution) return "";
  const runs = timing.sambanova_runs;
  const attribution = timing.attribution;
  const span = timing.axis_end_ms - timing.axis_start_ms;
  const labels = {claude: "Claude outside SambaNova", sambanova: "SambaNova", unknown: "Unrecorded gap"};
  const colors = {claude: "claude-time", sambanova: "samba-time", unknown: "unknown-time"};
  const durations = attribution.duration_ms;
  const directOnly = Boolean(session.direct_sambanova_events?.length) && !session.events?.length;
  const visibleLabels = Object.entries(labels).filter(([provider]) =>
    (provider !== "unknown" || durations.unknown > 0) && (provider !== "claude" || !directOnly));
  const description = visibleLabels.map(([provider, label]) => `${label}: ${durationText(durations[provider])}`).join(". ");
  const bars = attribution.segments.map((segment) => {
    const left = span > 0 ? (segment.start_ms - timing.axis_start_ms) / span * 100 : 0;
    const width = span > 0 ? segment.duration_ms / span * 100 : 0;
    const title = `${labels[segment.provider]} · ${durationText(segment.duration_ms)} · ${timeLabel(segment.start_ms)} → ${timeLabel(segment.end_ms)}`;
    return `<span class="timing-bar ${colors[segment.provider]}" style="left:${left.toFixed(4)}%;width:${width.toFixed(4)}%" title="${escapeHtml(title)}"></span>`;
  }).join("");
  return `<section class="session-timing" aria-label="Session timing">
    <div class="timing-heading"><h3>Session timing</h3><span>${durationText(attribution.has_timing ? span : null)} elapsed · local time</span></div>
    <div class="timing-legend">
      ${visibleLabels.map(([provider, label]) =>
        `<span class="${colors[provider]}"><i class="timing-dot" aria-hidden="true"></i>${label} <b>${durationText(attribution.has_timing ? durations[provider] : null)}</b></span>`).join("")}
    </div>
    <div class="timing-track" role="img" aria-label="${escapeHtml(attribution.has_timing ? description : "Timing not recorded")}">
      ${bars || `<span class="timing-unavailable">${attribution.has_timing ? "0 ms recorded span" : "Timing not recorded"}</span>`}
    </div>
    <div class="timing-axis"><small>${escapeHtml(timeLabel(timing.axis_start_ms))}</small><small>${escapeHtml(timeLabel(timing.axis_end_ms))}</small></div>
    <details class="timing-details" data-disclosure="${escapeHtml(disclosureKey)}-timing" ${disclosureStates.get(`${disclosureKey}-timing`) ? "open" : ""}><summary>How to read this timeline</summary>${runs.map((run, index) => {
      const status = run.end_kind === "observed" ? "Claude Code · observed transcript span" : run.end_kind === "elapsed" ? "In progress · elapsed so far" : run.end_kind === "last_observed" ? "Incomplete · through last observed event" : "Launch to finish";
      const toolNote = run.timed_tool_count ? ` · Tool execution: ${durationText(run.tool_duration_ms)} across ${run.timed_tool_count}/${run.tool_count} timed calls (sum; may overlap)` : "";
      return `<p class="timing-run-note">${escapeHtml(`SambaNova run ${index + 1} · ${run.model} · ${durationText(run.duration_ms)} · ${status}${toolNote}`)}</p>`;
    }).join("") || '<p class="timing-run-note">No recorded SambaNova run for this session.</p>'}
    <p class="timing-note">${directOnly ? "Claude Code is using SambaNova models, so the recorded session span is purple. This includes tools and waiting; it is not a measurement of prefill or decoding time." : "Purple takes priority during SambaNova activity; orange is the remaining Claude session time. Overlaps count once. This is time attribution, not proof that Claude was idle; elapsed time includes waiting."}</p></details>
  </section>`;
}

function toolRows(tools = []) {
  return tools.map((tool) => `<li><b>${escapeHtml(tool.name)}</b>
    <span>${escapeHtml(tool.detail)}</span>${tool.status ? `<small>${escapeHtml(tool.status)}${tool.timing?.duration_ms != null ? ` · Tool execution ${durationText(tool.timing.duration_ms)}` : ""}</small>` : ""}</li>`).join("");
}

function usageText(event) {
  if (event.token_breakdown) {
    const tokens = event.token_breakdown;
    event = { input_tokens: tokens.input, output_tokens: tokens.output,
      cache_read_tokens: tokens.cache_read, cache_write_tokens: tokens.cache_creation };
  }
  return `${ints(event.input_tokens)} fresh in · ${ints(event.output_tokens)} out · ${ints(event.cache_read_tokens)} cache read · ${ints(event.cache_write_tokens)} cache write`;
}

function eventRow(event, provider, estimate = false) {
  const measured = estimate || provider === "claude" || event.usage != null;
  return `<div class="activity-step">
    <div class="step-head"><span class="pill ${estimate ? "estimate-pill" : provider === "sambanova" ? "samba-pill" : ""}">${estimate ? "estimate" : escapeHtml(provider)}</span>
      <span>${escapeHtml(event.model)}${event.timestamp ? ` · ${escapeHtml(new Date(event.timestamp).toLocaleTimeString())}` : ""}</span>
      <b>${measured ? money(event.cost) : "Usage pending"}</b></div>
    <small>${measured ? usageText(event) : "Waiting for model-step usage"}${event.rate_fallback ? " · Fallback rate" : ""}</small>
    ${event.tools?.length ? `<ul class="tool-list">${toolRows(event.tools)}</ul>` : '<p class="step-note">Model response · no tool call</p>'}
    ${event.run_label ? `<small>${escapeHtml(event.run_label)}</small>` : ""}
    ${estimate ? `<small>Claude request: ${money(event.claude_cost)} · With cache reuse: ${money(event.cache_reuse_cost)}</small>` : ""}
  </div>`;
}

function sambaRequests(runs) {
  return runs.flatMap((run) => (run.steps || []).map((step) => ({
    ...step,
    model: step.model || run.model,
    rate_fallback: run.rate_fallback,
    run_label: run.source === "claude-code" ? `Claude Code → SambaNova · ${step.agent || "Claude Code"}` : runs.length > 1 ? `${run.tool || "opencode"} · run ${run.id || "unknown"}` : "",
  }))).sort((left, right) =>
    (Date.parse(right.timestamp) || 0) - (Date.parse(left.timestamp) || 0));
}

function unavailableRunRows(runs) {
  return runs.filter((run) => !run.steps?.length).map((run) => `
    <div class="unavailable-run">
      <strong>${escapeHtml(run.model)} · ${escapeHtml(run.status)} · ${money(run.cost)} total</strong>
      <p class="step-note">${escapeHtml(run.tool || "opencode")} · ${ints(run.tokens)} tokens${run.estimated ? " (estimated)" : ""} · ${usageText(run)}</p>
      <p class="step-note">Request details unavailable for this run. Only aggregate usage was recorded or its detailed log is missing.</p>
    </div>`).join("");
}

function comparisonSummary(comparison) {
  if (!comparison.has_offload) return '<div class="cost-comparison neutral">Claude-only baseline · no recorded SambaNova cost to compare yet.</div>';
  const difference = comparison.savings;
  const equal = Math.abs(difference) < 1e-10;
  const direction = difference > 0 ? "less" : "more";
  const savingsPct = comparison.savings_pct == null ? "" : ` · ${Math.abs(comparison.savings_pct).toFixed(1)}% ${direction} than all-Claude`;
  const premium = comparison.all_claude_premium_pct == null ? "Percentage comparison unavailable because combined cost is zero." :
    `All-Claude would cost ${Math.abs(comparison.all_claude_premium_pct).toFixed(1)}% ${difference >= 0 ? "more" : "less"} than the combined run.`;
  const scale = Math.max(comparison.combined_cost || 0, comparison.all_claude_cost || 0);
  const width = (value) => scale > 0 ? Math.max(0, value || 0) / scale * 100 : 0;
  return `<div class="cost-comparison ${equal ? "neutral" : difference > 0 ? "saving" : "extra-cost"}">
    <div class="comparison-head"><strong>${equal ? "No estimated cost difference" : `${difference > 0 ? "Estimated saving" : "Estimated extra cost"}: ${money(Math.abs(difference))}${savingsPct}`}</strong><span>Same-token comparison</span></div>
    <div class="cost-bars"><div><span>This session</span><div class="cost-track"><i class="actual-bar" style="width:${width(comparison.combined_cost).toFixed(2)}%"></i></div><b>${money(comparison.combined_cost)}</b></div><div><span>All-Claude estimate</span><div class="cost-track"><i class="baseline-bar" style="width:${width(comparison.all_claude_cost).toFixed(2)}%"></i></div><b>${money(comparison.all_claude_cost)}</b></div></div>
    <p>${equal ? "All-Claude and combined token costs are equal." : premium}</p>
    <small>Same-token estimate: keep recorded Claude cost and price SambaNova tokens on ${escapeHtml(comparison.model)}, preserving cache reads and assuming 5-minute cache writes. Actual model usage and caching may differ.${comparison.model_basis === "fallback" ? " No Claude model recorded; using the overall comparison model." : ""}${comparison.rate_fallback ? " This model uses fallback pricing." : ""}${comparison.incomplete ? " Run incomplete: based on usage recorded so far." : ""}${comparison.estimated_tokens ? " Includes estimated token counts." : ""}</small>
  </div>`;
}

function renderSessions(sessions, containerId = "sessions") {
  const container = document.getElementById(containerId);
  const signature = JSON.stringify(sessions);
  if (renderedSessions.get(containerId) === signature && container.innerHTML) return;
  container.querySelectorAll("[data-scroll]").forEach((el) => scrollPositions.set(el.dataset.scroll, el.scrollTop));
  container.querySelectorAll("[data-disclosure]").forEach((el) => disclosureStates.set(el.dataset.disclosure, el.open));
  renderedSessions.set(containerId, signature);
  if (!sessions.length) { container.innerHTML = '<div class="empty empty-panel"><b>No sessions to show here yet.</b><p>Try another setup filter, browse saved sessions, or run a task in Claude Code.</p></div>'; return; }
  container.innerHTML = sessions.map((session) => {
    const samba = session.sambanova;
    const estimate = session.sambanova_estimate;
    const runs = session.matched_sambanova_runs;
    const sambaEvents = sambaRequests(runs);
    const models = [...new Set([...Object.keys(session.models), ...runs.map((run) => run.model)])].join(", ");
    const frameworkEvents = [...session.events, ...(session.direct_sambanova_events || [])];
    const endpointHosts = [...new Set(frameworkEvents.filter((event) => event.provider_basis === "session-endpoint").map((event) => event.endpoint_host))];
    const inferred = frameworkEvents.some((event) => event.provider_basis === "model-fallback");
    const providerNote = [endpointHosts.length ? `Provider from recorded endpoint: ${endpointHosts.join(", ")}` : "",
      inferred ? "Older requests without endpoint records use model-based inference" : ""].filter(Boolean).join(" · ");
    const directRuns = runs.filter((run) => run.source === "claude-code");
    const offloadCount = runs.length - directRuns.length;
    const sambaSource = [directRuns.length ? "via Claude Code" : "", offloadCount ? `${offloadCount} tool ${offloadCount === 1 ? "run" : "runs"}` : ""].filter(Boolean).join(" · ");
    const hasEstimate = estimate.eligible && estimate.request_count > 0;
    const comparison = session.cost_comparison;
    const mode = sessionMode(session);
    const detailKey = `${containerId}-${session.id}`;
    const folder = (session.cwd || "").split(/[\\/]/).filter(Boolean).pop() || "Untitled project";
    return `<article class="session">
      <div class="session-top"><div><span class="mode-badge ${mode.key}">${mode.label}</span><h3 class="session-title">${escapeHtml(folder)} <span>${escapeHtml(session.id.slice(0, 8))}</span></h3><small>${escapeHtml(mode.description)}</small></div>
        <div class="right"><b>${ints(session.claude.total + samba.total)} recorded tokens</b><small>${escapeHtml(models)}</small><small>${escapeHtml(timeLabel(Date.parse(session.updated_at) || null))}</small></div></div>
      <div class="session-cost-grid">
        <div><span class="provider-label"><img src="/static/claude-icon.png" alt="" width="18" height="18">Claude cost</span><strong>${money(session.claude_cost)}</strong><small>${ints(session.claude.total)} tokens · ${session.events.length} requests</small></div>
        <div><span class="provider-label"><img src="/static/sambanova-icon.png" alt="" width="18" height="18">SambaNova cost</span><strong>${money(samba.cost)}</strong><small>${ints(samba.total)} tokens · ${sambaEvents.length} recorded requests${sambaSource ? ` · ${sambaSource}` : ""}</small></div>
        <div class="combined-card"><span class="provider-label">Combined usage cost</span><strong>${money(session.hybrid_cost)}</strong><small>Recorded tokens × API rates</small></div>
        <div class="all-claude-card"><span class="provider-label"><img src="/static/claude-icon.png" alt="" width="18" height="18">If all usage ran on Claude</span><strong>${money(comparison.all_claude_cost)}</strong><small>Estimated · ${escapeHtml(comparison.model)}</small></div>
      </div>
      ${comparisonSummary(comparison)}
      ${timingBox(session, detailKey)}
      <details class="request-details" data-disclosure="${escapeHtml(detailKey)}-requests" ${disclosureStates.get(`${detailKey}-requests`) ? "open" : ""}><summary>Explore tokens & requests<span class="summary-hint">${session.events.length + sambaEvents.length} recorded model requests</span></summary>
      <div class="session-context"><p>${escapeHtml(session.cwd || "unknown cwd")}</p><p>Session ${escapeHtml(session.id)}</p>${providerNote ? `<p>${escapeHtml(providerNote)}</p>` : ""}</div>
      <div class="session-meta"><span>Claude fresh in ${ints(session.claude.input)}</span><span>Out ${ints(session.claude.output)}</span><span>Cache read ${ints(session.claude.cache_read)}</span><span>Cache write ${ints(session.claude.cache_creation)}</span></div>
      <div class="session-meta"><span>SN fresh in ${ints(samba.input)}</span><span>Out ${ints(samba.output)}</span><span>Cache read ${ints(samba.cache_read)}</span><span>Cache write ${ints(samba.cache_creation)}</span></div>
      ${hasEstimate ? `<div class="estimate-box"><div><b>SambaNova estimate · ${escapeHtml(estimate.model)}</b><span class="estimate-tag">Hypothetical</span></div>
        <p>${estimate.request_count} coding requests · ${estimate.tool_count} coding tool calls · ${ints(estimate.tokens.total)} source tokens</p>
        <div class="estimate-values"><span>No cache reuse <b>${money(estimate.cost)}</b></span><span>With cache reuse <b>${money(estimate.cache_reuse_cost)}</b></span><span>Same requests on Claude <b>${money(estimate.claude_cost)}</b></span><span>Projected session total <b>${money(estimate.projected_cost)}</b></span></div>
        <small>Projected total replaces eligible Claude requests with the no-cache estimate. Estimated difference: ${money(Math.abs(estimate.savings))} (${estimate.savings >= 0 ? "lower" : "higher"} cost).</small></div>` : ""}
      <div class="runs-grid"><div class="runs-col"><h3>${runs.length ? `SambaNova activity · ${sambaEvents.length} requests` : "SambaNova estimated coding activity"}</h3>
        ${runs.length ? `<div class="activity-list" data-scroll="samba-${escapeHtml(containerId)}-${escapeHtml(session.id)}">${sambaEvents.map((event) => eventRow(event, "sambanova")).join("")}${unavailableRunRows(runs)}</div>` : hasEstimate ? `<div class="activity-list" data-scroll="est-${escapeHtml(session.id)}">${[...estimate.events].reverse().map((event) => eventRow(event, "sambanova", true)).join("")}</div>` : '<p class="empty">No recorded SambaNova run or eligible coding-tool request in this session.</p>'}</div>
        <div class="runs-col"><h3>Claude model activity · ${session.events.length} requests</h3><div class="activity-list" data-scroll="claude-${escapeHtml(session.id)}">${[...session.events].reverse().map((event) => eventRow(event, "claude")).join("") || '<p class="empty">No Claude model requests.</p>'}</div></div>
      </details>
    </article>`;
  }).join("");
  container.querySelectorAll("[data-scroll]").forEach((el) => { el.scrollTop = scrollPositions.get(el.dataset.scroll) || 0; });
}

function renderRates(data) {
  const rates = data.rates;
  setText("pricingDate", `Verified ${rates._verified_at} · USD per 1M tokens`);
  document.getElementById("rateRows").innerHTML = ["sambanova", "claude"].flatMap((provider) =>
    Object.entries(rates[provider]).filter(([name]) => !name.startsWith("_")).map(([name, rate]) =>
      `<tr><td>${escapeHtml(name)}</td><td>${rate.input.toFixed(2)}</td><td>${rate.output.toFixed(2)}</td><td>${rate.cached_input == null ? "Not offered" : rate.cached_input.toFixed(2)}</td><td>${rate.cache_write_5m == null ? "—" : rate.cache_write_5m.toFixed(2)}</td><td>${rate.cache_write_1h == null ? "—" : rate.cache_write_1h.toFixed(2)}</td></tr>`)).join("");
}

function renderSessionBrowser(data) {
  const index = data.session_index || [];
  const picker = document.getElementById("sessionPicker");
  const signature = JSON.stringify(index);
  if (signature !== sessionIndexSignature) {
    picker.innerHTML = '<option value="">Choose a saved session…</option>' + index.map((session) => {
      const timestamp = new Date(session.updated_at);
      const date = Number.isNaN(timestamp.getTime()) ? "Unknown date" : timestamp.toLocaleString();
      return `<option value="${escapeHtml(session.id)}">${escapeHtml(`${date} · ${session.cwd || "unknown folder"} · ${session.id}`)}</option>`;
    }).join("");
    sessionIndexSignature = signature;
  }
  picker.value = selectedSessionId;
  picker.disabled = !index.length;
  setText("sessionCount", `Latest ${data.sessions.length} of ${index.length} saved sessions`);
  setText("sessionPickerStatus", data.selected_session_missing
    ? "This session is no longer available in the logs. Choose another session."
    : selectedSessionId
      ? "Showing your selected session below. Clear the selection to close it."
      : index.length ? `${index.length} saved sessions available, including older sessions.` : "No sessions with recorded usage found in the logs.");
  if (data.selected_session) renderSessions([data.selected_session], "selectedSession");
  else document.getElementById("selectedSession").innerHTML = "";
}

let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  const requestedSessionId = selectedSessionId;
  try {
    const response = await fetch(`/api/metrics?session_id=${encodeURIComponent(requestedSessionId)}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (requestedSessionId !== selectedSessionId) return;
    const totals = data.totals;
    setText("updated", `Checked ${new Date(data.updated_at).toLocaleTimeString()}`);
    const source = data.log_source;
    setText("logSource", source?.kind === "snapshot"
      ? `VM log snapshot · Exported ${source.manifest?.exported_at ? new Date(source.manifest.exported_at).toLocaleString() : "at an unknown time"} · ${source.folder}${source.manifest?.warnings?.length ? " · Export warnings: " + source.manifest.warnings.join(" ") : ""}`
      : "Local logs · Refreshed every 5 seconds");
    setText("hybridCost", money(totals.hybrid_cost));
    setText("allClaudeCost", money(totals.all_claude_cost));
    setText("savings", money(Math.abs(totals.savings || 0)));
    setText("savingsLabel", totals.savings < 0 ? "Estimated extra cost" : "Estimated savings");
    document.getElementById("savingsCard").className = totals.savings < 0 ? "extra-score" : "win";
    setText("savingsPct", `${Math.abs(Number(totals.savings_pct || 0)).toFixed(1)}% ${totals.savings < 0 ? "more" : "less"} than all-Claude`);
    setText("totalTokens", ints(totals.claude_tokens.total + totals.sambanova_tokens.total));
    setText("modelLabel", `Comparison: ${(totals.comparison_models || []).join(", ") || totals.dominant_claude_model}`);
    renderRates(data);
    latestSessions = data.sessions;
    renderRecentSessions();
    renderSessionBrowser(data);
  } catch (error) { setText("updated", `Unable to refresh: ${error.message}. Retrying…`); }
  finally {
    refreshing = false;
    if (requestedSessionId !== selectedSessionId) refresh();
  }
}

document.getElementById("sessionPicker").addEventListener("change", (event) => {
  selectedSessionId = event.target.value;
  setText("sessionPickerStatus", selectedSessionId ? "Loading selected session…" : "Selection cleared.");
  document.getElementById("selectedSession").innerHTML = "";
  refresh();
});
document.getElementById("sessionFilter").addEventListener("change", renderRecentSessions);
document.getElementById("directSetup").addEventListener("click", () => showSetup("direct", true));
document.getElementById("hybridSetup").addEventListener("click", () => showSetup("hybrid", true));
document.getElementById("guideDirect").addEventListener("click", () => showSetup("direct"));
document.getElementById("guideHybrid").addEventListener("click", () => showSetup("hybrid"));
const remoteCommands = {
  Prepare: `COST_LENS_VM="user@your-vm"
ssh "$COST_LENS_VM" 'mkdir -p "$HOME/cost-lens-tools"'
scp provider_tracking.py timing.py scripts/export_logs.py "$COST_LENS_VM:cost-lens-tools/"
ssh "$COST_LENS_VM" 'python3 "$HOME/cost-lens-tools/provider_tracking.py" --install --output "$HOME/.local/share/cost-lens/claude_providers.jsonl"'`,
  Sync: `ssh "$COST_LENS_VM" 'python3 "$HOME/cost-lens-tools/export_logs.py" --output "$HOME/cost-lens-export"'
mkdir -p data/imports/my-vm
rsync -az "$COST_LENS_VM:cost-lens-export/" data/imports/my-vm/`,
  Start: `COST_LENS_LOG_DIR="$PWD/data/imports/my-vm" \\
HOST=127.0.0.1 PORT=5055 .venv/bin/python app.py`,
  Offload: `ssh "$COST_LENS_VM" 'python3 "$HOME/cost-lens-tools/export_logs.py" --output "$HOME/cost-lens-export" --runs "/path/to/cost-lens/data/sambanova_runs.jsonl"'`,
};
for (const [name, command] of Object.entries(remoteCommands)) {
  setText(`remote${name}`, command);
  document.getElementById(`copyRemote${name}`).addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(command);
      setText(`remote${name}Status`, "Copied. Review the destination and paths before running.");
    } catch {
      setText(`remote${name}Status`, "Copy unavailable. Select and copy the commands above.");
    }
  });
}
showSetup("direct");
refresh();
setInterval(refresh, 5000);
