# ServeCompact replay table

Tokens are estimate (chars/4, no tokenizer installed). Cost at configured rate $3.00 per 1M input tokens (profile: configured-example); not a live provider quote.

## Trace-commons sessions (real shapes, planted canaries)

| session | msgs | tokens in (est) | tokens out (est) | saved (est) | reduction | fallback | pins surviving (compacted) |
|---|---|---|---|---|---|---|---|
| 07b57159 | 480 | 77022 | 67277 | 9745 | 12.65% | no | 6/6 |
| 09d9abe9 | 429 | 66378 | 46339 | 20039 | 30.19% | no | 6/6 |
| 0ca72c0c | 67 | 15564 | 14658 | 906 | 5.82% | no | 6/6 |
| 11ef2190 | 41 | 7509 | 7509 | 0 | 0.0% | no | 6/6 |
| 17c2cf98 | 572 | 183378 | 126961 | 56417 | 30.77% | no | 6/6 |
| 2965219c | 185 | 36062 | 26407 | 9655 | 26.77% | no | 6/6 |
| 39082932 | 937 | 175905 | 134671 | 41234 | 23.44% | no | 6/6 |
| 3be5ba0c | 908 | 214669 | 152646 | 62023 | 28.89% | no | 6/6 |
| 4130c9a7 | 174 | 68083 | 68083 | 0 | 0.0% | no | 6/6 |
| 4222016d | 863 | 203300 | 135337 | 67963 | 33.43% | no | 6/6 |
| 479c5e9f | 276 | 88821 | 71107 | 17714 | 19.94% | no | 6/6 |
| 47d9fc04 | 850 | 156829 | 132524 | 24305 | 15.5% | no | 6/6 |
| 4c09dfa9 | 3 | 6153 | 6153 | 0 | 0.0% | no | 6/6 |
| 674e0f5c | 30 | 3514 | 3329 | 185 | 5.26% | no | 6/6 |
| 6a7964ed | 416 | 105842 | 81954 | 23888 | 22.57% | no | 6/6 |
| 7563bddf | 622 | 117330 | 82402 | 34928 | 29.77% | no | 6/6 |
| 860618e1 | 698 | 135544 | 105369 | 30175 | 22.26% | no | 6/6 |
| 89cf9e13 | 163 | 22438 | 19350 | 3088 | 13.76% | no | 6/6 |
| 92833a43 | 164 | 43794 | 34111 | 9683 | 22.11% | no | 6/6 |
| a20a4070 | 744 | 203779 | 126310 | 77469 | 38.02% | no | 6/6 |
| ba1b4916 | 122 | 51154 | 45349 | 5805 | 11.35% | no | 6/6 |
| c84c0ad3 | 342 | 60073 | 55308 | 4765 | 7.93% | no | 6/6 |
| c99032e9 | 891 | 202675 | 163261 | 39414 | 19.45% | no | 6/6 |
| comparia | 16 | 5498 | 5498 | 0 | 0.0% | no | 6/6 |
| d4c4d47d | 400 | 66318 | 51487 | 14831 | 22.36% | no | 6/6 |
| da5d32d6 | 1626 | 176556 | 157896 | 18660 | 10.57% | no | 6/6 |
| da6566ff | 37 | 6655 | 6655 | 0 | 0.0% | no | 6/6 |
| f93127ef | 146 | 45463 | 24004 | 21459 | 47.2% | no | 6/6 |

**Trace total: 2546306 -> 1951955 tokens (est), saved 594351 (est), token-weighted reduction 23.34%, fallback rate 0.0%, cost saved $1.7831 [configured rate $3.00 per 1M input tokens (profile: configured-example); not a live provider quote].**
Unweighted mean reduction: 17.86%.

## Synthetic sessions (reported separately)

| session | tokens in (est) | tokens out (est) | reduction | fallback |
|---|---|---|---|---|
| synthetic-1-deploy-debug | 4100 | 840 | 79.51% | no |
| synthetic-2-verbose-only | 11180 | 1933 | 82.71% | no |
| synthetic-3-repeat-heavy | 171 | 171 | 0.0% | no |
| synthetic-4-small-clean | 127 | 127 | 0.0% | no |

**Synthetic total: 15578 -> 3071 tokens (est), reduction 80.29%.**

**Control (verifier not vacuous):** canary removed=True, verifier caught miss=True, missing-pin log carries no pin text=True.

**Verdict: PASS: no pre-registered kill bar hit; proceed to fixture gate (K3) and then the live demo**
