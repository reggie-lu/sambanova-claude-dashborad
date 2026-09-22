"""Endpoint-first provider attribution across new, resumed, and historical sessions."""
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from activity import claude_requests
from provider_tracking import endpoint_identity, install_hooks, load_provider_history, record_endpoint


class ProviderTrackingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.routes = self.root / 'routes.log'
        for name, value in [('CLAUDE_PROJECTS_DIR', self.root),
                            ('SAMBANOVA_RUNS_PATH', self.root / 'runs.log'),
                            ('PROVIDER_LOG', self.routes)]:
            patched = patch.object(app, name, value)
            patched.start()
            self.addCleanup(patched.stop)

    def observation(self, second, provider):
        return {'session_id': 'session', 'recorded_at': f'2026-09-22T12:00:{second:02d}Z',
                'provider': provider, 'endpoint_host': f'api.{provider}.ai'}

    def record(self, second, model):
        return {'timestamp': f'2026-09-22T12:00:{second:02d}Z',
                'message': {'id': f'msg-{second}', 'model': model, 'role': 'assistant',
                            'usage': {'input_tokens': 1000, 'output_tokens': 20}}}

    def summarize(self, records, observations):
        (self.root / 'session.jsonl').write_text('\n'.join(map(json.dumps, records)))
        self.routes.write_text('\n'.join(map(json.dumps, observations)))
        return app.summarize()['sessions'][0]

    def test_sambanova_endpoint_overrides_claude_alias_and_accepts_unknown_model(self):
        session = self.summarize([self.record(1, 'claude-opus-5'), self.record(2, 'new-model')],
                                 [self.observation(0, 'sambanova')])
        self.assertEqual(session['claude']['total'], 0)
        self.assertEqual(session['sambanova']['total'], 2040)
        self.assertTrue(all(event['provider_basis'] == 'session-endpoint'
                            for event in session['direct_sambanova_events']))
        self.assertTrue(all(run['rate_fallback'] for run in session['matched_sambanova_runs']))
        self.assertEqual([s['provider'] for s in session['timing']['attribution']['segments']],
                         ['sambanova'])

    def test_non_sambanova_endpoint_keeps_model_traffic_on_claude_and_tool_on_samba(self):
        (self.root / 'runs.log').write_text(json.dumps({
            'id': 'tool', 'claude_session_id': 'session', 'model': 'MiniMax-M3',
            'input_tokens': 100, 'output_tokens': 10, 'status': 'finished',
        }))
        session = self.summarize([self.record(1, 'MiniMax-M3')],
                                 [self.observation(0, 'claude')])
        self.assertEqual(session['claude']['total'], 1020)
        self.assertEqual(session['sambanova']['total'], 110)
        self.assertEqual(session['direct_sambanova_events'], [])
        self.assertTrue(session['events'][0]['rate_fallback'])

    def test_resumed_session_switches_provider_even_when_model_name_is_unchanged(self):
        session = self.summarize([self.record(n, 'claude-opus-5') for n in (1, 3, 5, 7)],
                                 [self.observation(0, 'claude'), self.observation(2, 'sambanova'),
                                  self.observation(4, 'claude')])
        self.assertEqual(len(session['events']), 3)
        self.assertEqual(len(session['direct_sambanova_events']), 1)
        self.assertEqual([s['provider'] for s in session['timing']['attribution']['segments']],
                         ['claude', 'sambanova', 'claude'])

    def test_new_endpoint_observation_does_not_relabel_past_or_other_session(self):
        other = self.observation(0, 'sambanova')
        other['session_id'] = 'different-session'
        with patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'https://api.sambanova.ai'}):
            session = self.summarize([self.record(1, 'claude-opus-5')],
                                     [other, self.observation(2, 'sambanova')])
        self.assertEqual(session['claude']['total'], 1020)
        self.assertEqual(session['events'][0]['provider_basis'], 'model-fallback')

    def test_subagent_uses_its_parent_sessions_endpoint(self):
        child = self.root / 'session/subagents/agent-test.jsonl'
        child.parent.mkdir(parents=True)
        child.write_text(json.dumps({**self.record(2, 'custom'), 'agentId': 'test'}))
        session = self.summarize([self.record(1, 'claude-opus-5')],
                                 [self.observation(0, 'sambanova')])
        self.assertEqual(session['sambanova']['total'], 2040)
        self.assertEqual(session['claude']['total'], 0)

    def test_host_matching_and_secret_redaction(self):
        payload = {'session_id': 'session', 'hook_event_name': 'SessionStart',
                   'prompt': 'private prompt', 'transcript_path': '/private/path'}
        environment = {'ANTHROPIC_BASE_URL': 'https://user:password@API.SAMBANOVA.AI/v1?key=secret',
                       'ANTHROPIC_API_KEY': 'private-key'}
        record_endpoint(payload, environment, self.routes)
        raw = self.routes.read_text()
        for secret in ('password', 'user:', 'secret', 'private-key', 'private prompt', '/private/path'):
            self.assertNotIn(secret, raw)
        self.assertEqual(load_provider_history(self.routes)['session'][0]['provider'], 'sambanova')
        for host in ('api.sambanova.ai.evil.test', 'not-sambanova.ai', 'proxy.test/sambanova.ai'):
            self.assertEqual(endpoint_identity({'ANTHROPIC_BASE_URL': 'https://' + host})['provider'],
                             'claude')
        self.assertEqual(endpoint_identity({})['provider'], 'claude')
        self.assertEqual(endpoint_identity({'ANTHROPIC_BASE_URL': 'not-a-url'}), {})

    def test_synthetic_messages_stay_excluded_even_with_endpoint(self):
        self.routes.write_text(json.dumps(self.observation(0, 'sambanova')) + '\n{partial')
        history = load_provider_history(self.routes)['session']
        self.assertEqual(claude_requests([self.record(1, '<synthetic>')], set(), history), [])

    def test_install_preserves_existing_settings_and_is_idempotent(self):
        settings_path = self.root / 'settings.json'
        existing = {'permissions': {'allow': ['Read']}, 'hooks': {
            'Stop': [{'hooks': [{'type': 'command', 'command': 'existing-stop'}]}],
            'SessionStart': [{'hooks': [{'type': 'command', 'command': 'existing-start'}]}],
        }, 'env': {'ANTHROPIC_BASE_URL': 'https://api.sambanova.ai'}}
        settings_path.write_text(json.dumps(existing))
        self.assertTrue(install_hooks(settings_path, self.routes))
        self.assertFalse(install_hooks(settings_path, self.routes))
        settings = json.loads(settings_path.read_text())
        self.assertEqual(settings['permissions'], existing['permissions'])
        self.assertEqual(settings['env'], existing['env'])
        self.assertEqual(settings['hooks']['Stop'], existing['hooks']['Stop'])
        self.assertEqual(settings['hooks']['SessionStart'][0], existing['hooks']['SessionStart'][0])
        self.assertEqual(len(settings['hooks']['SessionStart']), 2)
        self.assertEqual(len(settings['hooks']['UserPromptSubmit']), 1)
        backups = list(self.root.glob('settings.json.cost-lens-*.bak'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), existing)

    def test_installed_command_captures_its_own_environment_without_hook_output(self):
        settings = self.root / 'settings.json'
        install_hooks(settings, self.routes)
        command = json.loads(settings.read_text())['hooks']['SessionStart'][0]['hooks'][0]['command']
        result = subprocess.run(shlex.split(command), input=json.dumps({
            'session_id': 'session', 'hook_event_name': 'SessionStart', 'prompt': 'not recorded',
        }), text=True, capture_output=True, check=True,
            env={**os.environ, 'ANTHROPIC_BASE_URL': 'https://api.sambanova.ai',
                 'ANTHROPIC_API_KEY': 'must-not-be-recorded'})
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, '')
        history = load_provider_history(self.routes)
        self.assertEqual(history['session'][0]['provider'], 'sambanova')
        self.assertNotIn('must-not-be-recorded', self.routes.read_text())


if __name__ == '__main__':
    unittest.main()
