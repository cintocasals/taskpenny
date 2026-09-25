# Configuration

## Commands

| Command | What it does |
|---|---|
| `siac run "request"` | Runs one request and prints the answer and the cost receipt. `-` or a pipe reads stdin; `--file` reads a file. |
| `siac demo` | The same, on a sample request, with simulated models: no key, no cost. |
| `siac ui` | The live page in your browser: run requests, watch the task tree, replay and export saved runs. |
| `siac serve` | The OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`) plus the live page, without opening a browser. |
| `siac export runs/<id>.json` | A saved run as a Markdown report. |
| `siac doctor` | What your keys reach, which model decides, whether each model exists on its provider. Spends nothing. |
| `siac models [--check]` | The catalog; `--check` compares prices with the live Vercel catalog. |

Common options: `--profile` (which providers may be used), `--max-cost` (budget per run in USD, default 0.50),
`--ceiling` (the strongest model SIAC may use), `--no-split`, `--dry-run`, `--models` (your own catalog file),
`--save-dir` (where runs are saved).

## Environment variables

| Variable | Meaning |
|---|---|
| `AI_GATEWAY_API_KEY` | Vercel AI Gateway key: Jev and every model in the catalog. |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` (or `GOOGLE_API_KEY`), `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY` | Provider keys. Without Vercel, only these providers are used. |
| `SIAC_DIRECT` | With Vercel: providers to call directly with their own key, for example `anthropic,openai`. |
| `SIAC_CEILING` | The strongest model SIAC may use, usually the one you would otherwise use for everything, for example `anthropic/claude-sonnet-5`. Dearer models are left out, that model covers the top tiers, and receipts compare with it. |
| `SIAC_DECIDER=llm` | Use the stand-in decider (a basic language model) even when Jev is reachable. |
| `SIAC_LOCAL` | Local Ollama models for basic tasks: `qwen3:4b`, `qwen3:8b@2` (tiers 1 and 2), or `auto`. |
| `OLLAMA_HOST` | Where Ollama listens (default `http://127.0.0.1:11434`). |
| `<PROVIDER>_BASE_URL` | Another address for a provider's API, for example `OPENAI_BASE_URL`. |
| `SIAC_API_KEY` | Key that clients of `siac serve` must send. Set it whenever others can reach the port. |
| `SIAC_RUNS_DIR` | Where runs are saved (default `runs`). |
| `AI_GATEWAY_BASE_URL` | Another address for Vercel AI Gateway. |

## models.yaml

The catalog is one YAML file (`src/siac/models.yaml`; pass your own with `--models`).

```yaml
decider:                      # Jev on Vercel AI Gateway
  id: typesafe-ai/jev
  price: { input: 0.042, output: 0.0 }   # USD per million tokens

planner: anthropic/claude-sonnet-5        # plans critical (tier 4) requests
planner_light: google/gemini-3.8-flash    # plans the rest
baseline: anthropic/claude-opus-5.5       # the model receipts compare with

tiers:                        # what each tier means; Jev reads these descriptions
  1: { name: basic, description: "..." }

min_tier:                     # lowest tier for some kinds of work
  math: 2
  code: 2

models:
  - id: openai/gpt-6-luna     # provider/name, as Vercel AI Gateway names it
    provider: openai
    tiers: [1]                # the tiers this model may serve
    price: { input: 0.10, output: 0.50 }
    context: 1050000
    direct_id: gpt-6-luna     # optional: its name on the provider's own API

profiles:                     # --profile limits the providers
  anthropic: [anthropic]
```

For each task SIAC takes the **cheapest** model that lists the task's tier and that your keys and profile
allow; on a price tie, the one listed first. To add a model, add an entry and choose its tiers. Tier choices
are a starting point, and the public benchmark is how to check them.

## In code

```python
import asyncio
from siac.catalog import Catalog
from siac.engine import Engine, Limits
from siac.providers import connect

async def main():
    gateway, catalog = connect(Catalog.load())
    engine = Engine(gateway, catalog, limits=Limits(max_cost=0.2), on_event=print)
    result = await engine.run("Summarise this contract in five points: ...")
    print(result.answer, result.receipt["total_cost"])
    await gateway.aclose()

asyncio.run(main())
```

`on_event` receives every step as a dictionary (gate, plan, route, llm_call, verify, repair, jev_solve,
model_fallback, aggregate, final_check, node_done, run_done), the same events the live page shows.
