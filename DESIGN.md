# ServeCompact v1 — design

An OpenAI-compatible proxy between an agent and a model API. The agent
points at it with a base-URL change and nothing else. The proxy compacts
the conversation, verifies the compaction, and only then forwards it.

## 1. The proxy

- `POST /v1/chat/completions` (plus `GET /v1/models` passthrough,
  `GET /health`, `GET /meter`).
- Everything the client sent is forwarded except `messages`, which is
  replaced by the verified compacted (or fallback original) list.
  Model, temperature, tools, tool_choice, stream flag, and all other
  fields pass through untouched.
- Streaming responses are forwarded chunk-by-chunk, untouched. The proxy
  never buffers a stream to completion before forwarding.
- Upstream base URL from `SERVECOMPACT_UPSTREAM_BASE_URL`, key from
  `SERVECOMPACT_UPSTREAM_API_KEY` or, if unset, the client's own
  `Authorization` header forwarded as-is. Keys are never logged.
- The proxy never stores prompts beyond the request. The meter log
  records counts, hashes, and timings only — no prompt text, no pin
  text. Pin identities in logs are `sha256(pin)[:12]` plus type.
- Dry-run mode (`SERVECOMPACT_DRY_RUN=1`) returns the compacted
  messages and the meter record instead of calling upstream. It is
  labelled dry-run in the response and is for testing, not a model
  response.

## 2. Pinning

A pin is a span of text that must reach the model verbatim. Two sources:

1. **Explicit markers in the conversation** (no client change needed
   beyond writing them into content):
   - `[PIN:safety] ... [/PIN]`, `[PIN:hard-constraint] ... [/PIN]`,
     `[PIN:decision] ... [/PIN]`, `[PIN:goal] ... [/PIN]`,
     `[PIN:fact] ... [/PIN]`
   - `<pin type="safety"> ... </pin>` (same five types)
2. **System messages**, whose whole content is pinned by default.
   System content is where safety rules and hard constraints usually
   live, and v1 does not try to be cleverer than that.

The typed taxonomy (safety rules, hard constraints, decisions,
goals/task state, facts) matches the earlier compaction-conformance
kit's canary types, so results here are comparable in vocabulary to
that kit's survival curves. ServeCompact is not that kit: the kit
measures compactors, this proxy *is* a compactor with a verifier.

**Protected messages** — never deduped, superseded, or compressed,
even if they contain a pin among other text:

- any message containing a pin marker,
- any system message,
- every message from the last user message onward (the current turn).

Protection costs tokens when a pinned message is verbose. That is the
trade, stated plainly: correctness first, savings second.

## 3. The compactor (v1: deterministic, $0, no model calls)

Applied in order, only to non-protected messages. Dropped content is
replaced by a short `[servecompact: ...]` stub so the compaction stays
inspectable in the outgoing prompt, and so tool-message slots stay
valid against their assistant tool calls.

1. **Dedup.** An exact duplicate (same role, same content, same
   tool_call_id if any) of an earlier non-protected message is replaced
   by a duplicate stub. First occurrence kept.
2. **Supersession.** A tool result is replaced by a superseded stub
   when a later tool result exists for:
   - the same `tool_call_id`, or
   - the same tool name with the same key argument: `file_path` /
     `path` / `file` for Read-like tools, `command` for Bash-like
     tools (key parsed from the preceding assistant message's
     tool_calls; unparseable arguments never trigger this rule).
   A message explicitly containing `[SUPERSEDED]` is also stubbed,
   unless protected.
3. **Correction.** A later message matching
   `Correction: <FIELD> is now <VALUE>` (or `Corrected <FIELD>:
   <VALUE>`) removes the `<FIELD>: <old>` line from earlier
   non-protected messages when `<old> != <VALUE>`. Line-level only;
   a message emptied this way becomes a stub. This is deliberately
   narrow: general correction detection is v2 work, not a regex
   pretending to understand the conversation.
4. **Structural compression of verbose tool outputs.** A tool message
   over 100 lines or 4000 characters is compressed to: its first 20
   lines, its last 10 lines, and any line matching an error or
   signature pattern (`error|exception|traceback|fail|panic|fatal`,
   `def |class |function |func `, `Signature:`), in original order,
   with a single note line stating how many lines were removed.
   Repetitive bodies in the middle go; headers, signatures, and error
   lines stay. Runs of >3 identical consecutive lines in such a
   message collapse to one. Non-tool messages are never structurally
   compressed: their prose is where task content lives.

What v1 does **not** do: summarize, paraphrase, reorder, or drop a
message because a model judged it unimportant. Every cut is a
mechanical rule above, auditable from the stub notes.

**Monotonicity invariant.** A replacement (stub or compression) is
applied only if it is shorter than the content it replaces. Without
this, stubs add tokens on tiny messages: validation caught one trace
session at -0.15% and one synthetic session at -20.47% before the
invariant was added; after it, those sessions sit at 0% instead of
going negative.

## 4. The fail-closed verifier

After compaction, the verifier checks that every pinned span appears
verbatim (exact substring) in the compacted prompt text. If any pin is
missing or altered, the proxy sends the **original uncompacted**
prompt instead and logs the fallback (pin type + hash, never text).

The guarantee, stated honestly: preservation is not "the compactor is
good". It is "the verifier caught every miss, and misses cost tokens,
not correctness." The fallback rate is itself a reported metric: a
high fallback rate means the compactor is unreliable even if no pin
was ever lost in a sent prompt.

## 5. The meter

Per request: original input tokens (estimate), compacted input tokens
(estimate), tokens saved, reduction %, fallback flag, pins planted /
surviving in the compacted prompt / surviving in the sent prompt,
compaction time ms, upstream time ms where applicable.

In aggregate: totals, token-weighted mean reduction, fallback rate,
cost saved at the configured rate. Every figure carries its estimate
label and method. The meter never prints a token or dollar figure
without that label in the same line/block.

## 6. Known limits (v1)

- Token counts are `chars/4` estimates, not tokenizer counts. Swap in a
  real tokenizer and the method label changes; the code is structured
  so `tokens.py` is the only place that changes.
- Supersession keys cover Read/Bash-shaped tools only. Other tools'
  repeated results compress only via the verbose-output rule, and
  identical tool payloads under different calls without a key argument
  are not deduped at all.
- Correction detection is the narrow pattern in §3.3, nothing more.
- Fixture task success in v1 is a deterministic harness (see
  KILLS.md), not a model A/B. The proxy's real-model regression gate
  still needs a paid, approved run and is not claimed here.
- The proxy is a single local process with an in-memory meter. No
  persistence, no multi-instance state, no auth of its own: put it
  behind the agent's own network boundary.
