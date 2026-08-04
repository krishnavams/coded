"""Test fixtures: a fake OpenAI-compatible server for end-to-end agent tests."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _Handler(BaseHTTPRequestHandler):
    # Populated per-server via the `script` attribute set on the server.
    def log_message(self, *args):  # silence
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        streaming = bool(body.get("stream"))
        # The server's scripting function decides the next assistant message.
        message, usage = self.server.script(body)  # type: ignore[attr-defined]
        if streaming:
            self._send_stream(message, usage)
        else:
            self._send_json(message, usage)

    def _send_json(self, message, usage):
        payload = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "fake",
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
            "usage": usage,
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_stream(self, message, usage):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def sse(obj):
            self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
            self.wfile.flush()

        base = {"id": "chatcmpl-test", "object": "chat.completion.chunk", "model": "fake"}
        if message.get("tool_calls"):
            tc = message["tool_calls"][0]
            sse({**base, "choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": tc["id"], "type": "function",
                 "function": {"name": tc["function"]["name"],
                              "arguments": tc["function"]["arguments"]}}]}, "finish_reason": None}]})
            sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        else:
            for piece in _chunks(message.get("content", "")):
                sse({**base, "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]})
            sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        # Final usage chunk (stream_options include_usage).
        sse({**base, "choices": [], "usage": usage})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def _chunks(text, n=8):
    for i in range(0, len(text), n):
        yield text[i:i + n]


class FakeServer:
    def __init__(self, script):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.script = script  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self):
        host, port = self.httpd.server_address
        return f"http://{host}:{port}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()


@pytest.fixture
def fake_server():
    return FakeServer


# ---------------------------------------------------------------------------
# Deterministic embeddings + generic routing server (for semantic/web/github).
# ---------------------------------------------------------------------------
import hashlib
import re


def embed_text(text: str, dim: int = 64):
    """Stable bag-of-words embedding: shared tokens => higher cosine similarity."""
    vec = [0.0] * dim
    for tok in re.findall(r"[a-zA-Z_]+", text.lower()):
        idx = int(hashlib.md5(tok.encode()).hexdigest(), 16) % dim
        vec[idx] += 1.0
    return vec


class _EmbedHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        inp = body.get("input")
        if isinstance(inp, str):
            inp = [inp]
        data = [{"object": "embedding", "index": i, "embedding": embed_text(t)}
                for i, t in enumerate(inp)]
        payload = {"object": "list", "data": data, "model": body.get("model", "fake"),
                   "usage": {"prompt_tokens": 0, "total_tokens": 0}}
        out = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class EmbeddingsServer:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _EmbedHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self):
        host, port = self.httpd.server_address
        return f"http://{host}:{port}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()


@pytest.fixture
def embeddings_server():
    return EmbeddingsServer


class _RouteHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _handle(self, method):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        status, ctype, body = self.server.route(method, self.path, raw)  # type: ignore[attr-defined]
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


class RouteServer:
    """Generic HTTP server driven by a route(method, path, body) callable."""

    def __init__(self, route):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RouteHandler)
        self.httpd.route = route  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self):
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()


@pytest.fixture
def route_server():
    return RouteServer
