# SIAC

**Split a big prompt into small tasks and send each one to the cheapest model that can do it well.**

> Status: work in progress, private. The core loop (v0.1) and the live page (v0.2) work; they are being measured and tuned before the public benchmark (v0.3).

Most prompts don't need your most expensive model for every part of the job. SIAC works like a good project lead: it decides whether a request is worth splitting, breaks it into small, well defined tasks, gives each task to the cheapest model that can handle it, checks every result before moving on, and puts everything together into one answer. You watch the whole process live, and at the end you get a receipt that compares what it cost with what it would have cost to send everything to the strongest model.

The decisions (split or not, which tier of model, is this result good enough) are made by [Jev](https://typesafe.ai), TypeSafe AI's decision model, which costs about 3 cents per thousand decisions. When a task is simply a choice (pick an option, answer yes or no, give a score), Jev solves it directly: no language model needed.

## Quickstart

```bash
pip install -e .              # from a clone of this repository (a PyPI package comes with v1.0)
siac demo                     # watch SIAC work on a sample request: no key, no cost
export AI_GATEWAY_API_KEY=... # one Vercel AI Gateway key: Jev plus Claude, GPT and Gemini models
siac doctor                   # what your keys reach (other ways to connect: see Keys)
siac run "Write a short email to move tomorrow's meeting to Thursday"
siac ui                       # the live task tree in your browser
siac serve                    # OpenAI-compatible API for your tools (see below)
```

Every run is saved in `runs/` as JSON. `siac export runs/<id>.json` turns one into a Markdown report,
and `siac models --check` compares the catalog prices with the live Vercel catalog.

## Keys

The simplest setup is one [Vercel AI Gateway](https://vercel.com/ai-gateway) key: it reaches Jev and every
model in the catalog, and reports the real cost of each call.

You can also use your own provider keys, with or without Vercel:

| You set | What SIAC does |
|---|---|
| `AI_GATEWAY_API_KEY` | Everything through Vercel; Jev decides. |
| `AI_GATEWAY_API_KEY` and `SIAC_DIRECT=anthropic,openai` plus those providers' keys | Those providers go straight to their own API with your key; the rest through Vercel; Jev decides. |
| Only provider keys: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY` | Only those providers are used. Without Vercel there is no Jev, so the cheapest basic model you can reach answers the decision questions in its place: it works, but it is less sharp and a little dearer than Jev. |

`siac doctor` shows what your keys reach, which model decides and whether each model name exists on its
provider's API, without spending tokens. When a provider names a model differently from the catalog, add
`direct_id: <name>` to that model in `models.yaml`. Costs of direct calls are worked out from the catalog prices.

## Use it from any tool

`siac serve` starts an OpenAI-compatible endpoint. Any tool or library that talks to OpenAI can send its
prompts through SIAC by changing only the base URL:

```bash
siac serve                    # http://127.0.0.1:8765/v1 · the live page is on the same address
```

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="unused")
reply = client.chat.completions.create(
    model="siac",             # or "siac/anthropic" to use only Claude models
    messages=[{"role": "user", "content": "Draft a polite reminder for an unpaid invoice."}],
)
print(reply.choices[0].message.content)
print(reply.model_extra["siac"])   # run id, cost in USD, baseline estimate, saving
```

- Every request is one SIAC run: it is saved in `runs/` and you can watch it live on the page while it works.
- System messages and earlier turns are passed to SIAC as context; the last user message is the request.
- `stream: true` works, but the answer arrives in one piece at the end: SIAC checks the work before answering.
- Optional per request: `"siac": {"max_cost": 0.2, "no_split": true}` in the body, or the header `X-SIAC-Max-Cost`.
- Not yet: tool calling and images. SIAC answers them with a clear error instead of guessing.
- The server listens only on your machine. If you open it to others, set `--api-key` (or `SIAC_API_KEY`):
  every request spends your credit.

## How it works

```
request
  |
  v
[gate]       Jev: split it? which tier (1-4)? what kind of task? what kind of answer?
  |                                   |
  | worth splitting                   | atomic task
  v                                   v
[planner]    strong model          [router]    choice, yes/no or score -> Jev itself
  subtasks with goal, prompt,                  anything else -> cheapest model for the tier
  success criteria, answer type               |
  and dependencies                            v
  |                                 [executor]  result + hand-off notes for the next task
  +-> each subtask goes back                  |
      to the gate                             v
                                    [verifier]  Jev: does it meet the criteria?
                                              |  no -> retry, then one tier up (max 2)
                                              v
                                    [aggregator]  one answer, checked against the request
                                              |
                                              v
                                    answer + cost receipt + full task tree
```

## Why

- **Money, not tokens.** Splitting usually uses more tokens in total. The saving comes from moving most of them to models that are 4 to 40 times cheaper, and from letting Jev answer the choice tasks outright.
- **Visible.** Every step, model, decision confidence and cent is on screen.
- **Verifiable.** A public, reproducible benchmark (coming in v0.3) compares SIAC with a single strong model on cost, quality and time.

## Reading the live page

- **What SIAC did**: how many Jev decisions, plans, model calls, Jev-solved tasks and checks the run needed.
- **Task tree**: every task with its tier (1 basic to 4 critical), who did it (a model, or JEV) and what it cost.
  Click a task to see its prompt, what Jev decided about it, each attempt and its result.
- **Cost receipt**: planning, work, Jev decisions, checks and assembly add up to the total. The comparison is an
  estimate of sending the same request once to the baseline model (Claude Opus 5.5) with an answer as long as SIAC's,
  scaled by the hidden reasoning SIAC's own models used in that run.

## What we have learned so far

- On 42 development prompts in English, Catalan and Spanish, Jev's gate chose well when to split (41 of 42) and what
  kind of answer was expected (41 of 42), and SIAC cost 41% less than the estimate for one strong model.
  Details and limits in [`bench/DEV_RESULTS.md`](bench/DEV_RESULTS.md).
- Simple requests save the most (about 95% on basic ones): they go straight to a cheap model.
- Splitting only pays when the parts can go to cheaper models than the whole would need. So SIAC splits only when Jev
  is clearly sure, never rewrites the parts to assemble them, and never falls back to a more expensive model.
- Public, reproducible numbers come with the v0.3 benchmark.

## Roadmap

| Version | What |
|---|---|
| v0.1 | Core loop from the terminal: gate, planner, router, executor, verifier, aggregator, cost receipt (done) |
| v0.2 | Live task tree in the browser, run replay, export, English, Catalan and Spanish UI (done) |
| v0.3 | Public benchmark: SIAC against a single strong model |
| v0.4 | OpenAI-compatible endpoint, direct provider keys, local models with Ollama, Docker |
| v1.0 | Public release |

## Models

The catalog lives in [`src/siac/models.yaml`](src/siac/models.yaml): one entry per model with its tiers, prices and context. Adding a model is adding a few lines.

## License

MIT. Made by [Cinto Casals](https://github.com/cintocasals) · SerIA Nativa.
