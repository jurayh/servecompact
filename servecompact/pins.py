"""Pin extraction.

A pin is a span of text that must reach the model verbatim.
Sources (see DESIGN.md §2):
  1. Explicit markers in message content: [PIN:<type>] ... [/PIN]
     and <pin type="<type>"> ... </pin>, for types safety,
     hard-constraint, decision, goal, fact.
  2. Whole system messages (type "system").

A Pin carries its text and a content hash for logging. Logs must use
the hash, never the text: the meter log must not become a prompt store.
"""

import hashlib
import re

PIN_TYPES = ("safety", "hard-constraint", "decision", "goal", "fact")

_BRACKET_RE = re.compile(
    r"\[PIN:([a-z-]+)\](.*?)\[/PIN\]", re.DOTALL)
_TAG_RE = re.compile(
    r'<pin\s+type="([a-z-]+)">(.*?)</pin>', re.DOTALL | re.IGNORECASE)


class Pin:
    def __init__(self, pin_type, text, message_index):
        self.type = pin_type
        self.text = text
        self.message_index = message_index

    @property
    def digest(self):
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:12]

    def loggable(self):
        return {"type": self.type, "sha256_12": self.digest,
                "message_index": self.message_index,
                "chars": len(self.text)}


def _content_text(msg):
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


def extract_pins(messages):
    """Return (pins, protected_indices).

    protected_indices are message positions that must never be altered:
    any message containing a pin marker, any system message, and every
    message from the last user message onward (the current turn).
    """
    pins = []
    protected = set()
    last_user = -1
    for i, msg in enumerate(messages):
        if msg.get("role") == "user":
            last_user = i
    for i, msg in enumerate(messages):
        text = _content_text(msg)
        found = False
        for rx in (_BRACKET_RE, _TAG_RE):
            for m in rx.finditer(text):
                ptype = m.group(1).lower()
                if ptype not in PIN_TYPES:
                    continue
                inner = m.group(2).strip()
                if inner:
                    pins.append(Pin(ptype, inner, i))
                    found = True
        if msg.get("role") == "system":
            stripped = text.strip()
            if stripped:
                pins.append(Pin("system", stripped, i))
            protected.add(i)
        if found:
            protected.add(i)
    if last_user >= 0:
        for i in range(last_user, len(messages)):
            protected.add(i)
    return pins, protected


def has_pin_marker(text):
    return bool(_BRACKET_RE.search(text) or _TAG_RE.search(text))
