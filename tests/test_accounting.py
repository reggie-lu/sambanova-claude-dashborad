import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from activity import claude_requests, coding_tool, opencode_steps


class AccountingTests(unittest.TestCase):
    def record(self, usage, content=None, message_id="msg-1"):
        return {"timestamp": "2026-09-18T07:51:00Z", "cwd": "/repo",
                "message": {"id": message_id, "model": "claude-opus-5",
                            "usage": usage, "content": content or []}}

    def test_claude_fragments_count_once_and_merge_tools(self):
        usage = {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 100}
        tool = {"type": "tool_use", "id": "tool-1", "name": "Edit", "input": {"file_path": "a.py"}}
        records = [self.record(usage), self.record(usage, [tool]), self.record(usage, [tool])]
        self.assertEqual(len(claude_requests(records)), 1)
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "session.jsonl").write_text('\n'.join(map(json.dumps, records)))
            with patch.object(app, 'CLAUDE_PROJECTS_DIR', Path(directory)):
                session = app.scan_claude_sessions()[0]
        self.assertEqual(session['claude']['total'], 130)
        self.assertEqual(len(session['events'][0]['tools']), 1)
        self.assertAlmostEqual(session['claude_cost'], .0006)

    def test_latest_usage_snapshot_wins(self):
        records = [self.record({'output_tokens': 1}), self.record({'output_tokens': 20})]
        self.assertEqual(claude_requests(records)[0]['usage']['output_tokens'], 20)

    def test_known_rates_and_cache_durations(self):
        self.assertEqual(app.rate_for('claude', 'claude-opus-5-20260901')['input'], 5)
        self.assertEqual(app.rate_for('claude', 'claude-opus-4-8')['input'], 5)
        self.assertEqual(app.rate_for('claude', 'claude-sonnet-5')['output'], 10)
        usage = {'input_tokens': 1000000, 'output_tokens': 1000000,
                 'cache_read_input_tokens': 1000000, 'cache_creation_input_tokens': 2000000,
                 'cache_creation': {'ephemeral_5m_input_tokens': 1000000,
                                    'ephemeral_1h_input_tokens': 1000000}}
        self.assertEqual(app.claude_usage_cost('claude-opus-5', usage), 46.75)
        self.assertEqual(app.claude_usage_cost('claude-fable-5-1', {'cache_read_input_tokens': 1000000}), .25)

    def test_samba_cache_conventions_and_writes(self):
        modern = {'model': 'MiniMax-M2.7', 'input_tokens': 100, 'output_tokens': 20,
                  'cache_read_tokens': 1000, 'cache_write_tokens': 50}
        legacy = {'model': 'MiniMax-M2.7', 'input_tokens': 1100, 'output_tokens': 20,
                  'cached_input_tokens': 1000, 'cache_write_tokens': 50}
        self.assertEqual(app.sambanova_tokens(modern), app.sambanova_tokens(legacy))
        self.assertEqual(app.sambanova_tokens(modern)['total'], 1170)
        self.assertAlmostEqual(app.sambanova_run_cost(modern), .000198)

    def test_opencode_steps_deduplicate_tools_and_usage(self):
        events = [
            {'type': 'step_start', 'timestamp': 1000, 'part': {'messageID': 'a'}},
            {'type': 'tool_use', 'part': {'messageID': 'a', 'callID': 't', 'tool': 'read', 'state': {'input': {'filePath': 'a.py'}, 'status': 'running'}}},
            {'type': 'tool_use', 'part': {'messageID': 'a', 'callID': 't', 'tool': 'read', 'state': {'input': {'filePath': 'a.py'}, 'status': 'completed'}}},
            {'type': 'step_finish', 'part': {'messageID': 'a', 'tokens': {'input': 10, 'output': 20, 'reasoning': 5, 'cache': {'read': 100}}}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory, 'run.log')
            log.write_text('\n'.join(map(json.dumps, events + [events[-1]])) + '\n{unfinished')
            steps = opencode_steps({'log_path': str(log), 'model': 'MiniMax-M2.7'})
        self.assertEqual(len(steps), 1)
        self.assertEqual(len(steps[0]['tools']), 1)
        self.assertEqual(steps[0]['tools'][0]['status'], 'completed')
        self.assertEqual(steps[0]['usage']['output_tokens'], 25)
        self.assertEqual(opencode_steps({'log_path': '/missing/log'}), [])

    def test_coding_classification_excludes_delegation(self):
        self.assertTrue(coding_tool('Edit', {}))
        self.assertTrue(coding_tool('Bash', {'command': 'uv run pytest'}))
        self.assertFalse(coding_tool('Bash', {'command': 'python /tmp/samba-claude/code.py'}))
        self.assertFalse(coding_tool('WebSearch', {}))
        self.assertFalse(coding_tool('Agent', {}))

    def test_estimate_uses_whole_request_once_and_keeps_recorded_total(self):
        session = {'claude_cost': 8., 'hybrid_cost': 8., 'events': [
            {'coding': True, 'model': 'claude-opus-5', 'cost': 5., 'input_tokens': 1000000,
             'cache_write_tokens': 1000000, 'cache_read_tokens': 1000000,
             'output_tokens': 1000000, 'tools': [{'coding': True}, {'coding': True}]},
            {'coding': False, 'cost': 3.},
        ]}
        estimate = app.estimate_coding(session, 'MiniMax-M2.7')
        self.assertEqual(estimate['request_count'], 1)
        self.assertEqual(estimate['tool_count'], 2)
        self.assertAlmostEqual(estimate['cost'], 4.2)
        self.assertAlmostEqual(estimate['cache_reuse_cost'], 3.66)
        self.assertAlmostEqual(estimate['projected_cost'], 7.2)
        self.assertEqual(session['claude_cost'], 8.)
        session['matched_sambanova_runs'] = [{'id': 'actual'}]
        self.assertEqual(app.estimate_coding(session, 'MiniMax-M2.7')['request_count'], 0)

    def test_no_tool_session_has_no_estimate(self):
        estimate = app.estimate_coding({'claude_cost': 2, 'events': [{'coding': False}]}, 'MiniMax-M2.7')
        self.assertEqual(estimate['cost'], 0)
        self.assertEqual(estimate['projected_cost'], 2)

    def test_counterfactual_does_not_inflate_claude_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'session.jsonl').write_text(json.dumps(self.record({'cache_read_input_tokens': 1000000})))
            with patch.object(app, 'CLAUDE_PROJECTS_DIR', Path(directory)), patch.object(app, 'SAMBANOVA_RUNS_PATH', Path(directory, 'missing')):
                data = app.summarize()
        self.assertEqual(data['totals']['claude_cost'], .5)
        self.assertEqual(data['totals']['all_claude_cost'], .5)
        self.assertEqual(data['totals']['savings'], 0)

    def comparison_session(self, samba_cost=2., claude_cost=10., fresh=1000000):
        return {"models": {"claude-opus-5": 3}, "claude_cost": claude_cost,
                "hybrid_cost": claude_cost + samba_cost,
                "sambanova": {**app.empty_tokens(), "input": fresh},
                "matched_sambanova_runs": [{"status": "finished"}]}

    def test_session_comparison_percentages_use_correct_denominators(self):
        result = app.session_cost_comparison(self.comparison_session(), 'claude-sonnet-5')
        self.assertEqual(result['model'], 'claude-opus-5')
        self.assertEqual(result['all_claude_cost'], 15.)
        self.assertEqual(result['combined_cost'], 12.)
        self.assertEqual(result['savings'], 3.)
        self.assertEqual(result['savings_pct'], 20.)
        self.assertEqual(result['all_claude_premium_pct'], 25.)

    def test_session_comparison_preserves_cache_and_output(self):
        session = self.comparison_session()
        session['sambanova'].update({'output': 1000000, 'cache_read': 1000000,
                                    'cache_creation': 1000000})
        result = app.session_cost_comparison(session, 'claude-sonnet-5')
        self.assertEqual(result['offloaded_claude_cost'], 36.75)
        self.assertEqual(result['all_claude_cost'], 46.75)

    def test_session_comparison_reports_higher_cost_and_zero_baselines(self):
        result = app.session_cost_comparison(self.comparison_session(samba_cost=8.), 'claude-opus-5')
        self.assertEqual(result['savings'], -3.)
        self.assertEqual(result['savings_pct'], -20.)
        zero = app.session_cost_comparison(self.comparison_session(0., 0., 0), 'claude-opus-5')
        self.assertIsNone(zero['savings_pct'])
        self.assertIsNone(zero['all_claude_premium_pct'])

    def test_no_offload_has_equal_cost_and_fallback_is_labeled(self):
        session = self.comparison_session(0., 10., 0)
        session['models'] = {}
        session['matched_sambanova_runs'] = []
        result = app.session_cost_comparison(session, 'claude-sonnet-5')
        self.assertEqual(result['all_claude_cost'], 10.)
        self.assertEqual(result['savings'], 0.)
        self.assertFalse(result['has_offload'])
        self.assertEqual(result['model_basis'], 'fallback')

    def test_global_comparison_uses_each_sessions_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = []
            for session_id, model in [('opus', 'claude-opus-5'), ('sonnet', 'claude-sonnet-5')]:
                record = self.record({'input_tokens': 1000000})
                record['message']['model'] = model
                (root / f'{session_id}.jsonl').write_text(json.dumps(record))
                records.append({'id': session_id, 'claude_session_id': session_id,
                                'model': 'MiniMax-M2.7', 'input_tokens': 1000000,
                                'status': 'finished'})
            runs_path = root / 'runs.log'
            runs_path.write_text('\n'.join(map(json.dumps, records)))
            with patch.object(app, 'CLAUDE_PROJECTS_DIR', root), patch.object(app, 'SAMBANOVA_RUNS_PATH', runs_path):
                data = app.summarize()
        # Claude recorded = 5 + 2; offloaded usage on each model = 5 + 2.
        self.assertEqual(data['totals']['all_claude_cost'], 14.)
        self.assertEqual(sum(s['cost_comparison']['all_claude_cost'] for s in data['sessions']), 14.)
        self.assertEqual(data['totals']['comparison_models'], ['claude-opus-5', 'claude-sonnet-5'])

    def test_invalid_estimation_model(self):
        response = app.app.test_client().get('/api/metrics?estimate_model=_default')
        self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
