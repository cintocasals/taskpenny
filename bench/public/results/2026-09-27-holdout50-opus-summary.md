# 50 new tasks against Claude Opus 5.5, default setup · 27 September 2026

Taskpenny v0.7.0 with no ceiling, budget $0.30 per task; baseline Claude Opus 5.5 in one call; judge Gemini 3.1 Pro. `python bench/public/summarize.py bench/public/results/2026-09-27-holdout50-opus.jsonl --ci`.

## By set

| Tasks | n | Taskpenny | One model | Taskpenny cost | As good or better | Wins / ties / losses | Time (Taskpenny / one model) |
|---|---|---|---|---|---|---|---|
| **All** | 48 | $0.606 | $2.733 | **78% less** | **50%** | 1 / 23 / 24 | 14.3 s / 28.5 s |
| Arena-Hard | 38 | $0.555 | $2.448 | **77% less** | **45%** | 1 / 16 / 21 | 14.5 s / 31.9 s |
| Four asks in one message | 10 | $0.051 | $0.285 | **82% less** | **70%** | 0 / 7 / 3 | 13.6 s / 15.3 s |

Jev was billed by the gateway in 50 of these 50 runs, at its list price ($0.042 per million input tokens): its cost is already in Taskpenny's column.

## With Jev at its list price ($0.042 per million input tokens)

Where Jev was free on Vercel AI Gateway (until 26 September 2026), at most its decisions would have added:

| Tasks | n | Taskpenny | Jev at list price, at most | One model | Taskpenny cost: free Jev → Jev at list price |
|---|---|---|---|---|---|
| **All** | 48 | $0.606 | $0.0000 | $2.733 | 77.8% less → **77.8% less** |
| Arena-Hard | 38 | $0.555 | $0.0000 | $2.448 | 77.3% less → **77.3% less** |
| Four asks in one message | 10 | $0.051 | $0.0000 | $0.285 | 82.2% less → **82.2% less** |
| Basic and standard (tier 1-2), one model | 25 | $0.048 | $0.0000 | $1.110 | 95.7% less → **95.7% less** |
| Advanced (tier 3), one model | 11 | $0.276 | $0.0000 | $1.178 | 76.5% less → **76.5% less** |
| Critical (tier 4), one model | 5 | $0.245 | $0.0000 | $0.248 | 1.2% less → **1.2% less** |
| Split into parts | 7 | $0.037 | $0.0000 | $0.198 | 81.1% less → **81.1% less** |

## By the route Taskpenny chose

| Tasks | n | Taskpenny | One model | Taskpenny cost | As good or better | Wins / ties / losses | Time (Taskpenny / one model) |
|---|---|---|---|---|---|---|---|
| Basic and standard (tier 1-2), one model | 25 | $0.048 | $1.110 | **96% less** | **48%** | 0 / 12 / 13 | 7.7 s / 21.6 s |
| Advanced (tier 3), one model | 11 | $0.276 | $1.178 | **77% less** | **27%** | 1 / 2 / 8 | 24.6 s / 53.8 s |
| Critical (tier 4), one model | 5 | $0.245 | $0.248 | **1% less** | **100%** | 0 / 5 / 0 | 26.3 s / 25.9 s |
| Split into parts | 7 | $0.037 | $0.198 | **81% less** | **57%** | 0 / 4 / 3 | 13.3 s / 15.1 s |

Average answer length in judged tasks: Taskpenny 3,520 characters, one model 5,028.

Left out because the baseline failed or gave no answer: h210, h235.

95% bootstrap intervals over the 48 compared tasks (20,000 resamples): saving with Jev at list price 77.8% (66.1 to 86.4); as good or better 50% (35 to 65).
