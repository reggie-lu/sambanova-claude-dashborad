// Run with: node --test tests/test_frontend.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const tick = () => new Promise(setImmediate);

test('saved-session selection survives refresh, clears, and handles missing logs', async () => {
  const elements = new Map();
  const element = (id) => {
    assert(!['estimateModel', 'estimateSummary'].includes(id), 'Removed panel is not accessed');
    if (!elements.has(id)) elements.set(id, {
      innerHTML: '', textContent: '', value: '', disabled: false, handlers: {},
      attributes: {}, setAttribute(name, value) { this.attributes[name] = value; },
      querySelectorAll: () => [],
      addEventListener(name, handler) { this.handlers[name] = handler; },
    });
    return elements.get(id);
  };
  const tokens = {input: 100, output: 20, cache_read: 0, cache_creation: 0, total: 120};
  const sessions = Array.from({length: 28}, (_, index) => ({
    id: `session-${index}`, cwd: '/test/<folder>', updated_at: '2026-09-22T12:00:00Z',
    models: {'claude-opus-5': 1}, claude: tokens, claude_cost: 1, hybrid_cost: 1,
    sambanova: {...tokens, cost: 0}, matched_sambanova_runs: [], events: [],
    sambanova_estimate: {eligible: true, request_count: 0},
    cost_comparison: {has_offload: false, all_claude_cost: 1, model: 'claude-opus-5'},
  }));
  let missing = false;
  const requests = [];
  const context = vm.createContext({
    document: {getElementById: element}, setInterval() {},
    fetch: async (url) => {
      requests.push(url);
      const id = new URL(url, 'http://localhost').searchParams.get('session_id');
      return {ok: true, json: async () => ({
        updated_at: '2026-09-22T12:00:00Z',
        totals: {claude_tokens: tokens, sambanova_tokens: tokens, savings_pct: 0},
        rates: {_verified_at: '2026-09-21', claude: {}, sambanova: {}},
        sessions: sessions.slice(0, 10), session_index: sessions,
        selected_session: missing ? null : sessions.find((s) => s.id === id),
        selected_session_missing: missing,
      })};
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8'), context);
  await tick();
  assert.equal((element('sessions').innerHTML.match(/<article class="session">/g) || []).length, 10);
  assert.equal(element('sessionCount').textContent, 'Latest 10 of 28 saved sessions');
  assert(element('sessionPicker').innerHTML.includes('session-27'));
  assert(element('sessionPicker').innerHTML.includes('&lt;folder&gt;'));
  assert(element('sessions').innerHTML.includes('/static/claude-icon.png'));
  // Requests from multiple runs are visible directly, with newest first.
  const step = (id, timestamp, usage) => ({
    id, model: 'MiniMax-M2.7', timestamp, usage, input_tokens: 100,
    output_tokens: 20, cost: .01,
    tools: [{name: 'read', detail: id, status: 'completed'}],
  });
  sessions[0].matched_sambanova_runs = [
    {id: 'run-a', model: 'MiniMax-M2.7', tool: 'opencode', status: 'finished',
      steps: [step('old-request', '2026-09-22T10:00:00Z', {}),
              step('new-request', '2026-09-22T12:00:00Z', {})]},
    {id: 'run-b', model: 'MiniMax-M2.7', tool: 'opencode', status: 'running',
      steps: [step('pending-request', '2026-09-22T13:00:00Z', null)]},
    {id: 'run-c', model: 'MiniMax-M2.7', tool: 'opencode', status: 'finished',
      cost: 1, tokens: 100, steps: []},
  ];
  sessions[0].timing = {
    axis_start_ms: 1000, axis_end_ms: 5000,
    attribution: {has_timing: true, duration_ms: {claude: 1000, sambanova: 3000, unknown: 0},
      segments: [{provider: 'claude', start_ms: 1000, end_ms: 2000, duration_ms: 1000},
                 {provider: 'sambanova', start_ms: 2000, end_ms: 5000, duration_ms: 3000}]},
    claude: {start_ms: 1000, end_ms: 4000, duration_ms: 3000},
    sambanova_runs: [{start_ms: 2000, end_ms: 5000, duration_ms: 3000,
      model: 'MiniMax-M2.7', end_kind: 'finished', timed_tool_count: 1,
      tool_count: 2, tool_duration_ms: 250}],
  };
  await vm.runInContext('refresh()', context);
  const activity = element('sessions').innerHTML;
  assert(activity.includes('SambaNova activity · 3 requests'));
  assert(activity.includes('Claude outside SambaNova'));
  assert(activity.includes('SambaNova run 1'));
  assert(activity.includes('timing-bar claude-time'));
  assert(activity.includes('timing-bar samba-time'));
  assert.equal((activity.match(/class="timing-track"/g) || []).length, 1);
  assert(activity.includes('left:25.0000%;width:75.0000%'));
  assert(activity.includes('250 ms across 1/2 timed calls'));
  assert(!activity.includes('NaN'));
  const zeroTiming = vm.runInContext('timingBox({timing: {axis_start_ms: 0, axis_end_ms: 0, attribution: {has_timing: true, duration_ms: {claude: 0, sambanova: 0, unknown: 0}, segments: []}, claude: {start_ms: 0, end_ms: 0, duration_ms: 0}, sambanova_runs: []}})', context);
  assert(zeroTiming.includes('0 ms'));
  assert(zeroTiming.includes('No recorded SambaNova run'));
  assert(!zeroTiming.includes('NaN'));
  assert(!zeroTiming.includes('Infinity'));
  assert.equal((activity.match(/class="activity-step"/g) || []).length, 3);
  assert(activity.indexOf('pending-request') < activity.indexOf('new-request'));
  assert(activity.indexOf('new-request') < activity.indexOf('old-request'));
  assert(activity.includes('Usage pending'));
  assert(activity.includes('Request details unavailable for this run'));
  assert(activity.includes('Explore tokens &amp; requests') || activity.includes('Explore tokens & requests'));
  assert(activity.includes('data-disclosure="sessions-session-0-requests"'));
  // Opening request details survives automatic refresh and filtering.
  element('sessions').querySelectorAll = (selector) => selector === '[data-disclosure]'
    ? [{dataset: {disclosure: 'sessions-session-0-requests'}, open: true}] : [];
  sessions[0].updated_at = '2026-09-22T12:01:00Z';
  await vm.runInContext('refresh()', context);
  assert(element('sessions').innerHTML.includes('data-disclosure="sessions-session-0-requests" open'));
  element('sessions').querySelectorAll = () => [];
  // The framework name does not assign direct M3 traffic to Claude's provider lane.
  const direct = sessions[1];
  direct.models = {};
  direct.direct_sambanova_events = [step('direct-m3', '2026-09-22T12:00:00Z', {})];
  direct.matched_sambanova_runs = [{id: 'direct', source: 'claude-code',
    tool: 'Claude Code', model: 'MiniMax-M3', steps: direct.direct_sambanova_events}];
  direct.direct_sambanova_events[0].model = 'MiniMax-M3';
  direct.timing = {
    axis_start_ms: 1000, axis_end_ms: 5000,
    attribution: {has_timing: true, duration_ms: {claude: 0, sambanova: 4000, unknown: 0},
      segments: [{provider: 'sambanova', start_ms: 1000, end_ms: 5000, duration_ms: 4000}]},
    sambanova_runs: [{model: 'MiniMax-M3', end_kind: 'observed', duration_ms: 4000}],
  };
  context.directFixture = direct;
  vm.runInContext('renderSessions([directFixture], "selectedSession")', context);
  const directHtml = element('selectedSession').innerHTML;
  assert(directHtml.includes('MiniMax-M3'));
  assert(directHtml.includes('SambaNova activity · 1 requests'));
  assert(directHtml.includes('Claude Code → SambaNova'));
  assert(directHtml.includes('observed transcript span'));
  assert(directHtml.includes('timing-bar samba-time'));
  assert(!directHtml.includes('claude-time'));
  assert(!directHtml.includes('Usage pending'));
  assert(directHtml.includes('Claude model activity · 0 requests'));
  assert(directHtml.includes('01 · Full SambaNova'));
  element('sessionFilter').value = 'direct';
  element('sessionFilter').handlers.change();
  assert.equal((element('sessions').innerHTML.match(/<article class="session">/g) || []).length, 1);
  assert(element('sessions').innerHTML.includes('session-1'));
  await vm.runInContext('refresh()', context);
  assert.equal(element('filterStatus').textContent, '1 of 10 recent sessions shown');
  element('sessionFilter').value = 'mixed';
  element('sessionFilter').handlers.change();
  assert(element('sessions').innerHTML.includes('No sessions to show here yet.'));
  element('sessionFilter').value = 'all';
  element('sessionFilter').handlers.change();
  element('hybridSetup').handlers.click();
  assert.equal(element('setupGuide').open, true);
  assert.equal(element('guideHybrid').attributes['aria-pressed'], 'true');
  assert(element('setupContent').innerHTML.includes('/code MiniMax-M3'));
  assert(!element('setupContent').innerHTML.includes('Switch back to Claude'));
  element('guideDirect').handlers.click();
  assert(element('setupContent').innerHTML.includes('ANTHROPIC_BASE_URL'));
  assert(element('setupContent').innerHTML.includes('YOUR_SAMBANOVA_API_KEY'));
  await element('copySetup').handlers.click();
  assert(element('copyStatus').textContent.includes('Copy unavailable'));
  assert(element('setupContent').innerHTML.includes('Switch back to Claude'));
  let copiedCommands = '';
  context.navigator = {clipboard: {writeText: async (value) => { copiedCommands = value; }}};
  await element('copyReturnToClaude').handlers.click();
  assert.equal(copiedCommands, 'unset ANTHROPIC_BASE_URL\nunset ANTHROPIC_API_KEY\nunset ANTHROPIC_AUTH_TOKEN\nunset ANTHROPIC_MODEL');
  assert(element('copyReturnStatus').textContent.includes('Copied.'));
  await element('copyRemotePrepare').handlers.click();
  assert(copiedCommands.includes('provider_tracking.py timing.py scripts/export_logs.py'));
  await element('copyRemoteSync').handlers.click();
  assert(copiedCommands.includes('rsync -az'));
  assert(copiedCommands.includes('data/imports/my-vm/'));
  await element('copyRemoteStart').handlers.click();
  assert(copiedCommands.includes('COST_LENS_LOG_DIR='));
  assert(copiedCommands.includes('HOST=127.0.0.1 PORT=5055'));
  assert(element('remoteStartStatus').textContent.includes('Copied.'));
  element('sessionPicker').handlers.change({target: {value: 'session-27'}});
  await tick();
  assert(requests.at(-1).includes('session_id=session-27'));
  assert(element('selectedSession').innerHTML.includes('session-27'));
  await vm.runInContext('refresh()', context);
  assert.equal(element('sessionPicker').value, 'session-27');
  assert(element('selectedSession').innerHTML.includes('session-27'));
  missing = true;
  await vm.runInContext('refresh()', context);
  assert.equal(element('selectedSession').innerHTML, '');
  assert(element('sessionPickerStatus').textContent.includes('no longer available'));
  missing = false;
  element('sessionPicker').handlers.change({target: {value: ''}});
  await tick();
  assert.equal(element('selectedSession').innerHTML, '');
  assert.equal(element('sessionPicker').value, '');
});

test('latency timeline survives malformed, empty and live data', async () => {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      innerHTML: '', textContent: '', value: '', handlers: {}, setAttribute() {},
      querySelectorAll: () => [], addEventListener(name, handler) { this.handlers[name] = handler; },
    });
    return elements.get(id);
  };
  const tokens = {input: 0, output: 0, cache_read: 0, cache_creation: 0, total: 0};
  const context = vm.createContext({
    document: {getElementById: element}, setInterval() {},
    fetch: async () => ({ok: true, json: async () => ({
      updated_at: '2026-11-22T00:00:00Z', totals: {claude_tokens: tokens, sambanova_tokens: tokens},
      rates: {_verified_at: '2026-11-22', claude: {}, sambanova: {}}, sessions: [], session_index: [],
      selected_session: null, latency_comparison: [],
    })}),
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8'), context);
  await tick();
  const run = (code, vars = {}) => { Object.assign(context, vars); return vm.runInContext(code, context); };
  const CATS = ['generation', 'first_token', 'approval', 'tool', 'overhead', 'idle'];
  const stream = (label, segments, extra = {}) => ({
    label, request_count: 1, tool_count: 0, models: {}, approvals: {you: 0, 'auto-mode check': 0, rule: 0, unknown: 0},
    tool_summary: [], requests: [], breakdown_ms: {}, ttft_p50_ms: null, tokens_per_s_p50: null,
    timeline: {start_ms: 1e12, end_ms: 1e12 + 2e6, live: false, categories: CATS, labels: ['', 'Bash', '<img src=x onerror=alert(1)>'], segments},
    ...extra,
  });

  // Collapsing a long idle gap keeps active time readable; the axis stays monotonic and bounded.
  const lanes = [[{start: 0, end: 1000, category: 'tool'}, {start: 1000, end: 1e6, category: 'idle'},
                  {start: 1e6, end: 1e6 + 1000, category: 'generation'}]];
  const collapsed = run('buildAxis(L, 0, 1e6 + 1000, true)', {L: lanes});
  const linear = run('buildAxis(L, 0, 1e6 + 1000, false)', {L: lanes});
  assert(collapsed.x(1000) > 0.3 && collapsed.x(1e6) < 0.7, 'collapsed gap is a sliver');
  assert(linear.x(1000) < 0.01, 'uncollapsed axis is proportional');
  let previous = -1;
  for (let t = -10; t <= 1e6 + 2000; t += 50000) { const x = collapsed.x(t); assert(x >= previous && x >= 0 && x <= 1); previous = x; }
  const empty = run('buildAxis([], 5, 5, true)');
  assert(Number.isFinite(empty.x(5)) && empty.x(5) >= 0 && empty.x(5) <= 1);

  // Bad rows are dropped, labels are escaped, a missing main timeline does not throw.
  const junkRows = [[0, 1000, 3, 2], [1000, -5, 3, 1], [NaN, 10, 0, 0], [2000, 10, 99, 0], 'row', [3000, 5000, 0, 1]];
  const session = {id: 's', latency: {main: {...stream('Main agent', []), timeline: null},
                                     subagents: [stream('Subagent · docs', junkRows)]}};
  const html = run('timelineBox(S, "k1")', {S: session});
  assert(html.includes('docs'));
  assert(!html.includes('lat-undefined'));
  assert(!html.includes('<img src=x'));
  assert(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert.equal(run('timelineBox({latency: {main: {timeline: null}, subagents: []}}, "k2")'), '');
  assert.equal(run('timelineBox({latency: {main: {timeline: {start_ms: "x", end_ms: 1}}}}, "k3")'), '');

  // Live: badge names the current phase, the window defaults to 15 minutes, idle subagents are noted.
  const live = stream('Main agent', [[0, 2e6, 3, 1]], {current: 'tool'});
  live.timeline.live = true;
  const liveHtml = run('timelineBox(S, "k4")', {S: {id: 'l', latency: {main: live, subagents: [stream('Subagent · old', [[0, 1000, 3, 1]])]}}});
  assert(liveHtml.includes('Live · now Running tools'));
  assert(liveHtml.includes('value="900000" selected'));
  assert(liveHtml.includes('1 subagent idle in this window'));
  assert(liveHtml.includes('tl-now'));

  // Changing the window is remembered per session.
  const select = {dataset: {timelineKey: 'k4', timelineField: 'win'}, type: 'select-one', value: 'all',
                  closest: () => ({querySelector: (q) => q === 'select' ? select : {checked: false}})};
  run('onTimelineChange(E)', {E: {target: select}});
  assert.equal(run('timelineViews.get("k4").win'), 'all');
  assert.equal(run('timelineViews.get("k4").collapse'), false);
  assert(run('timelineBox(S, "k4")', {S: {id: 'l', latency: {main: live, subagents: []}}}).includes('value="all" selected'));

  // A refresh never rebuilds a card while one of its controls is focused (an open dropdown would close).
  const container = element('sessions');
  container.innerHTML = 'kept';
  container.contains = () => true;
  context.document.activeElement = {matches: (selector) => selector.includes('select')};
  run('renderSessions(L, "sessions")', {L: [{id: 'x'}]});
  assert.equal(container.innerHTML, 'kept');
  context.document.activeElement = null;

  // Zero active time: no fake bottleneck, missing request times render as a dash.
  const quiet = stream('Main agent', [[0, 2e6, 5, 0]], {
    requests: [{request_ms: null, end_ms: null, model: 'GLM-5.3', ttft_ms: null, ttft_measured: false,
                generation_ms: null, output_tokens: 0, tokens_per_s: null, input_tokens: 0,
                cache_read_tokens: 0, tools: [], tool_ms: 0, server: null}],
  });
  const box = run('latencyBox(S, "k5")', {S: {id: 'q', latency: {main: quiet, subagents: []}}});
  assert(box.includes('No active time recorded'));
  assert(box.includes('<b>—</b>'));
  assert(!box.includes('1970'));
  assert(box.includes('Server-side split not recorded'));
});
