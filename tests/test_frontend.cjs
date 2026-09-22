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
  assert(!activity.includes('<details'));
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
