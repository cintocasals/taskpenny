# Taskpenny

**Frontier prices only for frontier work.**

Taskpenny splits a request only when it pays, gives each task to the cheapest model that does it well, checks every
result, and shows you the receipt. Open source, Python, one command to try.

[![tests](https://github.com/cintocasals/taskpenny/actions/workflows/ci.yml/badge.svg)](https://github.com/cintocasals/taskpenny/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-12161A)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-12161A)](pyproject.toml)
[![Benchmark: public](https://img.shields.io/badge/benchmark-public%20and%20reproducible-C1440E)](bench/public/RESULTS.md)

![Taskpenny splitting a launch plan into five tasks, each done by the cheapest model that can do it well](docs/taskpenny-demo.gif)

<sub>A real run replayed at 4x. Watch it in your browser: [live demo](https://cintocasals.github.io/taskpenny/demo/)
(no key, no cost). The saving on the page is the receipt's estimate against one strong model; the benchmark below
runs the strong model for real.</sub>

## Measured, not promised

On **50 tasks Taskpenny had never seen**, with Claude Sonnet 5 as its strongest model, against Sonnet 5 answering
alone ([full report](bench/public/RESULTS.md)):

- **52.5% cheaper**, and **as good or better in 82%** of the tasks (11 wins, 30 ties, 9 losses).
- Basic and standard tasks: **89% cheaper**, as good or better in 96%.
- Labelling customer messages (first benchmark): **95% cheaper**, same accuracy (96 of 100 on both sides).

Costs are the ones the provider billed, for both sides. Jev, the model that makes Taskpenny's decisions, was free
during the runs; every figure here counts it at its list price (the report has both). A judge from a third
provider (Gemini 3.1 Pro) compared every pair twice with the order swapped. The author also voted blind on 20
pairs: as good or better in 18 of 20, where the judge said 14. Twenty votes from one person are too few to correct
the judge, so the figures are the judge's.

We publish where Taskpenny loses too: the first full run was only 13% cheaper, hard tasks cost more before we added
a ceiling, and Catalan is our weakest set. It is all in the report.

## Try it

```bash
pip install taskpenny
taskpenny demo                     # replays a real run in the terminal: no key, no cost
```

Then give it a key:

```bash
export AI_GATEWAY_API_KEY=...      # one Vercel AI Gateway key: Jev decides, and it reaches Claude, GPT and Gemini
taskpenny run "Write a short email to move tomorrow's meeting to Thursday"
taskpenny ui                       # the live task tree in your browser
```

Already have an OpenAI, Anthropic or Gemini key? That works too, without Vercel: see [Keys](#keys).

Every run is saved in `runs/` as JSON. `taskpenny export runs/<id>.json` turns one into a Markdown report.

## Who it is for

- You send most of your requests to one strong model, and the bill grows with usage.
- Your requests mix easy and hard parts: a summary plus a decision, ten labels plus a reply, a plan plus copy.
- You want to see why each model was chosen and what each step cost.

**Who it is not for (yet):** if your calls are one short classification each, a per-request router is enough.
Tool calling and images are not supported yet; Taskpenny answers them with a clear error.

## How it is different

| | Routes | Who picks the model | Checks each result | Shows the work and the cost |
|---|---|---|---|---|
| RouteLLM, OpenRouter Auto | The whole request | A trained router or a classifier | No | No |
| Not Diamond | Each step an agent already takes | A learned router | No | Savings dashboard |
| LiteLLM Auto Router, tiershift | The whole request | Jev, rules or an LLM | No (tiershift escalates on failure) | Cost or savings per request |
| ROMA | **Each subtask**, recursively | A fixed model per task type | Optional verifier | Tree in the terminal, no receipt |
| JevRouter | Each step of a plan | Jev | No: it decides, it does not run the work | Decision log |
| **Taskpenny** | **Each subtask**, only when splitting pays | **Jev, for every task** | **Yes, every result** | **Live tree and cost receipt** |

All of them are good at what they do, and several can sit next to Taskpenny: it speaks the OpenAI API.

## How it works

```
request
  |
  v
[gate]       Jev: split it? which tier (1-4)? what kind of work? what form of answer?
  |                                   |
  | clearly worth splitting           | one task
  v                                   v
[planner]    subtasks with prompt,  [router]    closed question -> Jev answers it
  success criteria and                         anything else -> cheapest model of the tier
  dependencies; each one goes                  |
  back to the gate                             v
  |                                 [worker]    result + notes for the tasks that depend on it
  |                                            |
  |                                            v
  |                                 [check]     Jev: does it meet the criteria?
  |                                            no -> repair, then one tier up (at most 2)
  v
[assembly]   an outline from a cheap model; the parts are stitched in, not rewritten
  |
  v
[final check] Jev against the request; only what is missing gets written
  |
  v
answer + cost receipt + full task tree
```

Every decision is a closed question (split or not, which tier, is this good enough), and closed questions are
what [Jev](https://typesafe.ai), TypeSafe AI's decision model, answers well and cheaply. When a task is itself a
choice (pick an option, yes or no, a score), Jev answers it directly: no language model needed.

Splitting uses more tokens in total. The saving is in money: most tokens go to models that cost a fraction of a
frontier model, and Taskpenny drops a plan when the parts would not go to clearly cheaper models (5 of 11 plans in
the 50 new tasks). Details, thresholds and fallbacks: [docs/how-it-works.md](docs/how-it-works.md). Every option
and variable: [docs/configuration.md](docs/configuration.md).

### Reading the live page

- **What Taskpenny did**: how many Jev decisions, plans, model calls, Jev-solved tasks and checks the run needed.
- **Task tree**: every task with its tier (1 basic to 4 critical), who did it (a model, or JEV) and what it cost.
  Click a task to see its prompt, what Jev decided about it, each attempt and its result.
- **Cost receipt**: planning, work, Jev decisions, checks and assembly add up to the total. The comparison with
  one strong model (Claude Opus 5.5) is an estimate: the same request sent once, with an answer as long as
  Taskpenny's, scaled by the hidden reasoning Taskpenny's own models used in that run.

## Use it from any tool

`taskpenny serve` starts an OpenAI-compatible endpoint on `http://127.0.0.1:8765/v1`, with the live page on the
same address. Change only the base URL:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="unused")
reply = client.chat.completions.create(
    model="taskpenny",             # or "taskpenny/anthropic" to use only Claude models
    messages=[{"role": "user", "content": "Draft a polite reminder for an unpaid invoice."}],
)
print(reply.choices[0].message.content)
print(reply.model_extra["taskpenny"])   # run id, cost in USD, baseline estimate, saving
```

- Chat Completions and Responses both work, so n8n's OpenAI Chat Model node needs only the new base URL. A
  ready-to-import workflow: [examples/n8n](examples/n8n/README.md).
- Every request is one Taskpenny run, saved in `runs/` and visible live on the page while it works.
- System messages and earlier turns are passed as context; the last user message is the request.
- `stream: true` works, but the answer arrives in one piece at the end: Taskpenny checks the work before answering.
- `TASKPENNY_CEILING=anthropic/claude-sonnet-5` makes the model you use today the strongest one Taskpenny may use.
- Per request: `"taskpenny": {"max_cost": 0.2, "no_split": true}` in the body, or the header `X-Taskpenny-Max-Cost`.
- The server listens only on your machine. If you open it, set `TASKPENNY_API_KEY`: then the API and the page
  need the key (the page asks for it once). `TASKPENNY_MAX_COST` caps any request (2 USD by default).

### Docker

```bash
docker compose up                  # page and API on http://127.0.0.1:8765
# or: docker build -t taskpenny . && docker run -p 127.0.0.1:8765:8765 -e AI_GATEWAY_API_KEY taskpenny
```

Keys come from your environment and are never written into the image. Runs are kept in the `taskpenny-runs`
volume.

## Keys

| You set | What Taskpenny does |
|---|---|
| `AI_GATEWAY_API_KEY` | Everything through [Vercel AI Gateway](https://vercel.com/ai-gateway); Jev decides; real cost of every call. |
| That, plus `TASKPENNY_DIRECT=anthropic,openai` and those providers' keys | Those providers go straight to their API; the rest through Vercel; Jev decides. |
| Only provider keys: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY` | Only those providers. Without Vercel there is no Jev, so the cheapest basic model you can reach decides: it works, a little less sharp and a little dearer. |

We tried each of the OpenAI, Anthropic and Gemini keys on its own: every one answered a two-part request in full,
for less than half a cent. The catalog has no Gemini model for the two hardest tiers yet, so with a Gemini key
alone hard tasks go to Gemini Flash.

`taskpenny doctor` shows what your keys reach, which model decides and whether each model name exists on its
provider's API, without spending tokens.

### Local models (free)

With [Ollama](https://ollama.com) running, basic tasks can run on your machine at no cost:

```bash
ollama pull qwen3:4b
export TASKPENNY_LOCAL=qwen3:4b          # tier 1 goes local; "qwen3:8b@2" also covers tier 2; "auto" = all installed
```

Jev still checks every result, and a weak answer moves on to a cloud model. Use a model of at least 3 to 4
billion parameters: tiny ones answer fast but badly.

## What leaves your machine

Your prompt goes to Jev and to the providers you chose. Keys are read from environment variables only and are
never written to disk, run files or logs. Saved runs contain your requests and answers. See
[SECURITY.md](SECURITY.md).

## All results

<details>
<summary>First public benchmark: 142 tasks against Claude Sonnet 5, by route</summary>

With Jev at its list price. The report also has the figures with Jev free, as it was during the run.

| What Taskpenny did | Tasks | Taskpenny cost | As good or better |
|---|---|---|---|
| Basic and standard tasks, one cheap model | 103 | **92% less** | 76% |
| Labelling customer messages (true labels) | 10 | **95% less** | same accuracy, 96 of 100 |
| Advanced tasks, one model | 16 | 11% less | 69% |
| Critical tasks, sent to Claude Opus | 8 | 2.0x as much | 100% |
| Requests split into parts | 15 | 2.2x as much | 73% |
| **All** | 142 | **13% less** | **76%** |

That run is why the ceiling and the "split only when it pays" rule exist. The 50 new tasks above were run after
both changes. Method, raw data and how to rerun it: [bench/public](bench/public/README.md).
</details>

## What we have learned so far

- Jev's gate chooses well when to split and what kind of answer is expected (41 of 42 on our development set).
- The saving comes from everyday requests going to models that cost a small fraction of a frontier model.
- Splitting only pays when the parts can go to cheaper tiers than the whole; at advanced level it did not.
- Cheap models give correct but bare answers. The automatic judge prefers more explanation; in a blind check of 20
  pairs, the author often preferred the short answer that did only what was asked.

## FAQ

**Can you trust an LLM judge?** It is from a third provider, compares each pair twice in both orders, and counts
a tie when the two orders disagree. It does like longer answers: many of Taskpenny's losses on basic tasks were
short, correct answers against longer ones. You can rerun the benchmark with your own judge.

**Do I need Vercel?** No. With Vercel you get Jev and the real cost of each call; without it, a small model makes
the decisions.

**Why Jev?** Every decision Taskpenny makes is a closed question. Jev answers those with a probability and a
confidence, costs about 4 cents per thousand decisions at list price (about 1,000 tokens each in our benchmark),
and never writes text.

**What if Jev changes?** The decider is swappable (`TASKPENNY_DECIDER=llm`), and the development cases
(`bench/run_dev.py`) show quickly whether its decisions moved.

## Contributing

Adding a model is a few lines in [`models.yaml`](src/taskpenny/models.yaml). Translations, skills and good first
issues: [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/good-first-issues.md](docs/good-first-issues.md).

If Taskpenny saves you money, a star helps other people find it.

## License

MIT. Made by [Cinto Casals](https://github.com/cintocasals) · [SerIA Nativa](https://serianativa.com).
