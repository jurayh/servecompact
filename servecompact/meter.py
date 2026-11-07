"""Meter: per-request and aggregate accounting.

Honesty rule, enforced by construction: every token or dollar figure
this module emits travels with its method label. Token counts are
estimates (see tokens.METHOD); cost is computed at a configured rate
(see rates.json), never presented as a live provider quote.

The meter stores counts, hashes, and timings only. No prompt text, no
pin text, no keys ever pass through here.
"""

import json
import os
import threading
import time

from .tokens import LABEL, METHOD

DEFAULT_INPUT_RATE_USD_PER_1M = 3.00


def load_rate(path=None):
    """Configured input rate. Falls back to the default with a note.

    Looks for rates.json next to the working directory first, then
    next to the package (the repo root), so scripts work from any cwd.
    """
    candidates = []
    if path:
        candidates.append(path)
    candidates.append("rates.json")
    candidates.append(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "rates.json"))
    for candidate in candidates:
        try:
            with open(candidate, encoding="utf-8") as fh:
                data = json.load(fh)
            rate = float(data.get("input_usd_per_1m",
                                  DEFAULT_INPUT_RATE_USD_PER_1M))
            return rate, data.get("profile", "rates.json")
        except (OSError, ValueError, TypeError):
            continue
    return DEFAULT_INPUT_RATE_USD_PER_1M, "default (rates.json missing)"


class Meter:
    def __init__(self, input_rate_usd_per_1m=None, rate_profile=None):
        if input_rate_usd_per_1m is None:
            input_rate_usd_per_1m, rate_profile = load_rate()
        self.input_rate = input_rate_usd_per_1m
        self.rate_profile = rate_profile or "default"
        self._lock = threading.Lock()
        self.requests = []
        self._totals = {"requests": 0, "fallbacks": 0,
                        "tokens_original": 0, "tokens_compacted": 0,
                        "tokens_saved": 0, "pins_total": 0,
                        "pins_surviving_compacted": 0,
                        "pins_surviving_sent": 0,
                        "v2_requests": 0, "summarizer_calls": 0,
                        "summarizer_tokens_in": 0,
                        "summarizer_tokens_out": 0,
                        "summarizer_cost_usd": 0.0,
                        "net_saving_usd": 0.0,
                        "blocks_summarized": 0, "blocks_fallback_v1": 0,
                        "checklist_items_total": 0,
                        "checklist_passed_first_attempt": 0,
                        "checklist_retries": 0}

    def record(self, report, upstream_ms=None):
        """Record one pipeline report. Returns the loggable record."""
        with self._lock:
            self._totals["requests"] += 1
            self._totals["fallbacks"] += 1 if report["fallback"] else 0
            self._totals["tokens_original"] += report["tokens_original"]
            self._totals["tokens_compacted"] += report["tokens_compacted"]
            self._totals["tokens_saved"] += report["tokens_saved"]
            self._totals["pins_total"] += report["pins_total"]
            self._totals["pins_surviving_compacted"] += \
                report["pins_surviving_compacted"]
            self._totals["pins_surviving_sent"] += \
                report["pins_surviving_sent"]
            record = {
                "ts": time.time(),
                "tokens_original": report["tokens_original"],
                "tokens_compacted": report["tokens_compacted"],
                "tokens_saved": report["tokens_saved"],
                "reduction_pct": report["reduction_pct"],
                "fallback": report["fallback"],
                "pins_total": report["pins_total"],
                "pins_surviving_compacted":
                    report["pins_surviving_compacted"],
                "pins_surviving_sent": report["pins_surviving_sent"],
                "pins_missing": report["pins_missing"],
                "compaction_ms": report["compaction_ms"],
                "upstream_ms": upstream_ms,
                "token_method": METHOD,
            }
            self.requests.append(record)
            return record

    def record_v2(self, v2_report, upstream_ms=None):
        """Record a v2 pipeline report.

        The nested v1 report goes through record() (v1 figures stay
        the canonical accounting); v2 fields accumulate separately.
        Returns the loggable v2 record.
        """
        record = self.record(v2_report["v1_report"], upstream_ms)
        with self._lock:
            t = self._totals
            t["v2_requests"] += 1
            t["summarizer_calls"] += v2_report["summarizer_calls"]
            t["summarizer_tokens_in"] += v2_report["summarizer_tokens_in"]
            t["summarizer_tokens_out"] += v2_report["summarizer_tokens_out"]
            t["summarizer_cost_usd"] += v2_report["summarizer_cost_usd"]
            t["net_saving_usd"] += v2_report["net_saving_usd"]
            t["blocks_summarized"] += v2_report["blocks_summarized"]
            t["blocks_fallback_v1"] += v2_report["blocks_fallback_v1"]
            t["checklist_items_total"] += \
                v2_report["checklist_items_total"]
            t["checklist_passed_first_attempt"] += \
                v2_report["checklist_passed_first_attempt"]
            t["checklist_retries"] += v2_report["checklist_retries"]
        record["v2"] = {
            "blocks_summarized": v2_report["blocks_summarized"],
            "blocks_fallback_v1": v2_report["blocks_fallback_v1"],
            "checklist_items_total": v2_report["checklist_items_total"],
            "summarizer_calls": v2_report["summarizer_calls"],
            "summarizer_tokens_in": v2_report["summarizer_tokens_in"],
            "summarizer_tokens_out": v2_report["summarizer_tokens_out"],
            "summarizer_cost_usd": v2_report["summarizer_cost_usd"],
            "net_saving_usd": v2_report["net_saving_usd"],
            "fallback_tier": v2_report["fallback_tier"],
            "token_method": v2_report["token_method"],
        }
        return record

    def aggregate(self):
        """Aggregate figures. Every figure carries its label."""
        with self._lock:
            t = dict(self._totals)
        saved = t["tokens_saved"]
        cost_saved = saved * self.input_rate / 1_000_000.0
        weighted = (100.0 * saved / t["tokens_original"]
                    if t["tokens_original"] else 0.0)
        fallback_rate = (100.0 * t["fallbacks"] / t["requests"]
                         if t["requests"] else 0.0)
        return {
            "requests": t["requests"],
            "fallbacks": t["fallbacks"],
            "fallback_rate_pct": round(fallback_rate, 2),
            "tokens_original": t["tokens_original"],
            "tokens_compacted": t["tokens_compacted"],
            "tokens_saved": saved,
            "token_weighted_reduction_pct": round(weighted, 2),
            "pins_total": t["pins_total"],
            "pins_surviving_compacted": t["pins_surviving_compacted"],
            "pins_surviving_sent": t["pins_surviving_sent"],
            "cost_saved_usd": round(cost_saved, 4),
            "token_method": METHOD,
            "token_label": LABEL,
            "cost_basis": (f"configured rate ${self.input_rate:.2f} per 1M "
                           f"input tokens (profile: {self.rate_profile}); "
                           f"not a live provider quote"),
            "v2": {
                "v2_requests": t["v2_requests"],
                "summarizer_calls": t["summarizer_calls"],
                "summarizer_tokens_in": t["summarizer_tokens_in"],
                "summarizer_tokens_out": t["summarizer_tokens_out"],
                "summarizer_cost_usd": round(t["summarizer_cost_usd"], 4),
                "net_saving_usd": round(t["net_saving_usd"], 4),
                "blocks_summarized": t["blocks_summarized"],
                "blocks_fallback_v1": t["blocks_fallback_v1"],
                "checklist_items_total": t["checklist_items_total"],
                "checklist_passed_first_attempt":
                    t["checklist_passed_first_attempt"],
                "checklist_first_attempt_pass_rate_pct":
                    round(100.0 * t["checklist_passed_first_attempt"]
                          / t["checklist_items_total"], 2)
                    if t["checklist_items_total"] else 0.0,
                "checklist_retries": t["checklist_retries"],
            },
        }

    def label_v2(self, agg):
        """Human rendering of the v2 aggregate. Net saving is per
        single-send at the configured main rate minus measured
        summarizer cost; with a stub summarizer the cost is $0 and the
        figure is a ceiling, not a measurement."""
        v2 = agg["v2"]
        return (
            f"v2 requests: {v2['v2_requests']} | blocks summarized: "
            f"{v2['blocks_summarized']} | blocks fell back to v1: "
            f"{v2['blocks_fallback_v1']} | checklist items: "
            f"{v2['checklist_items_total']} | first-attempt pass rate: "
            f"{v2['checklist_first_attempt_pass_rate_pct']}% | retries: "
            f"{v2['checklist_retries']} | summarizer calls: "
            f"{v2['summarizer_calls']} | summarizer tokens in/out: "
            f"{v2['summarizer_tokens_in']}/{v2['summarizer_tokens_out']} "
            f"[measured from API usage] | summarizer cost: "
            f"${v2['summarizer_cost_usd']:.4f} | net saving: "
            f"${v2['net_saving_usd']:.4f} [per single-send at configured "
            f"main rate; stub cost is $0, so this is a ceiling]"
        )

    def label_figures(self, agg):
        """One-block human rendering where no figure stands alone."""
        return (
            f"tokens in (original): {agg['tokens_original']} [{LABEL}: "
            f"{METHOD}] | tokens out (compacted): "
            f"{agg['tokens_compacted']} [{LABEL}] | tokens saved: "
            f"{agg['tokens_saved']} [{LABEL}] | reduction "
            f"(token-weighted): {agg['token_weighted_reduction_pct']}% "
            f"[{LABEL}] | fallback rate: {agg['fallback_rate_pct']}% | "
            f"cost saved: ${agg['cost_saved_usd']:.4f} "
            f"[{agg['cost_basis']}] | pins surviving in sent prompts: "
            f"{agg['pins_surviving_sent']}/{agg['pins_total']}"
        )
