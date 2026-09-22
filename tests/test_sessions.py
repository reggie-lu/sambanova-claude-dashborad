"""Regression checks for browsing sessions outside the recent-session window."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class SessionBrowserTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        for number in range(28):
            record = {
                'timestamp': f'2026-08-{number + 1:02d}T12:00:00Z', 'cwd': '/test/repo',
                'message': {'id': f'msg-{number}', 'model': 'claude-opus-5',
                            'usage': {'input_tokens': 100, 'output_tokens': 20},
                            'content': []},
            }
            (root / f'session-{number:02d}.jsonl').write_text(json.dumps(record))
        for name, value in [('CLAUDE_PROJECTS_DIR', root),
                            ('SAMBANOVA_RUNS_PATH', root / 'missing.log')]:
            patched = patch.object(app, name, value)
            patched.start()
            self.addCleanup(patched.stop)
        self.client = app.app.test_client()

    def test_latest_ten_and_complete_session_index(self):
        data = self.client.get('/api/metrics').get_json()
        self.assertEqual([s['id'] for s in data['sessions']],
                         [f'session-{number:02d}' for number in range(27, 17, -1)])
        self.assertEqual(len(data['session_index']), 28)
        self.assertEqual(data['session_index'][-1]['id'], 'session-00')
        self.assertNotIn('events', data['session_index'][0])
        self.assertIsNone(data['selected_session'])

    def test_select_session_beyond_old_limit_preserves_totals_and_recent_list(self):
        default = self.client.get('/api/metrics').get_json()
        response = self.client.get('/api/metrics?session_id=session-00')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['selected_session']['id'], 'session-00')
        self.assertEqual(len(data['selected_session']['events']), 1)
        self.assertEqual(data['sessions'], default['sessions'])
        self.assertEqual(data['totals'], default['totals'])
        self.assertFalse(data['selected_session_missing'])

    def test_missing_session_keeps_recent_sessions_available(self):
        data = self.client.get('/api/metrics?session_id=missing').get_json()
        self.assertIsNone(data['selected_session'])
        self.assertTrue(data['selected_session_missing'])
        self.assertEqual(len(data['sessions']), 10)

    def test_removed_panel_and_local_claude_icon(self):
        html = self.client.get('/').get_data(as_text=True)
        self.assertNotIn('estimateModel', html)
        self.assertNotIn('estimateSummary', html)
        self.assertNotIn('estimate-panel', html)
        self.assertIn('id="sessionPicker"', html)
        self.assertIn('id="selectedSession"', html)
        response = self.client.get('/static/claude-icon.png')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/png')
        response.close()


if __name__ == '__main__':
    unittest.main()
