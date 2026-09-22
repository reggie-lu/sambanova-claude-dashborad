const money = (value) => `$${Number(value || 0).toFixed(4)}`;
const ints = (value) => Number(value || 0).toLocaleString();
const setText = (id, value) => { document.getElementById(id).textContent = value; };
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
}[char]));
let selectedSessionId = "";
let sessionIndexSignature = "";
const scrollPositions = new Map();

function toolRows(tools = []) {
  return tools.map((tool) => `<li><b>${escapeHtml(tool.name)}</b>
    <span>${escapeHtml(tool.detail)}</span>${tool.status ? `<small>${escapeHtml(tool.status)}</small>` : ""}</li>`).join("");
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
    run_label: runs.length > 1 ? `${run.tool || "opencode"} · run ${run.id || "unknown"}` : "",
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
  if (!comparison.has_offload) return '<div class="cost-comparison neutral">No SambaNova offload recorded · all-Claude and combined costs are equal.</div>';
  const difference = comparison.savings;
  const equal = Math.abs(difference) < 1e-10;
  const direction = difference > 0 ? "less" : "more";
  const savingsPct = comparison.savings_pct == null ? "" : ` · ${Math.abs(comparison.savings_pct).toFixed(1)}% ${direction} than all-Claude`;
  const premium = comparison.all_claude_premium_pct == null ? "Percentage comparison unavailable because combined cost is zero." :
    `All-Claude would cost ${Math.abs(comparison.all_claude_premium_pct).toFixed(1)}% ${difference >= 0 ? "more" : "less"} than the combined run.`;
  return `<div class="cost-comparison ${equal ? "neutral" : difference > 0 ? "saving" : "extra-cost"}">
    <strong>${equal ? "No estimated cost difference" : `${difference > 0 ? "Estimated saving" : "Estimated extra cost"}: ${money(Math.abs(difference))}${savingsPct}`}</strong>
    <p>${equal ? "All-Claude and combined token costs are equal." : premium}</p>
    <small>Same-token estimate: keep recorded Claude cost and price SambaNova tokens on ${escapeHtml(comparison.model)}, preserving cache reads and assuming 5-minute cache writes. Actual model usage and caching may differ.${comparison.model_basis === "fallback" ? " No Claude model recorded; using the overall comparison model." : ""}${comparison.rate_fallback ? " This model uses fallback pricing." : ""}${comparison.incomplete ? " Run incomplete: based on usage recorded so far." : ""}${comparison.estimated_tokens ? " Includes estimated token counts." : ""}</small>
  </div>`;
}

function renderSessions(sessions, containerId = "sessions") {
  const container = document.getElementById(containerId);
  container.querySelectorAll("[data-scroll]").forEach((el) => scrollPositions.set(el.dataset.scroll, el.scrollTop));
  if (!sessions.length) { container.innerHTML = '<div class="empty">No Claude sessions found.</div>'; return; }
  container.innerHTML = sessions.map((session) => {
    const samba = session.sambanova;
    const estimate = session.sambanova_estimate;
    const runs = session.matched_sambanova_runs;
    const sambaEvents = sambaRequests(runs);
    const models = Object.keys(session.models).join(", ");
    const hasEstimate = estimate.eligible && estimate.request_count > 0;
    const comparison = session.cost_comparison;
    return `<article class="session">
      <div class="session-top"><div><strong>${escapeHtml(session.id)}</strong><small>${escapeHtml(session.cwd || "unknown cwd")}</small></div>
        <div class="right"><b>${ints(session.claude.total + samba.total)} recorded tokens</b><small>${escapeHtml(models)}</small></div></div>
      <div class="session-cost-grid">
        <div><span class="provider-label"><img src="/static/claude-icon.png" alt="" width="18" height="18">Claude recorded usage cost</span><strong>${money(session.claude_cost)}</strong><small>${ints(session.claude.total)} tokens · ${session.events.length} requests</small></div>
        <div><span class="provider-label"><img src="/static/sambanova-icon.png" alt="" width="18" height="18">SambaNova recorded usage cost</span><strong>${money(samba.cost)}</strong><small>${ints(samba.total)} tokens · ${sambaEvents.length} recorded requests · ${runs.length} ${runs.length === 1 ? "run" : "runs"}</small></div>
        <div><span class="provider-label"><img src="/static/claude-icon.png" alt="" width="18" height="18"><img src="/static/sambanova-icon.png" alt="" width="18" height="18">Combined recorded usage cost</span><strong>${money(session.hybrid_cost)}</strong><small>Claude + SambaNova</small></div>
        <div class="all-claude-card"><span class="provider-label"><img src="/static/claude-icon.png" alt="" width="18" height="18">If all usage ran on Claude</span><strong>${money(comparison.all_claude_cost)}</strong><small>Estimated · ${escapeHtml(comparison.model)}</small></div>
      </div>
      ${comparisonSummary(comparison)}
      <div class="session-meta"><span>Claude fresh in ${ints(session.claude.input)}</span><span>Out ${ints(session.claude.output)}</span><span>Cache read ${ints(session.claude.cache_read)}</span><span>Cache write ${ints(session.claude.cache_creation)}</span></div>
      <div class="session-meta"><span>SN fresh in ${ints(samba.input)}</span><span>Out ${ints(samba.output)}</span><span>Cache read ${ints(samba.cache_read)}</span><span>Cache write ${ints(samba.cache_creation)}</span></div>
      ${hasEstimate ? `<div class="estimate-box"><div><b>SambaNova estimate · ${escapeHtml(estimate.model)}</b><span class="estimate-tag">Hypothetical</span></div>
        <p>${estimate.request_count} coding requests · ${estimate.tool_count} coding tool calls · ${ints(estimate.tokens.total)} source tokens</p>
        <div class="estimate-values"><span>No cache reuse <b>${money(estimate.cost)}</b></span><span>With cache reuse <b>${money(estimate.cache_reuse_cost)}</b></span><span>Same requests on Claude <b>${money(estimate.claude_cost)}</b></span><span>Projected session total <b>${money(estimate.projected_cost)}</b></span></div>
        <small>Projected total replaces eligible Claude requests with the no-cache estimate. Estimated difference: ${money(Math.abs(estimate.savings))} (${estimate.savings >= 0 ? "lower" : "higher"} cost).</small></div>` : ""}
      <div class="runs-grid"><div class="runs-col"><h3>${runs.length ? `SambaNova activity · ${sambaEvents.length} requests` : "SambaNova estimated coding activity"}</h3>
        ${runs.length ? `<div class="activity-list" data-scroll="samba-${escapeHtml(containerId)}-${escapeHtml(session.id)}">${sambaEvents.map((event) => eventRow(event, "sambanova")).join("")}${unavailableRunRows(runs)}</div>` : hasEstimate ? `<div class="activity-list" data-scroll="est-${escapeHtml(session.id)}">${[...estimate.events].reverse().map((event) => eventRow(event, "sambanova", true)).join("")}</div>` : '<p class="empty">No recorded SambaNova run or eligible coding-tool request in this session.</p>'}</div>
        <div class="runs-col"><h3>Claude Code activity · ${session.events.length} requests</h3><div class="activity-list" data-scroll="claude-${escapeHtml(session.id)}">${[...session.events].reverse().map((event) => eventRow(event, "claude")).join("") || '<p class="empty">No Claude requests.</p>'}</div></div>
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
    setText("updated", `Updated ${new Date(data.updated_at).toLocaleTimeString()}`);
    setText("hybridCost", money(totals.hybrid_cost));
    setText("allClaudeCost", money(totals.all_claude_cost));
    setText("savings", money(totals.savings));
    setText("savingsPct", `${Number(totals.savings_pct).toFixed(1)}% estimated difference`);
    setText("totalTokens", ints(totals.claude_tokens.total + totals.sambanova_tokens.total));
    setText("modelLabel", `Comparison: ${(totals.comparison_models || []).join(", ") || totals.dominant_claude_model}`);
    renderRates(data);
    renderSessions(data.sessions);
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
refresh();
setInterval(refresh, 5000);
