# Changelog

## Unreleased

- `taskpenny demo` replays a real run shipped with the package (every step, model, cost and the answer) instead
  of simulated text, and says which figures are measured and which are estimates. Simulated runs
  (`taskpenny run --dry-run`) no longer show a saving.
- A demo page for the browser (`docs/demo/`): the live page replaying the same real run, with no server, no key
  and no cost. `taskpenny ui` and `taskpenny serve` also list that run as an example to replay.
- The live page labels the comparison with one strong model and the saving as estimates.
- `RESULTS.md` adds the costs with Jev at its list price (it was free during the runs): 52.5% cheaper on the 50
  new tasks instead of 53.3%, 95% on labelling instead of 98%. `summarize.py` prints them.
- The README leads with the measured results and tries each provider key on its own (OpenAI, Anthropic, Gemini:
  all answered in full without Vercel).
- Images and other non-text content get a clear error instead of being dropped silently.
- A ready-to-import n8n workflow in `examples/n8n/`.
- Claude Haiku uses its exact name on Anthropic's own API, so `taskpenny doctor` finds it.

## v0.5.0

- SIAC is now **Taskpenny**: the package and the command are `taskpenny`, the settings are `TASKPENNY_*` (they
  were `SIAC_*`), and the models in the OpenAI-compatible API are `taskpenny` and `taskpenny/<profile>` (they were
  `siac`). The entries below use the new names. Benchmark results made before the rename are read as they are.
- Publishing to PyPI from GitHub releases, with trusted publishing: no token is stored anywhere.
- `summarize.py` and the runner's report count a judge verdict that could not be read as not as good, as
  `RESULTS.md` says, and average times over every compared task; the published tables are reproduced exactly.

## v0.4.0

- Works from n8n and other tools by changing only the base URL: besides Chat Completions, Taskpenny now accepts the
  Responses API (`/v1/responses`), the one n8n's OpenAI Chat Model node uses by default. Checked with a real n8n
  workflow.
- A ceiling (`TASKPENNY_CEILING`, `--ceiling`): Taskpenny's strongest model can be the model you would use anyway; dearer
  models are left out and receipts compare with it.
- Splitting only when it pays: Jev gates every part first, and if the parts would not go to clearly cheaper models
  the request is done in one go.
- Checked on 50 new tasks against Claude Sonnet 5: 53% cheaper and as good or better in 82% (`RESULTS.md`).
- Security: with `TASKPENNY_API_KEY` every API route needs the key, as a Bearer token or as a session cookie after
  signing in on the page; only the page itself and `/health` open without it. POST bodies must be JSON,
  `TASKPENNY_MAX_COST` caps the budget a request may ask for, a run that fails always ends, and failed plans are
  counted in the receipt.
- `/health` for container health checks.
- The live page numbers list items correctly when a list is broken by sub-points (it showed 1. for every item).
- A blind check of 20 pairs by a person, reported in `RESULTS.md`.

## v0.3.0

- First published results (`bench/public/RESULTS.md`): against Claude Sonnet 5 on 142 tasks, 93% cheaper on
  everyday tasks and as good or better in 76% of all tasks; more expensive on critical and split requests.
- Jev-solved items show their labels in the request's language, without the English question.
- Chat calls that time out are not sent again blindly (they may still be billed); timeouts scale with the
  output allowed, the receipt counts them, and the benchmark guard watches the real Vercel balance.
- Public benchmark: 143 tasks from pinned public sources plus our Catalan and Spanish cases, real costs for both
  sides, a pairwise judge from a third provider, true labels for classification, and a blind human sample.
- Fuller answers (the worker shows key steps and matches the depth asked for) and a minimum tier of 2 for maths
  and code, after the first 60 task run.
- Claude Opus 5 in tier 4, as the fallback when Opus 5.5 is refused.
- `taskpenny serve`: OpenAI-compatible endpoint (`/v1/chat/completions`, `/v1/models`); API runs appear live on the page.
- Direct provider keys (Anthropic, OpenAI, Google, DeepSeek, Alibaba), alone or next to Vercel, and a stand-in
  decider when Jev is not reachable.
- Local models with Ollama for basic tasks (`TASKPENNY_LOCAL`).
- `taskpenny doctor`, Docker image and compose file, docs, contributing guide and issue templates.

## v0.2.0

- The live page: task tree, process strip, cost receipt, replay and export of saved runs, English, Catalan and
  Spanish.
- Development results on 42 requests: 41% cheaper than the one-model estimate.

## v0.1.0

- The core loop from the terminal: Jev's gate, planner, router, workers with hand-off notes, Jev's checks and
  repairs, assembly without rewriting, final check, budget and cost receipt.
