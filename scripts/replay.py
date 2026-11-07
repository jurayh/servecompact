"""Replay sessions through compaction + verification.

Replay set (see KILLS.md): all trace-commons Claude Code sessions
found under --trace-dir (public proxy corpus, not Yuriy's own
sessions), each with 5 planted pinned canaries, PLUS the 4 synthetic
sessions (reported separately, not in the K2 denominator).

Also runs a CONTROL that proves the verifier is not vacuous: take one
session's compacted prompt, delete one canary from it, and confirm the
verifier reports the miss and the pipeline falls back to the original.

Usage: python3 scripts/replay.py [--trace-dir DIR]
         [--out reports/replay_report.json] [--table reports/replay_table.md]
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from convert_traces import convert_dir  # noqa: E402
from servecompact.meter import Meter  # noqa: E402
from servecompact.pipeline import compact_and_verify  # noqa: E402
from servecompact.tokens import METHOD, message_text  # noqa: E402
from servecompact.verifier import verify  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
DEFAULT_TRACE_DIR = os.path.expanduser(
    "~/workspace/user/corpus/public/trace-commons/sessions/claude_code")


def run_session(session, meter):
    to_send, compacted, report = compact_and_verify(session["messages"])
    meter.record(report)
    return {
        "id": session["id"],
        "messages_original": len(session["messages"]),
        "messages_compacted": len(compacted),
        "tokens_original": report["tokens_original"],
        "tokens_compacted": report["tokens_compacted"],
        "tokens_saved": report["tokens_saved"],
        "reduction_pct": report["reduction_pct"],
        "fallback": report["fallback"],
        "pins_total": report["pins_total"],
        "pins_surviving_compacted": report["pins_surviving_compacted"],
        "pins_surviving_sent": report["pins_surviving_sent"],
        "compactor_stats": report["compactor_stats"],
        "compaction_ms": report["compaction_ms"],
    }


def run_control(session):
    """Corrupt one canary in the compacted prompt; verifier must catch it."""
    _, compacted, report = compact_and_verify(session["messages"])
    if report["pins_total"] == 0:
        return {"ran": False, "reason": "no pins in session"}
    corrupted = json.loads(json.dumps(compacted))
    canary = "CANARY-SAFETY"
    removed = False
    for msg in corrupted:
        text = message_text(msg)
        if canary in text:
            if isinstance(msg.get("content"), str):
                msg["content"] = msg["content"].replace(
                    "CANARY-SAFETY: never run DROP TABLE on the production "
                    "database. ", "")
                removed = True
            break
    check = verify(session["messages"], corrupted)
    fallback_would_trigger = not check["ok"]
    return {
        "ran": True,
        "canary_removed": removed,
        "verifier_caught_miss": fallback_would_trigger,
        "missing_pins_reported": len(check["pins_missing"]),
        "pins_missing_carry_no_text": all(
            "CANARY" not in json.dumps(p) for p in check["pins_missing"]),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-dir", default=DEFAULT_TRACE_DIR)
    parser.add_argument("--out", default=os.path.join(
        ROOT, "reports", "replay_report.json"))
    parser.add_argument("--table", default=os.path.join(
        ROOT, "reports", "replay_table.md"))
    args = parser.parse_args()

    meter = Meter()
    trace_sessions = []
    if os.path.isdir(args.trace_dir):
        trace_sessions = convert_dir(args.trace_dir)
    else:
        print(f"WARNING: trace dir not found: {args.trace_dir}. "
              f"Replaying synthetic sessions only.", file=sys.stderr)

    with open(os.path.join(ROOT, "fixtures", "synthetic_sessions.json"),
              encoding="utf-8") as fh:
        synthetic = json.load(fh)

    trace_rows = [run_session(s, meter) for s in trace_sessions]
    # Synthetic sessions get their own meter so the K2 denominator is
    # trace-only, exactly as pre-registered.
    synth_meter = Meter()
    synth_rows = [run_session(s, synth_meter) for s in synthetic]

    agg = meter.aggregate()
    synth_agg = synth_meter.aggregate()
    control = run_control(trace_sessions[0]) if trace_sessions else \
        run_control(synthetic[0])

    unweighted = (sum(r["reduction_pct"] for r in trace_rows)
                  / len(trace_rows)) if trace_rows else 0.0
    verdicts = {
        "K1_fallback_rate_gt_10pct": agg["fallback_rate_pct"] > 10.0,
        "K2_token_weighted_reduction_lt_20pct":
            agg["token_weighted_reduction_pct"] < 20.0,
        "K2_unweighted_mean_lt_20pct_caveat": unweighted < 20.0,
        "sent_pin_survival_lt_100pct":
            agg["pins_surviving_sent"] < agg["pins_total"],
        "control_verifier_failed": not (
            control.get("verifier_caught_miss")
            and control.get("pins_missing_carry_no_text")),
    }
    kill_hit = (verdicts["K1_fallback_rate_gt_10pct"]
                or verdicts["K2_token_weighted_reduction_lt_20pct"]
                or verdicts["sent_pin_survival_lt_100pct"]
                or verdicts["control_verifier_failed"])

    report = {
        "token_method": METHOD,
        "cost_basis": agg["cost_basis"],
        "trace_sessions": len(trace_rows),
        "trace_aggregate": agg,
        "trace_unweighted_mean_reduction_pct": round(unweighted, 2),
        "synthetic_sessions": len(synth_rows),
        "synthetic_aggregate": synth_agg,
        "trace_rows": trace_rows,
        "synthetic_rows": synth_rows,
        "control": control,
        "verdicts": verdicts,
        "kill_hit": kill_hit,
        "verdict": ("STOP or RESCOPE (a pre-registered kill bar was hit)"
                    if kill_hit else
                    "PASS: no pre-registered kill bar hit; proceed to "
                    "fixture gate (K3) and then the live demo"),
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)

    lines = ["# ServeCompact replay table",
             "",
             f"Tokens are {METHOD}. Cost at {agg['cost_basis']}.",
             "",
             "## Trace-commons sessions (real shapes, planted canaries)",
             "",
             "| session | msgs | tokens in (est) | tokens out (est) | saved (est) | reduction | fallback | pins surviving (compacted) |",
             "|---|---|---|---|---|---|---|---|"]
    for r in trace_rows:
        lines.append(
            f"| {r['id'][:8]} | {r['messages_original']} | "
            f"{r['tokens_original']} | {r['tokens_compacted']} | "
            f"{r['tokens_saved']} | {r['reduction_pct']}% | "
            f"{'YES' if r['fallback'] else 'no'} | "
            f"{r['pins_surviving_compacted']}/{r['pins_total']} |")
    lines += ["",
              f"**Trace total: {agg['tokens_original']} -> "
              f"{agg['tokens_compacted']} tokens (est), saved "
              f"{agg['tokens_saved']} (est), token-weighted reduction "
              f"{agg['token_weighted_reduction_pct']}%, fallback rate "
              f"{agg['fallback_rate_pct']}%, cost saved "
              f"${agg['cost_saved_usd']:.4f} [{agg['cost_basis']}].**",
              f"Unweighted mean reduction: {round(unweighted, 2)}%.",
              "",
              "## Synthetic sessions (reported separately)",
              "",
              "| session | tokens in (est) | tokens out (est) | reduction | fallback |",
              "|---|---|---|---|---|"]
    for r in synth_rows:
        lines.append(f"| {r['id']} | {r['tokens_original']} | "
                     f"{r['tokens_compacted']} | {r['reduction_pct']}% | "
                     f"{'YES' if r['fallback'] else 'no'} |")
    lines += ["",
              f"**Synthetic total: {synth_agg['tokens_original']} -> "
              f"{synth_agg['tokens_compacted']} tokens (est), reduction "
              f"{synth_agg['token_weighted_reduction_pct']}%.**",
              "",
              f"**Control (verifier not vacuous):** "
              f"canary removed={control.get('canary_removed')}, "
              f"verifier caught miss={control.get('verifier_caught_miss')}, "
              f"missing-pin log carries no pin text="
              f"{control.get('pins_missing_carry_no_text')}.",
              "",
              f"**Verdict: {report['verdict']}**",
              ""]
    with open(args.table, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    print(f"trace sessions: {len(trace_rows)}, "
          f"tokens {agg['tokens_original']} -> {agg['tokens_compacted']} "
          f"(est), reduction {agg['token_weighted_reduction_pct']}% "
          f"(token-weighted, est), fallback {agg['fallback_rate_pct']}%, "
          f"pins sent {agg['pins_surviving_sent']}/{agg['pins_total']}")
    print(f"synthetic: {synth_agg['tokens_original']} -> "
          f"{synth_agg['tokens_compacted']} (est), "
          f"{synth_agg['token_weighted_reduction_pct']}%")
    print(f"control: verifier caught miss = "
          f"{control.get('verifier_caught_miss')}")
    print(f"verdict: {report['verdict']}")
    print(f"wrote {args.out} and {args.table}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
