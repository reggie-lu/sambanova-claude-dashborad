const money = (value) => `$${Number(value || 0).toFixed(4)}`;
const ints = (value) => Number(value || 0).toLocaleString();
const setText = (id, value) => { document.getElementById(id).textContent = value; };
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
}[char]));
let selectedModel = "MiniMax-M2.7";
const openDetails = new Set();
const seenRuns = new Set();
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
    ${estimate ? `<small>Claude request: ${money(event.claude_cost)} · With cache reuse: ${money(event.cache_reuse_cost)}</small>` : ""}
  </div>`;
}

function runRows(runs) {
  return runs.map((run) => {
    const key = `run-${run.id}`;
    if (!seenRuns.has(key)) { openDetails.add(key); seenRuns.add(key); }
    return `<details class="run-detail" data-key="${escapeHtml(key)}" ${openDetails.has(key) ? "open" : ""}>
      <summary><span class="pill samba-pill">sambanova</span> ${escapeHtml(run.tool)} · ${escapeHtml(run.status)}
        <strong>${money(run.cost)}${run.estimated ? " (estimated tokens)" : ""}</strong>
        <small>${escapeHtml(run.model)} · ${ints(run.tokens)} tokens · ${run.steps.length} model steps · ${run.tool_count} tool calls${run.rate_fallback ? " · Fallback rate" : ""}</small></summary>
      <p class="step-note">${usageText(run)}. Costs are per model step; tool calls share that request.</p>
      <div class="activity-list" data-scroll="${escapeHtml(key)}">${run.steps.length ? run.steps.map((step) => eventRow(step, "sambanova")).join("") : '<p class="empty">Detailed log unavailable. This run has aggregate usage only; individual actions cannot be recovered.</p>'}</div>
    </details>`;
  }).join("");
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

function renderSessions(sessions) {
  const container = document.getElementById("sessions");
  container.querySelectorAll("details[data-key]").forEach((el) => {
    if (el.open) openDetails.add(el.dataset.key); else openDetails.delete(el.dataset.key);
  });
  container.querySelectorAll("[data-scroll]").forEach((el) => scrollPositions.set(el.dataset.scroll, el.scrollTop));
  setText("sessionCount", `${sessions.length} recent sessions`);
  if (!sessions.length) { container.innerHTML = '<div class="empty">No Claude sessions found.</div>'; return; }
  container.innerHTML = sessions.map((session) => {
    const samba = session.sambanova;
    const estimate = session.sambanova_estimate;
    const runs = session.matched_sambanova_runs;
    const models = Object.keys(session.models).join(", ");
    const hasEstimate = estimate.eligible && estimate.request_count > 0;
    const comparison = session.cost_comparison;
    return `<article class="session">
      <div class="session-top"><div><strong>${escapeHtml(session.id)}</strong><small>${escapeHtml(session.cwd || "unknown cwd")}</small></div>
        <div class="right"><b>${ints(session.claude.total + samba.total)} recorded tokens</b><small>${escapeHtml(models)}</small></div></div>
      <div class="session-cost-grid">
        <div><span>Claude recorded usage cost</span><strong>${money(session.claude_cost)}</strong><small>${ints(session.claude.total)} tokens · ${session.events.length} requests</small></div>
        <div><span class="provider-label"><img src="/static/sambanova-icon.png" alt="" width="18" height="18">SambaNova recorded usage cost</span><strong>${money(samba.cost)}</strong><small>${ints(samba.total)} tokens · ${runs.length} runs</small></div>
        <div><span>Combined recorded usage cost</span><strong>${money(session.hybrid_cost)}</strong><small>Claude + SambaNova</small></div>
        <div class="all-claude-card"><span>If all usage ran on Claude</span><strong>${money(comparison.all_claude_cost)}</strong><small>Estimated · ${escapeHtml(comparison.model)}</small></div>
      </div>
      ${comparisonSummary(comparison)}
      <div class="session-meta"><span>Claude fresh in ${ints(session.claude.input)}</span><span>Out ${ints(session.claude.output)}</span><span>Cache read ${ints(session.claude.cache_read)}</span><span>Cache write ${ints(session.claude.cache_creation)}</span></div>
      <div class="session-meta"><span>SN fresh in ${ints(samba.input)}</span><span>Out ${ints(samba.output)}</span><span>Cache read ${ints(samba.cache_read)}</span><span>Cache write ${ints(samba.cache_creation)}</span></div>
      ${hasEstimate ? `<div class="estimate-box"><div><b>SambaNova estimate · ${escapeHtml(estimate.model)}</b><span class="estimate-tag">Hypothetical</span></div>
        <p>${estimate.request_count} coding requests · ${estimate.tool_count} coding tool calls · ${ints(estimate.tokens.total)} source tokens</p>
        <div class="estimate-values"><span>No cache reuse <b>${money(estimate.cost)}</b></span><span>With cache reuse <b>${money(estimate.cache_reuse_cost)}</b></span><span>Same requests on Claude <b>${money(estimate.claude_cost)}</b></span><span>Projected session total <b>${money(estimate.projected_cost)}</b></span></div>
        <small>Projected total replaces eligible Claude requests with the no-cache estimate. Estimated difference: ${money(Math.abs(estimate.savings))} (${estimate.savings >= 0 ? "lower" : "higher"} cost).</small></div>` : ""}
      <div class="runs-grid"><div class="runs-col"><h3>${runs.length ? "SambaNova activity" : "SambaNova estimated coding activity"}</h3>
        ${runs.length ? runRows(runs) : hasEstimate ? `<div class="activity-list" data-scroll="est-${escapeHtml(session.id)}">${[...estimate.events].reverse().map((event) => eventRow(event, "sambanova", true)).join("")}</div>` : '<p class="empty">No recorded SambaNova run or eligible coding-tool request in this session.</p>'}</div>
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
  const select = document.getElementById("estimateModel");
  if (!select.options.length) {
    Object.keys(rates.sambanova).filter((name) => !name.startsWith("_")).forEach((name) => select.add(new Option(name, name)));
    select.value = selectedModel;
  }
}

let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  const model = selectedModel;
  try {
    const response = await fetch(`/api/metrics?estimate_model=${encodeURIComponent(model)}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (model !== selectedModel) return;
    const totals = data.totals;
    setText("updated", `Updated ${new Date(data.updated_at).toLocaleTimeString()}`);
    setText("hybridCost", money(totals.hybrid_cost));
    setText("allClaudeCost", money(totals.all_claude_cost));
    setText("savings", money(totals.savings));
    setText("savingsPct", `${Number(totals.savings_pct).toFixed(1)}% estimated difference`);
    setText("totalTokens", ints(totals.claude_tokens.total + totals.sambanova_tokens.total));
    setText("modelLabel", `Comparison: ${(totals.comparison_models || []).join(", ") || totals.dominant_claude_model}`);
    setText("estimateSummary", `${data.estimation.eligible_requests} eligible requests · ${money(data.estimation.cost)} SambaNova estimate without cache reuse · ${money(data.estimation.projected_cost)} projected total across all sessions`);
    renderRates(data);
    renderSessions(data.sessions);
  } catch (error) { setText("updated", `Unable to refresh: ${error.message}. Retrying…`); }
  finally { refreshing = false; }
}

document.getElementById("estimateModel").addEventListener("change", (event) => {
  selectedModel = event.target.value;
  refresh();
});
refresh();
setInterval(refresh, 5000);
