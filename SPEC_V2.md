# ServeCompact v2 — verified summarization + LiteLLM distribution

Status: **spec approved Nov 6, 2026; stages 0–1 built the same day
($0, stub summarizer only, no paid calls).** Stage-1 results in
`reports/replay_v2_report.json`: 24/28 sessions trigger, ceiling
88.88% token-weighted reduction vs original (v1: 23.34%, so +65.54
points incremental), 1,261 checklist items, 4 sessions with zero
derived items (none of them triggered), 0 fallbacks with the echo
stub, verifier control passes. The paid pilot
in §11 still needs a second, separate approval: the estimate is
presented in this spec, and no paid call runs until he approves it
explicitly, per the standing paid-API rule (estimate first, contributor
tier first, $5/day hard cap).

## 0. Thesis

v1 proved deterministic compaction preserves pins. Its limit is that
deterministic rules stop where prose starts: the compressible middle
of a long session is mostly prose, tool chatter, and reasoning that no
regex can safely cut. A plain model summarizer could cut it, but every
naive summarizer loses constraints — the conformance kit already
measured that: lossy truncation and naive summaries lose safety,
facts, and goals, while checklists carry them.

v2 is the runtime version of that measurement: **a model summarizes
the middle, and the summary ships only if it passes a deterministic
checklist.** The checklist, not the summarizer, is the product. And
v2 ships where the users already are: as a LiteLLM guardrail, not as
a standalone proxy nobody adopts.

The pivot, in one line: from "cheaper prompts" to "agents that don't
forget" — with the proof attached to every request.

## 1. Goals and non-goals

Goals:

- G1. Summarize the compressible middle with a model, verified by a
  deterministic checklist. A summary that fails the checklist never
  ships.
- G2. Net savings must be positive: the summarizer must cost less
  than the main-model tokens it saves, measured per compaction event.
- G3. Drop-in distribution through LiteLLM, keeping the standalone
  proxy as a second path. One codebase, two entry points.
- G4. Real tokenizer, so v2 stops pricing everything in chars/4
  estimates.
- G5. A real-model A/B with task success as the gate (validation
  stage 3, separately approved).

Non-goals (not silently added later):

- Multi-provider routing (LiteLLM gives routing; v2 adds no routing
  logic of its own).
- Prompt-cache optimization (the cache-aware direction stays a
  separate, unchosen option).
- A hosted component, a fine-tuned summarizer, or any claim that
  free prose is preserved. The checklist covers typed and labelled
  state (§3); free-prose preservation is measured by the A/B task
  gate, not claimed by the checklist.

## 2. Architecture: tiered fallback

Per request, in order:

1. **v1 deterministic compact, unchanged.** Pins, dedup, supersession,
   correction, structural compression, monotonicity invariant.
2. **Trigger gate.** Summarization runs per contiguous middle block
   (messages before the last user message, excluding protected ones,
   grouped into contiguous runs), and only for a block whose tokens
   exceed `trigger_tokens` (default 2000 est tokens) after v1. Below
   the trigger, the block goes out as v1 output and no summarizer
   is called. Small sessions stay $0 and fast. Checklist extraction,
   summarization, verification, and fallback are all scoped per
   block: one bad block falls back to v1 without losing the others.
3. **Checklist extraction** (§3) from the middle, deterministic.
4. **Summarizer call** (§4) over the middle. Timeout
   `summarizer_timeout_s` (default 10 s). Any error, timeout, or
   unparseable output falls through to step 6a.
5. **Verifier v2** (§5) on the summary. One retry, with the failed
   items listed in the retry prompt. A second failure falls through
   to step 6a.
6. Fallback chain, in order:
   - 6a. Summary fails or the summarizer errors: send the **v1
     compacted prompt** (already verified by the v1 verifier).
   - 6b. v1 verifier fails: send the **original uncompacted prompt**
     (v1 rule, unchanged).
   Every fallback is logged with its tier and reason (counts and
   checklist-item hashes only, never prompt or pin text).

The invariant from v1 carries up: the final prompt must be shorter
than the v1 prompt, or v1 output is sent instead. Summarization that
does not shrink the prompt is not a saving.

## 3. Checklist extraction (deterministic, no model)

The checklist is built from the middle before summarization. Each
item has a type, a value, and a mechanical check. Sources, in order:

| Source | Items | Check |
|---|---|---|
| Explicit pins (`[PIN:<type>]`, `<pin>`, system messages) | Every pin, verbatim | Exact substring in the final prompt (v1 verifier, unchanged) |
| Labelled lines in the middle: `DECISION:`, `GOAL:`, `FACT:`, `OWNER:`, `REGION:`, `SAFE ACTION:` (case-insensitive, line-start after stripping) | The labelled value | Normalized substring: case-insensitive, whitespace-collapsed value appears in the summary |
| Latest-value pairs: any `Key: value` line in the middle that repeats with different values (last wins). This covers v1's own records — fields corrected (`Correction: F is now V`) and superseded tool results (same Read/Bash key) are instances of repeated keys — plus any other repeated state the middle carries. | The latest value, plus the superseded value | Latest value present (normalized); superseded value must NOT appear as a current value (see absence rule below) |
| Error/signature lines that v1's structural compression kept inside the middle | Each such line, capped at 10 per block (pure error lines first, then signature lines, then lines matching both) | Exact substring in the summary. The cap is a design parameter, recorded here: a code-heavy block can hold dozens of `def` lines, and forcing all of them verbatim into a summary would defeat summarization. The cap firing is recorded in the checklist stats. |

What the checklist does **not** cover, stated plainly: arbitrary
free-prose facts. A model can drop an unlabeled sentence and no
deterministic check will notice. That gap is real, it is why the
checklist is typed and labelled, and it is why the A/B task gate (§9,
stage 3) exists. Do not let v2 marketing (or the README) claim
otherwise.

Rescope (Nov 6, 2026): superseded items are capped at 10 per block
— per key only the most recent superseded value is kept, then at
most 10 overall by recency (see `checklist.py`,
`MAX_SUPERSEDED_ITEMS_PER_BLOCK`; the dropped count is in stats).
The pilot showed superseded values dominating the checklist (up to
143 of 193 items in one session) while labelled lines were zero
across the corpus. The checklist is therefore selective, not
exhaustive: a summary can state an old value the checklist no
longer checks, and no deterministic check will notice. That is a
second stated gap, alongside free prose, and the A/B gate is what
covers both.

Checklist extraction coverage is itself measured in validation stage
1: items per session, and sessions with zero derived items (pins only).
A session with an empty derived checklist is a session the checklist
cannot protect beyond pins, and the report must say so.

Absence rule, as implemented: the check is line-scoped against the
summary text with the `Superseded` section removed. A superseded
value fails only if it appears on a line that does not also carry its
replacement value, so an old value that is a substring of its
replacement (old path `D:\CFML` inside new `D:\CFML\project`) does
not fail merely by appearing inside it. And a superseded value that
is a substring of *any* current checklist item's value is dropped
from the checklist entirely, counted in stats, rather than checked
and faked: the mechanical check cannot distinguish it from the
current item that legitimately carries it.

## 4. Summarizer contract

- **Input.** The v1-compacted middle (turns only; see below) plus a
  fixed instruction template (~400 est tokens, counted in the pilot
  estimate) plus the checklist rendered as a "must include" list.
  Giving the checklist to the summarizer is legitimate: it guides
  preservation, and verification is an independent mechanical re-check,
  so there is no circularity. The summarizer never sees pinned spans
  as text to rewrite: pinned messages are excluded from the middle
  and re-attached verbatim.
- **Summarization unit: whole turns.** A block of contiguous middle
  turns (an assistant message plus its tool replies, as a unit) is
  replaced by a single assistant message carrying the summary (no
  `tool_calls`). Replacing a block as a unit keeps the request valid:
  a tool message is never orphaned from its assistant tool call, and
  a tool call is never kept without its reply. Turns containing pins
  are never in a summarized block. Rescope (Nov 6, 2026): a block
  larger than 30,000 est tokens is split into chunks of at most
  30,000 est tokens by message boundary, each chunk summarized and
  verified separately (each chunk contributes its own summary
  message). The pilot's 153k-token middle produced a 193-item
  checklist no summary could satisfy.
- **Output format.** Structured, parseable, with one section per
  type present (`Decisions`, `Facts`, `Goals / task state`,
  `Errors`), plus a `Superseded` section listing old values the
  summary deliberately dropped. Missing sections for types present
  in the checklist = parse failure = fallback (step 6a).
- **Model.** Configurable OpenAI-compatible model; pilot default
  `muse-spark-1.3-contributor` (contributor tier first, per the
  standing rule). Temperature 0. Max output tokens capped at 25% of
  the middle's est tokens: a summary longer than that cannot be a
  net saving and is rejected by the monotonicity check anyway.
  Rescope (Nov 6, 2026): floored at 4096 tokens for a reasoning
  model (cost-gate skill rule) and never let below that floor by
  the percentage rule; ceiling 8192. The pilot's 803-token cap on
  a small middle produced no answer text at all.
- **Latency.** The summarizer call is serial before the main call.
  Budget: `summarizer_timeout_s` = 10 s; the latency kill (VK5) is
  measured on medians, not one slow call. The 3 s VK5 bar was
  replaced by 60 s in the Nov 6 rescope after the pilot measured
  a 36.7 s median (see §9/§10).

## 5. Verifier v2

Deterministic checks, in order, on the candidate final prompt:

1. v1 verifier: every pin verbatim. (Unchanged.)
2. Checklist: every item passes its check from §3.
3. Structure: the summary parses into the §4 sections.
4. Monotonicity: final prompt tokens < v1 prompt tokens.
5. Old-value absence: no superseded value appears as a current value
   (outside an explicit `Superseded` note).

One retry on failure, with the failed items (values included)
listed in the retry prompt (rescope, Nov 6, 2026 — the pilot's
retry named only item types and the model could not fix its
misses). The log records types and hashes only, never values.
After the retry, step 6a.

The verifier never uses a model. A model-judged verifier would make
the proof circular: the thing being checked would be checked by the
thing that produced it.

## 6. Meter v2

Adds to the v1 meter, per request:

- Summarizer tokens in/out: **measured** from the API response's
  `usage` fields, not estimated. (Prompt tokens stay chars/4
  estimates until §7 lands; the meter labels each figure with its
  method, as v1 does.)
- Summarizer cost at the summarizer's configured rates.
- Net saving per compaction event: `(middle_after_v1_tokens -
  summary_tokens) * main_input_rate - summarizer_cost`, with both
  rates labelled. The main rate is the configured rate in
  `rates.json`; the summarizer rate is the summarizer's rate.
- Checklist pass rate (first attempt), retry rate, summarizer
  fallback rate by tier (§2 step 6), added latency (summarizer
  wall time).

Net saving is reported per event and in aggregate. An event with
negative net saving is a real result, not a rounding error, and it
feeds kill VK2.

## 7. Tokenizer prerequisite (build task, $0)

Add `tiktoken` (`cl100k_base`) for OpenAI-shaped models, behind the
same `tokens.py` seam as v1, so `METHOD` changes from
"estimate (chars/4…)" to "measured (tiktoken cl100k_base)" for those
models. Meta-model token counts stay estimates until that tokenizer
is available locally; the meter must label them as such rather than
silently mixing methods. No paid calls are needed for this task.

## 8. LiteLLM distribution

### The hook (grounded in LiteLLM docs, read Nov 2026)

- LiteLLM's `CustomGuardrail.async_pre_call_hook(user_api_key_dict,
  cache, data, call_type)` is the one hook that can replace a
  request: its returned `dict` becomes the request data.
  Docs: <https://docs.litellm.ai/docs/proxy/call_hooks>
- The guardrail is registered in the proxy config:
  ```yaml
  guardrails:
    - guardrail_name: "servecompact"
      litellm_params:
        guardrail: servecompact.litellm_guardrail.ServeCompactGuardrail
        mode: "pre_call"
        default_on: true
        summarizer_model: "muse-spark-1.3-contributor"
        trigger_tokens: 2000
  ```
  then `litellm --config config.yaml` as usual.
- The hook acts **only** when `call_type` is `"completion"` or
  `"acompletion"` (the async chat completions route uses the latter —
  verified against litellm 1.108.1, whose `CustomGuardrail`
  signature lists both) and `data` carries an OpenAI-format
  `messages` list. Every other route and call type (Anthropic native
  `/v1/messages`, Responses API, embeddings, image, audio, admin) is
  returned untouched: rewriting those bodies as a chat message array
  would corrupt the request. This "skip everything that is not chat
  completions" rule is the documented safe move, and the needlepath
  guardrail precedent does the same.
- Streaming is unaffected by construction: the hook runs before
  routing, and the response stream passes through untouched.
- The standalone proxy (`python -m servecompact`) stays as a second
  path, sharing `pipeline.py`. One codebase, two entry points.

### Gotchas recorded from the docs (do not re-learn them)

- `callbacks` (the `CustomLogger` path) must point at an **instance**,
  not a class: the proxy fails config load, or on older versions
  starts clean, serves traffic, and never runs the hooks, with no
  error and no log line. The guardrail path above is the supported
  mutation path; do not route this through `CustomLogger`.
- The hook must be async and must respect `summarizer_timeout_s`:
  a slow summarizer must fall back to v1 output, never hang the
  request.
- LiteLLM 1.108.1, tested Nov 6, 2026 (`.venv`, `pip install
  "litellm[proxy]"`; the bare `litellm` package lacks the proxy's
  `websockets` dependency). The adapter smoke test
  (`scripts/test_litellm_proxy.py`) runs a real proxy against the
  fake upstream ($0, v1-only, no model calls): compaction verified
  at the upstream, response passes through, streaming deltas intact.
- Environment quirk, recorded so it is not re-learned: this VM's
  `NO_PROXY` carries bracketed IPv6 entries (`[::1]`) that httpx
  cannot parse as URL patterns; LiteLLM's `LicenseCheck` builds an
  httpx client at import and crashes on them. The smoke test scrubs
  the proxy env vars for its subprocesses (localhost only, so
  nothing is lost). A real deployment with a clean `NO_PROXY` is
  unaffected.

### The competitor already on this path: Compresr

Compresr ships a first-party LiteLLM guardrail
(<https://api.compresr.ai/docs/framework-integration/litellm>,
published 2026-10-30) that compresses tool outputs query-aware
before forwarding, with per-role ratios, a 300 s result cache, and a
`fail_closed` flag that **defaults to false** (on Compresr
unavailability it forwards the original uncompressed request). It is
already shipped, purpose-built (latte models), and cached. ServeCompact
v2 must not pretend it is alone on this path. Its differentiators,
stated as claims to prove rather than slogans:

1. **Verified, not just compressed.** Compresr's docs state no
   preservation proof: no pins, no checklist, no per-request
   survival report. ServeCompact's guarantee is fail-closed on
   *content* (checklist fail → fallback), while Compresr's
   `fail_closed` is only about *availability* of its own service.
2. **No vendor key.** The summarizer is any OpenAI-compatible model
   the operator configures (local, Ollama, contributor tier).
   Compresr requires its API key and hosted models.
3. **Deterministic first pass.** v1 rules run before any model call,
   so short and structured middles never pay for a summary.
4. **Open source.** The checklist, verifier, and meter are inspectable.

And the honest weaknesses: Compresr is query-aware (it compresses
against the actual question, which is better relevance than a
generic summary) and its purpose-built models likely compress better
per token than a general summarizer. v2 proves its case only by
measuring checklist survival and net savings against those facts,
not by asserting them.

## 9. Validation, in order

- **Stage 0 ($0): plumbing, stub summarizer.** A deterministic fake
  summarizer that returns a canned summary (passing or failing the
  checklist on demand) exercises the fallback chain, the LiteLLM
  hook wiring, and the meter with no paid calls. Tests: fallback
  tiers, hook skip rules, meter labels. **Built Nov 6, 2026:**
  23 tests in `tests/test_v2.py` plus the LiteLLM proxy smoke test,
  all passing.
- **Stage 1 ($0): replay with the stub.** The stub echoes checklist
  items back, so the run measures trigger rates, checklist
  extraction coverage (§3), and the token-reduction ceiling, still
  with no paid calls. This is where the coverage gap on unlabeled
  prose gets its number. **Run Nov 6, 2026** (`scripts/replay_v2.py`,
  `reports/replay_v2_report.json`): 24/28 sessions trigger (96.0%
  of middle blocks), 1,261 checklist items (45.0/session), 4
  sessions with zero derived items — none of them triggered, so the
  gap is inert at this trigger — ceiling 88.88% token-weighted
  reduction vs original (v1: 23.34%, incremental +65.54 points),
  net-saving ceiling $5.01 at the configured rate with the stub's
  $0 cost. The ceiling is an upper bound, not a measurement: it
  assumes a summarizer that preserves exactly the checklist and
  nothing else. Whether a real summarizer can do that at this
  compression is what the pilot measures; VK2 is judged on the
  pilot, not on the ceiling.
- **Stage 2 (paid pilot, needs approval): real summarizer on the 28
  trace-commons sessions.** One summarization per triggering
  session, plus the retry budget. Cost estimate in §11. Gates:
  checklist pass rate, net savings, fallback rate, latency.
  **Run Nov 6, 2026** (`scripts/run_v2_pilot.py`,
  `reports/v2_pilot_report.json`): real summarizer
  `muse-spark-1.3-contributor` via the Meta Model API Responses
  API (`MetaResponsesSummarizer`: temperature 0, reasoning effort
  low, output capped at min(25% of middle, 8192)). 24/28 sessions
  trigger; 43 calls; **measured spend $0.3769 vs the $0.25 approved
  estimate (+51%), under the $0.50 cap**. Verdicts, judged on the
  pre-registered bars: **VK1 15.46% (bar ≥ 80%) — HIT; VK2 mean net
  saving +$0.0694 per triggered session (bar > $0) — PASS; VK3
  fallback 70.83% (bar ≤ 20%) — HIT; VK5 median added latency
  36.7 s (bar ≤ 3 s) — HIT.** Pins 100% (192/192), no critical
  failure. Three kills hit, so the pre-registered outcome is
  **STOP or RESCOPE**. The fail-closed design held: 17 sessions
  fell back to v1 safely and nothing unverified shipped.
  - Cost overrun, plainly: measured summarizer input ran 3,552,650
    tokens vs 2,076,295 est (1.71×) — chars/4 underestimates
    code-heavy text, and the estimate's 1.4× upper bound was too
    low — and 19 retries happened vs 6 assumed (79% retry rate).
    The smoke projection guard was also blind on this run: it read
    the pipeline row, which showed $0 for the smoke session because
    both of its attempts errored after being billed; it projected
    $0.0012 while true spend was already $0.0011 on that session
    alone. Fixed after the run (commit below): failed attempts now
    carry their billed usage into the report, attempts record their
    failure reason, and the smoke projection reads the client's own
    tally per call. A later dry re-check of the fixed guard
    projected $0.5944 from the smoke (per-call ratio × 1.25 retry
    share) and would have aborted — the fixed guard is stricter
    than the run that actually spent $0.3769.
  - Diagnosis (from the corpus, $0; no second paid run): the
    checklist is dominated by generic state, not the typed state
    the spec imagined — **zero** labelled lines (DECISION:/GOAL:/
    FACT:) in all 24 middles; items are latest-value pairs and
    their superseded old values (up to 143 of 193 items in one
    session) plus 10 verbatim error lines. Even 5-item and 10-item
    checklists failed, so this is not only scale: the model must
    reproduce exact strings (including 10 exact error lines) and
    the retry tells it only the failed item *types*, never which
    values it missed, so it cannot fix them. Second failure mode,
    separate: the smoke session (middle 3,212 est tokens) got an
    803-token output cap from the 25% rule and both attempts
    errored with no answer text — the reasoning model spent the
    cap on reasoning. The 25% rule needs a floor (4096 worked for
    Weight Doctor) if small middles are to summarize at all.
  - Rescope candidates (awaiting Yuriy's call; NOT built): chunk
    large blocks so each summary carries fewer items; pass the
    failed *values* in the retry prompt (§5 already allows this);
    make the checklist selective (load-bearing state only) instead
    of exhaustive; floor the output cap at 4096; reconsider the
    verbatim error-line rule; raise or reframe the VK5 bar with
    the measured 37 s median (or move summarization async).
    Any re-test runs on a small slice first, with its own
    estimate and approval.
  - Harness fixes made during wrap-up (not the rescope):
    `SummarizerError` now carries billed usage; the pipeline
    attributes failed attempts (`summarizer_calls_failed`,
    `attempt_failures`, `failed_types`); the pilot script's smoke
    projection uses the client tally per call with the 25% retry
    share; `--limit 0` now means zero sessions (it previously
    meant no limit and spent $0.0011 on an accidental smoke call
    during a dry check — total pilot-related spend $0.3780).
  - **Rescope (Nov 6, 2026, Yuriy's call: "Rescope v2, small
    re-test first").** Changes, all built $0: (1) blocks over
    30,000 est tokens are chunked (§4); (2) superseded items
    capped at 10 per block, most recent per key (§3); (3) the
    retry lists the failed items with their values (§5); (4) the
    output cap is floored at 4096 (§4); (5) the VK5 bar is 60 s
    for the re-test (was 3 s; the pilot measured a 36.7 s
    median — the old bar is recorded here and in §10). The
    error-line verbatim rule is unchanged for the re-test: the
    new failure-reason recording will say whether it is the
    blocker. Stage-1 re-run with the rescope
    (`scripts/replay_v2.py`, `reports/replay_v2_report.json`):
    71 blocks, 68 triggered, 1,091 items (was 1,261), ceiling
    88.75% token-weighted reduction (was 88.88%) — the rescope
    costs almost nothing in the best case. Re-test slice, 6
    sessions chosen to span the failure modes: `674e0f5c` (small,
    5 items, output-cap failure), `f93127ef` (10 error-only
    items), `0ca72c0c` and `4222016d` (passed first try —
    regression check), `39082932` (120 items, 98 superseded),
    `da5d32d6` (193 items, 143 superseded, largest middle).
    18 triggered chunks, 419,473 est middle tokens. Estimate in
    §11: ~$0.15, cap $0.30, awaiting a separate approval before
    any paid call. Kills for the re-test keep the same bars for
    VK1/VK2/VK3; VK5 uses 60 s. Six sessions is a small sample:
    the re-test verdicts are indicative, and a full re-run needs
    its own approval.
  - **Re-test RAN Nov 6, 2026** (approved ~$0.15, cap $0.30;
    `reports/v2_retest_report.json`): 27 calls, **measured spend
    $0.0877, under the $0.15 estimate**. The first launch crashed
    after the smoke session on a leftover variable name in the
    smoke print (my script bug, $0.0014 wasted); fixed, failure
    reasons added to the report rows, re-launched. Verdicts:
    **VK1 49.19% (bar ≥ 80%) — HIT; VK2 +$0.111/session — PASS;
    VK3 fallback 33.33% (bar ≤ 20%) — HIT; VK5 median 46.2 s
    (rescope bar 60 s) — PASS.** Pins 100% (36/36). The rescope
    moved VK1 15.46% → 49.19% and VK3 70.83% → 33.33%, but both
    bars are still missed by a wide margin. Per session: the
    smoke session now passes on retry (4096 floor + retry with
    values fixed it); `39082932` 4/4 chunks (superseded cap +
    chunking fixed the worst session); `da5d32d6` 4/6 (was 0/1);
    `0ca72c0c` passes first try again; `4222016d` REGRESSED 2/5
    (passed whole in the pilot — chunking split a session that
    passed whole, and its chunks fail); `f93127ef` still 0/1.
    Diagnosis, now measurable (every failure is a checklist
    failure — zero parse, zero summarizer errors, zero timeouts):
    only two item types fail, `latest_value` (11 failures) and
    `error_or_signature` (9). No decision/goal/fact/superseded
    failures. The retry-with-values works (smoke + one
    `39082932` chunk passed on retry). Note: the re-test
    report's `approved_estimate_usd` field reads $0.25 — the
    script's hardcoded default at the time, not the $0.15 this
    run was approved at; the flag exists now
    (`--approved-estimate-usd`). Total ServeCompact Model API
    spend Nov 6: $0.4671 (pilot $0.3769 + dry check $0.0011 +
    crashed re-test $0.0014 + re-test $0.0877), under the $5/day
    cap. Pre-registered outcome after the re-test is still STOP
    or RESCOPE; Levko's recommendation is STOP (see §12), with
    one targeted second rescope named there as the alternative.
- **Stage 3 (paid A/B, separate approval): agent tasks with and
  without the proxy, task success as the gate.** The task set is
  fixed first (candidate: terminal/coding tasks from an existing
  harness; the set is named in the stage-3 plan, not assumed here),
  the cost is estimated from that set's real token usage, and the
  estimate is presented before approval. No number is invented here.

## 10. Pre-registered kills (v2)

Written before any stage-2 run. They do not move after the numbers
land.

| # | Bar | Verdict if hit |
|---|---|---|
| VK1 | Checklist first-attempt pass rate **< 80%** | STOP or RESCOPE: the summarizer is unreliable, and the fallback chain would only be masking it. |
| VK2 | Mean net saving per compaction event **≤ $0** at contributor summarizer rates | STOP: the summarizer costs more than it saves. |
| VK3 | Fallback-to-v1 rate **> 20%** of triggered events | STOP or RESCOPE: summarization rarely ships, so v2 is v1 with a tax. |
| VK4 | A/B task success regresses with the proxy on (stage 3) | STOP or RESCOPE: same rule as v1 K3. |
| VK5 | Added summarizer latency, median **> 60 s** per triggered event (re-test bar; was 3 s — replaced by the Nov 6 rescope after the pilot measured a 36.7 s median) | RESCOPE: raise the trigger, pick a faster/cheaper summarizer model, or make summarization async where the harness allows it. |

Critical failure, separate from the kills: pins surviving in a sent
prompt below 100% means the v1 verifier itself broke. That stops
everything, same as v1.

## 11. Cost: the stage-2 pilot estimate

Measured with `scripts/measure_v2_pilot.py` from the trace-commons
corpus (no model called, no money spent). Token counts are chars/4
estimates; the output ratio and retry share are labelled assumptions.

- Sessions: 28. Triggering summarization (middle after v1 over 2000
  est tokens): **24**.
- Middle tokens total: 2,297,969 [estimate]; after v1: 1,703,618
  [estimate]. The summarizer only ever sees what v1 could not cut.
- Summarizer input tokens (first attempts + a 25% retry budget):
  **2,076,295** [estimate].

| Assumed summary output ratio | Output tokens [assumed] | Contributor tier | Standard tier |
|---|---|---|---|
| 5% | 103,814 | $0.23 | $3.04 |
| 10% | 207,629 | **$0.25** | $3.48 |
| 20% | 415,259 | $0.29 | $4.36 |

Rates: contributor `muse-spark-1.3-contributor` $0.10/1M in,
$0.20/1M out; standard $1.25/1M in, $4.25/1M out. The pilot is
priced at contributor tier first, per the standing rule, and its
total (~$0.25 at the central 10% assumption) sits far under the
$5/day hard cap. Note the standard-tier column: at a 20% output
ratio it reaches $4.36, most of a day's cap on its own — one more
reason the pilot runs on contributor tier, and why the pilot's real
cost is measured from API-returned usage, not from this table.

Net-saving projection (not a measurement): if summaries at 10%
replace the middle, main-model input saving is about
(1,703,618 − 207,629) tokens, ≈ $4.49 at the configured $3.00/1M
main rate, against ~$0.25 of summarizer cost — net ≈ +$4.24 across
the pilot if each summarized session is sent once. In a real agent
loop the compacted prompt is sent every turn, so the saving
multiplies per turn while the summarizer is paid once per event;
the pilot measures the single-send case, and VK2 is judged on the
pilot's measured numbers, not on this projection.

**What approval is being asked for, and when.** This spec asks only
for a go on the spec itself. The stage-2 pilot asks for a second,
separate approval after the spec go, on this estimate, before any
paid call. Stage 3 asks for a third approval, on its own estimate,
once its task set is fixed.

### Stage-2 re-test estimate (Nov 6, 2026; rescope)

Priced from the pilot's *measured* numbers, not chars/4: per-call
input ran 1.24× the est tokens (smoke session: 7,999 measured in
over 2 calls on a 3,212-est middle), and 19/24 sessions retried
(79% retry share). The re-test slice is 6 sessions, 18 triggered
chunks, 419,473 est middle tokens.

| Case | Input tokens [measured basis] | Output tokens [assumed] | Contributor tier | Standard tier |
|---|---|---|---|---|
| Base (10% output ratio) | 931,062 | 93,106 | **$0.11** | $1.56 |
| Cap case (20% output ratio) | 931,062 | 186,212 | $0.13 | $1.96 |

Calls: 18 first attempts plus up to 18 retries (36 max). Model:
`muse-spark-1.3-contributor` (contributor tier first, same model
behavior as the pilot; standard tier only if contributor is
unavailable — at $1.56–$1.96 it would still fit the day's cap,
but there is no reason to pay it). **Estimate presented for
approval: ~$0.15, cap $0.30** (2× the estimate; the cap case
plus every chunk retrying twice still lands under it). Today's
other Model API spend is $0.3780 (the pilot), so $0.3780 +
$0.15 ≈ $0.53, far under the $5/day hard cap. The runner aborts
after the smoke session if the re-priced projection exceeds the
cap, and the projection now reads the client's own tally per call
(see §9).

## 12. Risks and open questions

- **Recommendation after the re-test (Nov 6, 2026): STOP v2 as
  designed.** Two rounds of fixes moved VK1 15.46% → 49.19% and
  VK3 70.83% → 33.33%, but both bars (80% / 20%) are still
  missed by a wide margin, and the remaining failures are the
  checklist's core: latest values (11 failures) and verbatim
  error lines (9). Fixing latest values by weakening the check
  hollows out the "verified" claim itself, and chunking already
  regressed one session that passed whole. The v1 deterministic
  compactor (23.34% reduction, 0% fallback, pins intact) is the
  shippable piece; the fail-closed design meant nothing bad ever
  shipped, and the whole experiment cost $0.47. The alternative,
  if Yuriy wants one more round: a targeted second rescope —
  relax error lines to a normalized/truncated match and dedupe
  checklist items across chunk boundaries — re-tested on the
  same 6-session slice with its own estimate and approval.
- **Checklist coverage.** The typed checklist cannot see unlabeled
  prose. Stage 1 measures the gap; if most sessions carry zero
  derived items, v2's proof is pins-only and the spec must say so.
- **Summarizer quality vs cost.** The cheapest model may summarize
  worst (VK1 catches this), and a stronger model may fail VK2.
  The pilot measures one model; switching models re-runs stage 2.
- **Compresr is already there** (§8). Differentiation is a
  measurement claim, not a slogan: checklist survival and net
  savings, published with the same honesty rules as v1.
- **LiteLLM version drift.** The hook API is documented today; pin
  the tested version and re-check the docs page on upgrade.
- **Latency.** A serial summarizer call delays every triggered
  request. VK5 is the tripwire; async pre-summarization (summarize
  the previous turn while the agent works) is a later optimization,
  not in this spec.
- **Cache interplay.** v2 does not optimize for KV-cache reuse; the
  cache-aware direction stays separate. A summary that changes every
  turn can hurt cache hit rates even while it saves tokens — worth
  measuring in stage 3 if the harness reports cache metrics, but
  not a v2 gate.

## 13. Build order (after the spec go)

1. Tokenizer (`tokens.py`, tiktoken) + meter labels. $0.
2. Checklist extraction + verifier v2 + tiered fallback, behind the
   v1 pipeline, with the stub summarizer. $0, stage 0 tests.
3. Summarizer client (OpenAI-compatible, configurable model,
   timeout, usage capture) + stage 1 replay. $0.
4. LiteLLM guardrail adapter + local proxy smoke test against the
   fake upstream. $0 (litellm install only).
5. Stage 2 pilot (paid, separately approved) → kills judged.
6. Stage 3 A/B plan (task set fixed, costed, separately approved).

### File layout (planned)

```
servecompact/
  checklist.py         deterministic checklist extraction (§3)
  summarizer.py        summarizer client + output parsing (§4)
  verifier.py          v1 verifier + v2 checklist checks (§5)
  litellm_guardrail.py ServeCompactGuardrail, pre_call only (§8)
  meter.py             + summarizer usage, net saving (§6)
  tokens.py            + tiktoken, method labels (§7)
scripts/
  measure_v2_pilot.py  already landed: prices the pilot from the corpus
reports/
  v2_pilot_estimate.json  the §11 numbers, reproducible from the script
```
