# Configuration

## Commands

| Command | What it does |
|---|---|
| `taskpenny run "request"` | Runs one request and prints the answer and the cost receipt. `-` or a pipe reads stdin; `--file` reads a file. |
| `taskpenny demo` | Replays a real run shipped with the package: every step, model, cost and the answer. No key, no cost. `--full` prints the whole answer, `--instant` skips the pauses. |
| `taskpenny run --dry-run "request"` | The whole flow with simulated models and placeholder answers, at catalog prices: no key, no cost, no saving claimed. |
| `taskpenny ui` | The live page in your browser: run requests, watch the task tree, replay and export saved runs. `--host`, `--port` (default 8765), `--no-browser`, `--api-key`, `--dry-run` (simulated by default). |
| `taskpenny serve` | The OpenAI-compatible API (`/v1/chat/completions`, `/v1/responses`, `/v1/models`) plus the live page, without opening a browser. `--host`, `--port`, `--api-key`, `--dry-run` (simulated models for every request). |
| `taskpenny export runs/<id>.json` | A saved run as a Markdown report. |
| `taskpenny doctor` | Whether Jev is reachable (it needs `AI_GATEWAY_API_KEY`), what your keys reach, whether each model exists on its provider. Spends nothing. |
| `taskpenny models [--check]` | The catalog; `--check` compares prices with the live Vercel catalog. |

Options of `run`: `--profile` (which providers may be used), `--max-cost` (budget per run in USD, default 0.50),
`--ceiling` (the strongest model Taskpenny may use), `--no-split`, `--dry-run`, `--models` (your own catalog file),
`--save-dir` (where runs are saved), `--max-depth` (how deep splitting may go, default 3), `--json` (the whole run
as JSON), `--quiet` (only the answer and the receipt). Options of `demo`: `--speed` (default 4), `--instant`,
`--full`, `--quiet`.

`taskpenny run` exits with 0 when the run is `done` or `unverified` (the warnings are printed), 2 when it is
`partial` or `failed`, and 1 on a setup error such as a missing key or an unknown profile.

## The API

Through the API, a request may add `"taskpenny": {"max_cost": 0.2, "no_split": true, "dry_run": true}` to its
body, or send the budget as the header `X-Taskpenny-Max-Cost`. Without either, the budget is 0.50 USD, and it is
never above `TASKPENNY_MAX_COST`.

- **No key, no real answers.** Without `AI_GATEWAY_API_KEY` the API answers 503, unless the server was started
  with `--dry-run` or the request asks for `"taskpenny": {"dry_run": true}`; a simulated answer carries
  `taskpenny.simulated: true` and the header `X-Taskpenny-Simulated`.
- **Status.** Every answer carries `taskpenny.status` (`done`, `unverified`, `partial`), `taskpenny.warnings` and
  the header `X-Taskpenny-Status` (a stream has it only in its last chunk). `partial` answers with `finish_reason: "length"` (Responses: `incomplete`). A
  run with no answer is an HTTP 502 error, never an empty success. See [how it works](how-it-works.md#nothing-fails-in-silence).
- **Retries.** Every request is a paid run. Turn off your client's automatic retries (`max_retries=0` in the
  OpenAI SDKs, `maxRetries: 0` in n8n), or send an `Idempotency-Key` header: a retry with the same key, within
  24 hours and with the same request, gets the first run's answer instead of starting a new run (keys are kept
  in memory: a restart forgets them).
- **Going away.** A client that closes the connection before the answer is ready cancels its run (what was done
  so far is saved), unless it sent an `Idempotency-Key`. With `stream: true`, the headers and a keep-alive comment
  every 10 seconds go out at once, and the answer arrives in one piece at the end. `stream` must be `true` (or
  the text `"true"`).
- **Ignored parameters.** Taskpenny picks the models and their limits, so `max_tokens`, `temperature`, `top_p`,
  `stop`, `seed` and similar are ignored and listed in `taskpenny.ignored` and the header `X-Taskpenny-Ignored`.
  `n` above 1, tool calling, images and JSON mode are refused with a clear error.

## Environment variables

| Variable | Meaning |
|---|---|
| `AI_GATEWAY_API_KEY` | **Required for real runs.** Vercel AI Gateway key: Jev, which makes every decision, and every model in the catalog. Without it, runs are simulated. |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` (or `GOOGLE_API_KEY`), `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY` | Optional provider keys, used only for the providers listed in `TASKPENNY_DIRECT`. |
| `TASKPENNY_DIRECT` | Providers to call directly with their own key instead of through Vercel, for example `anthropic,openai`. Jev still decides, through Vercel. |
| `TASKPENNY_CEILING` | The strongest model Taskpenny may use, usually the one you would otherwise use for everything, for example `anthropic/claude-sonnet-5`. Dearer models are left out, that model covers the top tiers, and receipts compare with it. |
| `TASKPENNY_LOCAL` | Local Ollama models for basic tasks, next to the Vercel key: `qwen3:4b`, `qwen3:8b@2` (tiers 1 and 2), or `auto`. Adds the profile `local`. |
| `OLLAMA_HOST` | Where Ollama listens (default `http://127.0.0.1:11434`). |
| `<PROVIDER>_BASE_URL` | Another address for a provider's API, for example `OPENAI_BASE_URL`. |
| `TASKPENNY_API_KEY` | Key for `taskpenny serve` and `taskpenny ui`: API clients send it as a Bearer token, the page asks for it once and stays signed in for 30 days (sessions are kept, hashed, in the runs folder; "Sign out" ends one). Set it whenever others can reach the port, and make it long and random: `openssl rand -hex 24`. More than 20 wrong keys in a minute pause key checks for the whole server. |
| `TASKPENNY_ALLOWED_HOSTS` | Without a key, the server only answers to localhost, IP addresses and one-word names (such as a Docker service); list other host names here, for example `ai.example.lan`. |
| `TASKPENNY_MAX_COST` | Highest budget a request to `taskpenny serve` may ask for, in USD (default 2). |
| `TASKPENNY_RUNS_DIR` | Where runs are saved (default `runs`). |
| `AI_GATEWAY_BASE_URL` | Another address for Vercel AI Gateway. |

## models.yaml

The catalog is one YAML file (`src/taskpenny/models.yaml`; pass your own with `--models`).

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

For each task Taskpenny takes the **cheapest** model that lists the task's tier and that your keys and profile
allow; on a price tie, the one listed first. To add a model, add an entry and choose its tiers. Tier choices
are a starting point, and the public benchmark is how to check them.

## In code

```python
import asyncio
from taskpenny.catalog import Catalog
from taskpenny.engine import Engine, Limits
from taskpenny.providers import connect

async def main():
    gateway, catalog = connect(Catalog.load())
    engine = Engine(gateway, catalog, limits=Limits(max_cost=0.2), on_event=print)
    result = await engine.run("Summarise this contract in five points: ...")
    print(result.answer, result.receipt["total_cost"])
    await gateway.aclose()

asyncio.run(main())
```

`on_event` receives every step as a dictionary (gate, plan, plan_failed, split_rejected, route, llm_call, verify,
repair, jev_extract, jev_declined, jev_solve, fallback, model_fallback, aggregate, final_check, warning,
node_done, run_done), the same events the live page shows. `connect()` raises `GatewayError` (401) without
`AI_GATEWAY_API_KEY`.
