"""Unit tests for ServeCompact v1. Run: python -m unittest discover -s tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from servecompact.compactor import compact
from servecompact.pins import extract_pins
from servecompact.pipeline import compact_and_verify
from servecompact.tokens import messages_tokens

PIN_SYS = ("[PIN:safety] Never delete the production database without "
           "a backup. [/PIN]")


def verbose_tool_output(n=150, error_at=100):
    lines = ["header: build log", "version: 1.2.3"]
    for i in range(n):
        if i == error_at:
            lines.append("ERROR: test_auth.py failed: assertion error")
        elif i == 5:
            lines.append("def run_tests():")
        else:
            lines.append(f"line {i} of repetitive build output " + "x" * 40)
    lines.append("done.")
    return "\n".join(lines)


class TestPins(unittest.TestCase):
    def test_bracket_pin_extracted_and_protected(self):
        msgs = [{"role": "user", "content": PIN_SYS},
                {"role": "user", "content": "go"}]
        pins, protected = extract_pins(msgs)
        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0].type, "safety")
        self.assertIn("Never delete", pins[0].text)
        self.assertIn(0, protected)

    def test_system_message_pinned_whole(self):
        msgs = [{"role": "system", "content": "Be careful."},
                {"role": "user", "content": "hi"}]
        pins, protected = extract_pins(msgs)
        self.assertTrue(any(p.type == "system" for p in pins))
        self.assertIn(0, protected)

    def test_last_user_message_protected(self):
        msgs = [{"role": "user", "content": "first"},
                {"role": "assistant", "content": "ans"},
                {"role": "user", "content": "final question"}]
        _, protected = extract_pins(msgs)
        self.assertIn(2, protected)


class TestCompactor(unittest.TestCase):
    def test_pinned_message_untouched(self):
        msgs = [{"role": "user", "content": PIN_SYS},
                {"role": "user", "content": "go"}]
        out, _ = compact(msgs)
        self.assertEqual(out[0]["content"], PIN_SYS)

    def test_dedup_keeps_first(self):
        body = "same text, long enough that the stub is shorter " * 3
        msgs = [{"role": "assistant", "content": body},
                {"role": "assistant", "content": body},
                {"role": "user", "content": "next"}]
        out, stats = compact(msgs)
        self.assertEqual(stats["deduped"], 1)
        self.assertEqual(out[0]["content"], body)
        self.assertIn("duplicate", out[1]["content"])

    def test_superseded_same_tool_call_id(self):
        msgs = [
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "Read",
                                          "arguments": '{"file_path": "a.py"}'}}]},
            {"role": "tool", "tool_call_id": "c1",
             "content": "old body, long enough that the stub is shorter " * 3},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "Read",
                                          "arguments": '{"file_path": "a.py"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "new body"},
            {"role": "user", "content": "proceed"},
        ]
        out, stats = compact(msgs)
        self.assertEqual(stats["superseded"], 1)
        self.assertIn("superseded", out[1]["content"])
        self.assertEqual(out[3]["content"], "new body")

    def test_superseded_same_read_path(self):
        def read_msg(cid, body):
            return {"role": "tool", "tool_call_id": cid, "content": body}

        msgs = [
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "r1", "type": "function",
                             "function": {"name": "Read",
                                          "arguments": '{"file_path": "x.py"}'}}]},
            read_msg("r1", "version one, long enough for stubbing " * 3),
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "r2", "type": "function",
                             "function": {"name": "Read",
                                          "arguments": '{"file_path": "x.py"}'}}]},
            read_msg("r2", "version two"),
            {"role": "user", "content": "done"},
        ]
        out, stats = compact(msgs)
        self.assertEqual(stats["superseded"], 1)
        self.assertEqual(out[3]["content"], "version two")

    def test_correction_removes_old_line_only(self):
        msgs = [
            {"role": "assistant", "content": "Port: 8080\nHost: localhost"},
            {"role": "user", "content": "Correction: Port is now 9090"},
            {"role": "user", "content": "ship it"},
        ]
        # The correction message is not the last user message here, so
        # it is not protected; the final user message is protected.
        out, stats = compact(msgs)
        self.assertEqual(stats["correction_lines"], 1)
        self.assertNotIn("Port: 8080", out[0]["content"])
        self.assertIn("Host: localhost", out[0]["content"])

    def test_verbose_tool_compressed_keeps_error_and_signature(self):
        msgs = [{"role": "tool", "tool_call_id": "t1",
                 "content": verbose_tool_output()},
                {"role": "user", "content": "fix it"}]
        out, stats = compact(msgs)
        self.assertEqual(stats["compressed_messages"], 1)
        text = out[0]["content"]
        self.assertIn("ERROR: test_auth.py failed", text)
        self.assertIn("def run_tests():", text)
        self.assertIn("header: build log", text)
        self.assertLess(len(text), len(verbose_tool_output()))
        self.assertIn("compressed", text)

    def test_stub_never_grows_a_tiny_message(self):
        # A stub longer than the original must not be applied: the
        # compactor is monotonically non-increasing per message.
        msgs = [{"role": "assistant", "content": "ok"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "go"}]
        out, stats = compact(msgs)
        self.assertEqual(stats["deduped"], 0)
        self.assertEqual(out[1]["content"], "ok")

    def test_short_tool_output_untouched(self):
        msgs = [{"role": "tool", "tool_call_id": "t1",
                 "content": "ok\nline2"},
                {"role": "user", "content": "go"}]
        out, stats = compact(msgs)
        self.assertEqual(stats["compressed_messages"], 0)
        self.assertEqual(out[0]["content"], "ok\nline2")


class TestPipeline(unittest.TestCase):
    def test_pins_survive_and_no_fallback(self):
        msgs = [{"role": "system", "content": PIN_SYS},
                {"role": "tool", "tool_call_id": "t",
                 "content": verbose_tool_output()},
                {"role": "user", "content": "go"}]
        to_send, compacted, report = compact_and_verify(msgs)
        self.assertFalse(report["fallback"])
        self.assertEqual(report["pins_surviving_compacted"],
                         report["pins_total"])
        self.assertEqual(report["pins_surviving_sent"],
                         report["pins_total"])
        self.assertLess(report["tokens_compacted"],
                        report["tokens_original"])
        self.assertIs(to_send, compacted)

    def test_verifier_fallback_sends_original(self):
        def bad_compactor(messages):
            # Simulate a compactor that eats a pin.
            import copy
            out = copy.deepcopy(messages)
            out[0]["content"] = "stripped system prompt"
            return out, {"deduped": 0, "superseded": 0,
                         "correction_lines": 0, "compressed_messages": 0,
                         "lines_removed": 0}

        msgs = [{"role": "system", "content": "Never delete prod."},
                {"role": "user", "content": "go"}]
        to_send, _, report = compact_and_verify(msgs,
                                                compactor=bad_compactor)
        self.assertTrue(report["fallback"])
        self.assertEqual(to_send, msgs)
        self.assertEqual(report["pins_surviving_sent"],
                         report["pins_total"])
        self.assertEqual(len(report["pins_missing"]), 1)
        # Logs must not carry pin text.
        self.assertNotIn("Never delete",
                         str(report["pins_missing"]))

    def test_token_estimate_label_present(self):
        from servecompact.tokens import METHOD
        self.assertIn("estimate", METHOD)
        msgs = [{"role": "user", "content": "hello world"}]
        self.assertGreater(messages_tokens(msgs), 0)


if __name__ == "__main__":
    unittest.main()
