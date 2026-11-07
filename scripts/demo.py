"""Live demo: one session replayed, tokens in/out on screen, pins shown.

Replays synthetic-1-deploy-debug (pins of all 5 types, a superseded
Read, a duplicate status, a correction, and a verbose log) through
compact_and_verify and prints the before/after with every figure
labelled as an estimate at the configured cost rate.

Usage: python3 scripts/demo.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from servecompact.meter import Meter  # noqa: E402
from servecompact.pipeline import compact_and_verify  # noqa: E402
from servecompact.pins import extract_pins  # noqa: E402
from servecompact.tokens import METHOD  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")


def main():
    with open(os.path.join(ROOT, "fixtures", "synthetic_sessions.json"),
              encoding="utf-8") as fh:
        sessions = json.load(fh)
    session = next(s for s in sessions if s["id"] == "synthetic-1-deploy-debug")
    messages = session["messages"]
    pins, _ = extract_pins(messages)
    to_send, compacted, report = compact_and_verify(messages)
    meter = Meter()
    meter.record(report)
    agg = meter.aggregate()

    print("=== ServeCompact live demo: one session replayed ===")
    print(f"session: {session['id']} ({session['notes']})")
    print(f"messages: {len(messages)} -> {len(compacted)}")
    print(f"tokens in (original): {report['tokens_original']} [{METHOD}]")
    print(f"tokens out (compacted): {report['tokens_compacted']} [{METHOD}]")
    print(f"tokens saved: {report['tokens_saved']} [{METHOD}]")
    print(f"reduction: {report['reduction_pct']}% [{METHOD}]")
    print(f"cost saved: ${agg['cost_saved_usd']:.4f} [{agg['cost_basis']}]")
    print(f"fallback: {'YES - original sent' if report['fallback'] else 'no'}")
    print(f"compaction time: {report['compaction_ms']} ms "
          f"(upstream time: not applicable in a replay, no model called)")
    print(f"compactor actions: {report['compactor_stats']}")
    print()
    print("pins planted vs surviving (canary texts are fixture data):")
    compacted_text = "\n".join(
        (m.get("content") if isinstance(m.get("content"), str) else "")
        for m in compacted)
    for pin in pins:
        survived = pin.text in compacted_text
        print(f"  [{'SURVIVED' if survived else 'LOST'}] "
              f"({pin.type}) {pin.text}")
    print(f"pins surviving in compacted prompt: "
          f"{report['pins_surviving_compacted']}/{report['pins_total']}")
    print(f"pins surviving in sent prompt: "
          f"{report['pins_surviving_sent']}/{report['pins_total']}")
    print()
    print("what the model would receive instead of the verbose log:")
    for msg in compacted:
        if msg.get("role") == "tool" and "compressed" in (
                msg.get("content") or ""):
            print("--- compressed tool result (first 6 lines) ---")
            for line in msg["content"].split("\n")[:6]:
                print(f"  {line}")
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
