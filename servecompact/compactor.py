"""Deterministic v1 compactor. No model calls, no summarising.

Rules, in order, applied only to non-protected messages (see pins.py
and DESIGN.md §3):
  1. dedup of exact duplicates,
  2. supersession of older tool results (same tool_call_id, or same
     tool name + key argument for Read/Bash-shaped tools, or an
     explicit [SUPERSEDED] marker),
  3. narrow correction line removal,
  4. structural compression of verbose tool outputs.
Dropped content is replaced by a short [servecompact: ...] stub so the
result stays inspectable and tool-message slots stay valid.
"""

import copy
import json
import re

from .pins import extract_pins

VERBOSE_LINES = 100
VERBOSE_CHARS = 4000
KEEP_FIRST_LINES = 20
KEEP_LAST_LINES = 10

_ERROR_RE = re.compile(
    r"error|exception|traceback|fail(?:ed|ure)?|panic|fatal", re.I)
_SIG_RE = re.compile(
    r"^\s*(def |class |function |func |export\s+(function|const|class)\s)"
    r"|Signature:", re.M)
_CORRECTION_RE = re.compile(
    r"Correction:\s*([A-Za-z0-9_][A-Za-z0-9_ .-]{0,60}?)\s+is now\s+(.+)"
    r"|Corrected\s+([A-Za-z0-9_][A-Za-z0-9_ .-]{0,60}?)\s*:\s*(.+)",
    re.I)

STUB_DUP = "[servecompact: duplicate message removed]"
STUB_SUPERSEDED = ("[servecompact: superseded tool result removed; "
                    "later result for the same call kept]")
STUB_MARKED = "[servecompact: message marked superseded, removed]"


def _text_of(msg):
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                out.append(part.get("text") or "")
            elif isinstance(part, str):
                out.append(part)
        return "\n".join(out)
    return ""


def _set_text(msg, text):
    """Replace content with a plain string, preserving everything else."""
    msg["content"] = text


def _replace_if_shorter(msg, stub):
    """Replace content with a stub only if the stub is shorter.

    Invariant found during validation: on tiny messages a stub can be
    longer than the content it replaces, so naive stubbing ADDS tokens
    (one trace session went -0.15%, one synthetic session -20.47%).
    Compaction must be monotonically non-increasing per message, so a
    replacement that would grow the message is skipped and not counted.
    Returns True if replaced.
    """
    original = _text_of(msg)
    if len(stub) < len(original):
        _set_text(msg, stub)
        return True
    return False


def _tool_call_map(messages):
    """tool_call_id -> (tool name, arguments dict-or-None), in order."""
    mapping = {}
    for msg in messages:
        for call in msg.get("tool_calls") or []:
            cid = call.get("id")
            fn = call.get("function") or {}
            args = fn.get("arguments")
            parsed = None
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                except (ValueError, TypeError):
                    parsed = None
            elif isinstance(args, dict):
                parsed = args
            if cid:
                mapping[cid] = (fn.get("name"), parsed)
    return mapping


def _supersede_key(tool_name, args):
    """Key identifying 'the same read' for supersession, or None.

    Read-like tools key on the file argument; Bash-like tools key on
    the command. Anything else returns None (no supersession by key).
    """
    if not isinstance(args, dict):
        return None
    if tool_name in ("Read", "read", "read_file"):
        for key in ("file_path", "path", "file"):
            if args.get(key):
                return ("read", str(args[key]))
        return None
    if tool_name in ("Bash", "bash", "shell"):
        if args.get("command"):
            return ("bash", str(args["command"]))
        return None
    return None


def _compress_tool_text(text):
    """Structurally compress one verbose tool output. Returns new text."""
    lines = text.split("\n")
    if len(lines) <= VERBOSE_LINES and len(text) <= VERBOSE_CHARS:
        return text
    # Collapse runs of >3 identical consecutive lines to one.
    collapsed = []
    run = 0
    prev = None
    for line in lines:
        if line == prev:
            run += 1
            if run > 3:
                continue
        else:
            run = 1
        collapsed.append(line)
        prev = line
    lines = collapsed
    if len(lines) <= VERBOSE_LINES and len(text) <= VERBOSE_CHARS:
        return "\n".join(lines)
    keep = set(range(min(KEEP_FIRST_LINES, len(lines))))
    keep |= set(range(max(0, len(lines) - KEEP_LAST_LINES), len(lines)))
    for i, line in enumerate(lines):
        if _ERROR_RE.search(line) or _SIG_RE.match(line):
            keep.add(i)
    removed = len(lines) - len(keep)
    if removed <= 0:
        return "\n".join(lines)
    note = (f"[servecompact: compressed {removed} lines removed from "
            f"this tool result; headers, signatures, and error lines kept]")
    out = []
    note_done = False
    last = -1
    for i in sorted(keep):
        if i > last + 1 and out and not note_done:
            out.append(note)
            note_done = True
        out.append(lines[i])
        last = i
    if not note_done:
        out.insert(min(KEEP_FIRST_LINES, len(out)), note)
    return "\n".join(out)


def compact(messages):
    """Return (compacted_messages, stats dict). Never mutates input."""
    msgs = copy.deepcopy(messages)
    _, protected = extract_pins(msgs)
    stats = {"deduped": 0, "superseded": 0, "correction_lines": 0,
             "compressed_messages": 0, "lines_removed": 0}

    # --- 3. corrections first (a later message defines the new value;
    #     it supersedes only messages that came BEFORE it) ---
    corrections = []  # (source_index, field, new_value)
    for i, msg in enumerate(msgs):
        if i in protected:
            continue
        text = _text_of(msg)
        for m in _CORRECTION_RE.finditer(text):
            field = (m.group(1) or m.group(3) or "").strip()
            value = (m.group(2) or m.group(4) or "").strip()
            if field and value:
                corrections.append((i, field, value))
    if corrections:
        for i, msg in enumerate(msgs):
            if i in protected or msg.get("role") == "system":
                continue
            text = _text_of(msg)
            if not text:
                continue
            applicable = [(f, v) for (src, f, v) in corrections if src > i]
            if not applicable:
                continue
            lines = text.split("\n")
            new_lines = []
            changed = False
            for line in lines:
                dropped = False
                for field, value in applicable:
                    m = re.match(r"\s*" + re.escape(field) + r"\s*:\s*(.+?)\s*$",
                                 line)
                    if m and m.group(1).strip() != value:
                        dropped = True
                        break
                if dropped:
                    changed = True
                    stats["correction_lines"] += 1
                else:
                    new_lines.append(line)
            if changed:
                new_text = "\n".join(new_lines).strip()
                _set_text(msg, new_text if new_text else
                          "[servecompact: superseded line removed]")

    # --- 1. dedup (exact duplicates, first kept) ---
    seen = {}
    for i, msg in enumerate(msgs):
        if i in protected or msg.get("tool_calls"):
            # Messages carrying tool calls are identified by the call,
            # not their (often empty) text; deduping them could break
            # the call/result pairing. Supersession handles those.
            continue
        key = (msg.get("role"), _text_of(msg), msg.get("tool_call_id"),
               json.dumps(msg.get("tool_calls"), sort_keys=True,
                          default=str))
        if key in seen:
            if _replace_if_shorter(msg, STUB_DUP):
                stats["deduped"] += 1
        else:
            seen[key] = i

    # --- 2. supersession (later result wins) ---
    call_map = _tool_call_map(msgs)
    # For each tool message, find the latest index sharing its id or key.
    tool_indices = [i for i, m in enumerate(msgs)
                    if m.get("role") == "tool"]
    by_id = {}
    by_key = {}
    for i in tool_indices:
        cid = msgs[i].get("tool_call_id")
        if cid:
            by_id.setdefault(cid, []).append(i)
        name, args = call_map.get(cid, (None, None)) if cid else (None, None)
        key = _supersede_key(name, args) if name else None
        if key:
            by_key.setdefault(key, []).append(i)
    superseded = set()
    for group in list(by_id.values()) + list(by_key.values()):
        if len(group) > 1:
            for i in group[:-1]:
                superseded.add(i)
    for i, msg in enumerate(msgs):
        if i in protected:
            continue
        if "[SUPERSEDED]" in _text_of(msg):
            if _replace_if_shorter(msg, STUB_MARKED):
                stats["superseded"] += 1
        elif i in superseded and msg.get("role") == "tool":
            if _replace_if_shorter(msg, STUB_SUPERSEDED):
                stats["superseded"] += 1

    # --- 4. structural compression of verbose tool outputs ---
    for i, msg in enumerate(msgs):
        if i in protected or msg.get("role") != "tool":
            continue
        text = _text_of(msg)
        if len(text.split("\n")) > VERBOSE_LINES or len(text) > VERBOSE_CHARS:
            before = len(text.split("\n"))
            new_text = _compress_tool_text(text)
            after = len(new_text.split("\n"))
            if new_text != text and len(new_text) < len(text):
                _set_text(msg, new_text)
                stats["compressed_messages"] += 1
                stats["lines_removed"] += max(0, before - after)
    return msgs, stats
