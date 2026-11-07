"""Fail-closed verifier.

Checks that every pinned span appears verbatim in the compacted
prompt. A miss means: send the original, log the fallback. Logs carry
pin type + hash, never pin text.
"""

from .pins import extract_pins
from .tokens import message_text


def verify(original_messages, compacted_messages):
    """Return a report dict. `ok` is False iff any pin is missing."""
    pins, _ = extract_pins(original_messages)
    compacted_text = "\n".join(message_text(m) for m in compacted_messages)
    missing = []
    surviving = 0
    for pin in pins:
        if pin.text in compacted_text:
            surviving += 1
        else:
            missing.append(pin.loggable())
    return {
        "pins_total": len(pins),
        "pins_surviving_compacted": surviving,
        "pins_missing": missing,
        "ok": not missing,
    }


def verify_v2(original_messages, final_messages, checklist_items,
              summary_texts=None, tokens_v1=None, tokens_final=None):
    """v2 verification: v1 pins + checklist + monotonicity.

    Presence items are checked against the whole final prompt;
    absence items against the summary texts only (a superseded value
    may legitimately appear in a protected message). tokens_v1 /
    tokens_final are the token counts of the v1-compacted and final
    prompts (same counter for both). Monotonicity fails when the final
    prompt is not shorter than the v1 prompt.
    Returns {ok, pins_ok, checklist, monotonicity_ok}.
    """
    from .checklist import verify_checklist

    pins_report = verify(original_messages, final_messages)
    final_text = "\n".join(message_text(m) for m in final_messages)
    summary_scope = "\n".join(summary_texts or [final_text])
    checklist_report = verify_checklist(
        checklist_items, final_text, absent_scope=summary_scope)
    monotonicity_ok = True
    if tokens_v1 is not None and tokens_final is not None:
        monotonicity_ok = tokens_final < tokens_v1
    return {
        "ok": pins_report["ok"] and checklist_report["ok"]
        and monotonicity_ok,
        "pins_ok": pins_report["ok"],
        "pins_missing": pins_report["pins_missing"],
        "checklist": {
            "ok": checklist_report["ok"],
            "passed": checklist_report["passed"],
            "failed_loggable": checklist_report["failed_loggable"],
        },
        "monotonicity_ok": monotonicity_ok,
    }
