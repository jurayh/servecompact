"""Stage 0 tests for v2: tokenizer, checklist, summarizer, pipeline,
meter v2, and the LiteLLM guardrail hook. No model is called, $0.
"""

import asyncio
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from servecompact.checklist import (  # noqa: E402
    ChecklistItem, extract_checklist, verify_checklist)
from servecompact.meter import Meter  # noqa: E402
from servecompact.pipeline import V2Config, compact_and_verify_v2  # noqa: E402
from servecompact.summarizer import (  # noqa: E402
    CapExceeded, MetaResponsesSummarizer, OpenAISummarizer, ParseError,
    StubSummarizer, SummarizerError, expected_sections, parse_summary)
from servecompact.tokens import Counter, est_tokens  # noqa: E402

try:
    from servecompact.litellm_guardrail import ServeCompactGuardrail
    LITELLM = True
except ImportError:
    LITELLM = False


def make_messages():
    filler = "\n".join(f"filler line {i}" for i in range(200))
    return [
        {"role": "system",
         "content": "[PIN:safety]CANARY-SAFETY: never deploy without a "
                    "human review.[/PIN] You are a coding agent."},
        {"role": "user", "content": "Fix the deploy."},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "Read",
                                      "arguments": '{"file_path": "a.py"}'}}]},
        {"role": "tool", "tool_call_id": "c1",
         "content": "DECISION: Use blue-green deploys.\n"
                    "GOAL: Zero downtime.\n"
                    "FACT: The service runs on port 9090.\n"
                    "Status: building\nStatus: ready\n"
                    + filler + "\nERROR: disk full on node 3\n"
                    + "\n".join(f"def f{i}():" for i in range(50))},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c2", "type": "function",
                         "function": {"name": "Read",
                                      "arguments": '{"file_path": "a.py"}'}}]},
        {"role": "tool", "tool_call_id": "c2",
         "content": "FACT: The service runs on port 9091.\n"
                    "Status: done\n" + filler},
        {"role": "user", "content": "Now check the logs."},
    ]


def config(**overrides):
    kwargs = {"trigger_tokens": 100, "summarizer_timeout_s": 5.0}
    kwargs.update(overrides)
    return V2Config(**kwargs)


class TestCounter(unittest.TestCase):
    def test_default_reproduces_estimate(self):
        self.assertEqual(Counter.default().count("hello world"),
                         est_tokens("hello world"))
        self.assertEqual(Counter.default().method[:8], "estimate")

    def test_tiktoken_measures_openai_shaped(self):
        c = Counter.for_model("gpt-4o")
        self.assertTrue(c.is_measured)
        self.assertEqual(c.count("hello world"), 2)

    def test_meta_model_stays_estimate(self):
        c = Counter.for_model("muse-spark-1.3-contributor")
        self.assertFalse(c.is_measured)
        self.assertIn("no tokenizer for this model", c.method)


class TestChecklist(unittest.TestCase):
    def test_extraction(self):
        middle = make_messages()[1:6]
        items, stats = extract_checklist(middle)
        by_type = {}
        for it in items:
            by_type.setdefault(it.type, []).append(it.value)
        self.assertEqual(by_type["decision"], ["Use blue-green deploys."])
        self.assertEqual(by_type["goal"], ["Zero downtime."])
        # FACT repeats: last wins, earlier one superseded.
        self.assertEqual(by_type["fact"],
                         ["The service runs on port 9091."])
        self.assertIn("The service runs on port 9090.",
                      by_type["superseded_value"])
        self.assertEqual(by_type["latest_value"], ["status: done"])
        self.assertIn("building", by_type["superseded_value"])
        self.assertIn("ready", by_type["superseded_value"])
        # 1 error line + 9 signature lines (per-block cap of 10,
        # chosen from 50 def lines).
        self.assertEqual(len(by_type["error_or_signature"]), 10)
        self.assertIn("ERROR: disk full on node 3",
                      by_type["error_or_signature"])
        self.assertEqual(stats["error_lines_seen"], 51)
        self.assertTrue(stats["error_lines_capped"])
        self.assertEqual(stats["labelled"], 3)

    def test_absent_check_ignores_superseded_section(self):
        item = ChecklistItem("superseded_value", "port 9090", "absent", 0)
        summary = ("Facts:\n- The service runs on port 9091.\n"
                   "Superseded:\n- port 9090")
        self.assertTrue(verify_checklist([item], summary)["ok"])
        self.assertFalse(
            verify_checklist([item], "Facts:\n- port 9090 is live")["ok"])

    def test_absent_check_allows_old_value_inside_replacement(self):
        item = ChecklistItem(
            "superseded_value", r"D:\CFML", "absent", 0,
            context=r"D:\CFML\project")
        summary = ("Facts:\n- path: D:\\CFML\\project\n"
                   "Superseded:\n- D:\\CFML")
        self.assertTrue(verify_checklist([item], summary)["ok"])
        # Same old value on a line of its own still fails.
        self.assertFalse(verify_checklist(
            [item], "Facts:\n- D:\\CFML\nSuperseded:\n- D:\\CFML")["ok"])

    def test_loggable_carries_no_value(self):
        item = ChecklistItem("decision", "Secret plan", "normalized", 0)
        self.assertNotIn("Secret plan", str(item.loggable()))

    def test_superseded_cap_keeps_most_recent_per_key(self):
        # 12 keys, each with an old and a new value: 12 superseded
        # items, one per key. The rescope cap keeps the 10 most
        # recent by source message index.
        msgs = []
        for i in range(12):
            msgs.append({"role": "assistant",
                         "content": f"Key{i:02d}: old{i:02d}"})
        for i in range(12):
            msgs.append({"role": "assistant",
                         "content": f"Key{i:02d}: new{i:02d}"})
        items, stats = extract_checklist(msgs)
        by_type = {}
        for it in items:
            by_type.setdefault(it.type, []).append(it.value)
        self.assertEqual(len(by_type["latest_value"]), 12)
        self.assertEqual(len(by_type["superseded_value"]), 10)
        self.assertEqual(stats["superseded_capped"], 2)
        self.assertNotIn("old00", by_type["superseded_value"])
        self.assertNotIn("old01", by_type["superseded_value"])
        self.assertIn("old11", by_type["superseded_value"])


class TestSummarizer(unittest.TestCase):
    def test_parse_summary_missing_section(self):
        with self.assertRaises(ParseError):
            parse_summary("Facts:\n- x", {"decisions", "facts"})

    def test_stub_echo_passes_checklist(self):
        middle = make_messages()[1:6]
        items, _ = extract_checklist(middle)
        result = StubSummarizer("echo").summarize("text", items)
        sections = parse_summary(result.text, expected_sections(items))
        self.assertIn("decisions", sections)
        self.assertTrue(verify_checklist(items, result.text)["ok"])

    def test_stub_drop_first_fails(self):
        middle = make_messages()[1:6]
        items, _ = extract_checklist(middle)
        result = StubSummarizer("drop_first").summarize("text", items)
        self.assertFalse(verify_checklist(items, result.text)["ok"])


class TestOpenAIClient(unittest.TestCase):
    def test_usage_comes_from_response(self):
        payload = {"choices": [{"message": {"content": "Decisions:\n- x"}}],
                   "usage": {"prompt_tokens": 123, "completion_tokens": 45}}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = OpenAISummarizer(
                f"http://127.0.0.1:{server.server_address[1]}/v1",
                "dummy-key", "fake-model")
            result = client.summarize("middle text", [])
            self.assertEqual(result.usage_in, 123)
            self.assertEqual(result.usage_out, 45)
            self.assertEqual(result.usage_method, "measured (API response)")
        finally:
            server.shutdown()


class TestMetaResponsesClient(unittest.TestCase):
    def _server(self, payload):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                seen["body"] = json.loads(self.rfile.read(length))
                seen["auth"] = self.headers.get("Authorization")
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, seen

    def test_responses_shape_usage_and_tally(self):
        payload = {
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "Decisions:\n- x"}]}],
            "usage": {"input_tokens": 1000, "output_tokens": 50}}
        server, seen = self._server(payload)
        try:
            client = MetaResponsesSummarizer(
                cap_usd=10.0, max_output_tokens=4096,
                base_url=f"http://127.0.0.1:{server.server_address[1]}")
            client._surrogate = "hsurr:test"
            result = client.summarize("middle text", [])
            self.assertEqual(result.text, "Decisions:\n- x")
            self.assertEqual(result.usage_in, 1000)
            self.assertEqual(result.usage_out, 50)
            self.assertEqual(result.usage_method,
                             "measured (API response)")
            self.assertAlmostEqual(
                client.spent_usd, 1000 * 0.10 / 1_000_000
                + 50 * 0.20 / 1_000_000)
            body = seen["body"]
            self.assertEqual(body["model"], "muse-spark-1.3-contributor")
            self.assertEqual(body["reasoning"], {"effort": "low"})
            self.assertEqual(body["temperature"], 0)
            self.assertEqual(body["max_output_tokens"], 4096)
            self.assertEqual(seen["auth"], "Bearer hsurr:test")
        finally:
            server.shutdown()

    def test_retry_names_failed_values(self):
        payload = {
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "Decisions:\n- x"}]}],
            "usage": {"input_tokens": 100, "output_tokens": 10}}
        server, seen = self._server(payload)
        try:
            client = MetaResponsesSummarizer(
                cap_usd=10.0,
                base_url=f"http://127.0.0.1:{server.server_address[1]}")
            client._surrogate = "hsurr:test"
            failed = [ChecklistItem("decision",
                                    "Use blue-green deploys.",
                                    "normalized", 0)]
            client.summarize("middle text", [], failed=failed)
            prompt = seen["body"]["input"][0]["content"][0]["text"]
            self.assertIn("Use blue-green deploys.", prompt)
            self.assertIn("missed these items", prompt)
            self.assertNotIn("missed these item types", prompt)
        finally:
            server.shutdown()

    def test_cap_refuses_before_call(self):
        server, seen = self._server({"output": [], "usage": {}})
        try:
            client = MetaResponsesSummarizer(
                cap_usd=0.00001, max_output_tokens=4096,
                base_url=f"http://127.0.0.1:{server.server_address[1]}")
            client._surrogate = "hsurr:test"
            with self.assertRaises(CapExceeded):
                client.summarize("x" * 10000, [])
            self.assertEqual(client.calls, 0)
            self.assertNotIn("body", seen)
        finally:
            server.shutdown()

    def test_no_answer_text_is_an_error(self):
        payload = {"output": [{"type": "reasoning", "content": []}],
                   "usage": {"input_tokens": 10, "output_tokens": 1024}}
        server, seen = self._server(payload)
        try:
            client = MetaResponsesSummarizer(
                cap_usd=10.0,
                base_url=f"http://127.0.0.1:{server.server_address[1]}")
            client._surrogate = "hsurr:test"
            with self.assertRaises(SummarizerError) as ctx:
                client.summarize("middle text", [])
            # Still tallied: the call was made and billed.
            self.assertEqual(client.calls, 1)
            self.assertGreater(client.spent_usd, 0)
            # And the exception carries the billed usage, so the
            # pipeline can attribute it instead of dropping it.
            self.assertEqual(ctx.exception.usage_in, 10)
            self.assertEqual(ctx.exception.usage_out, 1024)
        finally:
            server.shutdown()


class TestPipelineV2(unittest.TestCase):
    def test_echo_stub_summarizes_and_keeps_pins(self):
        to_send, final, report = compact_and_verify_v2(
            make_messages(), summarizer=StubSummarizer("echo"),
            config=config())
        self.assertEqual(report["blocks_summarized"], 1)
        self.assertEqual(report["blocks_fallback_v1"], 0)
        self.assertEqual(report["checklist_passed_first_attempt"],
                         report["checklist_items_total"])
        self.assertLess(report["tokens_final"], report["tokens_v1"])
        self.assertEqual(report["pins_surviving_sent"],
                         report["pins_total"])
        self.assertEqual(to_send, final)
        self.assertTrue(any("[servecompact summary" in (m.get("content") or "")
                            for m in final))

    def test_drop_first_falls_back_to_v1(self):
        to_send, final, report = compact_and_verify_v2(
            make_messages(), summarizer=StubSummarizer("drop_first"),
            config=config())
        self.assertEqual(report["blocks_fallback_v1"], 1)
        self.assertEqual(report["blocks_summarized"], 0)
        self.assertEqual(report["tokens_final"], report["tokens_v1"])
        self.assertEqual(report["checklist_retries"], 1)
        # Dropping the only decision also drops the Decisions section,
        # so the failure is recorded as parse, not checklist: the
        # pipeline checks structure before item presence.
        self.assertEqual(report["attempt_failures"], {"parse": 2})

    def test_chunking_splits_large_block(self):
        msgs = [
            {"role": "system",
             "content": "[PIN:safety]CANARY[/PIN] agent."},
            {"role": "user", "content": "Do the work."},
        ]
        for i in range(12):
            msgs.append({"role": "assistant",
                         "content": f"step {i}\n" + "word " * 400})
        msgs.append({"role": "user", "content": "Now check."})
        # ~6,000 est tokens of middle, chunk 2000 -> 4 chunks.
        _to, _fin, report = compact_and_verify_v2(
            msgs, summarizer=StubSummarizer("echo"),
            config=config(chunk_tokens=2000, trigger_tokens=100))
        self.assertEqual(report["blocks_total"], 4)
        self.assertEqual(report["blocks_summarized"], 4)
        self.assertEqual(report["summarizer_calls"], 4)
        self.assertEqual(report["blocks_fallback_v1"], 0)

    def test_retry_receives_failed_values(self):
        # A middle whose Facts section holds two items: dropping the
        # fact leaves the section present, so the failure is a
        # CHECKLIST failure (not parse) and the retry gets the
        # failed items with their values.
        msgs = [
            {"role": "system",
             "content": "[PIN:safety]CANARY[/PIN] agent."},
            {"role": "user", "content": "Go."},
            {"role": "assistant",
             "content": "DECISION: Walk outside.\nGOAL: Fresh air.\n"
                        "FACT: The sky is blue.\n"
                        "Status: ready\nStatus: done\n"},
            {"role": "user", "content": "Now check."},
        ]

        class RecordingStub:
            def __init__(self):
                self.seen = []
                self._stub = StubSummarizer("drop_fact")

            def summarize(self, middle_text, items, failed=None):
                self.seen.append(failed)
                return self._stub.summarize(middle_text, items,
                                            failed=failed)

        stub = RecordingStub()
        _to, _fin, report = compact_and_verify_v2(
            msgs, summarizer=stub, config=config(trigger_tokens=10))
        self.assertEqual(len(stub.seen), 2)
        self.assertIsNone(stub.seen[0])
        failed = stub.seen[1]
        self.assertTrue(failed)
        self.assertTrue(all(hasattr(it, "value") for it in failed))
        self.assertIn("The sky is blue.",
                      " ".join(it.value for it in failed))
        self.assertEqual(report["attempt_failures"], {"checklist": 2})
        self.assertEqual(report["blocks_fallback_v1"], 1)

    def test_error_with_usage_is_attributed(self):
        # A failed attempt that was still billed must show up in the
        # report: the pilot's smoke session lost exactly this way.
        _to, _fin, report = compact_and_verify_v2(
            make_messages(), summarizer=StubSummarizer("error_usage"),
            config=config())
        self.assertEqual(report["blocks_fallback_v1"], 1)
        self.assertEqual(report["summarizer_calls_failed"], 2)
        self.assertEqual(report["summarizer_tokens_in"], 20000)
        self.assertEqual(report["summarizer_tokens_out"], 10000)
        self.assertGreater(report["summarizer_cost_usd"], 0)
        self.assertEqual(report["attempt_failures"],
                         {"summarizer_error": 2})

    def test_fail_once_then_pass_retries(self):
        _to, _fin, report = compact_and_verify_v2(
            make_messages(), summarizer=StubSummarizer("fail_once_then_pass"),
            config=config())
        self.assertEqual(report["blocks_summarized"], 1)
        self.assertEqual(report["checklist_retries"], 1)

    def test_error_stub_falls_back_without_raising(self):
        _to, _fin, report = compact_and_verify_v2(
            make_messages(), summarizer=StubSummarizer("error"),
            config=config())
        self.assertEqual(report["blocks_fallback_v1"], 1)
        self.assertEqual(report["fallback_tier"], "v1")

    def test_slow_stub_times_out(self):
        _to, _fin, report = compact_and_verify_v2(
            make_messages(), summarizer=StubSummarizer("slow"),
            config=config(summarizer_timeout_s=1.0))
        self.assertEqual(report["blocks_fallback_v1"], 1)

    def test_no_summarizer_is_v1_only(self):
        to_send, final, report = compact_and_verify_v2(
            make_messages(), summarizer=None, config=config())
        self.assertFalse(report["summarizer_configured"])
        self.assertEqual(report["blocks_triggered"], 0)
        self.assertEqual(report["tokens_final"], report["tokens_v1"])

    def test_below_trigger_makes_no_call(self):
        stub = StubSummarizer("echo")
        _to, _fin, report = compact_and_verify_v2(
            make_messages(), summarizer=stub,
            config=config(trigger_tokens=10_000_000))
        self.assertEqual(stub.calls, 0)
        self.assertEqual(report["blocks_below_trigger"], 1)


class TestMeterV2(unittest.TestCase):
    def test_record_v2_accumulates(self):
        _to, _fin, report = compact_and_verify_v2(
            make_messages(), summarizer=StubSummarizer("echo"),
            config=config())
        meter = Meter()
        meter.record_v2(report)
        agg = meter.aggregate()
        self.assertEqual(agg["v2"]["v2_requests"], 1)
        self.assertEqual(agg["v2"]["blocks_summarized"], 1)
        self.assertEqual(
            agg["v2"]["checklist_first_attempt_pass_rate_pct"], 100.0)
        self.assertIn("blocks summarized: 1", meter.label_v2(agg))


@unittest.skipUnless(LITELLM, "litellm not installed")
class TestGuardrail(unittest.TestCase):
    def run_hook(self, data, call_type="completion"):
        guardrail = ServeCompactGuardrail()
        return asyncio.run(guardrail.async_pre_call_hook(
            None, None, data, call_type))

    def test_non_chat_call_type_untouched(self):
        data = {"model": "x", "messages": make_messages()}
        out = self.run_hook(data, "embedding")
        self.assertIs(out, data)
        self.assertNotIn("servecompact_stats", data.get("metadata", {}))

    def test_completion_compacts_v1_only(self):
        data = {"model": "gpt-4o", "messages": make_messages()}
        out = self.run_hook(data, "completion")
        self.assertEqual(out, data)
        stats = data["metadata"]["servecompact_stats"]
        self.assertEqual(stats["blocks_summarized"], 0)  # no summarizer
        self.assertEqual(stats["token_method"],
                         "measured (tiktoken cl100k_base)")

    def test_acompletion_also_compacts(self):
        data = {"model": "gpt-4o", "messages": make_messages()}
        out = self.run_hook(data, "acompletion")
        self.assertEqual(out, data)
        self.assertIn("servecompact_stats", data["metadata"])

    def test_opt_out(self):
        data = {"model": "gpt-4o", "messages": make_messages(),
                "metadata": {"servecompact": {"enabled": False}}}
        out = self.run_hook(data)
        self.assertNotIn("servecompact_stats", data["metadata"])

    def test_non_list_messages_untouched(self):
        data = {"model": "x", "messages": "not a list"}
        self.assertEqual(self.run_hook(data), data)


if __name__ == "__main__":
    unittest.main()
