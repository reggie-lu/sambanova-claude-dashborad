"""Local timing proxy between Claude Code and a model provider.

Point Claude Code at it with the upstream host in the path:

    ANTHROPIC_BASE_URL=http://127.0.0.1:8787/via/api.sambanova.ai claude

Requests and streamed responses pass through unchanged. For each request it appends
one JSONL record with timestamps (arrival, upstream headers, first token, end), the
response message ID for joining with Claude Code's transcript, token counts, and any
server-side timing the provider reports in usage or headers. It never records API
keys, prompts, or response text.

Fast safety check: a request for a model named `<model>-check` (set
CLAUDE_CODE_AUTO_MODE_MODEL=GLM-5.3-check) is sent as `<model>` with
output_config.effort "low", so auto mode's check answers without a long reasoning pass.
Only those requests change; the main loop passes through as is.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import ssl
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
PROXY_LOG = Path(os.environ.get("COST_LENS_PROXY_LOG", BASE_DIR / "data" / "proxy_requests.jsonl"))
ALLOWED_UPSTREAMS = ("sambanova.ai", "anthropic.com")
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
              "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
# Asked for uncompressed responses so streamed events can be timed as they pass.
DROP_REQUEST = HOP_BY_HOP | {"accept-encoding"}
MAX_BODY = 64 * 1024 * 1024
STANDARD_USAGE = {"input_tokens", "output_tokens", "cache_read_input_tokens",
                  "cache_creation_input_tokens", "cache_creation", "server_tool_use",
                  "service_tier", "output_tokens_details", "inference_geo", "iterations",
                  "speed", "fallback_credit"}
TIMING_HEADER = re.compile(r"time|latency|duration|queue", re.I)
SESSION_HEADER = "x-claude-code-session-id"
CHECK_SUFFIX = "-check"
_write_lock = threading.Lock()


def upstream_for(path: str) -> tuple[str, str] | None:
    """`/via/<host>/rest` → (host, /rest), only for allowed provider hosts."""
    match = re.match(r"^/via/([A-Za-z0-9.-]+)(/.*)?$", path)
    if not match:
        return None
    host = match.group(1).lower().rstrip(".")
    if not any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_UPSTREAMS):
        return None
    return host, match.group(2) or "/"


def now_ms() -> float:
    return time.time() * 1000


class StreamTimer:
    """Parse server-sent events as they pass, keeping only timing and usage."""

    def __init__(self) -> None:
        self.buffer = b""
        self.first_byte_ms: float | None = None
        self.first_event_ms: float | None = None
        self.first_token_ms: float | None = None
        self.message_id: str | None = None
        self.model: str | None = None
        self.usage: dict[str, Any] = {}
        self.error: str | None = None

    def feed(self, chunk: bytes, at_ms: float) -> None:
        if self.first_byte_ms is None:
            self.first_byte_ms = at_ms
        # Server-sent events may end lines with CRLF; normalize once both bytes have arrived.
        self.buffer = (self.buffer + chunk).replace(b"\r\n", b"\n")
        while b"\n\n" in self.buffer:
            raw, self.buffer = self.buffer.split(b"\n\n", 1)
            self.event(raw, at_ms)

    def event(self, raw: bytes, at_ms: float) -> None:
        data = b"\n".join(line[5:].strip() for line in raw.splitlines() if line.startswith(b"data:"))
        try:
            payload = json.loads(data) if data else None
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        if self.first_event_ms is None:
            self.first_event_ms = at_ms
        kind = payload.get("type")
        if kind == "message_start" and isinstance(payload.get("message"), dict):
            message = payload["message"]
            self.message_id, self.model = message.get("id"), message.get("model")
            self.merge_usage(message.get("usage"))
        elif kind == "content_block_delta" and self.first_token_ms is None:
            self.first_token_ms = at_ms
        elif kind == "message_delta":
            self.merge_usage(payload.get("usage"))
        elif kind == "error":
            self.error = str((payload.get("error") or {}).get("type") or "error")
        for key in ("usage", "metrics", "timings"):
            if key in payload and kind not in {"message_start", "message_delta"}:
                self.merge_usage(payload.get(key))

    def merge_usage(self, usage: Any) -> None:
        if isinstance(usage, dict):
            self.usage.update({key: value for key, value in usage.items()
                               if isinstance(value, (int, float, str, dict)) and not isinstance(value, bool)})

    def finish_json(self, body: bytes) -> None:
        """Non-streaming responses: one JSON body."""
        try:
            payload = json.loads(body) if body else None
        except ValueError:
            return
        if isinstance(payload, dict):
            self.message_id, self.model = payload.get("id"), payload.get("model")
            self.merge_usage(payload.get("usage"))


def _short(value: Any) -> Any:
    """Numbers, booleans and short words only: settings, never content."""
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str) and len(value) <= 40:
        return value
    return None


def _count_cache_markers(value: Any, depth: int = 0) -> int:
    if depth > 12:
        return 0
    if isinstance(value, dict):
        return ("cache_control" in value) + sum(_count_cache_markers(item, depth + 1) for item in value.values())
    if isinstance(value, list):
        return sum(_count_cache_markers(item, depth + 1) for item in value)
    return 0


def request_settings(request: Any) -> dict[str, Any]:
    """How the client asked: output limit, thinking, effort, tools, cache markers.
    Records settings only, never prompt text, tool definitions or schemas."""
    if not isinstance(request, dict):
        return {}
    settings: dict[str, Any] = {}
    for key in ("max_tokens", "temperature", "top_p"):
        if _short(request.get(key)) is not None:
            settings[key] = _short(request[key])
    for key in ("thinking", "output_config", "tool_choice"):
        block = request.get(key)
        if isinstance(block, dict):
            kept = {name: _short(value) for name, value in block.items()
                    if name in {"type", "budget_tokens", "effort", "display", "disable_parallel_tool_use"}
                    and _short(value) is not None}
            if kept:
                settings[key] = kept
    if isinstance(request.get("tools"), list):
        settings["tools"] = len(request["tools"])
    if isinstance(request.get("messages"), list):
        settings["messages"] = len(request["messages"])
    settings["cache_markers"] = _count_cache_markers({key: request.get(key) for key in ("system", "messages", "tools")})
    return settings


def server_metrics(usage: dict[str, Any], headers: list[tuple[str, str]]) -> dict[str, Any]:
    """Anything beyond standard token counts: provider-reported timing lives here."""
    metrics = {key: value for key, value in usage.items()
               if key not in STANDARD_USAGE and isinstance(value, (int, float)) and not isinstance(value, bool)}
    for name, value in headers:
        if TIMING_HEADER.search(name):
            metrics[f"header:{name.lower()}"] = value[:200]
    return metrics


def write_record(record: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(record) + "\n").encode()
    with _write_lock:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, data)
        finally:
            os.close(descriptor)


def fast_check(request_json: Any) -> bool:
    """`<model>-check` → `<model>` with low effort, in place. True when it applied."""
    model = request_json.get("model") if isinstance(request_json, dict) else None
    if not (isinstance(model, str) and model.lower().endswith(CHECK_SUFFIX) and len(model) > len(CHECK_SUFFIX)):
        return False
    request_json["model"] = model[:-len(CHECK_SUFFIX)]
    config = request_json.get("output_config")
    request_json["output_config"] = {**(config if isinstance(config, dict) else {}), "effort": "low"}
    return True


def make_handler(log_path: Path, connect_to: tuple[str, int] | None = None,
                 scheme: str = "https") -> type[BaseHTTPRequestHandler]:
    """`connect_to` and `scheme` redirect the connection itself; tests use a local fake upstream."""
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # keep the terminal quiet
            pass

        def do_GET(self) -> None:
            if self.path == "/status":  # lets launchers find a running proxy
                payload = json.dumps({"fast_check": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.forward()

        def do_POST(self) -> None:
            self.forward()

        def forward(self) -> None:
            target = upstream_for(self.path)
            if target is None:
                self.send_error(404, "Use /via/<provider host>/... with a sambanova.ai or anthropic.com host")
                return
            host, path = target
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY or "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                self.send_error(400, "A Content-Length body up to 64 MB is required")
                return
            body = self.rfile.read(length)
            received = now_ms()
            try:
                request_json = json.loads(body) if body else {}
            except ValueError:
                request_json = {}
            headers = {name: value for name, value in self.headers.items() if name.lower() not in DROP_REQUEST}
            checked = fast_check(request_json)
            if checked:
                body = json.dumps(request_json).encode()
            connection_class = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            kwargs: dict[str, Any] = {"timeout": 600}
            if scheme == "https":
                kwargs["context"] = ssl.create_default_context()
            connect_host, connect_port = connect_to or (host, None)
            connection = connection_class(connect_host, connect_port, **kwargs)
            timer = StreamTimer()
            record: dict[str, Any] = {
                "id": str(uuid.uuid4()), "received_at": datetime.now(timezone.utc).isoformat(),
                "received_ms": received, "upstream_host": host, "path": path.split("?")[0],
                "method": self.command, "request_model": request_json.get("model") if isinstance(request_json, dict) else None,
                "stream": bool(isinstance(request_json, dict) and request_json.get("stream")),
                "request_bytes": len(body), "claude_session_id": self.headers.get(SESSION_HEADER),
                "request_settings": request_settings(request_json),
                "fast_check": checked,
            }
            replied = False
            try:
                connection.request(self.command, path, body=body or None, headers=headers)
                sent = now_ms()
                response = connection.getresponse()
                headers_at = now_ms()
                response_headers = response.getheaders()
                replied = True
                self.send_response(response.status, response.reason)
                for name, value in response_headers:
                    if name.lower() not in HOP_BY_HOP:
                        self.send_header(name, value)
                streaming = "text/event-stream" in (response.getheader("Content-Type") or "")
                if streaming:
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    while chunk := response.read1(65536):
                        timer.feed(chunk, now_ms())
                        try:
                            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                            self.wfile.flush()
                        except OSError:
                            # Claude Code hung up (interrupted): stop reading, keep what was timed.
                            timer.error = "client_disconnected"
                            self.close_connection = True
                            break
                    else:
                        self.wfile.write(b"0\r\n\r\n")
                else:
                    payload = response.read()
                    timer.first_byte_ms = now_ms()
                    timer.finish_json(payload)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                self.wfile.flush()
                ended = now_ms()
                record.update({
                    "status": response.status,
                    "request_id": response.getheader("request-id") or response.getheader("x-request-id"),
                    "message_id": timer.message_id, "model": timer.model or record["request_model"],
                    "upstream_sent_ms": sent, "headers_ms": headers_at, "first_byte_ms": timer.first_byte_ms,
                    "first_event_ms": timer.first_event_ms, "first_token_ms": timer.first_token_ms,
                    "end_ms": ended, "usage": {key: timer.usage.get(key) for key in (
                        "input_tokens", "output_tokens", "cache_read_input_tokens",
                        "cache_creation_input_tokens") if key in timer.usage},
                    "server_metrics": server_metrics(timer.usage, response_headers),
                    "error": timer.error,
                })
            except (OSError, http.client.HTTPException) as exc:
                record.update({"status": None, "error": type(exc).__name__, "end_ms": now_ms(),
                               "message_id": timer.message_id})
                self.close_connection = True
                if not replied:
                    try:
                        self.send_error(502, "Upstream request failed")
                    except OSError:
                        pass
            finally:
                connection.close()
                if record["path"].startswith("/v1/messages"):
                    write_record(record, log_path)

    return Handler


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Clients drop idle keep-alive connections all the time; that is not an error.
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--output", type=Path, default=PROXY_LOG)
    args = parser.parse_args(argv)
    server = ProxyServer((args.host, args.port), make_handler(args.output))
    print(f"Cost Lens timing proxy on http://{args.host}:{args.port}/via/<provider host>")
    print(f"  export ANTHROPIC_BASE_URL=http://{args.host}:{args.port}/via/api.sambanova.ai")
    print(f"  records → {args.output}")
    print(f"  fast check: CLAUDE_CODE_AUTO_MODE_MODEL=<model>{CHECK_SUFFIX} → <model> at low effort")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
