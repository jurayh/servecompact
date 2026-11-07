# ServeCompact v1 — pre-registered kill criteria

Written **before** the replay and fixture runs. The bars below do not move
after the numbers land. If a bar is hit, the verdict is STOP or RESCOPE,
not a retune-and-rerun until it passes.

## The bars

| # | Bar | Verdict if hit |
|---|-----|----------------|
| K1 | Fallback rate across replay requests **> 10%** | STOP or RESCOPE: the compactor keeps missing pins, so most requests would go out uncompacted anyway. |
| K2 | Token-weighted mean reduction across the replay set **< 20%** | STOP or RESCOPE: the saving is not worth a proxy in the path. Token-weighted = total tokens saved / total original tokens, because cost is token-weighted by nature. The unweighted per-session mean is reported alongside, and if it is < 20% while the weighted mean passes, that gap is reported as a caveat, not hidden. |
| K3 | Any fixture task that passes **without** the proxy but fails **with** it | STOP or RESCOPE: a token saving that breaks tasks is a failure, not a win. |

## What counts as what

- **Replay set** = all 28 public trace-commons Claude Code sessions in
  `~/workspace/user/corpus/public/trace-commons/sessions/claude_code/`
  (a public proxy corpus, NOT Yuriy's own sessions — this VM has none),
  converted to OpenAI chat messages, each prepended with the same 5
  planted pinned canaries (one per type: safety, hard-constraint,
  decision, goal, fact), plus 4 hand-built synthetic sessions that
  exercise supersession, dedup, correction, and verbose-output paths
  the real traces may not contain. Synthetic sessions are reported
  separately and are NOT folded into the K2 denominator.
- **Pin survival** is reported at two points, never conflated:
  - survival in the *compacted* prompt (what the compactor preserved),
  - survival in the *sent* prompt (what actually went out; 100% by
    construction when the verifier works, because a miss triggers
    fallback to the original). A sent-prompt survival below 100% means
    the verifier itself is broken and is a K1-class failure.
- **Tokens are estimates** everywhere in v1: `ceil(chars / 4)` per message
  content plus tool-call name/arguments, because no tokenizer is
  installed in this environment. Every token figure in every report is
  printed with the word "estimate" and the method next to it. Measured
  figures and estimates are never printed apart.
- **Cost saved** is computed at the configured rate in `rates.json`
  ($3.00 per 1M input tokens, labelled as a configured example, not a
  live provider quote). It is illustrative until the user substitutes
  their provider's real rate.
- **Fixture tasks** are a deterministic information-preservation harness,
  not an LLM eval: a small solver reads the (possibly compacted) prompt
  and must return the expected answer. It gates information loss, not
  model behaviour. A real-model A/B eval is follow-up work that needs
  its own paid-API approval (v1 is $0 by design) and is NOT claimed here.
- **Latency delta** is reported as compaction overhead in milliseconds
  plus upstream time where a proxy run exists. No latency *saving* is
  claimed: a smaller prompt may reduce time-to-first-token at some
  providers, but that is unmeasured here.

## Out of scope for v1 (not silently added later)

Multi-provider routing, prompt-cache optimization, any hosted component,
and any model-based summarizer. The summarizer is v2 and, under the
standing paid-API rule, gets an estimate-first approval before any paid
run.
