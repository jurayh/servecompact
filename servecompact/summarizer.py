"""Summarizer client (v2) and the $0 stub for stages 0-1.

The real client talks to any OpenAI-compatible chat completions
endpoint over stdlib HTTP. It deliberately does NOT go through
LiteLLM: a summarizer call routed through the same proxy would hit
the guardrail again and recurse. Usage comes from the API response
(measured), never estimated.

The stub summarizer makes no network calls and spends $0. It is the
only summarizer stages 0-1 ever use. Modes:
  echo               summary carries every checklist value (passes)
  drop_first         always omits the first derived item (fails)
  fail_once_then_pass  drops it on the first call, passes after
  error              raises SummarizerError (tests error fallback)
  slow               sleeps 30 s (tests timeout fallback; the pipeline
                     enforces its timeout around the call)
"""

import json
import math
import re
import time
import urllib.error
import urllib.request

INSTRUCTION_TEMPLATE = (
    "Summarize the following excerpt from a long agent conversation. "
    "Keep it short and factual. Organize under these sections, one per "
    "line group, including only sections that have content: Decisions, "
    "Facts, Goals, Errors. Under 'Must include', every item must appear "
    "in the summary, worded closely to the original. Under 'Superseded', "
    "list old values this summary deliberately drops, one per line. "
    "Do not invent facts, do not restate the current request, and do "
    "not copy the excerpt verbatim."
)

def _section_for(item_type):
    """Checklist item type -> summary section heading."""
    return {"decision": "Decisions", "goal": "Goals",
            "error_or_signature": "Errors"}.get(item_type, "Facts")
_SECTION_RE = re.compile(r"^\s*#{0,3}\s*([A-Za-z][A-Za-z /]*?)\s*:?\s*$")


class SummarizerError(Exception):
    """A summarizer call failed. May carry the usage of a call that
    was made and billed even though it produced no usable summary
    (e.g. the output budget went to reasoning). The pipeline must
    attribute that usage: a failed attempt still costs money."""

    def __init__(self, message, usage_in=0, usage_out=0):
        super().__init__(message)
        self.usage_in = usage_in
        self.usage_out = usage_out


class ParseError(Exception):
    pass


class SummaryResult:
    def __init__(self, text, usage_in=0, usage_out=0, model="",
                 latency_ms=0.0, usage_method="measured (API response)"):
        self.text = text
        self.usage_in = usage_in
        self.usage_out = usage_out
        self.model = model
        self.latency_ms = latency_ms
        self.usage_method = usage_method


def render_checklist(items):
    """Checklist as 'Must include' text for the summarizer input."""
    lines = []
    for it in items:
        if it.check == "absent":
            lines.append(f"- SUPERSEDED (do not present as current): "
                         f"{it.value}")
        else:
            lines.append(f"- [{it.type}] {it.value}")
    return "\n".join(lines)


def parse_summary(text, expected_types):
    """Parse a summary into sections.

    expected_types are the canonical section names the checklist
    requires (e.g. {"decisions", "facts"}). Raises ParseError when a
    required section is missing or the text is empty. The Superseded
    section is required only when the checklist has superseded items.
    """
    if not text or not text.strip():
        raise ParseError("empty summary")
    sections = {}
    current = None
    for line in text.split("\n"):
        m = _SECTION_RE.match(line)
        head = m.group(1).strip().lower() if m else ""
        if head in ("decisions", "facts", "goals", "task state",
                    "errors", "superseded"):
            current = {"goals": "goals", "task state": "goals"}.get(
                head, head)
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    missing = [t for t in expected_types if t not in sections]
    if missing:
        raise ParseError(f"missing sections: {missing}")
    return sections


def expected_sections(items):
    """Canonical section names a summary must carry for this checklist."""
    types = set()
    for it in items:
        if it.check == "absent":
            types.add("superseded")
        elif it.type in ("decision",):
            types.add("decisions")
        elif it.type in ("goal",):
            types.add("goals")
        elif it.type in ("error_or_signature",):
            types.add("errors")
        else:
            types.add("facts")
    return types


META_BASE_URL = "https://api.meta.ai/v1"
META_MODEL_CONTRIBUTOR = "muse-spark-1.3-contributor"
# Contributor tier: $0.10 input / $0.20 output per 1M tokens.
META_IN_RATE_CONTRIBUTOR = 0.10 / 1_000_000
META_OUT_RATE_CONTRIBUTOR = 0.20 / 1_000_000
# Standard tier, for the comparison price only: $1.25 / $4.25 per 1M.
META_IN_RATE_STANDARD = 1.25 / 1_000_000
META_OUT_RATE_STANDARD = 4.25 / 1_000_000
_BPE_UPPER = 1.4  # chars/4 underestimates tokens; x1.4 is the upper bound


class CapExceeded(RuntimeError):
    pass


def extract_response_text(payload):
    """Extract answer text from a Responses API payload.

    Raises RuntimeError with a truncated shape dump when no
    output_text part exists (e.g. the output budget was spent on
    reasoning items), so the failure is diagnosable from the record.
    """
    text = ""
    for item in payload.get("output", []):
        for part in item.get("content", []):
            if part.get("type") == "output_text" and part.get("text"):
                text += part.get("text", "")
    if not text:
        shape = str({k: (str(v)[:300]) for k, v in payload.items()})[:2000]
        raise RuntimeError(f"no output_text in response; shape: {shape}")
    return text


class MetaResponsesSummarizer:
    """Meta Model API summarizer via the Responses API (sync, stdlib).

    This is the pilot summarizer. The Meta Model API speaks the
    Responses API (POST /responses), not Chat Completions, so the
    OpenAI client above cannot be used against it. The request shape
    follows the proven Sluice live-benchmark / Weight Doctor
    kill-3 pattern: one user message, temperature 0, reasoning
    effort low (without it the model spends the output budget on
    reasoning items and returns no answer text), max_output_tokens
    capped. Auth is the stored credential's surrogate, fetched via
    the dynamic-credentials helper and sent only to api.meta.ai;
    it is never printed, logged, or persisted.

    Cost is bounded before each call (chars/4 x 1.4 upper bound on
    input, plus the output cap) against cap_usd, and tallied from
    the provider's usage after each call. A call that would cross
    the cap is refused before it is made.
    """

    def __init__(self, model=META_MODEL_CONTRIBUTOR, cap_usd=0.50,
                 max_output_tokens=8192, timeout_s=300,
                 base_url=META_BASE_URL):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.cap_usd = cap_usd
        self.max_output_tokens = max_output_tokens
        self.timeout_s = timeout_s
        if "contributor" in model:
            self.in_rate = META_IN_RATE_CONTRIBUTOR
            self.out_rate = META_OUT_RATE_CONTRIBUTOR
        else:
            self.in_rate = META_IN_RATE_STANDARD
            self.out_rate = META_OUT_RATE_STANDARD
        self.spent_usd = 0.0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0
        self._surrogate = None

    def _get_surrogate(self):
        if self._surrogate is None:
            import sys
            sys.path.insert(
                0, "/opt/muse/skills/custom-connector-setup/bin")
            from dynamic_credentials import dynamic_credential_entry
            entry = dynamic_credential_entry(
                "custom.meta-model-api", "access_token")
            surrogate = str(entry["surrogate"]).strip()
            if not surrogate.startswith("hsurr:"):
                raise RuntimeError(
                    "meta-model-api credential surrogate not in "
                    "expected form; refusing to call.")
            self._surrogate = surrogate
        return self._surrogate

    def estimate_cost_usd(self, prompt):
        in_tokens = math.ceil(len(prompt) / 4 * _BPE_UPPER)
        return (in_tokens * self.in_rate
                + self.max_output_tokens * self.out_rate)

    def summarize(self, middle_text, items, failed=None):
        started = time.monotonic()
        checklist = render_checklist(items)
        user_text = (f"{INSTRUCTION_TEMPLATE}\n\n{middle_text}\n\n"
                     f"Must include:\n{checklist}" if checklist
                     else f"{INSTRUCTION_TEMPLATE}\n\n{middle_text}")
        if failed:
            # Rescope (Nov 6, 2026): failed is a list of
            # ChecklistItems (values included), not type names, so
            # the model can fix the actual misses. A summarizer
            # input, never a log.
            user_text += ("\n\nThe previous summary missed these "
                          "items; include each of them this time:\n"
                          + render_checklist(failed))
        est = self.estimate_cost_usd(user_text)
        if self.spent_usd + est > self.cap_usd:
            raise CapExceeded(
                f"refusing call: estimated ${est:.4f} would take spend "
                f"to ${self.spent_usd + est:.4f}, over the "
                f"${self.cap_usd:.2f} cap.")
        body = json.dumps({
            "model": self.model,
            "input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text",
                                    "text": user_text}]}],
            "stream": False,
            "temperature": 0,
            "reasoning": {"effort": "low"},
            "max_output_tokens": self.max_output_tokens,
        }).encode()
        req = urllib.request.Request(
            self.base_url + "/responses", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._get_surrogate()}"},
            method="POST")
        try:
            with urllib.request.urlopen(req,
                                       timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError,
                TimeoutError) as exc:
            raise SummarizerError(f"summarizer call failed: {exc}") from exc
        # Tally FIRST, before extracting text: a call that returns no
        # answer text was still made and billed.
        usage = payload.get("usage") or {}
        in_tok = int(usage.get("input_tokens",
                               usage.get("prompt_tokens", 0)) or 0)
        out_tok = int(usage.get("output_tokens",
                                usage.get("completion_tokens", 0)) or 0)
        cost = in_tok * self.in_rate + out_tok * self.out_rate
        self.spent_usd += cost
        self.prompt_tokens += in_tok
        self.completion_tokens += out_tok
        self.calls += 1
        try:
            text = extract_response_text(payload)
        except RuntimeError as exc:
            raise SummarizerError(str(exc), usage_in=in_tok,
                                  usage_out=out_tok) from exc
        return SummaryResult(
            text=text, usage_in=in_tok, usage_out=out_tok,
            model=self.model,
            latency_ms=round((time.monotonic() - started) * 1000.0, 2))


class OpenAISummarizer:
    """OpenAI-compatible chat completions summarizer (sync, stdlib)."""

    def __init__(self, base_url, api_key, model, timeout_s=10,
                 max_output_tokens=None):
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/chat/completions"):
            self.url = self.base_url
        else:
            self.url = self.base_url + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.max_output_tokens = max_output_tokens

    def summarize(self, middle_text, items, failed=None):
        started = time.monotonic()
        instruction = INSTRUCTION_TEMPLATE
        checklist = render_checklist(items)
        user_text = (f"{middle_text}\n\nMust include:\n{checklist}"
                     if checklist else middle_text)
        if failed:
            # Rescope (Nov 6, 2026): failed is a list of
            # ChecklistItems (values included), not type names.
            user_text += ("\n\nThe previous summary missed these "
                          "items; include each of them this time:\n"
                          + render_checklist(failed))
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": user_text},
            ],
        }
        if self.max_output_tokens:
            body["max_tokens"] = self.max_output_tokens
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError,
                TimeoutError) as exc:
            raise SummarizerError(f"summarizer call failed: {exc}") from exc
        usage = payload.get("usage") or {}
        try:
            text = payload["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise SummarizerError(
                "summarizer response unparseable",
                usage_in=int(usage.get("prompt_tokens") or 0),
                usage_out=int(usage.get("completion_tokens") or 0)) from exc
        return SummaryResult(
            text=text,
            usage_in=int(usage.get("prompt_tokens") or 0),
            usage_out=int(usage.get("completion_tokens") or 0),
            model=self.model,
            latency_ms=round((time.monotonic() - started) * 1000.0, 2))


class StubSummarizer:
    """Deterministic $0 stand-in for stages 0-1. No network, no spend."""

    def __init__(self, mode="echo"):
        self.mode = mode
        self.calls = 0

    def summarize(self, middle_text, items, failed=None):
        self.calls += 1
        started = time.monotonic()
        if self.mode == "error":
            raise SummarizerError("stub error (test mode)")
        if self.mode == "error_usage":
            raise SummarizerError("stub error with usage (test mode)",
                                  usage_in=10000, usage_out=5000)
        if self.mode == "slow":
            time.sleep(30)
        derived = [it for it in items if it.check != "absent"]
        superseded = [it for it in items if it.check == "absent"]
        drop = set()
        if self.mode == "drop_first" and derived:
            drop.add(derived[0].value)
        if self.mode == "drop_fact":
            # Drop one fact but keep the Facts section (other items
            # still land there): the summary then fails the
            # CHECKLIST (not the parse), which is the path that
            # exercises the retry-with-values.
            for it in derived:
                if it.type == "fact":
                    drop.add(it.value)
                    break
        if self.mode == "fail_once_then_pass" and self.calls == 1 \
                and derived:
            drop.add(derived[0].value)
        sections = {"Decisions": [], "Facts": [], "Goals": [],
                    "Errors": []}
        for it in derived:
            if it.value in drop:
                continue
            sections[_section_for(it.type)].append(f"- {it.value}")
        out = []
        for head in ("Decisions", "Facts", "Goals", "Errors"):
            if sections[head]:
                out.append(f"{head}:")
                out.extend(sections[head])
        if superseded:
            out.append("Superseded:")
            out.extend(f"- {it.value}" for it in superseded)
        return SummaryResult(
            text="\n".join(out) if out else "Decisions:\n- (none)",
            usage_in=0, usage_out=0, model=f"stub:{self.mode}",
            latency_ms=round((time.monotonic() - started) * 1000.0, 2),
            usage_method="stub (no model called)")
