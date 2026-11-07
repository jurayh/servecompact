"""One request through compaction + verification.

compact_and_verify(messages, compactor=None) returns
(messages_to_send, compacted_messages, report) where report carries
fallback status, pin survival at both points, and compactor stats.
The `compactor` parameter exists only as a test seam (dependency
injection); production always uses the real compactor.

compact_and_verify_v2(messages, summarizer=None, config=None) adds
verified summarization (SPEC_V2): the v1-compacted middle is
summarized per contiguous block, each summary must pass a
deterministic checklist, and the tiered fallback is summary -> v1
output -> original. summarizer=None means v1 only (no model call).
"""

import threading
import time
from dataclasses import dataclass, field

from .checklist import extract_checklist, verify_checklist
from .compactor import compact
from .pins import extract_pins
from .summarizer import ParseError, SummarizerError, expected_sections, parse_summary
from .tokens import Counter, message_text, messages_tokens
from .verifier import verify, verify_v2


@dataclass
class V2Config:
    trigger_tokens: int = 2000
    # Rescope (Nov 6, 2026): a middle block larger than this is
    # split into chunks of at most this many est tokens, each with
    # its own checklist and summary. The pilot's 153k-token middle
    # produced a 193-item checklist no summary could satisfy.
    chunk_tokens: int = 30000
    summarizer_timeout_s: float = 10.0
    max_retries: int = 1
    main_input_rate_usd_per_1m: float = 3.0
    summarizer_in_rate_usd_per_1m: float = 0.10
    summarizer_out_rate_usd_per_1m: float = 0.20
    counter: Counter = field(default_factory=Counter.default)


def _run_with_timeout(fn, timeout_s):
    """Run fn() in a daemon thread with a timeout.

    Returns (ok, result, elapsed_ms). On timeout the thread keeps
    running as a daemon (it cannot block interpreter exit) and the
    caller treats the attempt as failed. Daemon threads are deliberate
    here: a stuck summarizer must never hang the proxy or the tests.
    """
    box = {}
    started = time.monotonic()

    def _target():
        try:
            box["result"] = fn()
        except Exception as exc:  # noqa: BLE001 - reported to caller
            box["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout_s)
    elapsed_ms = round((time.monotonic() - started) * 1000.0, 2)
    if thread.is_alive():
        return False, TimeoutError(f"timed out after {timeout_s}s"), elapsed_ms
    if "error" in box:
        return False, box["error"], elapsed_ms
    return True, box.get("result"), elapsed_ms


def compact_and_verify(messages, compactor=None):
    started = time.monotonic()
    pins, _ = extract_pins(messages)
    compactor = compactor or compact
    compacted, stats = compactor(messages)
    check = verify(messages, compacted)
    fallback = not check["ok"]
    to_send = messages if fallback else compacted
    sent_text_pins = len(pins) if fallback else check["pins_surviving_compacted"]
    # On fallback the sent prompt is the original, so every pin
    # survives in the sent prompt by construction; verify that claim
    # rather than asserting it.
    if fallback:
        sent_check = verify(messages, messages)
        sent_text_pins = sent_check["pins_surviving_compacted"]
    elapsed_ms = (time.monotonic() - started) * 1000.0
    report = {
        "fallback": fallback,
        "pins_total": len(pins),
        "pins_surviving_compacted": check["pins_surviving_compacted"],
        "pins_surviving_sent": sent_text_pins,
        "pins_missing": check["pins_missing"],
        "compactor_stats": stats,
        "tokens_original": messages_tokens(messages),
        "tokens_compacted": messages_tokens(compacted),
        "tokens_sent": messages_tokens(to_send),
        "compaction_ms": round(elapsed_ms, 2),
    }
    report["tokens_saved"] = (report["tokens_original"]
                              - report["tokens_compacted"])
    if report["tokens_original"]:
        report["reduction_pct"] = round(
            100.0 * report["tokens_saved"] / report["tokens_original"], 2)
    else:
        report["reduction_pct"] = 0.0
    return to_send, compacted, report


def _contiguous_blocks(indices):
    blocks = []
    for i in indices:
        if blocks and i == blocks[-1][-1] + 1:
            blocks[-1].append(i)
        else:
            blocks.append([i])
    return blocks


def _chunk_block(block, messages, counter, chunk_tokens):
    """Split one contiguous block into chunks of at most
    chunk_tokens est tokens, by message boundary. A single message
    over the limit forms its own chunk (never split mid-message)."""
    chunks = []
    chunk, tokens = [], 0
    for i in block:
        t = messages_tokens([messages[i]], counter)
        if chunk and tokens + t > chunk_tokens:
            chunks.append(chunk)
            chunk, tokens = [], 0
        chunk.append(i)
        tokens += t
    if chunk:
        chunks.append(chunk)
    return chunks


def middle_blocks(compacted_messages, counter, chunk_tokens=30000):
    """The middle of a v1-compacted prompt, as summarization blocks.

    Blocks are computed on the COMPACTED list, not the original:
    v1 dedup can remove messages, so original indices do not align
    with compacted indices. Pins in the compacted list still mark
    the same protected messages (v1 never drops them). Each
    contiguous unprotected run before the last user message is a
    block, split into chunks of at most chunk_tokens est tokens
    (rescope, Nov 6, 2026). Returns a list of index lists.
    """
    _, protected = extract_pins(compacted_messages)
    last_user = max((i for i, m in enumerate(compacted_messages)
                     if m.get("role") == "user"), default=-1)
    middle = [i for i in range(last_user) if i not in protected] \
        if last_user >= 0 else []
    blocks = []
    for block in _contiguous_blocks(middle):
        blocks.extend(_chunk_block(block, compacted_messages, counter,
                                   chunk_tokens))
    return blocks


def compact_and_verify_v2(messages, summarizer=None, config=None):
    """v2 pipeline: v1 + verified summarization per middle block.

    Returns (messages_to_send, final_messages, report). final_messages
    is the v2 attempt (for diagnosis); messages_to_send is what goes
    out after the tiered fallback. With summarizer=None this is v1
    only and makes no model call.
    """
    config = config or V2Config()
    counter = config.counter or Counter.default()
    to_send_v1, compacted_v1, v1_report = compact_and_verify(messages)
    tokens_v1 = messages_tokens(compacted_v1, counter)
    tokens_original = messages_tokens(messages, counter)

    v2 = {
        "v1_report": v1_report,
        "token_method": counter.method,
        "tokens_original": tokens_original,
        "tokens_v1": tokens_v1,
        "summarizer_configured": summarizer is not None,
        "blocks_total": 0,
        "blocks_triggered": 0,
        "blocks_below_trigger": 0,
        "blocks_summarized": 0,
        "blocks_fallback_v1": 0,
        "checklist_items_total": 0,
        "checklist_passed_first_attempt": 0,
        "checklist_retries": 0,
        "summarizer_calls": 0,
        "summarizer_calls_failed": 0,
        "summarizer_tokens_in": 0,
        "summarizer_tokens_out": 0,
        "summarizer_usage_method": "none",
        "summarizer_cost_usd": 0.0,
        "summarizer_latency_ms": 0.0,
        # Why attempts failed, so the next run is diagnosable without
        # re-running paid calls: timeout | summarizer_error | parse |
        # checklist. Failed checklist attempts also record the failed
        # item types (counts only, never values).
        "attempt_failures": {},
        "failed_types": {},
        "fallback_tier": "none",
    }
    if summarizer is None or v1_report["fallback"]:
        if v1_report["fallback"]:
            v2["fallback_tier"] = "original"
        final = compacted_v1
        v2["tokens_final"] = messages_tokens(final, counter)
        v2["tokens_saved_v2"] = tokens_v1 - v2["tokens_final"]
        v2["net_saving_usd"] = 0.0
        return to_send_v1, final, v2

    blocks = middle_blocks(compacted_v1, counter, config.chunk_tokens)
    v2["blocks_total"] = len(blocks)

    replacement = {}  # block start index -> replacement message or None
    all_items = []
    summary_texts = []
    for block in blocks:
        block_msgs = [compacted_v1[i] for i in block]
        block_tokens = messages_tokens(block_msgs, counter)
        if block_tokens <= config.trigger_tokens:
            v2["blocks_below_trigger"] += 1
            replacement[block[0]] = None
            continue
        v2["blocks_triggered"] += 1
        items, _ = extract_checklist(block_msgs)
        all_items.extend(items)
        v2["checklist_items_total"] += len(items)
        block_text = "\n".join(message_text(m) for m in block_msgs)
        accepted = None
        # Rescope (Nov 6, 2026): the retry gets the failed ITEMS
        # (values included), not just their types. The pilot's retry
        # named only types, so the model could not fix its misses;
        # §5 already allows values in the retry prompt (a summarizer
        # input, never a log). failed_types stays for the report.
        failed_items = None
        failed_types = None

        def _tally_cost():
            v2["summarizer_cost_usd"] = round(
                (v2["summarizer_tokens_in"]
                 * config.summarizer_in_rate_usd_per_1m
                 + v2["summarizer_tokens_out"]
                 * config.summarizer_out_rate_usd_per_1m) / 1_000_000.0, 4)

        def _record_failure(reason):
            v2["attempt_failures"][reason] = \
                v2["attempt_failures"].get(reason, 0) + 1

        for attempt in range(1 + config.max_retries):
            if attempt > 0:
                v2["checklist_retries"] += 1
            ok, result, elapsed_ms = _run_with_timeout(
                lambda: summarizer.summarize(
                    block_text, items, failed=failed_items),
                config.summarizer_timeout_s)
            v2["summarizer_latency_ms"] = round(
                v2["summarizer_latency_ms"] + elapsed_ms, 2)
            if not ok:
                # A failed attempt can still have been made and
                # billed (e.g. the output budget went to reasoning).
                # Attribute its usage instead of silently dropping it:
                # the pilot's smoke session lost exactly this way.
                v2["summarizer_calls_failed"] += 1
                err = result
                reason = ("timeout" if isinstance(err, TimeoutError)
                          else "summarizer_error")
                _record_failure(reason)
                v2["summarizer_tokens_in"] += getattr(
                    err, "usage_in", 0)
                v2["summarizer_tokens_out"] += getattr(
                    err, "usage_out", 0)
                _tally_cost()
                failed_items = None
                failed_types = ["summarizer_error"]
                continue
            v2["summarizer_calls"] += 1
            v2["summarizer_tokens_in"] += result.usage_in
            v2["summarizer_tokens_out"] += result.usage_out
            v2["summarizer_usage_method"] = result.usage_method
            _tally_cost()
            try:
                parse_summary(result.text, expected_sections(items))
            except ParseError:
                _record_failure("parse")
                failed_items = None
                failed_types = ["parse"]
                continue
            check = verify_checklist(items, result.text)
            if check["ok"]:
                if attempt == 0:
                    v2["checklist_passed_first_attempt"] += len(items)
                accepted = result.text
                break
            _record_failure("checklist")
            failed_items = check["failed"]
            failed_types = sorted({it.type for it in failed_items})
            for t in failed_types:
                v2["failed_types"][t] = v2["failed_types"].get(t, 0) + 1
        if accepted is None:
            v2["blocks_fallback_v1"] += 1
            v2["fallback_tier"] = "v1"
            replacement[block[0]] = None
            continue
        summary_msg = {
            "role": "assistant",
            "content": (f"[servecompact summary of {len(block)} messages]\n"
                        f"{accepted}"),
        }
        if counter.count(summary_msg["content"]) < block_tokens:
            v2["blocks_summarized"] += 1
            summary_texts.append(accepted)
            replacement[block[0]] = summary_msg
        else:
            v2["blocks_fallback_v1"] += 1
            v2["fallback_tier"] = "v1"
            replacement[block[0]] = None

    # Assemble in original order: a summarized block contributes its
    # single summary message; every other message passes through.
    block_of = {}
    for block in blocks:
        for idx in block:
            block_of[idx] = block
    final = []
    i = 0
    while i < len(compacted_v1):
        block = block_of.get(i)
        if block is not None and i == block[0] and replacement[i] is not None:
            final.append(replacement[i])
            i = block[-1] + 1
        else:
            final.append(compacted_v1[i])
            i += 1

    # v1 pins must still survive in the final prompt; if not, original.
    check = verify(messages, final)
    to_send = final
    if not check["ok"]:
        to_send = messages
        v2["fallback_tier"] = "original"
    elif v2["blocks_summarized"] > 0:
        # Independent whole-prompt re-check of everything v2 produced.
        # Any failure falls back to the v1 output (final stays the v2
        # attempt for diagnosis).
        final_check = verify_v2(messages, final, all_items, summary_texts,
                                tokens_v1,
                                messages_tokens(final, counter))
        v2["final_verification"] = {
            "ok": final_check["ok"],
            "pins_ok": final_check["pins_ok"],
            "checklist_ok": final_check["checklist"]["ok"],
            "monotonicity_ok": final_check["monotonicity_ok"],
            "checklist_failed": final_check["checklist"]["failed_loggable"],
        }
        if not final_check["ok"]:
            to_send = compacted_v1
            v2["fallback_tier"] = "v1"
    tokens_final = messages_tokens(final, counter)
    v2["tokens_final"] = tokens_final
    v2["tokens_saved_v2"] = tokens_v1 - tokens_final
    v2["reduction_pct_v2"] = round(
        100.0 * v2["tokens_saved_v2"] / tokens_v1, 2) if tokens_v1 else 0.0
    v2["net_saving_usd"] = round(
        v2["tokens_saved_v2"] * config.main_input_rate_usd_per_1m
        / 1_000_000.0 - v2["summarizer_cost_usd"], 4)
    sent_check = verify(messages, to_send)
    v2["pins_surviving_sent"] = sent_check["pins_surviving_compacted"]
    v2["pins_total"] = sent_check["pins_total"]
    return to_send, final, v2
