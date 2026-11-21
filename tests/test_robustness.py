"""Corner cases and malformed logs: the latency analysis must never crash or invent time."""
import copy
import math
import random
import unittest

import app
from latency import MAX_SEGMENTS, count, duration, server_split, session_latency, stream_latency, sweep
from timing import timestamp_ms
from test_latency import assistant, at, session, tool_result


def ms(seconds):
    return timestamp_ms(at(seconds))


def check_invariants(test, result):
    """Parts add up to the span; segments are contiguous, ordered and non-negative."""
    breakdown, t = result["breakdown_ms"], result["timeline"]
    if result["span_ms"] is None:
        return
    test.assertAlmostEqual(sum(breakdown.values()), result["span_ms"], delta=1)
    test.assertTrue(all(value >= 0 for value in breakdown.values()))
    cursor = 0
    for offset, length, category, label in t["segments"]:
        test.assertGreaterEqual(length, 0)
        test.assertAlmostEqual(offset, cursor, delta=1)
        test.assertLess(category, len(t["categories"]))
        test.assertLess(label, len(t["labels"]))
        cursor = offset + length
    for request in result["requests"]:
        for key in ("ttft_ms", "generation_ms", "tool_ms"):
            test.assertTrue(request[key] is None or request[key] >= 0, (key, request[key]))


class NumberGuardTests(unittest.TestCase):
    def test_counts_and_durations_reject_junk(self):
        for junk in (None, "abc", [], {}, True, False, -5, float("nan")):
            self.assertEqual(count(junk), 0, junk)
        self.assertEqual(count("12"), 12)
        self.assertEqual(count(7.9), 7)
        for junk in (None, "5", True, -1, float("nan"), float("inf"), [1]):
            self.assertIsNone(duration(junk))
        self.assertEqual(duration(0), 0)


class MalformedRecordTests(unittest.TestCase):
    def test_repeated_block_records_count_once(self):
        records = session()
        records[3:4] = [records[3], copy.deepcopy(records[3])]  # tool_use block replayed
        records[2:3] = [records[2], copy.deepcopy(records[2])]  # thinking block replayed
        result = stream_latency(records, "Main agent")
        self.assertEqual(result["tool_count"], 1)
        self.assertEqual(result["requests"][0]["tools"], ["Bash"])
        self.assertEqual(result["requests"][0]["ttft_ms"], 2000)
        check_invariants(self, result)

    def test_junk_streamed_durations_mean_unmeasured(self):
        for junk in (True, -100, "2000", float("nan"), None):
            records = session()
            records[2]["thinkingDurationMs"] = junk
            first = stream_latency(records, "Main agent")["requests"][0]
            self.assertFalse(first["ttft_measured"], junk)
            self.assertEqual(first["ttft_ms"], 4000)  # upper bound: first block finished at 5 s
            self.assertIsNone(first["tokens_per_s"])

    def test_streaming_longer_than_the_request_existed_is_not_trusted(self):
        records = session()
        records[2]["thinkingDurationMs"] = 9000  # would put the first token at -4 s
        result = stream_latency(records, "Main agent")
        first = result["requests"][0]
        self.assertIsNone(first["ttft_ms"])
        self.assertFalse(first["ttft_measured"])
        self.assertEqual(first["generation_ms"], 5000)  # from the request at 1 s to 6 s
        check_invariants(self, result)

    def test_garbage_usage_and_shapes_do_not_crash(self):
        records = session() + [
            {"type": "assistant", "timestamp": at(13), "message": "not a dict"},
            {"type": "assistant", "timestamp": at(14), "message": {"id": "x", "model": "GLM-5.3", "content": "text"}},
            {"type": "assistant", "message": {"id": "y", "model": "GLM-5.3", "content": [{"type": "text"}]}},
            {"type": "assistant", "timestamp": "garbage", "message": {"id": "z", "model": 5, "content": [7, None]}},
            {"type": "user", "timestamp": at(15), "message": {"content": [{"type": "tool_result"}]}},
            {"type": "system", "subtype": "turn_duration", "timestamp": at(16), "durationMs": True},
            {"type": "api-request", "timestamp": None},
            {},
        ]
        for index in (2, 3):  # both fragments of m1: the latest usage snapshot wins
            records[index]["message"]["usage"] = {"input_tokens": "lots", "output_tokens": [1],
                                                  "cache_read_input_tokens": None}
        result = stream_latency(records, "Main agent")
        self.assertEqual(result["requests"][0]["output_tokens"], 0)
        check_invariants(self, result)

    def test_blocks_logged_out_of_order_use_the_earliest(self):
        records = session()
        records[2], records[3] = records[3], records[2]
        first = stream_latency(records, "Main agent")["requests"][0]
        self.assertEqual(first["ttft_ms"], 2000)
        self.assertEqual(first["generation_ms"], 3000)

    def test_tool_result_before_its_call_has_no_duration(self):
        records = session()
        records[4] = tool_result(5.5, "t1")
        result = stream_latency(records, "Main agent")
        self.assertEqual(result["breakdown_ms"]["tool"], 0)
        self.assertIsNone(result["tool_summary"][0]["p50_ms"])
        check_invariants(self, result)

    def test_junk_turn_durations_fall_back_to_prompts(self):
        for junk in (True, -1, float("nan"), "12000"):
            records = session()
            records[7]["durationMs"] = junk
            result = stream_latency(records, "Main agent")
            self.assertEqual(result["breakdown_ms"]["idle"], 30000, junk)
            check_invariants(self, result)

    def test_session_without_main_agent_requests_uses_first_subagent(self):
        latency = session_latency([("Main agent", [{"type": "user", "timestamp": at(0)}]),
                                   ("Subagent · a", session())])
        self.assertEqual(latency["main"]["label"], "Subagent · a")
        self.assertEqual(latency["subagents"], [])

    def test_unlogged_usage_ignores_junk_numbers_and_entries(self):
        records = session() + [{"type": "cost-state", "modelUsage": {
            "GLM-5.3": {"inputTokens": "junk", "outputTokens": True, "cacheReadInputTokens": -5},
            "broken": "not a dict", "claude-haiku-5-5": {"inputTokens": 40}}}]
        result = app.unlogged_usage([("Main agent", records)])
        self.assertEqual(list(result["models"]), ["claude-haiku-5-5"])
        self.assertIsNone(app.unlogged_usage([("Main agent", [{"type": "cost-state", "modelUsage": "x"}])]))


class LiveTests(unittest.TestCase):
    def base(self):
        return session()[:4]  # prompt, request, thinking, tool_use t1 (no result yet)

    def test_failed_request_is_not_waiting_forever(self):
        records = self.base() + [tool_result(7, "t1"), {"type": "api-request", "id": "r2", "timestamp": at(8)},
                                 {"type": "assistant", "timestamp": at(9), "isApiErrorMessage": True,
                                  "message": {"id": "e", "model": "<synthetic>", "content": [{"type": "text"}]}}]
        result = stream_latency(records, "Main agent", now_ms=ms(30))
        self.assertTrue(result["timeline"]["live"])
        self.assertNotEqual(result["current"], "first_token")
        self.assertEqual(result["breakdown_ms"]["first_token"], 2000)  # only the answered request

    def test_interrupted_tool_is_not_running_after_the_model_moves_on(self):
        records = self.base() + [{"type": "api-request", "id": "r2", "timestamp": at(8)},
                                 assistant(10, {"type": "text"}, request="r2", message="m2")]
        result = stream_latency(records, "Main agent", now_ms=ms(30))
        self.assertTrue(result["timeline"]["live"])
        self.assertEqual(result["breakdown_ms"]["tool"], 0)  # t1 never finished, but m2 came after it

    def test_running_tool_from_latest_response_extends_to_now(self):
        result = stream_latency(self.base(), "Main agent", now_ms=ms(20))
        self.assertEqual(result["current"], "tool")
        self.assertEqual(result["breakdown_ms"]["tool"], 14000)
        check_invariants(self, result)

    def test_clock_behind_the_log_is_not_live(self):
        result = stream_latency(self.base(), "Main agent", now_ms=ms(3))
        self.assertFalse(result["timeline"]["live"])
        self.assertIsNone(result["current"])

    def test_open_prompt_counts_as_overhead_until_now(self):
        records = session() + [{"type": "user", "timestamp": at(50), "message": {"content": "next"}}]
        result = stream_latency(records, "Main agent", now_ms=ms(53))
        self.assertEqual(result["current"], "overhead")
        self.assertEqual(result["breakdown_ms"]["overhead"], 1500 + 3000)
        check_invariants(self, result)


class SweepTests(unittest.TestCase):
    def test_zero_span_and_out_of_span_intervals(self):
        totals, segments = sweep([(0, 10, "tool")], (5, 5))
        self.assertEqual(sum(totals.values()), 0)
        self.assertEqual(segments, [])
        totals, segments = sweep([(100, 200, "tool"), (-50, -10, "generation")], (0, 50))
        self.assertEqual(totals["idle"], 50)
        self.assertEqual(segments, [[0, 50, "idle", ""]])

    def test_overlapping_same_label_keeps_its_label_until_both_end(self):
        totals, segments = sweep([(0, 10, "tool", "Bash"), (5, 20, "tool", "Bash"),
                                  (8, 12, "tool", "Read")], (0, 20))
        self.assertEqual(totals["tool"], 20)
        self.assertEqual([(s[0], s[1], s[3]) for s in segments], [(0, 8, "Bash"), (8, 12, "Read"), (12, 20, "Bash")])

    def test_huge_timelines_keep_the_latest_segments(self):
        records = [{"type": "user", "timestamp": at(0), "message": {"content": "go"}}]
        for index in range(MAX_SEGMENTS):
            start = 1 + index * 0.01
            records += [{"type": "api-request", "id": f"r{index}", "timestamp": at(start)},
                        assistant(start + 0.005, {"type": "text"}, request=f"r{index}", message=f"m{index}")]
        t = stream_latency(records, "Main agent")["timeline"]
        self.assertTrue(t["truncated"])
        self.assertEqual(len(t["segments"]), MAX_SEGMENTS)
        last = t["segments"][-1]
        self.assertAlmostEqual(last[0] + last[1], t["end_ms"] - t["start_ms"], delta=1)


class ServerSplitTests(unittest.TestCase):
    def test_junk_metrics_and_reversed_times_are_not_recorded(self):
        split = server_split({"received_ms": "x", "upstream_sent_ms": 2000, "headers_ms": 1500,
                              "first_token_ms": True, "end_ms": float("nan"),
                              "server_metrics": {"time_to_first_token": True, "queue_time": -1,
                                                 "total_latency": "0.5"}}, 1000)
        for key in ("client_ms", "upstream_headers_ms", "upstream_ttft_ms", "upstream_total_ms",
                    "server_ttft_ms", "queue_ms", "server_total_ms", "network_ms"):
            self.assertIsNone(split[key], key)
        self.assertEqual(server_split({"server_metrics": "junk"}, None)["metrics"], {})


class FuzzTests(unittest.TestCase):
    """Randomly corrupted logs: never raise, never break the accounting invariants."""

    def mutate(self, rng, records):
        records = copy.deepcopy(records)
        junk = [None, "", "x", -1, True, float("nan"), [], {}, 10 ** 15, at(rng.uniform(-5, 80))]
        for record in records:
            for _ in range(rng.randint(0, 2)):
                key = rng.choice(["timestamp", "thinkingDurationMs", "toolInputStreamMs", "durationMs",
                                  "requestRef", "id", "type", "permissionDecision", "message"])
                record[key] = rng.choice(junk)
            message = record.get("message")
            if isinstance(message, dict) and rng.random() < .3:
                message[rng.choice(["id", "model", "content", "usage"])] = rng.choice(junk)
        rng.shuffle(records)
        return records + [copy.deepcopy(rng.choice(records)) for _ in range(rng.randint(0, 3))]

    def test_random_corruption(self):
        rng = random.Random(20261122)
        for _ in range(400):
            records = self.mutate(rng, session() + [{"type": "cost-state", "modelUsage": {
                "GLM-5.3": {"inputTokens": rng.choice([5000, "x", None])}}}])
            now = ms(rng.uniform(-10, 400)) if rng.random() < .5 else None
            latency = session_latency([("Main agent", records), ("Subagent · s", self.mutate(rng, session()))],
                                      now_ms=now)
            for stream in [latency["main"], *latency["subagents"]] if latency else []:
                check_invariants(self, stream)
                self.assertFalse(any(isinstance(v, float) and math.isnan(v)
                                     for v in stream["breakdown_ms"].values()))
            app.unlogged_usage([("Main agent", records)])


if __name__ == "__main__":
    unittest.main()
