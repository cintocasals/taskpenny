# 50 new tasks against Claude Sonnet 5, Sonnet 5 as ceiling · 25 September 2026

Made with SIAC before the rename; regenerated on 27 September 2026 with `summarize.py --ci`. Raw results: `2026-09-25-holdout50.jsonl`.

## By set

| Tasks | n | Taskpenny | One model | Taskpenny cost | As good or better | Wins / ties / losses | Time (Taskpenny / one model) |
|---|---|---|---|---|---|---|---|
| **All** | 50 | $0.446 | $0.955 | **53% less** | **82%** | 11 / 30 / 9 | 14.8 s / 18.6 s |
| Arena-Hard | 40 | $0.400 | $0.840 | **52% less** | **80%** | 10 / 22 / 8 | 14.7 s / 20.0 s |
| Four asks in one message | 10 | $0.046 | $0.115 | **60% less** | **90%** | 1 / 8 / 1 | 15.5 s / 12.9 s |

## With Jev at its list price ($0.042 per million input tokens)

Where Jev was free on Vercel AI Gateway (until 26 September 2026), at most its decisions would have added:

| Tasks | n | Taskpenny | Jev at list price, at most | One model | Taskpenny cost: free Jev → Jev at list price |
|---|---|---|---|---|---|
| **All** | 50 | $0.446 | $0.0078 | $0.955 | 53.3% less → **52.5% less** |
| Arena-Hard | 40 | $0.400 | $0.0042 | $0.840 | 52.4% less → **51.9% less** |
| Four asks in one message | 10 | $0.046 | $0.0036 | $0.115 | 60.0% less → **56.9% less** |
| Basic and standard (tier 1-2), one model | 26 | $0.039 | $0.0031 | $0.384 | 90.0% less → **89.2% less** |
| Advanced (tier 3), one model | 13 | $0.289 | $0.0016 | $0.374 | 22.8% less → **22.4% less** |
| Critical (tier 4), one model | 5 | $0.088 | $0.0005 | $0.130 | 32.2% less → **31.8% less** |
| Split into parts | 6 | $0.030 | $0.0026 | $0.066 | 54.2% less → **50.3% less** |

## By the route Taskpenny chose

| Tasks | n | Taskpenny | One model | Taskpenny cost | As good or better | Wins / ties / losses | Time (Taskpenny / one model) |
|---|---|---|---|---|---|---|---|
| Basic and standard (tier 1-2), one model | 26 | $0.039 | $0.384 | **90% less** | **96%** | 8 / 17 / 1 | 8.9 s / 14.5 s |
| Advanced (tier 3), one model | 13 | $0.289 | $0.374 | **23% less** | **62%** | 2 / 6 / 5 | 23.6 s / 27.8 s |
| Critical (tier 4), one model | 5 | $0.088 | $0.130 | **32% less** | **60%** | 0 / 3 / 2 | 19.8 s / 23.6 s |
| Split into parts | 6 | $0.030 | $0.066 | **54% less** | **83%** | 1 / 4 / 1 | 17.2 s / 11.9 s |

Average answer length in judged tasks: Taskpenny 3,384 characters, one model 3,874.

95% bootstrap intervals over the 50 compared tasks (20,000 resamples): saving with Jev at list price 52.5% (38.9 to 64.4); as good or better 82% (70 to 92).
