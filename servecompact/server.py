"""OpenAI-compatible proxy server (stdlib only).

Endpoints:
  POST /v1/chat/completions  compact + verify + forward (stream or not)
  GET  /v1/models            passthrough to upstream
  GET  /health               {"status": "ok", ...}
  GET  /meter                aggregate meter figures (counts only)

Configuration (environment):
  SERVECOMPACT_HOST / SERVECOMPACT_PORT (default 127.0.0.1:8080)
  SERVECOMPACT_UPSTREAM_BASE_URL (default https://api.openai.com/v1)
  SERVECOMPACT_UPSTREAM_API_KEY (else the client's Authorization is
      forwarded as-is)
  SERVECOMPACT_METER_LOG (optional path for JSONL meter records;
      counts/hashes/timings only, never prompt or pin text)
  SERVECOMPACT_DRY_RUN=1 (return compaction result instead of calling
      upstream; labelled dry-run, for local testing only)
"""

import json
import os
import time
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .meter import Meter
from .pipeline import compact_and_verify

METER = Meter()


def _upstream():
    base = os.environ.get("SERVECOMPACT_UPSTREAM_BASE_URL",
                          "https://api.openai.com/v1").rstrip("/")
    return base


def _auth_header(handler):
    key = os.environ.get("SERVECOMPACT_UPSTREAM_API_KEY")
    if key:
        return f"Bearer {key}"
    return handler.headers.get("Authorization")


def _connection(base):
    parsed = urlparse(base)
    host = parsed.hostname
    port = parsed.port
    if parsed.scheme == "https":
        return HTTPSConnection(host, port or 443, timeout=30), parsed.path
    return HTTPConnection(host, port or 80, timeout=30), parsed.path


def _append_meter_log(record):
    path = os.environ.get("SERVECOMPACT_METER_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass  # meter logging must never fail a request


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep stdout clean; errors still raise
        pass

    # -- helpers -----------------------------------------------------
    def _send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # -- routes ------------------------------------------------------
    def do_GET(self):
        if self.path in ("/health", "/v1/health"):
            self._send_json(200, {"status": "ok",
                                  "compactor": "deterministic-v1",
                                  "verifier": "fail-closed"})
            return
        if self.path == "/meter":
            self._send_json(200, METER.aggregate())
            return
        if self.path == "/v1/models":
            self._proxy_get("/models")
            return
        self._send_json(404, {"error": {"message": "not found",
                                        "type": "proxy_error"}})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self._send_json(404, {"error": {"message": "not found",
                                            "type": "proxy_error"}})
            return
        raw = self._read_body()
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": {"message": "invalid JSON",
                                            "type": "proxy_error"}})
            return
        if not isinstance(body, dict):
            self._send_json(400, {"error": {"message": "body must be an "
                                            "object",
                                            "type": "proxy_error"}})
            return
        messages = body.get("messages")
        if isinstance(messages, list):
            to_send, compacted, report = compact_and_verify(messages)
            body = dict(body)
            body["messages"] = to_send
        else:
            compacted, report = None, None
        if os.environ.get("SERVECOMPACT_DRY_RUN") == "1":
            record = METER.record(report) if report is not None else None
            _append_meter_log(record) if record else None
            self._send_json(200, {"dry_run": True,
                                  "messages_to_send": body.get("messages"),
                                  "compacted_messages": compacted,
                                  "meter_record": record})
            return
        upstream_ms = self._proxy_post("/chat/completions", body,
                                       stream=body.get("stream") is True)
        if report is not None:
            record = METER.record(report, upstream_ms=upstream_ms)
            _append_meter_log(record)

    # -- upstream proxying ------------------------------------------
    def _proxy_get(self, path):
        base = _upstream()
        try:
            conn, prefix = _connection(base)
            headers = {}
            auth = _auth_header(self)
            if auth:
                headers["Authorization"] = auth
            conn.request("GET", prefix + path, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            self.send_response(resp.status)
            ctype = resp.getheader("Content-Type") or "application/json"
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            conn.close()
        except OSError:
            self._send_json(502, {"error": {"message": "upstream "
                                            "unreachable",
                                            "type": "proxy_error"}})

    def _proxy_post(self, path, body, stream=False):
        base = _upstream()
        payload = json.dumps(body).encode("utf-8")
        try:
            conn, prefix = _connection(base)
            headers = {"Content-Type": "application/json"}
            auth = _auth_header(self)
            if auth:
                headers["Authorization"] = auth
            started = time.monotonic()
            conn.request("POST", prefix + path, body=payload,
                         headers=headers)
            resp = conn.getresponse()
            if conn.sock is not None:
                conn.sock.settimeout(300 if stream else 120)
            if stream:
                self.send_response(resp.status)
                ctype = (resp.getheader("Content-Type")
                         or "text/event-stream")
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(f"{len(chunk):X}\r\n".encode())
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            else:
                data = resp.read()
                self.send_response(resp.status)
                ctype = (resp.getheader("Content-Type")
                         or "application/json")
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            upstream_ms = round((time.monotonic() - started) * 1000.0, 2)
            conn.close()
            return upstream_ms
        except OSError:
            try:
                self._send_json(502, {"error": {"message": "upstream "
                                                "unreachable",
                                                "type": "proxy_error"}})
            except (OSError, ValueError):
                pass
            return None


def serve(host=None, port=None):
    host = host or os.environ.get("SERVECOMPACT_HOST", "127.0.0.1")
    port = int(port or os.environ.get("SERVECOMPACT_PORT", "8080"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"ServeCompact listening on http://{host}:{port} "
          f"(upstream: {_upstream()})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return server
