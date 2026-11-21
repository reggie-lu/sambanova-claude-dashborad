"""Timing proxy checks: passthrough, timing capture, secrets kept out, and the join."""
import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from latency import server_split
from provider_tracking import endpoint_identity
from proxy import make_handler, upstream_for

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
        if json.loads(body).get("stream"):
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


class ProxyTests(unittest.TestCase):
    def setUp(self):
        FakeUpstream.seen = []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        self.directory = tempfile.TemporaryDirectory()
        self.log = Path(self.directory.name, "proxy.jsonl")
        handler = make_handler(self.log, connect_to=("127.0.0.1", self.upstream.server_port), scheme="http")
        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        for server in (self.upstream, self.proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()

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
        return response, response.read()

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
