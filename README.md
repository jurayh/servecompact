# ServeCompact

An OpenAI-compatible proxy that sits between an agent and the model
API, compacts the conversation before it goes out, and **proves** what
it preserved instead of summarizing and hoping. Cutting tokens is the
feature; the proof is the product.

Point any agent or client at it with a base-URL change and nothing
else. It passes requests through to the configured provider, streams
responses back untouched, and never stores prompts beyond the request.

**v1 is deterministic and $0: no model calls, no summarizer.** The
optional model-based summarizer for the compressible middle is v2, and
it gets an estimate-first approval before any paid run.

---

## The replayed session (read this first)

One session, replayed live with `python3 scripts/demo.py` — the
`synthetic-1-deploy-debug` session: 5 planted pins of every type, a
superseded file Read, a duplicate status, a corrected port, and a
verbose build log with an error line.

| | |
|---|---|
| Tokens in (original) | 4,100 [estimate: chars/4, no tokenizer installed] |
| Tokens out (compacted) | 840 [estimate: chars/4, no tokenizer installed] |
| Tokens saved | 3,260 [estimate] |
| Reduction | 79.51% [estimate] |
| Cost saved | $0.0098 [at the configured $3.00/1M input rate in `rates.json`; not a live provider quote] |
| Pins planted vs surviving (compacted prompt) | 6/6 |
| Pins planted vs surviving (sent prompt) | 6/6 |
| Fallback on this session | No (0/1) |
| Compactor actions | 1 deduped, 1 superseded, 1 correction line, 1 message compressed (153 lines removed) |
| Compaction time | 2.59 ms (replay; no model called, so no upstream time) |

Then the full replay, not just the friendly session: all 28 public
trace-commons Claude Code sessions (a public proxy corpus, **not**
anyone's own sessions), each with the same 5 planted canaries, run
through `scripts/replay.py`. Full per-session table in
[`reports/replay_table.md`](reports/replay_table.md).

| | |
|---|---|
| Sessions replayed | 28 trace-commons + 4 synthetic (synthetic reported separately) |
| Tokens in → out (trace) | 2,546,306 → 1,951,955 [estimate] |
| Tokens saved (trace) | 594,351 [estimate] |
| Reduction, token-weighted (trace) | **23.34%** [estimate] |
| Reduction, unweighted mean (trace) | 17.86% [estimate] — see the caveat below |
| Cost saved (trace) | $1.7831 [at the configured $3.00/1M rate; not a live provider quote] |
| Fallback rate (trace) | **0.0%** (0/28) |
| Pins surviving in sent prompts (trace) | 168/168 |
| Pins surviving in compacted prompts (trace) | 168/168 |
| Synthetic sessions | 15,578 → 3,071 [estimate], 80.29% (they are designed to exercise every rule, so they run hotter than real traces) |

Fixture task success, with vs without the proxy
(`scripts/fixtures.py`): **12/12 with, 12/12 without, 0 regressions.**
That harness is deterministic, not an LLM eval: a small solver reads
the prompt and must return the expected answer, so it gates
information loss, not model behaviour. A real-model A/B is still owed
and is not claimed here.

### Verdict against the pre-registered kill criteria (`KILLS.md`)

The bars were written before any run, and they did not move.

| Bar | Result | Verdict |
|---|---|---|
| K1: fallback rate > 10% kills | 0.0% | PASS |
| K2: token-weighted reduction < 20% kills | 23.34% | PASS, with a caveat |
| K3: any fixture task passing without the proxy but failing with it kills | 0 regressions | PASS |
| Control: verifier must catch a deliberately removed canary | Caught, fallback would trigger, missing-pin log carries no pin text | PASS |

**Overall: PASS — no kill bar hit.** Two honest caveats travel with
that pass:

1. **Small sessions barely compress.** The unweighted mean reduction
   is 17.86%, below the 20% bar that the weighted mean clears. Sessions
   with no verbose output, no repeats, and no supersession simply have
   nothing safe to cut, and v1 correctly cuts nothing there (several
   trace sessions sit at exactly 0%). The saving concentrates in long,
   tool-heavy sessions: the best trace session cut 47.2%, the worst
   large ones ~30–38%.
2. **Stubs used to add tokens on tiny messages.** Validation caught
   one trace session at −0.15% and one synthetic session at −20.47%
   because a stub was longer than the message it replaced. That is now
   a code invariant, not a hope: a replacement is applied only if it
   is shorter than the original, so no session goes negative anymore
   (minimum observed: 0.0%). The fix is in the compactor, with a
   regression test, and the replay above was re-run after it.

---

## How it works

### The proxy

`python -m servecompact [--host 127.0.0.1] [--port 8080]`, then point
the client at `http://127.0.0.1:8080/v1`. Upstream from
`SERVECOMPACT_UPSTREAM_BASE_URL` (default `https://api.openai.com/v1`),
key from `SERVECOMPACT_UPSTREAM_API_KEY` or the client's own
`Authorization` header forwarded as-is. Keys are never logged.

- `POST /v1/chat/completions` — compact, verify, forward. Every field
  except `messages` passes through untouched; streaming responses are
  forwarded chunk-by-chunk, untouched.
- `GET /v1/models` — passthrough. `GET /health`, `GET /meter` —
  aggregate meter figures (counts only).
- `SERVECOMPACT_DRY_RUN=1` returns the compacted messages and the
  meter record instead of calling upstream. Labelled dry-run, for
  local testing only.
- Meter logs (`SERVECOMPACT_METER_LOG`, JSONL) record counts, hashes,
  and timings only — no prompt text, no pin text, no keys.

### Pinning

A pin is a span that must reach the model verbatim:

- Explicit markers in conversation content — `[PIN:safety] ... [/PIN]`
  (also `hard-constraint`, `decision`, `goal`, `fact`), or
  `<pin type="safety"> ... </pin>`. No client change beyond writing
  them into content.
- Whole system messages, pinned by default.

Pin-bearing messages, system messages, and everything from the last
user message onward (the current turn) are **protected**: never
deduped, superseded, or compressed. Protection costs tokens when a
pinned message is verbose. That is the trade, stated plainly.

### The compactor (deterministic v1)

Applied in order, only to non-protected messages. Dropped content
becomes a short `[servecompact: ...]` stub, so the compaction stays
inspectable and tool-message slots stay valid:

1. **Dedup** — exact duplicates (same role, content, call id) replaced
   by a stub; first occurrence kept.
2. **Supersession** — an older tool result is stubbed when a later
   result exists for the same `tool_call_id`, or the same tool name
   with the same key argument (`file_path`/`path`/`file` for Read,
   `command` for Bash), or the message is marked `[SUPERSEDED]`.
3. **Correction** — a later `Correction: <FIELD> is now <VALUE>` line
   removes the old `<FIELD>: <old>` line from earlier messages.
   Deliberately narrow; general correction detection is v2.
4. **Structural compression** — a tool message over 100 lines or
   4000 characters keeps its first 20 lines, last 10 lines, and any
   error/signature line, in order, with one note stating how many
   lines were removed. Headers, signatures, and error lines stay;
   repetitive bodies go. Non-tool messages are never compressed.

No summarizing, paraphrasing, reordering, or model-judged drops.

### The fail-closed verifier

After compaction, every pinned span must appear verbatim in the
compacted prompt. If any pin is missing or altered, the proxy sends
the **original uncompacted** prompt instead and logs the fallback
(pin type + hash, never text).

The guarantee, honestly stated: preservation is not "the compactor is
good". It is "the verifier caught every miss, and misses cost tokens,
not correctness." The fallback rate is itself a reported metric.

### The meter

Per request and in aggregate: tokens in vs out, cost saved at the
configured rate, compaction time (plus upstream time on proxy runs),
fallback rate, and pin survival at both points (compacted prompt and
sent prompt). **Measured figures and estimates are never printed
apart**: every token figure carries its estimate label and method,
every dollar figure carries its configured-rate basis.

---

## Run it yourself

```bash
# unit tests (14) + proxy integration test against a fake upstream (9 checks)
python3 -m unittest discover -s tests
python3 scripts/test_proxy.py

# replay: trace-commons sessions + synthetic sessions + verifier control
python3 scripts/replay.py        # writes reports/replay_report.json, reports/replay_table.md

# fixture tasks with/without the proxy
python3 scripts/fixtures.py       # writes reports/fixture_report.json

# the live demo: one session, tokens in/out, pins surviving
python3 scripts/demo.py

# the proxy itself (needs an upstream; dry-run needs none)
SERVECOMPACT_DRY_RUN=1 python3 -m servecompact --port 8080
```

Replay needs the trace-commons corpus; it defaults to
`~/workspace/user/corpus/public/trace-commons/sessions/claude_code`
and replays synthetic sessions only, with a loud warning, if that
directory is absent. The trace corpus is not vendored into this repo.

## Limits, plainly

- Token counts are `chars/4` estimates, not tokenizer counts. Swap in
  a real tokenizer and only `servecompact/tokens.py` and its method
  label change.
- Supersession keys cover Read/Bash-shaped tools. Identical tool
  payloads under different calls without a key argument are not
  deduped.
- Correction detection is the narrow pattern above, nothing more.
- Fixture success is a deterministic harness, not a model A/B. The
  real-model regression gate still needs a paid, approved run.
- Single local process, in-memory meter, no auth of its own. Put it
  behind the agent's own network boundary.
- Out of scope for v1: multi-provider routing, prompt-cache
  optimization, any hosted component, any model-based summarizer.

## Layout

```
servecompact/          the proxy package (stdlib only)
  tokens.py            estimate counting (the only place a tokenizer swap touches)
  pins.py              pin extraction + protected-message rules
  compactor.py         deterministic v1 compactor
  verifier.py          fail-closed verbatim check
  pipeline.py          compact + verify, one request
  meter.py             per-request + aggregate accounting
  server.py            OpenAI-compatible HTTP proxy (streaming passthrough)
scripts/
  replay.py            trace + synthetic replay, verifier control, kill verdicts
  fixtures.py          deterministic fixture harness (K3 gate)
  demo.py              live demo of one session
  convert_traces.py    trace-commons JSONL -> OpenAI messages + planted canaries
  build_synthetic.py   rebuilds fixtures/synthetic_sessions.json
  test_proxy.py        proxy integration test vs a fake upstream
  fake_upstream.py     fake upstream (tests only)
fixtures/              synthetic sessions, fixture tasks
reports/               replay + fixture reports from the runs above
KILLS.md               pre-registered kill criteria (written before the runs)
DESIGN.md              full design, rules, and limits
rates.json             configured input rate ($3.00/1M example, not a live quote)
```

## v2 (spec approved; stage-2 pilot run Nov 6, 2026 — 3 kills hit, STOP or RESCOPE)

[`SPEC_V2.md`](SPEC_V2.md) specs verified summarization plus LiteLLM
distribution: a model summarizes the compressible middle, and the
summary ships only if it passes a deterministic checklist (pins
verbatim, labelled decisions/facts/goals present, latest values
current, superseded values gone); otherwise the tiered fallback
sends v1 output, then the original. Distribution is a LiteLLM
`CustomGuardrail` (`async_pre_call_hook`, chat completions only),
with the standalone proxy kept as a second path. Pre-registered
kills: checklist first-attempt pass < 80%, mean net saving ≤ $0,
fallback-to-v1 > 20%, A/B regression, median added latency > 3 s.

Stages 0–1 are built (Nov 6, 2026): real tokenizer (tiktoken
`cl100k_base` for OpenAI-shaped models; Meta-model counts stay
labelled estimates), deterministic checklist extraction, verifier v2,
tiered fallback per middle block, OpenAI-compatible summarizer
client (usage measured from the API response), stub summarizer, and
the LiteLLM guardrail (tested against litellm 1.108.1 with a real
proxy against the fake upstream: compaction verified, streaming
intact). Stage-1 replay (`scripts/replay_v2.py`,
`reports/replay_v2_report.json`): 24/28 sessions trigger, ceiling
88.88% token-weighted reduction vs original (v1: 23.34%), 1,261
checklist items, 0 fallbacks with the echo stub, verifier control
passes. The ceiling assumes a summarizer that preserves exactly the
checklist and nothing else; the pilot measures whether a real one
can.

Stage-2 pilot **ran Nov 6, 2026** (`scripts/run_v2_pilot.py`,
`reports/v2_pilot_report.json`), approved at ~$0.25, real
summarizer `muse-spark-1.3-contributor` via the Meta Model API
Responses API: **measured spend $0.3769 (+51% over the estimate,
under the $0.50 cap)**. Verdicts on the pre-registered bars: VK1
first-attempt pass 15.46% (bar ≥ 80%) — HIT; VK2 mean net saving
+$0.0694/session — PASS; VK3 fallback 70.83% (bar ≤ 20%) — HIT;
VK5 median latency 36.7 s (bar ≤ 3 s) — HIT; pins 100%. **Outcome:
STOP or RESCOPE.** The fail-closed design held (17 sessions fell
back to v1, nothing unverified shipped). Diagnosis: the checklist
is dominated by generic key-value churn (zero labelled lines in
all 24 middles; up to 143 superseded values in one session) and
the retry names only failed item types, never the values, so the
model cannot fix its misses; small middles also got output caps
too small for a reasoning model (the smoke session's 803-token
cap produced no answer text at all).

**Rescope built Nov 6, 2026** (Yuriy's call: "Rescope v2, small
re-test first"): blocks over 30,000 est tokens are chunked, each
chunk with its own checklist and summary; superseded items capped
at 10 per block (most recent per key); the retry lists the failed
items with their values; the output cap is floored at 4096; the
VK5 bar is 60 s for the re-test (was 3 s). Stage-1 re-run with the
rescope: 71 blocks, 68 triggered, 1,091 items (was 1,261),
ceiling 88.75% (was 88.88%).

**Re-test ran Nov 6, 2026** (`reports/v2_retest_report.json`),
approved ~$0.15, cap $0.30: **measured spend $0.0877, under
estimate**. VK1 49.19% (bar 80%) — HIT; VK2 +$0.111/session —
PASS; VK3 fallback 33.33% (bar 20%) — HIT; VK5 median 46.2 s
(rescope bar 60 s) — PASS; pins 100%. The rescope moved VK1
15% → 49% and VK3 71% → 33%, but both bars are still missed
by a wide margin. Every failure is a checklist failure (no
parse, no errors, no timeouts), and only two item types fail:
latest values (11) and verbatim error lines (9). The smoke
session now passes on retry and the worst superseded session
went 4/4 chunks, but chunking regressed one session that had
passed whole. **Recommendation: STOP v2 as designed** (v1 is
the shippable piece); a targeted second rescope on the error
rule and cross-chunk item dedup is named in `SPEC_V2.md` §12
as the alternative. See `SPEC_V2.md` §9/§11.

## Publishing

This tree is a local git repo with no remote yet, committed and
push-ready. To publish (repo creation is a manual click):

1. Create the repo on GitHub: owner `jurayh`, name `servecompact`,
   Public, completely empty (no README, .gitignore, or license from
   GitHub).
2. From this directory:
   ```bash
   git remote add origin git@github.com:jurayh/servecompact.git
   git push -u origin main
   ```
3. Verify the remote tree matches local before announcing anything.
