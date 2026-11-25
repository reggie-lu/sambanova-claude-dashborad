"""Timing proxy checks: passthrough, timing capture, secrets kept out, and the join."""
import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from latency import server_split
from provider_tracking import endpoint_identity
from proxy import ProxyServer, StreamTimer, make_handler, upstream_for

EVENTS = [
    ("message_start", {"type": "message_start", "message": {"id": "msg_1", "model": "GLM-5.3",
                                                            "usage": {"input_tokens": 50, "output_tokens": 1}}}),
    ("content_block_delta", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}),
    ("message_delta", {"type": "message_delta", "usage": {
        "output_tokens": 7, "time_to_first_token": 0.2, "queue_time": 0.05, "total_latency": 0.4}}),
    ("message_stop", {"type": "message_stop"}),
]


class FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeUpstream.seen.append((self.path, dict(self.headers), body))
        request = json.loads(body)
        if request.get("slow"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                self.wfile.write(f"data: {json.dumps(EVENTS[0][1])}\n\n".encode())
                for _ in range(200):
                    self.wfile.write(f"data: {json.dumps(EVENTS[1][1])}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.01)
            except OSError:
                pass
            return
        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("request-id", "req_1")
            self.send_header("x-queue-time", "0.05")
            self.send_header("Connection", "close")
            self.end_headers()
            for name, payload in EVENTS:
                self.wfile.write(f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.02)
        else:
            payload = json.dumps({"id": "msg_2", "model": "GLM-5.3", "usage": {"input_tokens": 3}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)


def serve(server):
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return server


class ProxyCase(unittest.TestCase):
    def setUp(self):
        FakeUpstream.seen = []
        self.upstream = ProxyServer(("127.0.0.1", 0), FakeUpstream)
        self.directory = tempfile.TemporaryDirectory()
        self.log = Path(self.directory.name, "proxy.jsonl")
        handler = make_handler(self.log, connect_to=("127.0.0.1", self.upstream.server_port), scheme="http")
        self.proxy = ProxyServer(("127.0.0.1", 0), handler)
        for server in (self.upstream, self.proxy):
            serve(server)

    def tearDown(self):
        for server in (self.proxy, self.upstream):
            server.shutdown()
            server.server_close()
        self.directory.cleanup()

    def logged(self):
        """The proxy writes its record just after the client has the whole response."""
        for _ in range(100):
            if self.log.exists() and self.log.read_text().endswith("\n"):
                return self.log.read_text()
            time.sleep(0.02)
        self.fail("proxy wrote no record")

    def post(self, path, body):
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=10)
        connection.request("POST", path, body=json.dumps(body),
                           headers={"x-api-key": "secret-key", "content-type": "application/json",
                                    "X-Claude-Code-Session-Id": "session-1"})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response, body


class ProxyTests(ProxyCase):
    def test_streams_through_unchanged_and_records_timing_without_secrets(self):
        response, body = self.post("/via/api.sambanova.ai/v1/messages?beta=true",
                                   {"model": "GLM-5.3", "stream": True, "messages": [{"content": "private prompt"}]})
        self.assertEqual(response.status, 200)
        self.assertEqual(body.count(b"event: "), 4)
        path, headers, _ = FakeUpstream.seen[0]
        self.assertEqual(path, "/v1/messages?beta=true")
        self.assertEqual(headers["x-api-key"], "secret-key")  # forwarded to the provider...
        raw = self.logged()
        for secret in ("secret-key", "private prompt", '"hi"'):  # ...but never written down
            self.assertNotIn(secret, raw)
        record = json.loads(raw)
        self.assertEqual((record["message_id"], record["request_id"], record["claude_session_id"]),
                         ("msg_1", "req_1", "session-1"))
        self.assertEqual(record["usage"], {"input_tokens": 50, "output_tokens": 7})
        self.assertEqual(record["server_metrics"]["time_to_first_token"], 0.2)
        self.assertEqual(record["server_metrics"]["header:x-queue-time"], "0.05")
        self.assertLessEqual(record["upstream_sent_ms"], record["headers_ms"])
        self.assertLessEqual(record["headers_ms"], record["first_token_ms"])
        self.assertLess(record["first_token_ms"], record["end_ms"])

    def test_request_settings_are_recorded_without_content(self):
        self.post("/via/api.sambanova.ai/v1/messages", {
            "model": "GLM-5.3", "max_tokens": 2048, "temperature": 0,
            "thinking": {"type": "disabled", "budget_tokens": 1024, "note": "private prompt"},
            "output_config": {"effort": "low"}, "tool_choice": {"type": "tool", "name": "private prompt"},
            "system": [{"type": "text", "text": "private prompt", "cache_control": {"type": "ephemeral"}}],
            "tools": [{"name": "classify", "description": "private prompt"}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "private prompt",
                                                       "cache_control": {"type": "ephemeral"}}]}]})
        raw = self.logged()
        self.assertNotIn("private prompt", raw)
        self.assertNotIn("classify", raw)
        self.assertEqual(json.loads(raw)["request_settings"], {
            "max_tokens": 2048, "temperature": 0, "thinking": {"type": "disabled", "budget_tokens": 1024},
            "output_config": {"effort": "low"}, "tool_choice": {"type": "tool"}, "tools": 1, "messages": 1,
            "cache_markers": 2})

    def test_non_streaming_responses_keep_their_length(self):
        response, body = self.post("/via/api.sambanova.ai/v1/messages", {"model": "GLM-5.3"})
        self.assertEqual(json.loads(body)["id"], "msg_2")
        self.assertEqual(json.loads(self.logged())["message_id"], "msg_2")

    def test_only_provider_hosts_are_forwarded(self):
        response, _ = self.post("/via/example.com/v1/messages", {"model": "x"})
        self.assertEqual(response.status, 404)
        self.assertEqual(FakeUpstream.seen, [])
        self.assertIsNone(upstream_for("/v1/messages"))
        self.assertEqual(upstream_for("/via/api.sambanova.ai/v1/messages"), ("api.sambanova.ai", "/v1/messages"))


class FastCheckTests(ProxyCase):
    def test_check_alias_is_sent_as_the_real_model_at_low_effort(self):
        self.post("/via/api.sambanova.ai/v1/messages",
                  {"model": "GLM-5.3-check", "max_tokens": 64, "output_config": {"format": "x"}})
        _, headers, body = FakeUpstream.seen[0]
        sent = json.loads(body)
        self.assertEqual(sent["model"], "GLM-5.3")
        self.assertEqual(sent["output_config"], {"format": "x", "effort": "low"})
        self.assertEqual(headers["x-api-key"], "secret-key")
        record = json.loads(self.logged())
        self.assertEqual((record["request_model"], record["fast_check"]), ("GLM-5.3", True))  # what was sent
        self.assertEqual(record["request_settings"]["output_config"]["effort"], "low")

    def test_main_loop_requests_pass_through_untouched(self):
        self.post("/via/api.sambanova.ai/v1/messages", {"model": "GLM-5.3", "max_tokens": 64})
        _, _, body = FakeUpstream.seen[0]
        self.assertEqual(json.loads(body), {"model": "GLM-5.3", "max_tokens": 64})
        self.assertFalse(json.loads(self.logged())["fast_check"])

    def test_status_answers(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=10)
        connection.request("GET", "/status")
        self.assertEqual(json.loads(connection.getresponse().read()), {"fast_check": True})
        connection.close()

    def test_alias_rules(self):
        from proxy import fast_check
        for model in ("-check", "GLM-5.3", None):
            self.assertFalse(fast_check({"model": model}))
        request = {"model": "gemma-4-31B-it-check", "output_config": "junk"}
        self.assertTrue(fast_check(request))
        self.assertEqual(request, {"model": "gemma-4-31B-it", "output_config": {"effort": "low"}})


class ProxyEdgeTests(ProxyCase):
    """Bad requests, dead upstreams, dropped clients and concurrency."""

    def raw(self, request: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", self.proxy.server_port), timeout=10) as sock:
            sock.sendall(request)
            return sock.recv(4096)

    def test_bad_or_missing_lengths_are_rejected_before_forwarding(self):
        for headers in (b"Content-Length: nope\r\n", b"Content-Length: -4\r\n",
                        b"Content-Length: 999999999999\r\n", b"Transfer-Encoding: chunked\r\n"):
            reply = self.raw(b"POST /via/api.sambanova.ai/v1/messages HTTP/1.1\r\nHost: x\r\n" + headers + b"\r\n")
            self.assertIn(b" 400 ", reply.split(b"\r\n")[0])
        self.assertEqual(FakeUpstream.seen, [])
        self.assertFalse(self.log.exists())

    def test_compression_is_not_requested_upstream(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=10)
        connection.request("POST", "/via/api.sambanova.ai/v1/messages", body=json.dumps({"model": "m"}),
                           headers={"Accept-Encoding": "gzip, br", "content-type": "application/json"})
        connection.getresponse().read()
        connection.close()
        sent = {name.lower(): value for name, value in FakeUpstream.seen[0][1].items()}
        self.assertIn(sent.get("accept-encoding", "identity"), ("identity",))  # the client's gzip ask is dropped

    def test_dead_upstream_returns_502_and_is_recorded(self):
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()
        handler = make_handler(self.log, connect_to=("127.0.0.1", port), scheme="http")
        server = serve(ProxyServer(("127.0.0.1", 0), handler))
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            connection.request("POST", "/via/api.sambanova.ai/v1/messages", body=b"{}")
            self.assertEqual(connection.getresponse().status, 502)
            connection.close()
            record = json.loads(self.logged())
            self.assertIsNone(record["status"])
            self.assertEqual(record["error"], "ConnectionRefusedError")
        finally:
            server.shutdown()
            server.server_close()

    def test_client_hanging_up_mid_stream_keeps_the_timing(self):
        body = json.dumps({"model": "GLM-5.3", "stream": True, "slow": True}).encode()
        with socket.create_connection(("127.0.0.1", self.proxy.server_port), timeout=10) as sock:
            sock.sendall(b"POST /via/api.sambanova.ai/v1/messages HTTP/1.1\r\nHost: x\r\n"
                         b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
            received = b""
            while b"content_block_delta" not in received:
                received += sock.recv(4096)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        record = json.loads(self.logged())
        self.assertEqual(record["error"], "client_disconnected")
        self.assertEqual(record["message_id"], "msg_1")
        self.assertIsNotNone(record["first_token_ms"])

    def test_concurrent_requests_each_get_one_intact_record(self):
        threads = [threading.Thread(target=self.post, args=("/via/api.sambanova.ai/v1/messages",
                                                            {"model": "GLM-5.3", "stream": True}))
                   for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for _ in range(100):
            lines = self.log.read_text().splitlines() if self.log.exists() else []
            if len(lines) == 12:
                break
            time.sleep(0.02)
        self.assertEqual(len(lines), 12)
        self.assertEqual({json.loads(line)["message_id"] for line in lines}, {"msg_1"})


class ServerErrorTests(unittest.TestCase):
    def test_dropped_connections_are_quiet_but_real_errors_are_reported(self):
        server = ProxyServer(("127.0.0.1", 0), FakeUpstream)
        reported = []
        try:
            with unittest.mock.patch.object(ThreadingHTTPServer, "handle_error",
                                            lambda self, request, address: reported.append(address)):
                for error in (ConnectionResetError(), BrokenPipeError(), ValueError()):
                    try:
                        raise error
                    except Exception:
                        server.handle_error(None, type(error).__name__)
        finally:
            server.server_close()
        self.assertEqual(reported, ["ValueError"])


class StreamTimerTests(unittest.TestCase):
    def stream(self, text: str, chunk: int) -> StreamTimer:
        timer, data = StreamTimer(), text.encode()
        for index in range(0, len(data), chunk):
            timer.feed(data[index:index + chunk], float(index))
        return timer

    def test_crlf_events_split_at_any_byte_still_parse(self):
        text = "".join(f"event: {name}\r\ndata: {json.dumps(payload)}\r\n\r\n" for name, payload in EVENTS)
        for chunk in (1, 2, 7, 4096):
            timer = self.stream(text, chunk)
            self.assertEqual(timer.message_id, "msg_1", chunk)
            self.assertEqual(timer.usage["output_tokens"], 7)
            self.assertIsNotNone(timer.first_token_ms)

    def test_junk_and_comments_are_ignored(self):
        timer = self.stream(": keep-alive\n\ndata: not json\n\ndata: [1, 2]\n\ndata:\n\n"
                            'data: {"type": "error", "error": {"type": "overloaded_error"}}\n\n', 3)
        self.assertEqual(timer.error, "overloaded_error")
        self.assertIsNone(timer.message_id)
        self.assertIsNone(timer.first_token_ms)

    def test_multi_line_data_joins_with_newlines(self):
        timer = self.stream('data: {"type": "message_start",\ndata: "message": {"id": "msg_9"}}\n\n', 5)
        self.assertEqual(timer.message_id, "msg_9")

    def test_boolean_usage_values_are_dropped(self):
        timer = StreamTimer()
        timer.merge_usage({"output_tokens": 3, "flag": True, "nested": {"a": 1}, "bad": None})
        self.assertEqual(timer.usage, {"output_tokens": 3, "nested": {"a": 1}})


class SplitTests(unittest.TestCase):
    def test_network_is_upstream_first_token_minus_server_first_token(self):
        split = server_split({"received_ms": 1010, "upstream_sent_ms": 1012, "headers_ms": 1300,
                              "first_token_ms": 1900, "end_ms": 3000,
                              "server_metrics": {"time_to_first_token": 0.5, "queue_time": 0.1}}, 1000)
        self.assertEqual(split["client_ms"], 10)
        self.assertEqual(split["upstream_ttft_ms"], 888)
        self.assertEqual(split["server_ttft_ms"], 500)
        self.assertEqual(split["queue_ms"], 100)
        self.assertEqual(split["network_ms"], 388)
        self.assertIsNone(split["server_total_ms"])

    def test_proxy_url_keeps_sambanova_attribution(self):
        identity = endpoint_identity({"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787/via/api.sambanova.ai"})
        self.assertEqual((identity["provider"], identity["endpoint_host"], identity["endpoint_source"]),
                         ("sambanova", "api.sambanova.ai", "cost-lens-proxy"))
        self.assertEqual(endpoint_identity({"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787"})["provider"], "claude")


if __name__ == "__main__":
    unittest.main()
