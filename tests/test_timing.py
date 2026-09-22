"""Timing checks: elapsed spans, missing data, overlaps, and tool log timestamps."""
import json
import tempfile
import unittest
from pathlib import Path

from activity import opencode_steps
from timing import run_timing, session_timing, timing_interval


class TimingTests(unittest.TestCase):
    def test_iso_offsets_and_millisecond_precision(self):
        result = timing_interval('2026-09-18T16:00:00.125+09:00', '2026-09-18T07:00:01.625Z')
        self.assertEqual(result['duration_ms'], 1500)

    def test_missing_invalid_reversed_and_zero_duration(self):
        for start, end in [(None, 1000), (1000, None), ('invalid', 1000),
                           (2000, 1000), (float('nan'), 1000)]:
            self.assertIsNone(timing_interval(start, end)['duration_ms'])
        self.assertEqual(timing_interval(0, 0)['duration_ms'], 0)

    def test_tool_times_survive_parsing_without_changing_usage(self):
        records = [
            {'type': 'step_start', 'timestamp': 1000, 'part': {'messageID': 'm'}},
            {'type': 'tool_use', 'timestamp': 1500, 'part': {
                'messageID': 'm', 'callID': 't', 'tool': 'bash',
                'state': {'status': 'completed', 'input': {}, 'time': {'start': 1200, 'end': 1450}}}},
            {'type': 'step_finish', 'timestamp': 1600, 'part': {
                'messageID': 'm', 'tokens': {'input': 10, 'output': 5}}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory, 'log.jsonl')
            log.write_text('\n'.join(map(json.dumps, records)))
            steps = opencode_steps({'log_path': str(log), 'model': 'MiniMax-M2.7'})
        self.assertEqual(steps[0]['tools'][0]['timing']['duration_ms'], 250)
        self.assertEqual(steps[0]['finished_at'], '1970-01-01T00:00:01.600000+00:00')
        self.assertEqual(steps[0]['usage']['output_tokens'], 5)

    def test_running_and_stale_are_not_presented_as_finished(self):
        run = {'started_at': 1000, 'status': 'running', 'steps': []}
        timing = run_timing(run, 5000)
        self.assertEqual(timing['duration_ms'], 4000)
        self.assertEqual(timing['end_kind'], 'elapsed')
        run.update(status='stale', steps=[{'finished_at': 3000, 'tools': []}])
        timing = run_timing(run, 5000)
        self.assertEqual(timing['duration_ms'], 2000)
        self.assertEqual(timing['end_kind'], 'last_observed')
        run['steps'] = []
        self.assertIsNone(run_timing(run, 5000)['duration_ms'])

    def test_shared_axis_covers_runs_outside_session_and_does_not_add_overlap(self):
        session = {'started_at': 1000, 'updated_at': 4000, 'matched_sambanova_runs': [
            {'id': 'a', 'started_at': 2000, 'finished_at': 5000},
            {'id': 'b', 'started_at': 3000, 'finished_at': 6000},
        ]}
        timing = session_timing(session, 9000)
        self.assertEqual(timing['claude']['duration_ms'], 3000)
        self.assertEqual(timing['axis_start_ms'], 1000)
        self.assertEqual(timing['axis_end_ms'], 6000)
        self.assertEqual(timing['completed_response_ms'], 6000)
        self.assertEqual(timing['completed_run_count'], 2)
        self.assertEqual([r['duration_ms'] for r in timing['sambanova_runs']], [3000, 3000])

    def test_overlap_is_attributed_only_to_sambanova(self):
        timing = session_timing({'started_at': 1000, 'updated_at': 5000,
                                 'matched_sambanova_runs': [
                                     {'started_at': 2000, 'finished_at': 4000},
                                     {'started_at': 3000, 'finished_at': 6000},
                                 ]}, 9000)
        attribution = timing['attribution']
        self.assertEqual(attribution['duration_ms'],
                         {'claude': 1000, 'sambanova': 4000, 'unknown': 0})
        self.assertEqual([s['provider'] for s in attribution['segments']],
                         ['claude', 'sambanova'])
        self.assertEqual(sum(attribution['duration_ms'].values()), 5000)

    def test_bar_returns_to_claude_after_offload_and_preserves_unknown_gaps(self):
        timing = session_timing({'started_at': 1000, 'updated_at': 5000,
                                 'matched_sambanova_runs': [
                                     {'started_at': 2000, 'finished_at': 3000},
                                     {'started_at': 6000, 'finished_at': 7000},
                                 ]}, 9000)
        attribution = timing['attribution']
        self.assertEqual([s['provider'] for s in attribution['segments']],
                         ['claude', 'sambanova', 'claude', 'unknown', 'sambanova'])
        self.assertEqual(attribution['duration_ms'],
                         {'claude': 3000, 'sambanova': 2000, 'unknown': 1000})
        segments = attribution['segments']
        for left, right in zip(segments, segments[1:]):
            self.assertEqual(left['end_ms'], right['start_ms'])

    def test_zero_and_unknown_timing_are_distinct(self):
        zero = session_timing({'started_at': 0, 'updated_at': 0}, 1000)['attribution']
        self.assertTrue(zero['has_timing'])
        self.assertEqual(zero['segments'], [])
        unknown = session_timing({}, 1000)['attribution']
        self.assertFalse(unknown['has_timing'])
        self.assertEqual(unknown['segments'], [])

    def test_tool_sum_deduplicates_calls_and_reports_coverage(self):
        tool = {'id': 't', 'timing': timing_interval(1000, 1500)}
        run = {'started_at': 1000, 'finished_at': 3000, 'steps': [
            {'tools': [tool, {'id': 'unknown', 'timing': timing_interval(None, None)}]},
            {'tools': [tool]},
        ]}
        timing = run_timing(run, 5000)
        self.assertEqual(timing['tool_duration_ms'], 500)
        self.assertEqual(timing['timed_tool_count'], 1)
        self.assertEqual(timing['tool_count'], 2)

    def test_session_without_sambanova_has_no_invented_duration(self):
        timing = session_timing({'started_at': 1000, 'updated_at': 3000}, 5000)
        self.assertEqual(timing['sambanova_runs'], [])
        self.assertIsNone(timing['completed_response_ms'])


if __name__ == '__main__':
    unittest.main()
