"""Deterministic checklist extraction and checking (v2).

The checklist is what the summarizer must preserve. It is built
without any model, from the v1-compacted middle, and every item has a
mechanical check. Sources (SPEC_V2 §3):

- explicit pins (verbatim, checked by the v1 verifier);
- labelled lines: DECISION / GOAL / FACT / OWNER / REGION /
  SAFE ACTION (last value wins; earlier values become superseded
  items, checked for absence as current values);
- latest-value pairs: any `Key: value` line that repeats with
  different values (last wins, earlier ones superseded);
- error/signature lines that v1's structural compression kept inside
  the middle (verbatim).

What the checklist does NOT cover: arbitrary free-prose facts. That
gap is real and is measured in stage 1 (coverage stats), not hidden.

Rescope (Nov 6, 2026): superseded items are capped per block
(MAX_SUPERSEDED_ITEMS_PER_BLOCK). The stage-2 pilot showed the
checklist dominated by generic key-value churn — zero labelled
lines in all 24 middles, up to 143 superseded values in one
session — while even 5-item checklists failed. The checklist is
now selective, not exhaustive: per key only the most recent
superseded value is kept, then at most 10 overall by recency.
The dropped count is in stats; the spec says so.

Log hygiene: items carry a content hash for logs, never the value.
The retry prompt may name failed values (it is a summarizer input,
not a log); the meter log must use loggable() only.
"""

import hashlib
import re

from .compactor import _ERROR_RE, _SIG_RE

LABELLED = ("DECISION", "GOAL", "FACT", "OWNER", "REGION", "SAFE ACTION")
_LABEL_RE = re.compile(
    r"^\s*(DECISION|GOAL|FACT|OWNER|REGION|SAFE ACTION)\s*:\s*(.+?)\s*$",
    re.I)
_GENERIC_KV_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9_ .\-]{0,40})\s*:\s*(.+?)\s*$")
_SUPERSEDED_HEADING_RE = re.compile(r"^\s*#{0,3}\s*superseded\b", re.I)

MAX_ITEMS = 200
# Per-block cap on error/signature items. v1 keeps signature lines as
# orientation in compressed output, but a code-heavy block can hold
# dozens; forcing all of them verbatim into a summary would defeat
# summarization. Errors are prioritized over signatures within the cap.
MAX_ERROR_SIG_ITEMS_PER_BLOCK = 10
# Per-block cap on superseded items (rescope, Nov 6, 2026; see the
# module docstring). Per key only the most recent superseded value
# is kept — the one the model is most likely to confuse with the
# latest — then at most this many overall, by recency.
MAX_SUPERSEDED_ITEMS_PER_BLOCK = 10


def normalize(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


class ChecklistItem:
    def __init__(self, item_type, value, check, source_message_index,
                 context=None, group=None):
        self.type = item_type
        self.value = value
        self.check = check  # "verbatim" | "normalized" | "absent"
        self.source_message_index = source_message_index
        # For superseded items: the key whose value this replaced.
        # The rescope cap keeps the most recent superseded value per
        # group, so the check stays spread across keys.
        self.group = group
        # For superseded items: the latest value that replaced this
        # one. The absence check exempts lines that also carry the
        # latest value, so an old value that is a substring of the new
        # one (e.g. old path D:\CFML inside new D:\CFML\project) does
        # not fail merely by appearing inside its replacement.
        self.context = context

    def loggable(self):
        return {"type": self.type, "check": self.check,
                "sha256_12": _digest(self.value),
                "message_index": self.source_message_index,
                "chars": len(self.value)}


def strip_superseded_section(text):
    """Remove a trailing 'Superseded' section from summary text.

    Superseded values may legitimately appear inside that section, so
    absence checks run on the text with it removed.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if _SUPERSEDED_HEADING_RE.match(line):
            return "\n".join(lines[:i])
    return text


def _lines_of(messages):
    for idx, msg in enumerate(messages):
        content = msg.get("content")
        if isinstance(content, str):
            for line in content.split("\n"):
                yield idx, line


def extract_checklist(middle_messages):
    """Build the checklist for one summarized block.

    middle_messages are the v1-compacted messages of the block.
    Returns (items, stats). Items are capped at MAX_ITEMS; the cap
    firing is recorded in stats, not silently applied.
    """
    items = []
    stats = {"labelled": 0, "latest_value_pairs": 0,
             "superseded_values": 0, "superseded_capped": 0,
             "error_lines": 0,
             "error_lines_seen": 0, "error_lines_capped": False,
             "truncated": False}
    seen_values = {}  # normalized key -> list of (value, msg idx), in order

    for idx, line in _lines_of(middle_messages):
        if len(line) > 200 or not line.strip():
            continue
        m = _LABEL_RE.match(line)
        if m:
            key = m.group(1).strip().upper()
            seen_values.setdefault(key, []).append(
                (m.group(2).strip(), idx))
            continue
        g = _GENERIC_KV_RE.match(line)
        if g:
            key = g.group(1).strip()
            value = g.group(2).strip()
            if 1 <= len(value) <= 120 and len(key) >= 2:
                seen_values.setdefault(key.lower(), []).append(
                    (value, idx))

    for key, occurrences in seen_values.items():
        distinct = []
        for value, idx in occurrences:
            if normalize(value) not in [normalize(v) for v, _ in distinct]:
                distinct.append((value, idx))
        if len(distinct) == 1:
            value, idx = distinct[0]
            if key.upper() in LABELLED:
                items.append(ChecklistItem(
                    key.lower().replace(" ", "_"), value, "normalized", idx))
                stats["labelled"] += 1
            continue
        # Repeated key, different values: last wins.
        latest_value, latest_idx = distinct[-1]
        if key.upper() in LABELLED:
            items.append(ChecklistItem(
                key.lower().replace(" ", "_"), latest_value, "normalized",
                latest_idx))
            stats["labelled"] += 1
        else:
            items.append(ChecklistItem(
                "latest_value", f"{key}: {latest_value}", "normalized",
                latest_idx))
            stats["latest_value_pairs"] += 1
        for old_value, old_idx in distinct[:-1]:
            items.append(ChecklistItem(
                "superseded_value", old_value, "absent", old_idx,
                context=latest_value, group=key))
            stats["superseded_values"] += 1

    # Per-block cap on error/signature items (see
    # MAX_ERROR_SIG_ITEMS_PER_BLOCK). Priority: pure error lines
    # first, then signature lines, then lines matching both (often a
    # test def whose name contains "fail"). A code-heavy block can
    # hold dozens of such lines; forcing all of them verbatim into a
    # summary would defeat summarization.
    candidates = []  # (priority, idx, line) in first-seen order
    for idx, line in _lines_of(middle_messages):
        stripped = line.strip()
        if not stripped:
            continue
        is_error = bool(_ERROR_RE.search(line))
        is_sig = bool(_SIG_RE.match(line))
        if is_error or is_sig:
            stats["error_lines_seen"] += 1
            priority = 0 if (is_error and not is_sig) else \
                1 if is_sig and not is_error else 2
            candidates.append((priority, idx, stripped))
    chosen = sorted(candidates, key=lambda c: c[0])[
        :MAX_ERROR_SIG_ITEMS_PER_BLOCK]
    if len(chosen) < len(candidates):
        stats["error_lines_capped"] = True
    for _priority, idx, stripped in chosen:
        if not any(it.value == stripped and it.check == "verbatim"
                   for it in items):
            items.append(ChecklistItem(
                "error_or_signature", stripped, "verbatim", idx))
            stats["error_lines"] += 1

    # A superseded value that is a substring of some current item's
    # value cannot be checked for absence by substring: its presence
    # in the summary is explained by the current item (e.g. old path
    # D:\CFML inside a current error line's D:\CFML\utils.py). Such
    # items are dropped, not faked; the count is in stats.
    stats["superseded_skipped_substring"] = 0
    current_values = [normalize(it.value) for it in items
                      if it.check != "absent"]
    kept = []
    for it in items:
        if it.check == "absent" and any(
                normalize(it.value) in cv for cv in current_values):
            stats["superseded_skipped_substring"] += 1
            continue
        kept.append(it)
    items = kept

    # Rescope cap on superseded items (see MAX_SUPERSEDED_ITEMS_
    # PER_BLOCK): per group (key) keep only the most recent
    # superseded value, then at most the cap overall, by recency.
    superseded = [it for it in items if it.check == "absent"]
    if len(superseded) > MAX_SUPERSEDED_ITEMS_PER_BLOCK:
        by_group = {}
        for it in superseded:
            group = it.group if it.group is not None else it.value
            prev = by_group.get(group)
            if prev is None or (it.source_message_index
                                >= prev.source_message_index):
                by_group[group] = it
        kept_superseded = sorted(
            by_group.values(),
            key=lambda it: it.source_message_index,
            reverse=True)[:MAX_SUPERSEDED_ITEMS_PER_BLOCK]
        keep_ids = {id(it) for it in kept_superseded}
        stats["superseded_capped"] = sum(
            1 for it in superseded if id(it) not in keep_ids)
        items = [it for it in items
                 if it.check != "absent" or id(it) in keep_ids]

    if len(items) > MAX_ITEMS:
        items = items[:MAX_ITEMS]
        stats["truncated"] = True
    return items, stats


def check_item(item, final_text):
    """Mechanical check for one item against final prompt text."""
    if item.check == "verbatim":
        return item.value in final_text
    if item.check == "normalized":
        return normalize(item.value) in normalize(final_text)
    if item.check == "absent":
        # Line-scoped: the old value must not appear on any line
        # outside the Superseded section, except on a line that also
        # carries the latest value (the old value as a substring of
        # its replacement is not the summary presenting it as current).
        scoped = strip_superseded_section(final_text)
        nvalue = normalize(item.value)
        ncontext = normalize(item.context) if item.context else None
        for line in scoped.split("\n"):
            nline = normalize(line)
            if nvalue in nline:
                if ncontext and ncontext in nline:
                    continue
                return False
        return True
    raise ValueError(f"unknown check {item.check!r}")


def verify_checklist(items, final_text, absent_scope=None):
    """Check every item. Returns {ok, passed, failed (full items),
    failed_loggable} — failed values stay in memory only; logs use the
    loggable form.

    absent_scope is the text absence items are checked against. It
    defaults to final_text, but the whole-prompt check must use the
    summary text only: a superseded value may legitimately appear in
    a protected message (the user correcting themselves), which says
    nothing about what the summary claims is current.
    """
    scope = final_text if absent_scope is None else absent_scope
    failed = [it for it in items
              if not check_item(it, final_text if it.check != "absent"
                                else scope)]
    return {
        "ok": not failed,
        "passed": len(items) - len(failed),
        "failed": failed,
        "failed_loggable": [it.loggable() for it in failed],
    }
