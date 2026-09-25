# SIAC public benchmark

Does SIAC give answers as good as one strong model, for less money? This folder answers that with public tasks,
real costs and a method anyone can rerun.

## The tasks

`tasks.jsonl` has 143 tasks. `python bench/public/build.py` rebuilds it byte for byte from pinned sources
(each file is checked against its SHA-256; the output hash is printed).

| Set | Tasks | What | Source | License |
|---|---|---|---|---|
| `mtbench` | 80 | Everyday requests in 8 categories: writing, roleplay, reasoning, maths, coding, extraction, science, humanities (first turn only) | [MT-Bench](https://github.com/lm-sys/FastChat/tree/main/fastchat/llm_judge), lm-sys/FastChat | Apache 2.0 |
| `hard` | 15 | Hard real user prompts, random sample (seed 2026) | [Arena-Hard-Auto v0.1](https://github.com/lmarena/arena-hard-auto) | Apache 2.0 |
| `multi` | 10 | Four different asks in one message (a question about a text, ideas, a short piece of writing, a sorting job) | [databricks-dolly-15k](https://huggingface.co/datasets/databricks/databricks-dolly-15k) | CC BY-SA 3.0 |
| `classify` | 10 | Ten customer messages to label per request, six labels each, with the true labels | [Banking77](https://github.com/PolyAI-LDN/task-specific-datasets) test set | CC BY 4.0 |
| `ca`, `es` | 28 | Our development requests in Catalan and Spanish | This repository | MIT |

The `multi` tasks are built from Dolly prompts, so that part of `tasks.jsonl` is shared under CC BY-SA 3.0;
the `classify` tasks carry Banking77 messages under CC BY 4.0 (Casanueva et al., 2020). Each task records its
source rows. The result files in `results/` include the task prompts, under the same licences.

## The method

For every task:

1. **SIAC** answers it with its normal settings (budget cap $0.30 per task).
2. **The baseline** answers it in one call with its default settings (`--baseline`; the catalog's baseline if not
   given). The first published run used Claude Sonnet 5, because Claude Opus refused most calls that day. Its
   cost is the real one reported by the gateway, not an estimate.
3. **Quality**
   - `classify`: accuracy against the true labels, plus whether the answer used the exact format asked for.
   - everything else: a pairwise judge from a third provider (`google/gemini-3.1-pro-preview` by default)
     compares the two answers without knowing which is which, twice with the order swapped. A side wins only
     if both orders agree; otherwise it is a tie. Maths, reasoning and coding tasks give the judge the MT-Bench
     reference answer. The judge prompt is adapted from MT-Bench's pairwise prompt.
4. **A person** reviews a blind random sample of 20 pairs, to check that the judge agrees with people.

The headline is two numbers per set: how much less SIAC spent, and in how many tasks its answer was as good
as or better than the baseline's.

## Run it

```bash
python bench/public/run.py --dry-run --limit 5          # no key, no cost: checks the pipeline
python bench/public/run.py --sets classify --total-budget 1
python bench/public/run.py --baseline anthropic/claude-sonnet-5 --total-budget 5   # the whole set
python bench/public/run.py --core ...                   # a 60 task core with the same shape
python bench/public/summarize.py bench/results/public-live-<time>.jsonl            # the tables
```

Results go to `bench/results/public-*.jsonl` (one line per task, with both answers and every judge note) and a
Markdown report next to it. `--resume <file>` continues an interrupted run; `--reuse-siac` and `--reuse-baseline`
take one side's answers from an earlier run instead of paying for them again. With a Vercel key the budget guard
also watches the real balance, and the report says what Vercel billed against what the runner recorded.

`python bench/public/review.py pick <results>` draws the blind sample of 20 pairs for a person to judge, and
`review.py score` compares that person's votes with the judge.

The published results are in [RESULTS.md](RESULTS.md), with the raw files in `results/`.
