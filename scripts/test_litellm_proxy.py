"""End-to-end smoke test: real LiteLLM proxy + ServeCompact guardrail.

Starts the fake upstream (port 18081) and a real `litellm` proxy
(port 18082) with the ServeCompact guardrail registered (v1-only, no
summarizer), sends one chat completion through the proxy, and checks
that (a) the request reaching the fake upstream was compacted, (b)
the response passes through, and (c) streaming still works.

Run: .venv/bin/python scripts/test_litellm_proxy.py
$0. No model is called; the "upstream" is local.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
VENV_BIN = os.path.join(ROOT, ".venv", "bin")
FAKE_PORT = 18081
PROXY_PORT = 18082
MASTER_KEY = "test-key-local-only"
CAPTURE = "/tmp/litellm_smoke_capture.json"
CONFIG = "/tmp/litellm_smoke_config.yaml"


def _wait_up(url, timeout_s=90):
    # /health needs the master key on this LiteLLM version.
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {MASTER_KEY}"})
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(1)
    return False


def _post(url, body, stream=False):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {MASTER_KEY}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode()


def main():
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    from test_v2 import make_messages  # noqa: E402

    with open(CONFIG, "w", encoding="utf-8") as fh:
        fh.write(f"""model_list:
  - model_name: fake
    litellm_params:
      model: openai/fake
      api_base: http://127.0.0.1:{FAKE_PORT}/v1
      api_key: fake-key
general_settings:
  master_key: {MASTER_KEY}
guardrails:
  - guardrail_name: "servecompact"
    litellm_params:
      mode: "pre_call"
      guardrail: servecompact.litellm_guardrail.ServeCompactGuardrail
      default_on: true
""")

    env = dict(os.environ)
    env["CAPTURE_FILE"] = CAPTURE
    env["PYTHONPATH"] = ROOT
    # The VM's NO_PROXY carries bracketed IPv6 entries ([::1]) that
    # httpx cannot parse as URL patterns; LiteLLM's LicenseCheck builds
    # an httpx client at import and crashes on them. The test only
    # talks to localhost, so scrub the proxy vars entirely.
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
                "https_proxy", "all_proxy", "NO_PROXY", "no_proxy"):
        env.pop(key, None)
    env["NO_PROXY"] = "localhost,127.0.0.1"
    fake = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "fake_upstream.py"),
         "--port", str(FAKE_PORT)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proxy = subprocess.Popen(
        [os.path.join(VENV_BIN, "litellm"), "--config", CONFIG,
         "--port", str(PROXY_PORT)],
        env=env, cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not _wait_up(f"http://127.0.0.1:{PROXY_PORT}/health"):
            print("FAIL: litellm proxy did not come up", file=sys.stderr)
            return 1
        messages = make_messages()
        body = {"model": "fake", "messages": messages}
        out = _post(f"http://127.0.0.1:{PROXY_PORT}/chat/completions",
                    body)
        resp = json.loads(out)
        assert resp["choices"][0]["message"]["content"] == "fake answer", \
            f"unexpected response: {out[:200]}"
        captured = json.load(open(CAPTURE, encoding="utf-8"))
        sent = captured["messages"]
        print(f"messages in: {len(messages)}, messages reaching upstream: "
              f"{len(sent)}")
        # The guardrail compacted (dedup + supersession + structural
        # compression): message count is unchanged (v1 never drops a
        # message here) but the body shrinks; the pin and the last
        # user message must survive.
        assert len(json.dumps(sent)) < len(json.dumps(messages)), \
            "guardrail did not shrink the request"
        sent_text = "\n".join(
            (m.get("content") or "") for m in sent)
        assert "CANARY-SAFETY" in sent_text, "pin lost by guardrail"
        assert "Now check the logs." in sent_text, "last user lost"
        # Streaming must still work end to end. LiteLLM may split a
        # delta's text across SSE events, so join the deltas before
        # checking rather than grepping the raw stream.
        stream_out = _post(
            f"http://127.0.0.1:{PROXY_PORT}/chat/completions",
            {"model": "fake", "messages": messages, "stream": True})
        deltas = []
        for line in stream_out.splitlines():
            if line.startswith("data: ") and line[6:].strip() != "[DONE]":
                try:
                    deltas.append(json.loads(line[6:])["choices"][0]
                                  ["delta"].get("content") or "")
                except (ValueError, KeyError, IndexError):
                    pass
        assert "".join(deltas) == "chunk-onechunk-twochunk-three", \
            f"streaming deltas wrong: {deltas}"
        # LiteLLM terminates its own stream with a finish_reason chunk
        # and consumes the fake upstream's [DONE]; it also emits a
        # usage-accounting error event at the end because the fake's
        # minimal chunks carry no usage fields. That is a fake-upstream
        # artifact, not the guardrail: the content deltas above all
        # arrived intact and in order.
        assert '"finish_reason":"stop"' in stream_out.replace(" ", ""), \
            "streaming missing finish_reason stop"
        print("PASS: guardrail compacted, response passed through, "
              "streaming intact")
        return 0
    finally:
        proxy.terminate()
        fake.terminate()
        for proc in (proxy, fake):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
