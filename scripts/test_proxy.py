"""Proxy integration test against the fake upstream.

Starts the fake upstream (port 18081) and the real proxy (port 18080,
upstream pointed at the fake), then checks:
  1. non-stream response passes through untouched (fake answer intact),
  2. the upstream received the COMPACTED messages, not the original,
  3. streaming chunks pass through in order, untouched,
  4. /health and /meter respond with the expected shape,
  5. a pinned message reaches the upstream verbatim.
No model is called and no key is used anywhere in this test.

Usage: python3 scripts/test_proxy.py
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
CAPTURE = "/tmp/servecompact_capture.json"


def wait_for(url, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def wait_for_port(port, timeout=15):
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def post(url, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, resp.read()


def verbose(n=150):
    lines = ["header: log", "def authenticate():"]
    for i in range(n):
        lines.append(f"step {i}: filler " + "z" * 50)
    lines.insert(80, "ERROR: test_auth.py failed")
    lines.append("done.")
    return "\n".join(lines)


def main():
    env = dict(os.environ)
    env["CAPTURE_FILE"] = CAPTURE
    fake = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "fake_upstream.py"),
         "--port", "18081"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proxy_env = dict(os.environ)
    proxy_env["SERVECOMPACT_UPSTREAM_BASE_URL"] = "http://127.0.0.1:18081/v1"
    proxy = subprocess.Popen(
        [sys.executable, "-m", "servecompact", "--port", "18080"],
        cwd=ROOT, env=proxy_env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    checks = []
    try:
        assert wait_for("http://127.0.0.1:18080/health"), "proxy did not start"
        assert wait_for_port(18081), "fake upstream did not start"
        status, data = post("http://127.0.0.1:18080/v1/chat/completions", {
            "model": "fake-model", "temperature": 0.5,
            "messages": [
                {"role": "system", "content":
                 "[PIN:safety] Never delete prod. [/PIN]"},
                {"role": "tool", "tool_call_id": "t1",
                 "content": verbose()},
                {"role": "user", "content": "go"},
            ]})
        body = json.loads(data)
        checks.append(("non-stream response untouched",
                       body["choices"][0]["message"]["content"] == "fake answer"))
        checks.append(("non-stream status 200", status == 200))
        with open(CAPTURE, encoding="utf-8") as fh:
            captured = json.load(fh)
        checks.append(("temperature forwarded untouched",
                       captured.get("temperature") == 0.5))
        checks.append(("model forwarded untouched",
                       captured.get("model") == "fake-model"))
        sent_text = json.dumps(captured["messages"])
        # A middle step must be gone (last-10-lines steps legitimately
        # survive, so step 149 is the wrong probe; step 75 is middle).
        checks.append(("upstream got compacted log, not the full log",
                       "step 75" not in sent_text
                       and "compressed" in sent_text))
        checks.append(("pin reached upstream verbatim",
                       "Never delete prod." in sent_text))
        # Streaming: raw bytes must contain the three chunks in order.
        req = urllib.request.Request(
            "http://127.0.0.1:18080/v1/chat/completions",
            data=json.dumps({"model": "fake-model", "stream": True,
                             "messages": [{"role": "user",
                                           "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
        order = [raw.find("chunk-one"), raw.find("chunk-two"),
                 raw.find("chunk-three"), raw.find("[DONE]")]
        checks.append(("stream chunks in order, untouched",
                       all(i >= 0 for i in order) and order == sorted(order)))
        with urllib.request.urlopen(
                "http://127.0.0.1:18080/meter", timeout=5) as resp:
            meter = json.loads(resp.read())
        checks.append(("meter reports requests + fallback rate",
                       meter["requests"] >= 2
                       and "fallback_rate_pct" in meter))
        with urllib.request.urlopen(
                "http://127.0.0.1:18080/health", timeout=5) as resp:
            health = json.loads(resp.read())
        checks.append(("health ok", health.get("status") == "ok"))
    finally:
        proxy.terminate()
        fake.terminate()
    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"  {'OK ' if ok else 'FAIL'} {name}")
    print(f"{len(checks) - len(failed)}/{len(checks)} proxy checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
