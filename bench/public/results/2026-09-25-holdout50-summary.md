## By set

| Tasks | n | SIAC | One model | SIAC cost | As good or better | Wins / ties / losses | Time (SIAC / one model) |
|---|---|---|---|---|---|---|---|
| **All** | 50 | $0.446 | $0.955 | **53% less** | **82%** | 11 / 30 / 9 | 14.8 s / 18.6 s |
| Arena-Hard | 40 | $0.400 | $0.840 | **52% less** | **80%** | 10 / 22 / 8 | 14.7 s / 20.0 s |
| Four asks in one message | 10 | $0.046 | $0.115 | **60% less** | **90%** | 1 / 8 / 1 | 15.5 s / 12.9 s |

## By the route SIAC chose

| Tasks | n | SIAC | One model | SIAC cost | As good or better | Wins / ties / losses | Time (SIAC / one model) |
|---|---|---|---|---|---|---|---|
| Basic and standard (tier 1-2), one model | 26 | $0.039 | $0.384 | **90% less** | **96%** | 8 / 17 / 1 | 8.9 s / 14.5 s |
| Advanced (tier 3), one model | 13 | $0.289 | $0.374 | **23% less** | **62%** | 2 / 6 / 5 | 23.6 s / 27.8 s |
| Critical (tier 4), one model | 5 | $0.088 | $0.130 | **32% less** | **60%** | 0 / 3 / 2 | 19.8 s / 23.6 s |
| Split into parts | 6 | $0.030 | $0.066 | **54% less** | **83%** | 1 / 4 / 1 | 17.2 s / 11.9 s |

## Tasks used to tune SIAC and tasks it never saw

| Tasks | n | SIAC | One model | SIAC cost | As good or better | Wins / ties / losses | Time (SIAC / one model) |
|---|---|---|---|---|---|---|---|
| Core 60 (seen while tuning) | 0 | $0.000 | $0.000 | **0% less** | **0%** | 0 / 0 / 0 | 0.0 s / 0.0 s |
| Other tasks (never seen) | 50 | $0.446 | $0.955 | **53% less** | **82%** | 11 / 30 / 9 | 14.8 s / 18.6 s |

Average answer length in judged tasks: SIAC 3,384 characters, one model 3,874.
