"""Claude Code is a framework: provider accounting must follow the actual model."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class CustomModelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name, value in [('CLAUDE_PROJECTS_DIR', self.root),
                            ('SAMBANOVA_RUNS_PATH', self.root / 'runs.log')]:
            patched = patch.object(app, name, value)
            patched.start()
            self.addCleanup(patched.stop)

    def record(self, number, model='MiniMax-M3', usage=None, agent=None):
        record = {
            'timestamp': f'2026-09-22T12:00:{number:02d}Z', 'cwd': '/repo',
            'message': {'id': f'msg-{number}', 'model': model, 'role': 'assistant',
                        'usage': usage if usage is not None else
                            {'input_tokens': 1000, 'output_tokens': 20},
                        'content': [{'type': 'tool_use', 'id': f'tool-{number}',
                                     'name': 'Read', 'input': {'file_path': 'a.py'}}]},
        }
        if agent:
            record['agentId'] = agent
        return record

    def write(self, records, path='session.jsonl'):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('\n'.join(map(json.dumps, records)))

    def test_direct_m3_merges_fragments_includes_subagents_and_counts_only_samba(self):
        record = self.record(1)
        self.write([record, record, self.record(3)])
        self.write([self.record(2, agent='worker')], 'session/subagents/agent-worker.jsonl')
        data = app.summarize(selected_session_id='session')
        session = data['selected_session']
        runs = session['matched_sambanova_runs']
        self.assertEqual(sum(len(run['steps']) for run in runs), 3)
        self.assertEqual(sum(run['tool_count'] for run in runs), 3)
        self.assertEqual(session['events'], [])
        self.assertEqual(session['claude']['total'], 0)
        self.assertEqual(session['models'], {})
        self.assertEqual(session['sambanova']['total'], 3060)
        self.assertAlmostEqual(session['sambanova']['cost'], .001944)
        self.assertEqual(data['totals']['sambanova_tokens']['total'], 3060)
        self.assertEqual(data['totals']['claude_cost'], 0)
        self.assertEqual(data['totals']['hybrid_cost'], session['hybrid_cost'])
        self.assertAlmostEqual(data['totals']['all_claude_cost'], .0165)
        self.assertEqual(session['cost_comparison']['model_basis'], 'fallback')
        self.assertFalse(session['sambanova_estimate']['eligible'])
        timing = session['timing']
        self.assertEqual([s['provider'] for s in timing['attribution']['segments']], ['sambanova'])
        self.assertEqual(timing['attribution']['duration_ms']['claude'], 0)
        self.assertEqual(timing['completed_run_count'], 0)
        self.assertTrue(all(run['end_kind'] == 'observed' for run in timing['sambanova_runs']))
        self.assertTrue(all(item['provider'] == 'sambanova' for item in data['timeline']))

    def test_cache_is_a_subset_of_samba_messages_input_and_reasoning_is_not_added_twice(self):
        self.write([self.record(1, 'MiniMax-M2.7', {
            'input_tokens': 57870, 'output_tokens': 312,
            'cache_read_input_tokens': 53248, 'cache_creation_input_tokens': 4096,
            'output_tokens_details': {'thinking_tokens': 100},
        })])
        session = app.summarize()['sessions'][0]
        self.assertEqual(session['sambanova']['input'], 526)
        self.assertEqual(session['sambanova']['total'], 58182)
        self.assertAlmostEqual(session['sambanova']['cost'], .00671688)
        step = session['matched_sambanova_runs'][0]['steps'][0]
        self.assertEqual(step['usage']['input_tokens'], 57870)
        self.assertEqual(step['output_tokens'], 312)

    def test_model_switch_uses_claude_comparison_and_returns_bar_to_claude(self):
        self.write([self.record(1, 'claude-sonnet-5'), self.record(2),
                    self.record(3, 'claude-sonnet-5'), self.record(4, 'claude-sonnet-5')])
        session = app.summarize()['sessions'][0]
        self.assertEqual(len(session['events']), 3)
        self.assertEqual(session['sambanova']['total'], 1020)
        self.assertEqual(session['claude']['total'], 3060)
        self.assertEqual(session['cost_comparison']['model'], 'claude-sonnet-5')
        self.assertAlmostEqual(session['cost_comparison']['all_claude_cost'], .0088)
        self.assertEqual([s['provider'] for s in session['timing']['attribution']['segments']],
                         ['claude', 'sambanova', 'claude'])

    def test_direct_requests_and_tracked_tool_requests_both_count_once(self):
        self.write([self.record(1), self.record(3)])
        log = self.root / 'opencode.log'
        log.write_text(json.dumps({'type': 'step_finish', 'timestamp': 1000,
                                  'part': {'messageID': 'offload-msg', 'tokens': {
                                      'input': 100, 'output': 10}}}))
        (self.root / 'runs.log').write_text(json.dumps({
            'id': 'offload', 'claude_session_id': 'session', 'model': 'MiniMax-M2.7',
            'input_tokens': 100, 'output_tokens': 10, 'status': 'finished',
            'log_path': str(log),
        }))
        data = app.summarize()
        session = data['sessions'][0]
        self.assertEqual(session['sambanova']['total'], 2150)
        self.assertEqual(data['totals']['sambanova_tokens']['total'], 2150)
        self.assertEqual(sum(len(r['steps']) for r in session['matched_sambanova_runs']), 3)

    def test_synthetic_unknown_and_user_messages_are_not_provider_requests(self):
        user = self.record(4)
        user['message']['role'] = 'user'
        self.write([self.record(1), self.record(2, '<synthetic>'),
                    self.record(3, 'unconfigured-model'), user])
        session = app.summarize()['sessions'][0]
        self.assertEqual(session['sambanova']['total'], 1020)
        self.assertEqual(len(session['direct_sambanova_events']), 1)


if __name__ == '__main__':
    unittest.main()
