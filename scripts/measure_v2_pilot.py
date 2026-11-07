"""Measure the v2 paid pilot from real replay token counts.

No model is called and no money is spent here. This script measures,
from the trace-commons corpus, what a verified-summarization pilot
would feed a summarizer, then prices it at contributor and standard
rates under stated assumptions. The estimate it prints is what goes
before Yuriy for approval; nothing paid runs on the back of it.

Definitions (see SPEC_V2.md):
- "Middle" = messages before the last user message that are NOT
  protected (not pin-bearing, not system). That is the only text the
  summarizer ever sees.
- "Middle after v1" = the same middle after the deterministic v1
  compactor has already done its work. The summarizer only sees what
  v1 could not cut, so this is the honest input size.
- Trigger = middle-after-v1 over TRIGGER_TOKENS (default 2000 est).
  Sessions under the trigger never call the summarizer.
- Summarizer input = middle-after-v1 + instruction overhead
  (INSTRUCTION_TOKENS est, a fixed template; labelled an assumption).
- Summarizer output = OUTPUT_RATIO of input (default 10%; sensitivity
  table at 5/10/20% — the ratio is an assumption until measured).
- One retry is budgeted for checklist failures: the retry input is
  priced as a second full call on RETRY_SHARE (default 25%) of
  triggered sessions. Also labelled an assumption.

Token counts are chars/4 estimates throughout (no tokenizer installed).
Summarizer usage in a real pilot would be MEASURED from the API
response's usage fields; this estimate exists only to size approval.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from convert_traces import convert_dir  # noqa: E402
from servecompact.compactor import compact  # noqa: E402
from servecompact.pins import extract_pins  # noqa: E402
from servecompact.tokens import METHOD, messages_tokens  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
DEFAULT_TRACE_DIR = os.path.expanduser(
    "~/workspace/user/corpus/public/trace-commons/sessions/claude_code")

TRIGGER_TOKENS = 2000
INSTRUCTION_TOKENS = 400
OUTPUT_RATIO = 0.10
RETRY_SHARE = 0.25

CONTRIBUTOR = {"in": 0.10, "out": 0.20, "label": "contributor tier"}
STANDARD = {"in": 1.25, "out": 4.25, "label": "standard tier"}


def price(input_tokens, output_tokens, rates):
    return (input_tokens * rates["in"] + output_tokens * rates["out"]) / 1e6


def main():
    trace_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TRACE_DIR
    sessions = convert_dir(trace_dir)
    rows = []
    for session in sessions:
        messages = session["messages"]
        _, protected = extract_pins(messages)
        last_user = max(i for i, m in enumerate(messages)
                        if m.get("role") == "user")
        middle_idx = [i for i in range(last_user) if i not in protected]
        middle = [messages[i] for i in middle_idx]
        compacted, _ = compact(messages)
        middle_after = [compacted[i] for i in middle_idx]
        rows.append({
            "id": session["id"],
            "middle_tokens": messages_tokens(middle),
            "middle_after_v1_tokens": messages_tokens(middle_after),
            "triggers": messages_tokens(middle_after) > TRIGGER_TOKENS,
        })
    triggered = [r for r in rows if r["triggers"]]
    base_in = sum(r["middle_after_v1_tokens"] + INSTRUCTION_TOKENS
                  for r in triggered)
    retry_in = sum(r["middle_after_v1_tokens"] + INSTRUCTION_TOKENS
                   for r in triggered[:max(1, int(len(triggered) * RETRY_SHARE))])
    total_in = base_in + retry_in
    table = {}
    for ratio in (0.05, 0.10, 0.20):
        out_tokens = int(total_in * ratio)
        table[f"{int(ratio * 100)}%"] = {
            "summarizer_output_tokens": out_tokens,
            "cost_contributor_usd": round(
                price(total_in, out_tokens, CONTRIBUTOR), 4),
            "cost_standard_usd": round(
                price(total_in, out_tokens, STANDARD), 4),
        }
    report = {
        "sessions": len(rows),
        "sessions_triggering": len(triggered),
        "trigger_tokens": TRIGGER_TOKENS,
        "instruction_tokens_assumed": INSTRUCTION_TOKENS,
        "retry_share_assumed": RETRY_SHARE,
        "summarizer_input_tokens": total_in,
        "summarizer_input_tokens_first_attempt": base_in,
        "summarizer_input_tokens_retry_budget": retry_in,
        "total_middle_tokens": sum(r["middle_tokens"] for r in rows),
        "total_middle_after_v1_tokens": sum(
            r["middle_after_v1_tokens"] for r in rows),
        "token_method": METHOD,
        "rates": {"contributor_usd_per_1m": CONTRIBUTOR,
                  "standard_usd_per_1m": STANDARD},
        "sensitivity_output_ratio": table,
        "note": ("Estimate only, for approval sizing. No model called. "
                 "Output ratio and retry share are assumptions; input "
                 "tokens are measured from the corpus as chars/4 "
                 "estimates. A real pilot prices from API-returned usage."),
    }
    out_path = os.path.join(ROOT, "reports", "v2_pilot_estimate.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    print(f"sessions: {len(rows)}, triggering summarization: "
          f"{len(triggered)} (middle after v1 > {TRIGGER_TOKENS} est tokens)")
    print(f"middle tokens total: {report['total_middle_tokens']} "
          f"[estimate]; after v1: "
          f"{report['total_middle_after_v1_tokens']} [estimate]")
    print(f"summarizer input tokens (incl. retry budget): {total_in} "
          f"[estimate]")
    for ratio, vals in table.items():
        print(f"  output ratio {ratio}: output "
              f"{vals['summarizer_output_tokens']} tokens [assumed], "
              f"cost contributor ${vals['cost_contributor_usd']:.4f}, "
              f"standard ${vals['cost_standard_usd']:.4f}")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
