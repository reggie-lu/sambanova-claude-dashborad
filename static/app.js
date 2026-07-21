const money = (value) => `$${Number(value || 0).toFixed(4)}`;
const ints = (value) => Number(value || 0).toLocaleString();

function setText(id, value) {
  document.getElementById(id).textContent = value;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  }[char]));
}

function providerPill(provider) {
  return `<span class="pill ${provider === "sambanova" ? "samba-pill" : ""}">${provider}</span>`;
}

function renderSessions(sessions) {
  const container = document.getElementById("sessions");
  setText("sessionCount", `${Math.min(sessions.length, 5)} of ${sessions.length} sessions`);
  if (!sessions.length) {
    container.innerHTML = `<div class="empty">No Claude sessions found.</div>`;
    return;
  }
  container.innerHTML = sessions.slice(0, 5).map((session) => {
    const models = Object.keys(session.models || {}).join(", ") || "unknown";
    const sambaRuns = session.matched_sambanova_runs || [];
    const claudeEvents = session.events || [];
    const samba = session.sambanova || { total: 0, cost: 0 };
    const hybridCost = Number(session.hybrid_cost ?? session.claude_cost ?? 0);
    const totalTokens = Number(session.claude.total || 0) + Number(samba.total || 0);
    const sambaRunRows = sambaRuns.slice(0, 5).map((run) => `
      <div class="event">
        ${providerPill("sambanova")}
        <span>${escapeHtml(run.tool || "opencode")} · ${escapeHtml(run.status || "finished")}</span>
        <span>${escapeHtml(run.model || "MiniMax")}</span>
        <b>${
          run.status === "running"
            ? `pid ${run.pid || "?"} · ${run.log_path ? escapeHtml(run.log_path) : "waiting for final tokens"}`
            : run.status === "stale"
              ? "stale: no final record"
              : `${ints(run.tokens || 0)} tok · ${money(run.cost || 0)}`
        }</b>
      </div>
    `).join("");
    const claudeRunRows = claudeEvents.slice(0, 10).map((ev) => `
      <div class="event">
        ${providerPill("claude")}
        <span>${escapeHtml(ev.agent || "Claude Code")} · ${escapeHtml(ev.model || "unknown")}</span>
        <span>${ints(ev.input_tokens || 0)} in · ${ints(ev.output_tokens || 0)} out</span>
        <b>${money(ev.cost || 0)}</b>
      </div>
    `).join("");
    return `
      <article class="session">
        <div class="session-top">
          <div>
            <strong>${session.id}</strong>
            <small>${session.cwd || "unknown cwd"}</small>
          </div>
          <div class="right">
            <b>${ints(totalTokens)} tokens</b>
            <small>${money(hybridCost)} hybrid · ${models}</small>
          </div>
        </div>
        <div class="session-cost-grid">
          <div>
            <span>Claude Code tokens / cost</span>
            <strong>${money(session.claude_cost)}</strong>
            <small>${ints(session.claude.total)} tokens</small>
          </div>
          <div>
            <span>SambaNova tokens / cost</span>
            <strong>${money(samba.cost)}</strong>
            <small>${ints(samba.total)} tokens · ${sambaRuns.length} runs</small>
          </div>
          <div>
            <span>Combined</span>
            <strong>${money(hybridCost)}</strong>
            <small>Claude + coding tool</small>
          </div>
        </div>
        <div class="session-meta">
          <span>Input ${ints(session.claude.input)}</span>
          <span>Output ${ints(session.claude.output)}</span>
          <span>Cache ${ints(session.claude.cache_creation + session.claude.cache_read)}</span>
          <span>SN input ${ints(samba.input || 0)}</span>
          <span>SN output ${ints(samba.output || 0)}</span>
          <span>SN cache ${ints(samba.cache_read || 0)}</span>
        </div>
        <div class="runs-grid">
          <div class="runs-col">
            <h3>SambaNova Runs</h3>
            <div class="events">${sambaRunRows || "<span class='empty'>No SambaNova runs matched to this session yet.</span>"}</div>
          </div>
          <div class="runs-col">
            <h3>Claude Code Runs</h3>
            <div class="events">${claudeRunRows || "<span class='empty'>No Claude events in this session.</span>"}</div>
          </div>
        </div>
      </article>
    `;
  }).join("");
}

async function refresh() {
  const response = await fetch("/api/metrics", { cache: "no-store" });
  const data = await response.json();
  const totals = data.totals;
  setText("updated", `Updated ${new Date(data.updated_at).toLocaleTimeString()}`);
  setText("hybridCost", money(totals.hybrid_cost));
  setText("allClaudeCost", money(totals.all_claude_cost));
  setText("savings", money(totals.savings));
  setText("savingsPct", `${Number(totals.savings_pct || 0).toFixed(1)}% cheaper`);
  setText("totalTokens", ints((totals.claude_tokens.total || 0) + (totals.sambanova_tokens.total || 0)));
  setText("modelLabel", `Claude model: ${totals.dominant_claude_model}`);
  renderSessions(data.sessions || []);
}

refresh();
setInterval(refresh, 2500);
