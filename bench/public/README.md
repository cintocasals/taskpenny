# Taskpenny public benchmark

Does Taskpenny give answers as good as one strong model, for less money? This folder answers that with public tasks,
real costs and a method anyone can rerun.

## The tasks

Two task files, both rebuilt byte for byte from pinned sources by `build.py` (each source is checked against its
SHA-256, and the output hash is printed):

| File | Tasks | Rebuild | SHA-256 |
|---|---|---|---|
| `tasks.jsonl` | 143: the first benchmark, whose 60-task core was used while tuning | `python bench/public/build.py` | `b9d7a83f…81f20` |
| `tasks-holdout.jsonl` | 50 new ones (40 Arena-Hard, 10 multi-part), never used before the changes they check, and sharing no prompt, Arena-Hard id or Dolly row with `tasks.jsonl` | `python bench/public/build.py --holdout` | `2b032d88…c7df` |

| Set | Tasks | What | Source | License |
|---|---|---|---|---|
| `mtbench` | 80 | Everyday requests in 8 categories: writing, roleplay, reasoning, maths, coding, extraction, science, humanities (first turn only) | [MT-Bench](https://github.com/lm-sys/FastChat/tree/main/fastchat/llm_judge), lm-sys/FastChat | Apache 2.0 |
| `hard` | 15 | Hard real user prompts, random sample (seed 2026) | [Arena-Hard-Auto v0.1](https://github.com/lmarena/arena-hard-auto) | Apache 2.0 |
| `multi` | 10 | Four different asks in one message (a question about a text, ideas, a short piece of writing, a sorting job) | [databricks-dolly-15k](https://huggingface.co/datasets/databricks/databricks-dolly-15k) | CC BY-SA 3.0 |
| `classify` | 10 | Ten customer messages to label per request, six labels each, with the true labels | [Banking77](https://github.com/PolyAI-LDN/task-specific-datasets) test set | CC BY 4.0 |
| `ca`, `es` | 28 | Our development requests in Catalan and Spanish | This repository | MIT |

The code is MIT, but the task texts keep their own licences: Apache 2.0 for MT-Bench and Arena-Hard, CC BY-SA 3.0
for the combined Dolly tasks (in both files), CC BY 4.0 for Banking77. [LICENSES.md](LICENSES.md) has the
attributions, the changes we made and the licence texts; it covers both task files and everything in
`results/`. Each task records its source rows.

## The method

For every task:

1. **Taskpenny** answers it with its normal settings (budget cap $0.30 per task).
2. **The baseline** answers it in one call with its default settings (`--baseline`; the catalog's baseline,
   Claude Opus 5.5, if not given). The first published runs used Claude Sonnet 5, because Claude Opus refused
   most calls that day. Its cost is the real one reported by the gateway, not an estimate. With
   `--baseline-system worker` the baseline gets the same system instruction as Taskpenny's workers: a control for
   the judge's taste for complete, step-by-step answers.
3. **Quality**
   - `classify`: accuracy against the true labels, plus whether the answer used the exact format asked for.
   - everything else: a pairwise judge from a third provider (`google/gemini-3.1-pro-preview` by default)
     compares the two answers without knowing which is which, twice with the order swapped. A side wins only
     if both orders agree; otherwise it is a tie. Maths, reasoning and coding tasks give the judge the MT-Bench
     reference answer. The judge prompt is adapted from MT-Bench's pairwise prompt.
4. **A person** reviews a blind random sample of 20 pairs, to check that the judge agrees with people. So far
   this was done once, by the author, on answers from before later changes; see the limits in RESULTS.md.

The headline is two numbers per set: how much less Taskpenny spent, and in how many tasks its answer was as good
as or better than the baseline's.

## Run it

```bash
python bench/public/run.py --dry-run --limit 5          # no key, no cost: checks the pipeline
python bench/public/run.py --sets classify --total-budget 1
python bench/public/run.py --baseline anthropic/claude-sonnet-5 --total-budget 5   # the whole first set
python bench/public/run.py --core ...                   # a 60-task core with the same shape
python bench/public/summarize.py bench/results/public-live-<time>.jsonl            # the tables

# the published runs on the 50 new tasks
python bench/public/run.py --tasks bench/public/tasks-holdout.jsonl --total-budget 4          # default setup vs Opus 5.5
python bench/public/run.py --tasks bench/public/tasks-holdout.jsonl --ceiling anthropic/claude-sonnet-5 \
    --total-budget 2                                                                          # Sonnet 5 ceiling vs Sonnet 5
```

Every run needs `AI_GATEWAY_API_KEY`: Taskpenny's decisions are Jev's, reached through Vercel AI Gateway.

Results go to `bench/results/public-*.jsonl` (one line per task, with both answers and every judge note) and a
Markdown report next to it. `--resume <file>` continues an interrupted run; `--reuse-taskpenny` and `--reuse-baseline`
take one side's answers from an earlier run instead of paying for them again. With a Vercel key the budget guard
also watches the real balance, and the report says what Vercel billed against what the runner recorded.

`python bench/public/review.py pick <results>` draws the blind sample of 20 pairs for a person to judge, and
`review.py score` compares that person's votes with the judge.

The published results are in [RESULTS.md](RESULTS.md), with the raw files in `results/` (made when Taskpenny was
called SIAC; the scripts read them as they are).
