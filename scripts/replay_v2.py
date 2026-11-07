"""Stage 1 replay (v2): echo-stub summarizer over the same corpus.

Measures, with NO model called and $0 spent:
- trigger rates (how many middle blocks exceed the trigger),
- checklist coverage (items per session, sessions with zero derived
  items — the free-prose gap, in the open),
- the reduction CEILING: v2 vs v1 and v2 vs original, with the stub
  summarizer always passing the checklist and costing $0, so every
  saving figure here is an upper bound, not a measurement. The pilot
  (stage 2) replaces the ceiling with measured numbers and judges VK2.

Also runs a CONTROL proving the v2 verifier is not vacuous: delete
one checklist value from a summary and confirm verify_v2 catches it.

Usage: python3 scripts/replay_v2.py [--trace-dir DIR]
         [--out reports/replay_v2_report.json]
         [--trigger 2000]
"""

import argparse
import json
import os
import sys
from collections import Counter as CounterType

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from convert_traces import convert_dir  # noqa: E402
from servecompact.checklist import extract_checklist  # noqa: E402
from servecompact.meter import Meter  # noqa: E402
from servecompact.pins import extract_pins  # noqa: E402
from servecompact.pipeline import V2Config, compact_and_verify_v2  # noqa: E402
from servecompact.summarizer import StubSummarizer  # noqa: E402
from servecompact.tokens import METHOD, message_text  # noqa: E402
from servecompact.verifier import verify_v2  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
DEFAULT_TRACE_DIR = os.path.expanduser(
    "~/workspace/user/corpus/public/trace-commons/sessions/claude_code")


def run_session(session, meter, config):
    stub = StubSummarizer("echo")
    to_send, final, report = compact_and_verify_v2(
        session["messages"], summarizer=stub, config=config)
    meter.record_v2(report)
    return {
        "id": session["id"],
        "messages_original": len(session["messages"]),
        "messages_final": len(final),
        "tokens_original": report["tokens_original"],
        "tokens_v1": report["tokens_v1"],
        "tokens_final": report["tokens_final"],
        "tokens_saved_v2": report["tokens_saved_v2"],
        "reduction_v2_vs_v1_pct": report["reduction_pct_v2"],
        "reduction_v2_vs_original_pct": round(
            100.0 * (report["tokens_original"] - report["tokens_final"])
            / report["tokens_original"], 2) if report["tokens_original"]
        else 0.0,
        "blocks_total": report["blocks_total"],
        "blocks_triggered": report["blocks_triggered"],
        "blocks_below_trigger": report["blocks_below_trigger"],
        "blocks_summarized": report["blocks_summarized"],
        "blocks_fallback_v1": report["blocks_fallback_v1"],
        "checklist_items_total": report["checklist_items_total"],
        "checklist_passed_first_attempt":
            report["checklist_passed_first_attempt"],
        "summarizer_calls": report["summarizer_calls"],
        "net_saving_usd_ceiling": report["net_saving_usd"],
        "fallback_tier": report["fallback_tier"],
        "pins_total": report["pins_total"],
        "pins_surviving_sent": report["pins_surviving_sent"],
    }


def run_control(session, config):
    """Delete one checklist value from a summary; verify_v2 must catch it."""
    stub = StubSummarizer("echo")
    _to, final, report = compact_and_verify_v2(
        session["messages"], summarizer=stub, config=config)
    if report["blocks_summarized"] == 0:
        return {"ran": False, "reason": "no block summarized"}
    # Re-derive the checklist from the v1-compacted middle blocks
    # (indices on the compacted list; v1 dedup can shift them).
    from servecompact.checklist import normalize
    from servecompact.pipeline import compact_and_verify
    _t, compacted_v1, _r = compact_and_verify(session["messages"])
    _, protected = extract_pins(compacted_v1)
    last_user = max((i for i, m in enumerate(compacted_v1)
                     if m.get("role") == "user"), default=-1)
    middle = [i for i in range(last_user) if i not in protected] \
        if last_user >= 0 else []
    items = []
    summary_texts = []
    for msg in final:
        content = msg.get("content") or ""
        if "[servecompact summary" in content:
            summary_texts.append(content)
    blocks = []
    for i in middle:
        if blocks and i == blocks[-1][-1] + 1:
            blocks[-1].append(i)
        else:
            blocks.append([i])
    for block in blocks:
        block_items, _ = extract_checklist(
            [compacted_v1[i] for i in block])
        items.extend(block_items)
    if not items or not summary_texts:
        return {"ran": False, "reason": "no items or summaries"}
    final_text = "\n".join(message_text(m) for m in final)
    norm_final = normalize(final_text)
    victim = next(
        (it for it in items
         if it.check == "normalized"
         and norm_final.count(normalize(it.value)) == 1), None)
    if victim is None:
        return {"ran": False,
                "reason": "no uniquely-occurring normalized item"}
    corrupted = json.loads(json.dumps(final))
    removed = False
    for msg in corrupted:
        content = msg.get("content") or ""
        if victim.value in content:
            msg["content"] = content.replace(victim.value, "", 1)
            removed = True
            break
    check = verify_v2(session["messages"], corrupted, items,
                      [s.replace(victim.value, "", 1) if victim.value in s
                       else s for s in summary_texts],
                      report["tokens_v1"], report["tokens_final"])
    return {
        "ran": True,
        "item_removed": removed,
        "item_type": victim.type,
        "verifier_caught_miss": not check["ok"],
        "checklist_failed_reported":
            len(check["checklist"]["failed_loggable"]),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-dir", default=DEFAULT_TRACE_DIR)
    parser.add_argument("--out", default=os.path.join(
        ROOT, "reports", "replay_v2_report.json"))
    parser.add_argument("--trigger", type=int, default=2000)
    args = parser.parse_args()

    meter = Meter()
    config = V2Config(trigger_tokens=args.trigger)
    trace_sessions = []
    if os.path.isdir(args.trace_dir):
        trace_sessions = convert_dir(args.trace_dir)
    else:
        print(f"WARNING: trace dir not found: {args.trace_dir}. "
              f"Replaying synthetic sessions only.", file=sys.stderr)

    with open(os.path.join(ROOT, "fixtures", "synthetic_sessions.json"),
              encoding="utf-8") as fh:
        synthetic = json.load(fh)

    trace_rows = [run_session(s, meter, config) for s in trace_sessions]
    synth_meter = Meter()
    synth_rows = [run_session(s, synth_meter, config)
                  for s in synthetic]

    agg = meter.aggregate()
    synth_agg = synth_meter.aggregate()
    control = {"ran": False, "reason": "no session with a summary found"}
    for session in (trace_sessions or synthetic):
        control = run_control(session, config)
        if control.get("ran"):
            control["session_id"] = session["id"]
            break

    total_items = sum(r["checklist_items_total"] for r in trace_rows)
    zero_item = [r["id"] for r in trace_rows
                 if r["checklist_items_total"] == 0]
    triggered = sum(r["blocks_triggered"] for r in trace_rows)
    blocks = sum(r["blocks_total"] for r in trace_rows)
    summarized = sum(r["blocks_summarized"] for r in trace_rows)
    fallback_blocks = sum(r["blocks_fallback_v1"] for r in trace_rows)
    weighted_v2 = round(
        100.0 * sum(r["tokens_original"] - r["tokens_final"]
                    for r in trace_rows)
        / sum(r["tokens_original"] for r in trace_rows), 2) \
        if trace_rows else 0.0
    weighted_v1 = round(
        100.0 * sum(r["tokens_original"] - r["tokens_v1"]
                    for r in trace_rows)
        / sum(r["tokens_original"] for r in trace_rows), 2) \
        if trace_rows else 0.0

    report = {
        "token_method": METHOD,
        "cost_basis": agg["cost_basis"],
        "trigger_tokens": args.trigger,
        "summarizer": "stub:echo (no model called, $0; ceiling only)",
        "trace_sessions": len(trace_rows),
        "trace_aggregate": agg,
        "trace_v2": {
            "token_weighted_reduction_v2_vs_original_pct": weighted_v2,
            "token_weighted_reduction_v1_vs_original_pct": weighted_v1,
            "token_weighted_incremental_v2_over_v1_pct":
                round(weighted_v2 - weighted_v1, 2),
            "blocks_total": blocks,
            "blocks_triggered": triggered,
            "blocks_summarized": summarized,
            "blocks_fallback_v1": fallback_blocks,
            "trigger_rate_pct": round(100.0 * triggered / blocks, 2)
                if blocks else 0.0,
            "checklist_items_total": total_items,
            "checklist_items_per_session_mean": round(
                total_items / len(trace_rows), 2) if trace_rows else 0.0,
            "sessions_with_zero_checklist_items": len(zero_item),
            "zero_item_session_ids": zero_item,
            "net_saving_usd_ceiling": round(
                sum(r["net_saving_usd_ceiling"] for r in trace_rows), 4),
        },
        "synthetic_sessions": len(synth_rows),
        "synthetic_aggregate": synth_agg,
        "trace_rows": trace_rows,
        "synthetic_rows": synth_rows,
        "control": control,
        "control_ok": bool(control.get("verifier_caught_miss")),
        "reading": (
            "Stage 1 ceiling only. The stub always passes the checklist "
            "and costs $0, so the v2 reduction and net saving are upper "
            "bounds. Reliability (VK1/VK3) and real net saving (VK2) are "
            "judged on the stage-2 pilot with a real summarizer, not here."),
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)

    print(f"trace sessions: {len(trace_rows)}, trigger {args.trigger} est "
          f"tokens, blocks {blocks} triggered {triggered} "
          f"({report['trace_v2']['trigger_rate_pct']}%), summarized "
          f"{summarized}, fallback {fallback_blocks}")
    print(f"checklist: {total_items} items total, "
          f"{report['trace_v2']['checklist_items_per_session_mean']}/session, "
          f"{len(zero_item)} sessions with zero derived items")
    print(f"reduction (token-weighted, est): v1 {weighted_v1}% -> "
          f"v2 {weighted_v2}% (incremental "
          f"{report['trace_v2']['token_weighted_incremental_v2_over_v1_pct']}%)")
    print(f"net saving ceiling (stub $0): "
          f"${report['trace_v2']['net_saving_usd_ceiling']:.4f} "
          f"[{agg['cost_basis']}]")
    print(f"control: verifier caught removed checklist value = "
          f"{control.get('verifier_caught_miss')}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
