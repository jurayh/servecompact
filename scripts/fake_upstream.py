"""Fake OpenAI-compatible upstream, for proxy integration tests only.

POST /v1/chat/completions:
  - stream=false (or absent): returns a fixed JSON completion and
    writes the received request body to CAPTURE_FILE.
  - stream=true: returns three SSE data chunks, then [DONE], and
    writes the received request body to CAPTURE_FILE.
GET /v1/models: returns an empty model list.

Run: CAPTURE_FILE=/tmp/cap.json python3 scripts/fake_upstream.py --port 18081
"""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/v1/models":
            body = json.dumps({"object": "list", "data": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        capture = os.environ.get("CAPTURE_FILE")
        if capture:
            with open(capture, "wb") as fh:
                fh.write(raw)
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = {}
        if body.get("stream") is True:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk in ("chunk-one", "chunk-two", "chunk-three"):
                payload = (f"data: {json.dumps({'choices': [{'delta': {'content': chunk}}]})}\n\n").encode()
                self.wfile.write(f"{len(payload):X}\r\n".encode())
                self.wfile.write(payload)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            done = b"data: [DONE]\n\n"
            self.wfile.write(f"{len(done):X}\r\n".encode())
            self.wfile.write(done)
            self.wfile.write(b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        else:
            resp = {"id": "fake-1", "object": "chat.completion",
                    "choices": [{"message": {"role": "assistant",
                                             "content": "fake answer"}}]}
            data = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18081)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"fake upstream on 127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
