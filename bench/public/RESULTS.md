# Public benchmark results

Three runs, all judged by **Gemini 3.1 Pro** (a third provider), comparing each pair twice with the order swapped;
all costs are the ones Vercel AI Gateway reported for each call.

| Run | Tasks | Against | Taskpenny cost | As good or better |
|---|---|---|---|---|
| 27 September 2026, v0.7.0, default setup | 50 new (48 compared) | **Claude Opus 5.5** | **78% less** (95% interval 66 to 86) | **50%** (35 to 65) |
| 25 September 2026, Sonnet 5 as ceiling | the same 50 new | Claude Sonnet 5 | **52.5% less** (39 to 64) | **82%** (70 to 92) |
| 25 September 2026, first run, no ceiling | 143 (142 compared) | Claude Sonnet 5 | 13% less | 76% |

The intervals come from resampling the tasks (`summarize.py --ci`): fifty tasks give a direction, not a
precise figure. The 50 new tasks are English only: 40 Arena-Hard prompts and 10 requests with four asks each.

## Against Claude Opus 5.5 · 27 September 2026

Taskpenny v0.7.0 in its default setup (no ceiling, so critical tasks go to Opus 5.5 itself), against Claude Opus
5.5 answering each task in one call, on the 50 new tasks (`tasks-holdout.jsonl`). Opus gave no answer on two
(h210, h235), which are left out. Jev was billed at its list price in every run; its cost ($0.008 in all) is in
Taskpenny's column. Raw results: `results/2026-09-27-holdout50-opus.jsonl`.

| Tasks | n | Taskpenny | Opus 5.5 alone | Taskpenny cost | As good or better | Wins / ties / losses |
|---|---|---|---|---|---|---|
| **All** | 48 | $0.606 | $2.733 | **78% less** | **50%** | 1 / 23 / 24 |
| Arena-Hard (new sample) | 38 | $0.555 | $2.448 | **77% less** | **45%** | 1 / 16 / 21 |
| Four asks in one message | 10 | $0.051 | $0.285 | **82% less** | **70%** | 0 / 7 / 3 |
| Basic and standard (tier 1-2), one model | 25 | $0.048 | $1.110 | **96% less** | **48%** | 0 / 12 / 13 |
| Advanced (tier 3), one model | 11 | $0.276 | $1.178 | **77% less** | **27%** | 1 / 2 / 8 |
| Critical (tier 4), one model: Opus 5.5 too | 5 | $0.245 | $0.248 | same | **100%** | 0 / 5 / 0 |
| Split into parts | 7 | $0.037 | $0.198 | **81% less** | **57%** | 0 / 4 / 3 |

**What it says.** Against Opus 5.5, Taskpenny's cheap tiers cost a small fraction, but their answers were judged
worse about half the time: Opus wrote longer, more thorough answers (5,028 characters on average, against
Taskpenny's 3,520), and the judge preferred them. Against Sonnet 5 on the same tasks the same cheap tiers held up
(96% as good or better on basic and standard tasks). So Taskpenny saves most of an Opus bill, and matches a
Sonnet-class model, but on hard prompts it does not match Opus. If you need Opus quality on every answer,
Taskpenny as it is today is not the tool: its saving comes from the cheap tiers, and those are what lose to Opus
here. A setting that sends more of the work to the strong tiers is the next thing we want to measure.

Of the 23 ties, 15 had both orders say "tie" and 8 had the two orders disagree. Spend: Taskpenny $0.66, Opus
$3.05, the judge $0.89 (all 50 tasks, before leaving two out: one of them cost Opus $0.32 for an empty
answer); Vercel billed what the runner recorded, to within a cent.

## The first two runs · 25 September 2026

Taskpenny against **Claude Sonnet 5** answering every task on its own, on 143 tasks (142 compared: the baseline
failed on one). Jev was free on the gateway during these runs, so the tables below count it at $0; [with Jev at
its list price](#if-jev-is-paid-at-its-list-price) the same figures drop by up to 3.1 points. Code: commit
`ada421c`.

Taskpenny was called SIAC when these runs were made, so the raw files use that name; the scripts read them as
they are.

### In one paragraph

On everyday requests, which were 103 of the 142 tasks, Taskpenny cost **93% less** than Sonnet 5 and its answer was
**as good or better in 76%** of them. Labelling customer messages cost 98% less with the same accuracy (96 of 100
right on both sides). On hard requests Taskpenny cost more than Sonnet 5: it sends critical tasks to Claude Opus, a
stronger and dearer model (those answers never lost to Sonnet's), and splitting requests into parts did not pay
off at advanced level. Over the whole set Taskpenny cost 14% less and was as good or better in 76% of the tasks.

### By the route Taskpenny chose

| Tasks | n | Taskpenny | Sonnet 5 alone | Taskpenny cost | As good or better | Wins / ties / losses | Time (Taskpenny / Sonnet) |
|---|---|---|---|---|---|---|---|
| Basic and standard (tier 1-2), one model | 103 | $0.046 | $0.684 | **93% less** | **76%** | 17 / 61 / 24 | 4.4 s / 7.7 s |
| Advanced (tier 3), one model | 16 | $0.221 | $0.252 | **12% less** | **69%** | 3 / 8 / 5 | 14.9 s / 15.9 s |
| Critical (tier 4), one model | 8 | $0.241 | $0.121 | **2.0x as much** | **100%** | 5 / 3 / 0 | 25.2 s / 18.8 s |
| Split into parts | 15 | $0.653 | $0.297 | **2.2x as much** | **73%** | 1 / 10 / 4 | 31.7 s / 22.5 s |

### By set

| Tasks | n | Taskpenny | Sonnet 5 alone | Taskpenny cost | As good or better | Wins / ties / losses | Time (Taskpenny / Sonnet) |
|---|---|---|---|---|---|---|---|
| **All** | 142 | $1.160 | $1.353 | **14% less** | **76%** | 26 / 82 / 33 | 9.7 s / 10.8 s |
| MT-Bench (8 categories) | 80 | $0.235 | $0.636 | **63% less** | **80%** | 16 / 48 / 16 | 6.2 s / 9.3 s |
| Arena-Hard | 14 | $0.402 | $0.306 | **1.3x as much** | **64%** | 4 / 5 / 5 | 21.4 s / 20.6 s |
| Four asks in one message | 10 | $0.079 | $0.113 | **30% less** | **90%** | 1 / 8 / 1 | 15.9 s / 12.6 s |
| Label 10 messages | 10 | $0.001 | $0.029 | **98% less** | **90%** | 1 / 8 / 1 | 2.3 s / 3.0 s |
| Catalan (ours) | 14 | $0.160 | $0.101 | **1.6x as much** | **43%** | 3 / 3 / 8 | 11.2 s / 11.7 s |
| Spanish (ours) | 14 | $0.285 | $0.168 | **1.7x as much** | **79%** | 1 / 10 / 2 | 17.2 s / 13.2 s |

Labelling against the true labels: Taskpenny 96 of 100, Sonnet 5 96 of 100.
One judge verdict could not be read (es08); it counts as not as good, so in its rows wins, ties and losses add up
to one less than n. Two of Taskpenny's runs stopped at the budget before finishing (ah01 and es10); they are
counted as they are.

### Tuning: the core and the other tasks

A first run on a 60-task core showed Taskpenny 66% cheaper and as good or better in 68%. Two general changes
followed (fuller answers with key steps, and tier 2 at least for maths and code), and the whole set was run
again. Of the 82 compared tasks outside the core, 59 come from outside sources and were never looked at while
tuning; the other 23 are our own Catalan and Spanish development cases, which were used to calibrate Jev's gate.

| Tasks | n | Taskpenny | Sonnet 5 alone | Taskpenny cost | As good or better |
|---|---|---|---|---|---|
| Core 60, before the changes | 60 | $0.202 | $0.591 | 66% less | 68% |
| Core 60, after | 60 | $0.301 | $0.591 | 49% less | 77% |
| The other 82, after | 82 | $0.860 | $0.762 | 1.1x as much | 76% |
| of which from outside sources | 59 | $0.488 | $0.560 | 13% less | 83% |
| of which our own development cases | 23 | $0.372 | $0.202 | 1.8x as much | 57% |

Quality held on the tasks from outside sources (83% against 77% on the core). Cost did not: the other tasks hold
most of the hard, Catalan and Spanish requests, where Taskpenny used Opus or split the work.

## How to read all of it

- **Ties dominate.** Most pairs were judged equal; "as good or better" counts wins and ties. A tie includes the
  pairs where the two orders disagreed.
- **The judge likes longer answers.** In the first run Taskpenny wrote 2,644 characters on average and Sonnet 5
  2,206; against Opus, Taskpenny wrote 3,520 and Opus 5,028. Many of Taskpenny's losses on basic tasks were short,
  correct answers against longer ones with extra explanation.
- **Taskpenny's workers get an instruction; the baseline does not.** After the 60-task core showed the judge's
  taste for fuller answers, Taskpenny's workers were told to "show the key steps briefly" and match the depth the
  request needs; the baseline answers with no system message. The new tasks protect against tuning to particular
  tasks, not against tuning to the judge's taste. The control is `run.py --baseline-system worker`, which gives the
  baseline the same instruction; it has not been run yet (about $1.50 for the 50 new tasks against Sonnet 5).
- **Taskpenny limits hidden reasoning; the baseline does not.** Reasoning tokens are billed as output. Taskpenny
  turns hidden reasoning off on tiers 1 and 2 and sets it to low on tiers 3 and 4 (`Limits.reasoning` in
  `engine.py`); the baseline runs with each model's default. Part of the cost gap therefore comes from that setting
  and not from routing. The fair control is the baseline with the same instruction and the same reasoning setting,
  reusing Taskpenny's answers and the same judge; it has not been run yet, and the figures above will be updated
  when it is.
- **The judge's provider.** Gemini is from a third provider, but Taskpenny's light planner is Gemini 3.8 Flash, so
  Gemini wrote the plans below tier 4. No final answer was written by Gemini: in the first run the work went to
  DeepSeek (98 calls), Anthropic (45) and OpenAI (45).
- **A person checked 20 pairs blind, with three limits.** The project's author voted on 20 pairs from the 60-task
  core without knowing which answer was Taskpenny's. He found most pairs hard to tell apart. He rated Taskpenny as
  good or better in 18 of 20, where the judge said 14, and the two gave the same verdict on 6. In the four pairs
  where they disagreed outright, Taskpenny's answer was the short one: the judge preferred Sonnet's longer answer,
  the author preferred Taskpenny's because it did only what was asked. The limits: the author has an interest in
  the result; the pairs come from the version before the two tuning changes, not from the one published; and
  length gives Taskpenny away (in those four pairs its answers had 38 to 286 characters, Sonnet's 678 to 1,487).
  Twenty votes from one person are too few to correct the judge, so the figures here are the judge's. Votes and
  key: `results/2026-09-25-blind20.json`.
- **Which baseline.** Sonnet 5 is a strong, mid-priced model; Opus 5.5 is the one many professionals use for
  everything. On 25 September Opus refused most calls, so both runs of that day used Sonnet 5; on 27 September
  Opus answered, and the first table above is against it.
- **Spend on the first run.** Taskpenny answered all 142 tasks again ($1.16); Sonnet 5 answered only the 82 outside
  the core ($0.76), since its 60 core answers were reused from the core run; the judge cost $1.86. Vercel billed
  $0.46 more than the runner recorded. The most likely cause is task ah15: the Sonnet 5 baseline got no answer in
  362 seconds over three tries of 120 seconds, so the runner recorded nothing while the provider probably billed
  the calls; that task is left out of the tables, so the comparison is not affected. The runner no longer repeats
  a call that timed out, and checks the real balance.

### Checked on 50 new tasks, after two changes

Two changes followed this report: a **ceiling** (Taskpenny's strongest model can be set to the model you would use
anyway, here Sonnet 5) and **splitting only when it pays** (Jev gates every part first; if the parts would not
go to clearly cheaper models, the request is done in one go). They were checked on 50 tasks Taskpenny had never seen:
40 more Arena-Hard prompts and 10 new four-part requests (`tasks-holdout.jsonl`, same sources and seeds recorded),
against Sonnet 5 with the same judge. Code: commit `f567a7c`. As good or better in 82% (95% interval 70 to 92);
52.5% cheaper with Jev at its list price (39 to 64).

| Tasks | n | Taskpenny | Sonnet 5 alone | Taskpenny cost | As good or better | Wins / ties / losses |
|---|---|---|---|---|---|---|
| **All 50 new tasks** | 50 | $0.446 | $0.955 | **53% less** | **82%** | 11 / 30 / 9 |
| Arena-Hard (new sample) | 40 | $0.400 | $0.840 | **52% less** | **80%** | 10 / 22 / 8 |
| Four asks in one message (new) | 10 | $0.046 | $0.115 | **60% less** | **90%** | 1 / 8 / 1 |
| Basic and standard (tier 1-2) | 26 | $0.039 | $0.384 | **90% less** | **96%** | 8 / 17 / 1 |
| Advanced (tier 3) | 13 | $0.289 | $0.374 | 23% less | 62% | 2 / 6 / 5 |
| Critical (tier 4, capped at Sonnet 5) | 5 | $0.088 | $0.130 | 32% less | 60% | 0 / 3 / 2 |
| Split into parts | 6 | $0.030 | $0.066 | 54% less | 83% | 1 / 4 / 1 |

On Arena-Hard, where the first run (14 other prompts, no ceiling) cost 1.3 times as much with 64% as good or
better, Taskpenny now costs about half and is as good or better in 80%. The samples differ, so read this as a direction,
not a precise gain. Five of eleven plans were dropped because splitting would not have paid. Vercel billed
exactly what the runner recorded ($2.27 in total, judge included). Raw results:
`results/2026-09-25-holdout50.jsonl`.

### If Jev is paid at its list price

Jev, the model that makes Taskpenny's decisions, was free on Vercel AI Gateway during the runs of 25 September (its list price
is $0.042 per million input tokens; output is free). Anyone who tries Taskpenny may pay it, so here are the
same costs with Jev at its list price. The receipts count every input token; those not spent by a language
model went to Jev or to the planner, so these are upper bounds. `summarize.py` prints them.

| Tasks | n | Jev at list price, at most | Taskpenny cost against Sonnet 5: free Jev → Jev paid |
|---|---|---|---|
| **The 50 new tasks** | 50 | $0.0078 | 53.3% less → **52.5% less** |
| Arena-Hard (new sample) | 40 | $0.0042 | 52.4% less → **51.9% less** |
| Four asks in one message (new) | 10 | $0.0036 | 60.0% less → **56.9% less** |
| Basic and standard (tier 1-2), new tasks | 26 | $0.0031 | 90.0% less → **89.2% less** |
| **First run, all tasks** | 142 | $0.0180 | 14.2% less → **12.9% less** |
| Basic and standard (tier 1-2), first run | 103 | $0.0077 | 93.3% less → **92.2% less** |
| Advanced (tier 3), first run | 16 | $0.0016 | 12.1% less → **11.4% less** |
| Critical (tier 4), first run | 8 | $0.0009 | 1.99x → **2.00x as much** |
| Split into parts, first run | 15 | $0.0079 | 2.20x → **2.22x as much** |
| Label 10 messages | 10 | $0.0007 | 97.9% less → **95.3% less** |

Jev matters most where the language models are cheapest: labelling and requests split into parts.

## What changed after each run, and what is next

1. **A ceiling.** Done after the first run (`TASKPENNY_CEILING`, `--ceiling`); checked on the 50 new tasks.
2. **Split only when it pays.** Done after the first run; checked on the 50 new tasks.
3. **Catalan.** The weakest set (43%): answers from the cheapest models were thin, and one showed an English
   line. Labels from Jev now appear in the request's language. Not measured again yet.
4. **Against Opus.** The cheap tiers lose to Opus on hard prompts about half the time. Next: measure a setting
   that sends more of the work to the strong tiers, and the control for the judge's taste.

Changes will be checked on new tasks, not on these.

## Reproduce

```bash
python bench/public/build.py                     # rebuilds tasks.jsonl (sha256 b9d7a83f...)
python bench/public/build.py --holdout           # rebuilds tasks-holdout.jsonl (sha256 2b032d88...)
python bench/public/run.py --baseline anthropic/claude-sonnet-5 --total-budget 5                      # first run
python bench/public/run.py --tasks bench/public/tasks-holdout.jsonl --ceiling anthropic/claude-sonnet-5 --total-budget 2
python bench/public/run.py --tasks bench/public/tasks-holdout.jsonl --total-budget 5                  # against Opus 5.5
python bench/public/summarize.py bench/results/public-live-<time>.jsonl --ci
```

Raw results, with every answer and every judge note: `results/2026-09-27-holdout50-opus.jsonl`,
`results/2026-09-25-holdout50.jsonl`, `results/2026-09-25-full143.jsonl` and
`results/2026-09-25-core60-before.jsonl`. The task texts keep their licences: see [LICENSES.md](LICENSES.md).
