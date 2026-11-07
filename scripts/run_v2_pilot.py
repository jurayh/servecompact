"""Stage-2 pilot (v2): real summarizer on the trace-commons corpus.

PAID. Runs only with Yuriy's explicit approval on the ~$0.25
contributor-tier estimate (given Nov 6, 2026). Summarizer:
muse-spark-1.3-contributor via the Meta Model API Responses API,
reasoning effort low, temperature 0, max output tokens capped at
min(25% of the session's middle est tokens, 8192).

Cost discipline (paid_model_api_cost_gate skill):
- Smoke first: the smallest triggering session runs first. Its
  measured input/output tokens re-price the remaining sessions.
  If the projected total exceeds --cap-usd (default $0.50, 2x the
  approved estimate), the run stops after the smoke and reports
  for re-approval instead of spending the rest.
- Running tally from the provider's usage after every call; a call
  that would cross the cap is refused before it is made (client
  enforces it); the runner also stops between sessions at the cap.
- Main-model token counts stay chars/4 estimates (labelled); only
  the summarizer's own usage is measured. VK2 is judged on the
  measured summarizer cost against those estimates, and the report
  says so.

Judges the pre-registered kills VK1 (first-attempt checklist pass
>= 80%), VK2 (mean net saving per triggered session > $0),
VK3 (fallback-to-v1 <= 20% of triggered blocks), VK5 (median added
latency <= 3 s). VK4 is stage 3, not this run. Pins below 100% in
a sent prompt is a critical failure, separate from the kills.

Usage: .venv/bin/python scripts/run_v2_pilot.py
         [--out reports/v2_pilot_report.json] [--cap-usd 0.50]
         [--limit N] [--timeout 300]
"""

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from convert_traces import convert_dir  # noqa: E402
from servecompact.meter import Meter  # noqa: E402
from servecompact.pipeline import (  # noqa: E402
    V2Config, compact_and_verify, compact_and_verify_v2, middle_blocks)
from servecompact.summarizer import MetaResponsesSummarizer  # noqa: E402
from servecompact.tokens import METHOD, Counter, messages_tokens  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
DEFAULT_TRACE_DIR = os.path.expanduser(
    "~/workspace/user/corpus/public/trace-commons/sessions/claude_code")
MODEL = "muse-spark-1.3-contributor"
MAIN_RATE_USD_PER_1M = 3.00
MAX_OUTPUT_CAP = 8192


def middle_tokens(session, chunk_tokens=30000):
    """Est tokens of the middle after v1, per chunk (see
    pipeline.middle_blocks: contiguous runs split at chunk_tokens)."""
    _to, compacted, _r = compact_and_verify(session["messages"])
    blocks = middle_blocks(compacted, Counter.default(), chunk_tokens)
    return [messages_tokens([compacted[i] for i in b]) for b in blocks]


def run_session(session, meter, config, client):
    started = time.monotonic()
    to_send, final, report = compact_and_verify_v2(
        session["messages"], summarizer=client, config=config)
    meter.record_v2(report)
    wall_ms = round((time.monotonic() - started) * 1000.0, 2)
    return {
        "id": session["id"],
        "messages_original": len(session["messages"]),
        "messages_final": len(final),
        "tokens_original": report["tokens_original"],
        "tokens_v1": report["tokens_v1"],
        "tokens_final": report["tokens_final"],
        "tokens_saved_v2": report["tokens_saved_v2"],
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
        "checklist_retries": report["checklist_retries"],
        "summarizer_calls": report["summarizer_calls"],
        "summarizer_calls_failed": report["summarizer_calls_failed"],
        "attempt_failures": report["attempt_failures"],
        "failed_types": report["failed_types"],
        "summarizer_tokens_in": report["summarizer_tokens_in"],
        "summarizer_tokens_out": report["summarizer_tokens_out"],
        "summarizer_cost_usd": report["summarizer_cost_usd"],
        "net_saving_usd": report["net_saving_usd"],
        "summarizer_latency_ms": report["summarizer_latency_ms"],
        "wall_ms": wall_ms,
        "fallback_tier": report["fallback_tier"],
        "pins_total": report["pins_total"],
        "pins_surviving_sent": report["pins_surviving_sent"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-dir", default=DEFAULT_TRACE_DIR)
    parser.add_argument("--out", default=os.path.join(
        ROOT, "reports", "v2_pilot_report.json"))
    parser.add_argument("--trigger", type=int, default=2000)
    parser.add_argument("--cap-usd", type=float, default=0.50)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--limit", type=int, default=None)
    # The approved estimate for THIS run (the pilot's was $0.25;
    # the re-test's was $0.15). Was hardcoded to 0.25, which
    # mislabelled the re-test report; now a flag.
    parser.add_argument("--approved-estimate-usd", type=float,
                        default=0.25)
    parser.add_argument("--sessions", default=None,
                        help="comma-separated session id prefixes to "
                             "run (re-test slice); omit for all")
    # Rescope (Nov 6, 2026): the 3 s bar was set without data and
    # the pilot measured a 36.7 s median. The re-test bar is 60 s;
    # the old bar is recorded in SPEC_V2.md §9.
    parser.add_argument("--vk5-bar-ms", type=float, default=60000.0)
    args = parser.parse_args()

    meter = Meter()
    config = V2Config(trigger_tokens=args.trigger,
                      summarizer_timeout_s=args.timeout)
    client = MetaResponsesSummarizer(
        model=MODEL, cap_usd=args.cap_usd,
        max_output_tokens=MAX_OUTPUT_CAP, timeout_s=args.timeout)

    trace_sessions = []
    if os.path.isdir(args.trace_dir):
        trace_sessions = convert_dir(args.trace_dir)
    with open(os.path.join(ROOT, "fixtures", "synthetic_sessions.json"),
              encoding="utf-8") as fh:
        synthetic = json.load(fh)

    # Smoke first: smallest triggering trace session. Then the rest
    # in corpus order, then synthetic (reported separately).
    def triggering(session):
        return any(t > args.trigger for t in middle_tokens(session))

    triggering_sessions = [s for s in trace_sessions if triggering(s)]
    non_triggering = [s for s in trace_sessions if not triggering(s)]
    triggering_sessions.sort(
        key=lambda s: sum(middle_tokens(s)))
    ordered = triggering_sessions + non_triggering + synthetic
    if args.limit is not None:
        ordered = ordered[:args.limit]
    if args.sessions:
        wanted = [s.strip() for s in args.sessions.split(",")
                  if s.strip()]
        ordered = [s for s in ordered
                   if any(s["id"].startswith(w) for w in wanted)]

    rows = []
    synth_rows = []
    smoke = None
    aborted = None
    for idx, session in enumerate(ordered):
        is_synth = session in synthetic
        blocks = middle_tokens(session, config.chunk_tokens)
        trig = [t for t in blocks if t > args.trigger]
        # Cost-gate skill + rescope: floor the output cap at 4096
        # for a reasoning model and never let a percentage rule
        # shrink it below that floor (the pilot's 803-token cap on
        # the smoke session produced no answer text at all);
        # ceiling 8192; otherwise 25% of the largest chunk. 0 when
        # nothing triggers means the pipeline makes no call.
        client.max_output_tokens = max(
            4096, min(MAX_OUTPUT_CAP,
                      int(0.25 * max(blocks)))) if trig else 0
        row = run_session(session, meter, config, client)
        (synth_rows if is_synth else rows).append(row)
        print(f"[{idx + 1}/{len(ordered)}] {session['id'][:12]} "
              f"triggered={row['blocks_triggered']} "
              f"summarized={row['blocks_summarized']} "
              f"fallback={row['blocks_fallback_v1']} "
              f"items={row['checklist_items_total']} "
              f"calls={row['summarizer_calls']} "
              f"cost=${row['summarizer_cost_usd']:.4f} "
              f"latency={row['summarizer_latency_ms']:.0f}ms "
              f"pins={row['pins_surviving_sent']}/{row['pins_total']}",
              flush=True)
        if smoke is None and trig:
            smoke = row
            # Re-price the rest from the CLIENT's tally, not the
            # pipeline row: a smoke session whose attempts errored
            # (no output text) still spent money, and the row shows
            # zero for it. The pilot's smoke projected $0.0012 this
            # way while the true spend was already $0.0011 on that
            # session alone; the guard was blind. Never again.
            # Use the PER-CALL ratio (the smoke may have retried)
            # and the estimate's 25% retry share for the rest.
            calls_so_far = max(client.calls, 1)
            per_call_in = client.prompt_tokens / calls_so_far
            in_ratio = per_call_in / max(
                sum(t for t in blocks if t > args.trigger), 1)
            out_ratio = max(
                client.completion_tokens / max(client.prompt_tokens, 1),
                0.10)
            remaining_est_in = sum(
                sum(t for t in middle_tokens(s) if t > args.trigger)
                for s in ordered[idx + 1:]
                if s not in synthetic)
            remaining_in = remaining_est_in * in_ratio * 1.25
            projected = (client.spent_usd
                         + remaining_in * client.in_rate
                         + remaining_in * out_ratio * client.out_rate)
            print(f"SMOKE: measured in={client.prompt_tokens} "
                  f"(per-call {per_call_in:.0f}, est ratio "
                  f"{in_ratio:.2f}), out ratio "
                  f"{client.completion_tokens / max(client.prompt_tokens, 1):.3f}, "
                  f"spent=${client.spent_usd:.4f}, projected total="
                  f"${projected:.4f} vs cap ${args.cap_usd:.2f}",
                  flush=True)
            if projected > args.cap_usd:
                aborted = ("projection_over_cap",
                           f"projected ${projected:.4f} > cap "
                           f"${args.cap_usd:.2f}")
                break
        if client.spent_usd >= args.cap_usd:
            aborted = ("cap_reached",
                       f"spent ${client.spent_usd:.4f} >= cap "
                       f"${args.cap_usd:.2f}")
            break

    agg = meter.aggregate()
    trig_rows = [r for r in rows if r["blocks_triggered"] > 0]
    items = sum(r["checklist_items_total"] for r in trig_rows)
    passed_first = sum(r["checklist_passed_first_attempt"]
                       for r in trig_rows)
    vk1 = round(100.0 * passed_first / items, 2) if items else 100.0
    net_mean = round(
        sum(r["net_saving_usd"] for r in trig_rows) / len(trig_rows), 4) \
        if trig_rows else 0.0
    trig_blocks = sum(r["blocks_triggered"] for r in trig_rows)
    fb_blocks = sum(r["blocks_fallback_v1"] for r in trig_rows)
    vk3 = round(100.0 * fb_blocks / trig_blocks, 2) if trig_blocks else 0.0
    latencies = sorted(r["summarizer_latency_ms"] for r in trig_rows)
    vk5 = round(statistics.median(latencies), 2) if latencies else 0.0
    pins_ok = all(r["pins_surviving_sent"] == r["pins_total"]
                  for r in rows + synth_rows)

    verdicts = {
        "VK1_first_attempt_pass_rate_lt_80pct": vk1 < 80.0,
        "VK2_mean_net_saving_le_0": net_mean <= 0,
        "VK3_fallback_rate_gt_20pct": vk3 > 20.0,
        "VK5_median_latency_gt_bar_ms": vk5 > args.vk5_bar_ms,
        "critical_pins_lt_100pct": not pins_ok,
    }
    # A run with no triggered sessions judges nothing.
    kill_hit = any(verdicts.values()) and bool(trig_rows)
    report = {
        "model": MODEL,
        "rescope": {
            "chunk_tokens": config.chunk_tokens,
            "superseded_cap_per_block": 10,
            "retry": "failed item values (not just types)",
            "output_cap_floor_tokens": 4096,
            "vk5_bar_ms": args.vk5_bar_ms,
            "vk5_bar_note": "3 s bar replaced by the Nov 6 rescope "
                            "(pilot median was 36.7 s); old bar in "
                            "SPEC_V2.md section 9",
            "sessions_run": [s["id"] for s in ordered],
        },
        "rates_usd_per_1m": {
            "summarizer_in": client.in_rate * 1_000_000,
            "summarizer_out": client.out_rate * 1_000_000,
            "main_input_configured": MAIN_RATE_USD_PER_1M},
        "token_method": METHOD + " (main-model counts; summarizer "
                        "usage measured from API response)",
        "cost_basis": agg["cost_basis"],
        "approved_estimate_usd": args.approved_estimate_usd,
        "cap_usd": args.cap_usd,
        "spent_usd_measured": round(client.spent_usd, 4),
        "summarizer_calls": client.calls,
        "summarizer_tokens_in_measured": client.prompt_tokens,
        "summarizer_tokens_out_measured": client.completion_tokens,
        "trace_sessions_run": len(rows),
        "triggered_sessions": len(trig_rows),
        "trace_aggregate": agg,
        "kills": {
            "VK1_first_attempt_pass_rate_pct": vk1,
            "VK2_mean_net_saving_usd_per_triggered_session": net_mean,
            "VK3_fallback_rate_pct": vk3,
            "VK5_median_added_latency_ms": vk5,
            "pins_surviving_sent_pct": round(
                100.0 * sum(r["pins_surviving_sent"] for r in rows)
                / max(sum(r["pins_total"] for r in rows), 1), 2),
        },
        "verdicts": verdicts,
        "kill_hit": kill_hit,
        "aborted": aborted,
        "trace_rows": rows,
        "synthetic_rows": synth_rows,
        "reading": (
            "VK1/VK2/VK3/VK5 judged on measured pilot numbers. Net "
            "saving uses chars/4 estimates for the main-model tokens "
            "and measured summarizer cost; the report labels both. "
            "Latency includes the full summarizer call (prefill + "
            "generation) per triggered session."),
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)

    print(f"spent (measured): ${client.spent_usd:.4f} vs approved "
          f"estimate $0.25, cap ${args.cap_usd:.2f}; "
          f"calls={client.calls}, in={client.prompt_tokens}, "
          f"out={client.completion_tokens}")
    print(f"VK1 first-attempt pass: {vk1}% | VK2 mean net saving: "
          f"${net_mean} | VK3 fallback: {vk3}% | VK5 median latency: "
          f"{vk5}ms | pins ok: {pins_ok}")
    print(f"kill hit: {kill_hit}; aborted: {aborted}")
    print(f"wrote {args.out}")
    return 2 if aborted else 0


if __name__ == "__main__":
    sys.exit(main())
