"""Verify portable archives preserve requests, provider routing and observed timing."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from scripts.export_logs import export_logs
from timing import run_timing


class LogExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.projects = self.root / "vm/projects"
        self.projects.mkdir(parents=True)
        self.bundle = self.root / "local/imported"
        self.provider = self.root / "vm/providers.jsonl"

    def write_jsonl(self, path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(record) + "\n" for record in records))

    def test_direct_session_and_subagent_keep_endpoint_and_usage(self):
        record = {"timestamp": "2026-09-23T10:00:00Z", "cwd": "/vm/repo",
                  "message": {"id": "m", "model": "custom-vm-model",
                              "usage": {"input_tokens": 120, "output_tokens": 20,
                                        "cache_read_input_tokens": 100}, "content": []}}
        self.write_jsonl(self.projects / "repo/session.jsonl", [record])
        self.write_jsonl(self.projects / "repo/session/subagents/agent-one.jsonl",
                         [{**record, "message": {**record["message"], "id": "sub"}}])
        self.write_jsonl(self.provider, [{"session_id": "session", "provider": "sambanova",
                                        "recorded_at": "2026-09-23T09:00:00Z",
                                        "endpoint_host": "api.sambanova.ai"}])
        manifest = export_logs(self.projects, self.provider, None, self.bundle)
        self.assertEqual(manifest["transcript_files"], 2)
        with patch.object(app, "CLAUDE_PROJECTS_DIR", self.bundle / "projects"), \
                patch.object(app, "PROVIDER_LOG", self.bundle / "claude_providers.jsonl"):
            sessions = app.scan_claude_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["cwd"], "/vm/repo")
        self.assertEqual(sessions[0]["events"], [])
        events = sessions[0]["direct_sambanova_events"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["input_tokens"], 20)

    def test_running_offload_retains_steps_without_checking_local_pid(self):
        details = self.root / "vm/tmp/request.log"
        self.write_jsonl(details, [
            {"type": "step_finish", "timestamp": "2026-09-23T10:00:10Z",
             "part": {"messageID": "step", "tokens": {"input": 40, "output": 10,
                                                           "cache": {"read": 80}}}},
            {"type": "tool_use", "part": {"messageID": "step", "callID": "tool",
                "tool": "read", "state": {"status": "completed", "input": {"path": "a.py"},
                                             "time": {"start": 1000, "end": 1250}}}},
        ])
        runs = self.root / "vm/runs.jsonl"
        self.write_jsonl(runs, [{"id": "run", "status": "running", "pid": 123,
                                "model": "MiniMax-M3", "cwd": "/vm/repo",
                                "started_at": "2026-09-23T10:00:00Z", "log_path": str(details)}])
        export_logs(self.projects, self.provider, runs, self.bundle)
        details.unlink()  # Only the exported copy is available on the destination.
        with patch.object(app, "LOG_BUNDLE", self.bundle), \
                patch.object(app, "SAMBANOVA_RUNS_PATH", self.bundle / "sambanova_runs.jsonl"), \
                patch.object(app.os, "kill") as process_check:
            run = app.scan_sambanova_runs()[0]
        process_check.assert_not_called()
        self.assertNotIn("pid", run)
        self.assertEqual(run["status"], "snapshot")
        self.assertEqual(run["total_tokens"], 130)
        self.assertEqual(run["tool_count"], 1)
        timing = run_timing(run, 9999999999999)
        self.assertEqual(timing["duration_ms"], 10000)
        self.assertEqual(timing["tool_duration_ms"], 250)

    def test_missing_details_warn_and_summary_survives(self):
        runs = self.root / "vm/runs.jsonl"
        self.write_jsonl(runs, [{"id": "run", "input_tokens": 50,
                                "log_path": "/missing/vm/details.log"}])
        with runs.open("a") as handle:
            handle.write('{"incomplete":')
        manifest = export_logs(self.projects, self.provider, runs, self.bundle)
        records = (self.bundle / "sambanova_runs.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(records[0])["input_tokens"], 50)
        self.assertNotIn("log_path", json.loads(records[0]))
        self.assertTrue(any("Detail log missing" in warning for warning in manifest["warnings"]))
        self.assertTrue(any("incomplete" in warning for warning in manifest["warnings"]))

    def test_reexport_updates_in_place_without_duplicate_transcripts(self):
        transcript = self.projects / "session.jsonl"
        transcript.write_text('{"value": 1}\n')
        export_logs(self.projects, self.provider, None, self.bundle)
        transcript.write_text('{"value": 2}\n')
        export_logs(self.projects, self.provider, None, self.bundle)
        self.assertEqual(len(list((self.bundle / "projects").rglob("*.jsonl"))), 1)
        self.assertEqual((self.bundle / "projects/session.jsonl").read_text(), transcript.read_text())

    def test_rejects_recursive_destination_and_missing_explicit_runs(self):
        with self.assertRaises(ValueError):
            export_logs(self.projects, self.provider, None, self.projects / "export")
        with self.assertRaises(ValueError):
            export_logs(self.projects, self.provider, self.root / "missing", self.bundle)

    def test_import_cannot_read_details_outside_bundle(self):
        self.bundle.mkdir(parents=True)
        self.write_jsonl(self.bundle / "sambanova_runs.jsonl",
                         [{"id": "unsafe", "log_path": "../private.log"}])
        with patch.object(app, "LOG_BUNDLE", self.bundle), \
                patch.object(app, "SAMBANOVA_RUNS_PATH", self.bundle / "sambanova_runs.jsonl"):
            self.assertEqual(app.scan_sambanova_runs()[0]["log_path"], "")

    def test_local_tracker_cannot_append_to_imported_archive(self):
        with patch.object(app, "LOG_BUNDLE", self.bundle):
            response = app.app.test_client().post("/api/sambanova-runs", json={"id": "local"})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.bundle.exists())


if __name__ == "__main__":
    unittest.main()
