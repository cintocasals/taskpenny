# Public benchmark results · 25 September 2026

SIAC against **Claude Sonnet 5** answering every task on its own, on 143 tasks (142 compared: the baseline
failed on one). Judge: **Gemini 3.1 Pro**, from a third provider, comparing each pair twice with the order swapped.
Costs are the ones Vercel AI Gateway reported for each call. Code: commit `ada421c`.

## In one paragraph

On everyday requests, which were 103 of the 142 tasks, SIAC cost **93% less** than Sonnet 5 and its answer was
**as good or better in 76%** of them. Labelling customer messages cost 98% less with the same accuracy (96 of 100
right on both sides). On hard requests SIAC cost more than Sonnet 5: it sends critical tasks to Claude Opus, a
stronger and dearer model (those answers never lost to Sonnet's), and splitting requests into parts did not pay
off at advanced level. Over the whole set SIAC cost 14% less and was as good or better in 76% of the tasks.

## By the route SIAC chose

| Tasks | n | SIAC | Sonnet 5 alone | SIAC cost | As good or better | Wins / ties / losses | Time (SIAC / Sonnet) |
|---|---|---|---|---|---|---|---|
| Basic and standard (tier 1-2), one model | 103 | $0.046 | $0.684 | **93% less** | **76%** | 17 / 61 / 24 | 4.4 s / 7.7 s |
| Advanced (tier 3), one model | 16 | $0.221 | $0.252 | **12% less** | **69%** | 3 / 8 / 5 | 14.9 s / 15.9 s |
| Critical (tier 4), one model | 8 | $0.241 | $0.121 | **2.0x as much** | **100%** | 5 / 3 / 0 | 25.2 s / 18.8 s |
| Split into parts | 15 | $0.653 | $0.297 | **2.2x as much** | **73%** | 1 / 10 / 4 | 31.7 s / 22.5 s |

## By set

| Tasks | n | SIAC | Sonnet 5 alone | SIAC cost | As good or better | Wins / ties / losses | Time (SIAC / Sonnet) |
|---|---|---|---|---|---|---|---|
| **All** | 142 | $1.160 | $1.353 | **14% less** | **76%** | 26 / 82 / 33 | 9.7 s / 10.8 s |
| MT-Bench (8 categories) | 80 | $0.235 | $0.636 | **63% less** | **80%** | 16 / 48 / 16 | 6.2 s / 9.3 s |
| Arena-Hard | 14 | $0.402 | $0.306 | **1.3x as much** | **64%** | 4 / 5 / 5 | 21.4 s / 20.6 s |
| Four asks in one message | 10 | $0.079 | $0.113 | **30% less** | **90%** | 1 / 8 / 1 | 15.9 s / 12.6 s |
| Label 10 messages | 10 | $0.001 | $0.029 | **98% less** | **90%** | 1 / 8 / 1 | 2.3 s / 3.0 s |
| Catalan (ours) | 14 | $0.160 | $0.101 | **1.6x as much** | **43%** | 3 / 3 / 8 | 11.2 s / 11.7 s |
| Spanish (ours) | 14 | $0.285 | $0.168 | **1.7x as much** | **79%** | 1 / 10 / 2 | 17.2 s / 13.2 s |

Labelling against the true labels: SIAC 96 of 100, Sonnet 5 96 of 100.
One judge verdict could not be read (es08); it counts as not as good.

## Tuning: seen and unseen tasks

A first run on a 60 task core showed SIAC 66% cheaper and as good or better in 68%. Two general changes
followed (fuller answers with key steps, and tier 2 at least for maths and code), and the whole set was run
again. The 82 tasks outside the core were never looked at while tuning.

| Tasks | n | SIAC | Sonnet 5 alone | SIAC cost | As good or better |
|---|---|---|---|---|---|
| Core 60, before the changes | 60 | $0.202 | $0.591 | 66% less | 68% |
| Core 60, after | 60 | $0.301 | $0.591 | 49% less | 77% |
| The 82 unseen tasks, after | 82 | $0.860 | $0.762 | 1.1x as much | 76% |

Quality held on unseen tasks (76% against 77% on the core). Cost did not: the unseen tasks hold most of the hard,
Catalan and Spanish requests, where SIAC used Opus or split the work.

## How to read it

- **Ties dominate.** Most pairs were judged equal; "as good or better" counts wins and ties.
- **The judge likes longer answers.** On average SIAC wrote 2,644 characters and Sonnet 5 2,206. Many of SIAC's
  losses on basic tasks were short, correct answers against longer ones with extra explanation.
- **A person checks the judge.** Twenty pairs are being reviewed blind by a person; agreement with the judge
  will be added here.
- **Sonnet 5 is a strong, mid-priced baseline.** SIAC's catalog goes above it (Claude Opus for critical tasks), so
  on critical tasks SIAC spends more by design. Against Opus as the baseline the picture would change, but Opus
  refused most calls on the day.
- **Spend.** SIAC $1.16, Sonnet 5 $0.76 for the 82 new tasks (the 60 core answers were reused), judge $1.86.
  Vercel billed $0.46 more than the runner recorded; the likely cause, calls that timed out and were sent again,
  is fixed, and the runner now checks the real balance.

## What we are changing next

1. **A ceiling.** Let SIAC's strongest model be the model you use today, so a comparison with Sonnet 5 uses
   Sonnet 5 as SIAC's top tier too.
2. **Split only when it pays.** At advanced level the parts went to the same tier as the whole and wrote more:
   splitting should be limited to requests whose parts can go to cheaper tiers.
3. **Catalan.** The weakest set (43%): answers from the cheapest models were thin, and one showed an English
   line. Labels from Jev now appear in the request's language.
4. **Tier 1 answers** that are correct but bare lose to fuller ones; worth testing a slightly richer basic tier.

Changes will be checked on new tasks, not on these.

## Reproduce

```bash
python bench/public/build.py                     # rebuilds tasks.jsonl (sha256 b9d7a83f...)
python bench/public/run.py --baseline anthropic/claude-sonnet-5 --total-budget 5
python bench/public/summarize.py bench/results/public-live-<time>.jsonl
```

Raw results, with every answer and every judge note: `results/2026-09-25-full143.jsonl` and
`results/2026-09-25-core60-before.jsonl`.
