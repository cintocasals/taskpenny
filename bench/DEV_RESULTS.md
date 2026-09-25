# Development results (v0.1, not the public benchmark)

These numbers come from 42 requests we wrote ourselves while building SIAC (14 in English, 14 in Catalan,
14 in Spanish; `bench/dev_cases.jsonl`). They were run live through Vercel AI Gateway on 2026-09-25 with the
settings in this repository. They tell us whether the router behaves as designed. They are **not** a claim
about answer quality: that is what the public benchmark (roadmap step 3) will measure, with outside task
sets and blind grading.

## What we measured

| | Result |
|---|---|
| Split decision matches what we expected | 41 of 42 |
| Answer type (text, choice, yes/no, score) matches | 41 of 42 |
| Tier chosen by Jev matches ours exactly | 37 of 42 (4 higher, 1 lower) |
| Runs finished | 41 done, 1 partial (it hit its $0.15 budget cap, as designed) |
| Total cost with SIAC | $0.56 |
| Estimated cost with one strong model (Claude Opus 5.5) | $0.95 |
| Saving | 41% |

By kind of request:

| Kind | Cases | SIAC | One strong model (estimate) | Saving | Average time |
|---|---|---|---|---|---|
| Tier 1 (basic) | 16 | $0.0006 | $0.0108 | 95% | 2.6 s |
| Tier 2 (standard) | 12 | $0.0106 | $0.0439 | 76% | 7.5 s |
| Tier 3 (advanced) | 2 | $0.0278 | $0.0410 | 32% | 17.2 s |
| Tier 4 (critical) | 5 | $0.0994 | $0.1548 | 36% | 28.2 s |
| Split into subtasks | 7 | $0.4231 | $0.7028 | 40% | 45.4 s |

## How to read it

- **The saving is largest where most everyday requests are**: short and standard tasks go to models that cost
  a tiny fraction of a frontier model, and Jev's checks cost almost nothing.
- **Hard single tasks save less** because they still go to a strong model; the saving there comes from
  Jev's decisions replacing model calls and from not retrying at a higher price.
- **Split requests save about 40%** today. Planning is where it pays to spend, so critical requests get a
  strong planner; parts go to the cheapest model that can do each one, and choice parts go to Jev.
- **The one-model figure is an estimate**: the same request sent once to the baseline model, with an answer as
  long as SIAC's, scaled by the hidden reasoning SIAC's own models used, and no retries. A real run of the
  baseline can cost more or less. The public benchmark runs the baseline for real.

## What we changed because of these runs

Our first live runs cost too much. We found five causes and fixed them before the numbers above:

1. A fallback from one tier 4 model could land on a model with a much higher price. Fallbacks now stay
   within 1.5x of the price of the first choice, and then go down a tier instead of up.
2. The assembly step rewrote the whole answer with an expensive model. It now writes a short outline and the
   parts are stitched in code.
3. The final check rewrote weak answers one tier up. Now a cheap model only fills what the check found
   missing.
4. Short requests were sometimes split. Requests under 160 characters, and tier 1 requests, are never split.
5. Hidden reasoning could use all the output room and return an empty answer. Reasoning is now off for tiers
   1 and 2 and low for 3 and 4, tier 4 has more output room, and an empty answer moves on to the next model.

## Known limits

- The dev set is small and written by us, so it fits what we built. The public benchmark will use outside
  task sets.
- Access to some frontier models is limited at times (the gateway answers "no access to this model at this
  time"); SIAC then falls back as described above.
- Two cases disagreed with our labels: six invoices to classify and total were done in one call at tier 1
  instead of being split (the answer was right, and cheaper), and a choice between three hosting offers was
  answered as a written analysis at tier 3.

Raw results: `bench/dev-summary.json`.
