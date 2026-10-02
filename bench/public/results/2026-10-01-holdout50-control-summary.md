## By set

| Tasks | n | Taskpenny | One model | Taskpenny cost | As good or better | Wins / ties / losses | Time (Taskpenny / one model) |
|---|---|---|---|---|---|---|---|
| **All** | 50 | $0.446 | $0.735 | **39% less** | **74%** | 11 / 26 / 13 | 14.8 s / 13.9 s |
| Arena-Hard | 40 | $0.400 | $0.633 | **37% less** | **72%** | 10 / 19 / 11 | 14.7 s / 14.8 s |
| Four asks in one message | 10 | $0.046 | $0.102 | **55% less** | **80%** | 1 / 7 / 2 | 15.5 s / 10.5 s |

## With Jev at its list price ($0.042 per million input tokens)

Where Jev was free on Vercel AI Gateway (until 26 September 2026), at most its decisions would have added:

| Tasks | n | Taskpenny | Jev at list price, at most | One model | Taskpenny cost: free Jev → Jev at list price |
|---|---|---|---|---|---|
| **All** | 50 | $0.446 | $0.0078 | $0.735 | 39.3% less → **38.3% less** |
| Arena-Hard | 40 | $0.400 | $0.0042 | $0.633 | 36.9% less → **36.2% less** |
| Four asks in one message | 10 | $0.046 | $0.0036 | $0.102 | 54.6% less → **51.1% less** |
| Basic and standard (tier 1-2), one model | 26 | $0.039 | $0.0031 | $0.338 | 88.6% less → **87.7% less** |
| Advanced (tier 3), one model | 13 | $0.289 | $0.0016 | $0.268 | 1.08x → **1.08x as much** |
| Critical (tier 4), one model | 5 | $0.088 | $0.0005 | $0.072 | 1.23x → **1.23x as much** |
| Split into parts | 6 | $0.030 | $0.0026 | $0.058 | 47.4% less → **42.9% less** |

## By the route Taskpenny chose

| Tasks | n | Taskpenny | One model | Taskpenny cost | As good or better | Wins / ties / losses | Time (Taskpenny / one model) |
|---|---|---|---|---|---|---|---|
| Basic and standard (tier 1-2), one model | 26 | $0.039 | $0.338 | **89% less** | **77%** | 7 / 13 / 6 | 8.9 s / 12.0 s |
| Advanced (tier 3), one model | 13 | $0.289 | $0.268 | **1.1x as much** | **69%** | 2 / 7 / 4 | 23.6 s / 19.9 s |
| Critical (tier 4), one model | 5 | $0.088 | $0.072 | **1.2x as much** | **80%** | 1 / 3 / 1 | 19.8 s / 13.9 s |
| Split into parts | 6 | $0.030 | $0.058 | **47% less** | **67%** | 1 / 3 / 2 | 17.2 s / 9.4 s |

Average answer length in judged tasks: Taskpenny 3,384 characters, one model 3,415.

95% bootstrap intervals over the 50 compared tasks (20,000 resamples): saving with Jev at list price 38.3% (19.4 to 56.1); as good or better 74% (62 to 86).
