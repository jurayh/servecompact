"""Token counting.

v1 counts are ESTIMATES: ceil(chars / 4) over message content plus
tool-call name/arguments. Every figure produced from those counts must
be printed with the estimate label and this method next to it (see
meter.py).

v2 adds measured counting via tiktoken (cl100k_base) for OpenAI-shaped
models, behind the Counter class below. A Counter carries its own
method label, so measured and estimated figures can never be mixed
silently: the caller prints counter.method next to every figure.
Meta-model token counts stay estimates until that tokenizer is
available locally; the Counter says so in its method string rather
than silently substituting cl100k_base.
"""

import math
import re

METHOD = "estimate (chars/4, no tokenizer installed)"
LABEL = "estimate"

_OPENAI_SHAPED_RE = re.compile(r"^(gpt-|o1|o3|o4|chatgpt|ft:gpt-)", re.I)
_tiktoken_encoding = None
_tiktoken_failed = False


def est_tokens(text):
    """Estimated tokens for a string. 0 for empty/None."""
    if not text:
        return 0
    return math.ceil(len(text) / 4)


def _load_tiktoken():
    global _tiktoken_encoding, _tiktoken_failed
    if _tiktoken_encoding is not None or _tiktoken_failed:
        return _tiktoken_encoding
    try:
        import tiktoken
        _tiktoken_encoding = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _tiktoken_failed = True
        _tiktoken_encoding = None
    return _tiktoken_encoding


class Counter:
    """A token counter with its method label attached.

    Counter.default() is the v1 estimator (chars/4), so v1 numbers
    reproduce exactly. Counter.for_model(name) measures with tiktoken
    cl100k_base when the model is OpenAI-shaped and tiktoken is
    installed; otherwise it estimates and says so.
    """

    def __init__(self, method, count_fn):
        self.method = method
        self._count_fn = count_fn

    @property
    def is_measured(self):
        return self.method.startswith("measured")

    def count(self, text):
        if not text:
            return 0
        return self._count_fn(text)

    @classmethod
    def default(cls):
        return cls(METHOD, est_tokens)

    @classmethod
    def for_model(cls, model):
        encoding = _load_tiktoken()
        if encoding is not None and model and _OPENAI_SHAPED_RE.match(model):
            return cls("measured (tiktoken cl100k_base)",
                       lambda text: len(encoding.encode(text)))
        if encoding is None:
            return cls("estimate (chars/4, tiktoken not installed)",
                       est_tokens)
        return cls("estimate (chars/4, no tokenizer for this model)",
                   est_tokens)


def message_text(msg):
    """Text content of an OpenAI chat message, as a single string.

    Handles str content, list-of-parts content, and None. Tool-call
    name/arguments are appended because they are input tokens too.
    """
    parts = []
    content = msg.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text") or "")
            elif isinstance(part, str):
                parts.append(part)
    for call in msg.get("tool_calls") or []:
        fn = (call or {}).get("function") or {}
        parts.append(fn.get("name") or "")
        args = fn.get("arguments")
        if isinstance(args, str):
            parts.append(args)
        elif args is not None:
            parts.append(str(args))
    return "\n".join(p for p in parts if p)


def messages_tokens(messages, counter=None):
    """Input tokens for a whole message list.

    counter defaults to the v1 estimator, so v1 numbers reproduce
    exactly. Pass a Counter.for_model(...) to count with a real
    tokenizer, and print counter.method next to the result.
    """
    counter = counter or Counter.default()
    return sum(counter.count(message_text(m)) for m in messages)
