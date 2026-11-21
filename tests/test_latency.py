"""Latency breakdown checks: first token, generation, tools, approvals, and partitioning."""
import unittest

import app
from latency import partition, session_latency, stream_latency


def _ms(seconds):
    from timing import timestamp_ms
    return timestamp_ms(at(seconds))


def at(seconds):
    return f"2026-11-20T00:00:{seconds:06.3f}Z" if seconds < 60 else f"2026-11-20T00:{int(seconds // 60):02d}:{seconds % 60:06.3f}Z"


def assistant(seconds, block, request="r1", message="m1", usage=None, **extra):
    return {"type": "assistant", "timestamp": at(seconds), "requestRef": request,
            "message": {"id": message, "model": "GLM-5.3", "role": "assistant",
                        "content": [block], "usage": usage or {"input_tokens": 1000, "output_tokens": 300,
                                                               "cache_read_input_tokens": 900}},
            **extra}


def tool_result(seconds, tool_id, decision=None):
    return {"type": "user", "timestamp": at(seconds), "permissionDecision": decision or {},
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id}]}}


def session(prompt_at=0.0):
    """Prompt, request, 2 s thinking, a tool call approved by auto mode, then a final answer."""
    return [
        {"type": "user", "timestamp": at(prompt_at), "message": {"role": "user", "content": "fix it"}},
        {"type": "api-request", "id": "r1", "timestamp": at(1)},
        assistant(5, {"type": "thinking"}, thinkingDurationMs=2000),
        assistant(6, {"type": "tool_use", "id": "t1", "name": "Bash"}, toolInputStreamMs=10),
        tool_result(10, "t1", {"source": "config", "reasonType": "classifier"}),
        {"type": "api-request", "id": "r2", "timestamp": at(10.5)},
        assistant(12, {"type": "text", "text": "done"}, request="r2", message="m2"),
        {"type": "system", "subtype": "turn_duration", "timestamp": at(12), "durationMs": 12000},
        {"type": "user", "timestamp": at(42), "message": {"role": "user", "content": "thanks"}},
    ]


class LatencyTests(unittest.TestCase):
    def test_first_token_uses_streamed_duration_and_marks_text_as_upper_bound(self):
        requests = stream_latency(session(), "Main agent")["requests"]
        first, second = requests
        self.assertEqual(first["ttft_ms"], 2000)  # sent at 1 s, thinking began at 5 - 2 = 3 s
        self.assertTrue(first["ttft_measured"])
        self.assertEqual(first["generation_ms"], 3000)  # 3 s until the last block at 6 s
        self.assertEqual(first["tools"], ["Bash"])
        self.assertEqual(first["tool_ms"], 4000)
        self.assertEqual(second["ttft_ms"], 1500)
        self.assertFalse(second["ttft_measured"])
        self.assertIsNone(second["tokens_per_s"])

    def test_decode_speed_needs_a_measured_first_token(self):
        first = stream_latency(session(), "Main agent")["requests"][0]
        self.assertAlmostEqual(first["tokens_per_s"], 100)  # 300 tokens over 3 s

    def test_wall_clock_partition_counts_each_instant_once(self):
        result = stream_latency(session(), "Main agent")
        breakdown = result["breakdown_ms"]
        self.assertEqual(breakdown["first_token"], 2000 + 1500 - 0)  # 1-3 s, and 10.5-12 s bounded
        self.assertEqual(breakdown["generation"], 3000)
        self.assertEqual(breakdown["tool"], 4000)
        self.assertEqual(breakdown["overhead"], 1000 + 500)  # 0-1 s and 10-10.5 s inside the turn
        self.assertEqual(breakdown["idle"], 30000)  # after the turn until the next prompt
        self.assertEqual(sum(breakdown.values()), result["span_ms"])

    def test_tools_you_approved_count_as_waiting_for_you(self):
        records = session()
        records[4] = tool_result(10, "t1", {"source": "user_temporary", "grantScope": "once"})
        result = stream_latency(records, "Main agent")
        self.assertEqual(result["breakdown_ms"]["approval"], 4000)
        self.assertEqual(result["breakdown_ms"]["tool"], 0)
        self.assertEqual(result["approvals"]["you"], 1)

    def test_turns_fall_back_to_prompts_when_durations_are_missing(self):
        records = [record for record in session() if record.get("subtype") != "turn_duration"]
        breakdown = stream_latency(records, "Main agent")["breakdown_ms"]
        self.assertEqual(breakdown["idle"], 30000)
        self.assertEqual(breakdown["overhead"], 1500)

    def test_overlapping_categories_follow_priority(self):
        totals = partition([(0, 10, "tool"), (5, 8, "generation"), (0, 20, "overhead")], (0, 30))
        self.assertEqual(totals, {"generation": 3, "first_token": 0, "approval": 0,
                                  "tool": 7, "overhead": 10, "idle": 10})

    def test_segments_follow_time_order_with_labels_and_cover_the_span(self):
        result = stream_latency(session(), "Main agent")
        t = result["timeline"]
        parts = [(t["categories"][category], t["labels"][label], duration)
                 for _, duration, category, label in t["segments"]]
        self.assertEqual(parts[:5], [("overhead", "", 1000), ("first_token", "GLM-5.3", 2000),
                                     ("generation", "GLM-5.3", 3000), ("tool", "Bash", 4000),
                                     ("overhead", "", 500)])
        self.assertEqual(parts[-1], ("idle", "", 30000))
        self.assertEqual(sum(duration for _, duration, _, _ in t["segments"]), result["span_ms"])
        self.assertFalse(t["live"])
        self.assertIsNone(result["current"])

    def test_live_stream_draws_open_request_and_tool_up_to_now(self):
        records = session()[:4] + [{"type": "api-request", "id": "r9", "timestamp": at(7)}]
        result = stream_latency(records, "Main agent", now_ms=_ms(9))
        self.assertTrue(result["timeline"]["live"])
        self.assertEqual(result["span_ms"], 9000)
        # The unanswered request (7-9 s) outranks the still-running Bash call.
        self.assertEqual(result["current"], "first_token")
        self.assertEqual(result["breakdown_ms"]["tool"], 1000)  # Bash 6-7 s, then the request takes over
        stale = stream_latency(records, "Main agent", now_ms=_ms(9) + 10 * 60 * 1000)
        self.assertFalse(stale["timeline"]["live"])

    def test_subagents_are_reported_beside_the_main_agent(self):
        latency = session_latency([("Main agent", session()), ("Subagent · docs", session())])
        self.assertEqual(latency["main"]["label"], "Main agent")
        self.assertEqual([agent["label"] for agent in latency["subagents"]], ["Subagent · docs"])
        self.assertIsNone(session_latency([("Main agent", [])]))


class UnloggedUsageTests(unittest.TestCase):
    def test_only_usage_beyond_the_transcript_before_the_snapshot_counts(self):
        records = session()
        snapshot = {"type": "cost-state", "modelUsage": {
            "GLM-5.3": {"inputTokens": 3000, "outputTokens": 700, "cacheReadInputTokens": 2000,
                        "cacheCreationInputTokens": 0},
            "claude-haiku-5-5": {"inputTokens": 100, "outputTokens": 10}}}
        # The snapshot sits before m2, so only m1 (1000 in, 300 out, 900 cached) is compared.
        records.insert(6, snapshot)
        result = app.unlogged_usage([("Main agent", records)])
        glm = result["models"]["GLM-5.3"]
        self.assertEqual((glm["input"], glm["output"], glm["cache_read"]), (2000, 400, 1100))
        rates = app.RATES["sambanova"]["GLM-5.3"]
        expected = (900 * rates["input"] + 1100 * rates["cached_input"] + 400 * rates["output"]) / 1e6
        self.assertAlmostEqual(glm["cost"], expected)
        self.assertEqual(result["models"]["claude-haiku-5-5"]["provider"], "claude")

    def test_snapshot_behind_the_transcript_reports_nothing(self):
        records = session() + [{"type": "cost-state", "modelUsage": {
            "GLM-5.3": {"inputTokens": 10, "outputTokens": 1}}}]
        self.assertIsNone(app.unlogged_usage([("Main agent", records)]))
        self.assertIsNone(app.unlogged_usage([("Main agent", session())]))


if __name__ == "__main__":
    unittest.main()
