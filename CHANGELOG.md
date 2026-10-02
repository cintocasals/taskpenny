# Changelog

## Unreleased

- **Public repository.** README: install from GitHub until the package is on PyPI; the Sonnet 5 comparison comes
  first in the results table.
- **Benchmark notes.** RESULTS.md and the README now say that Taskpenny caps hidden reasoning (off on tiers 1 and
  2, low on tiers 3 and 4) while the baselines use each model's default.
- **Equal-footing control run (1 October 2026).** The control that gives the baseline both the same worker
  instruction and the same per-tier reasoning cap (`--baseline-reasoning match`) was run on the 50 new tasks. The
  Sonnet 5 headline is now the equal-footing figure: about 38% cheaper (not 52.5%) and as good or better in 74%
  (not 82%). The old figures are kept once, labelled as against Sonnet 5 at its defaults.

## v0.7.0

From an independent review of v0.6.0 (27 September 2026). Breaking: Jev is required.

- **Jev is required.** Every decision in Taskpenny is Jev's, and Jev is reached through Vercel AI Gateway, so a real
  run needs `AI_GATEWAY_API_KEY`; without it Taskpenny says so and only simulates or replays. The stand-in decider
  (a small language model answering Jev's questions without Vercel) and `TASKPENNY_DECIDER` are gone: its
  decisions cost more and were never measured for accuracy. Provider keys (`TASKPENNY_DIRECT`) and local models
  (`TASKPENNY_LOCAL`) still work next to the Vercel key.
- **Nothing fails in silence.** A result that never passes Jev's check is kept (the best attempt) and marked
  `unverified`; a part that fails is named; every run ends `done`, `unverified`, `partial` or `failed`, with the
  reasons in `warnings`. The API adds `taskpenny.status`, `taskpenny.warnings` and the `X-Taskpenny-Status`
  header, answers `partial` runs with `finish_reason: "length"` (Responses: `incomplete`) and a run without an
  answer with HTTP 502. The live page shows the warnings (idea 10: carry on, visibly) and the unverified and
  partial tasks in the tree.
- **The budget is a ceiling.** Every call, Jev's included, reserves the most it can cost before it is made: all
  the output it allows, and the input counted cautiously (one token per character for non-ASCII text). Calls
  reserve inside their slot; Jev calls run eight at a time. When the budget is nearly spent, the last answer is
  shortened to what it can still pay, or not made. The budget message says what the run really spent.
- **Jev answers requests that are closed questions**, not only planned subtasks: "label these messages", "yes or
  no for each email", "rate each ticket". The cheapest model writes the question and points at the lines that
  hold the items, and Jev answers each one. Maths, code and quiz questions never go to Jev. Several decisions
  about the same items share one Jev call per item (proposal 15), and a decision has 50 items at most.
- **At most 12 subtasks per run**, across every level (proposal 7), and a plan with a single part is done in one
  go. A plan with too many parts is asked for again instead of being cut.
- **Robust to odd answers.** A gateway or provider answer that cannot be read (HTML from a proxy, a missing or
  wrong field) is a clear error, not a crash; an unexpected error still returns what was paid for. If the
  assembly model fails, the parts are stitched in plan order; if a check cannot run, the answer is kept and
  marked unverified; if the planner fails, the request is done in one go; if the run cannot be saved, the answer
  still reaches you. A verdict Jev cannot give is asked once more and never pays for a repair.
- **API clients**: a server without `AI_GATEWAY_API_KEY` refuses real requests (503) instead of answering with
  simulated text, and simulated answers say so (`taskpenny.simulated`, `X-Taskpenny-Simulated`).
  `Idempotency-Key` makes a retry get the first run's answer (the same key with another request is refused); a
  client that disconnects without one cancels its run, and what was done is saved; streams send their headers and a keep-alive line at once; a gateway 504 on a chat call is
  not sent again (it may have been billed). Sampling parameters are listed in `taskpenny.ignored`; `n` above 1 is
  refused; `stream: "false"` is not a stream. Run ids get a random suffix.
- **Security**: wrong keys are throttled for the whole server (more than 20 a minute pause key checks); sessions
  last 30 days, survive a restart (kept hashed in the runs folder) and end with the new "Sign out" button; a slow
  client is dropped after two minutes; deeply nested JSON gets a clear error; examples use a generated key and
  publish the Docker port on 127.0.0.1.
- **No third-party requests from the page.** The IBM Plex fonts (SIL Open Font License) ship with the package and
  the demo page, instead of loading from Google Fonts.
- The page copies and shows the whole answer (it stopped at 30,000 characters), and its Catalan and Spanish texts
  are corrected ("Reprodueix", "Sobrecost", "propón"...).
- `taskpenny export` and the page's export handle damaged run files; `--profile` with an unknown name gets a clear
  error. Costs, token counts and scores a provider sends that cannot be real (negative, NaN, infinite) are not
  trusted; a cancelled run stops its caller (`RunCancelled` carries what was done); the page shows each task's
  tokens and time.
- An independent second review of these changes (a separate agent, with 300 randomized runs against the budget)
  found 15 bugs, all fixed with tests: 105 tests in all.
- **Measured against Claude Opus 5.5** on the 50 new tasks, in the default setup: 78% cheaper (95% interval 66 to
  86), as good or better in 50% (35 to 65). Against Sonnet 5 with Sonnet as ceiling the figures stand: about half
  the cost, as good or better in 82%. The README now leads with both, with their intervals and limits.
- Benchmark: `tasks.jsonl` rebuilds byte for byte again (sha256 `b9d7a83f…`); `bench/public/LICENSES.md` has the
  attributions and licence texts of the task data; `summarize.py --ci` prints 95% bootstrap intervals and says
  which runs ended early; `run.py --baseline-system worker` gives the baseline the same instruction as Taskpenny's
  workers, as a control for the judge's taste.

## v0.6.0

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
- Security, from the release review: without a key the server only answers to localhost, IP addresses and one-word
  names (DNS rebinding); `taskpenny ui` honours `TASKPENNY_API_KEY` and `--api-key`; `serve --dry-run` cannot be
  switched off from the page; each sign-in gets its own random session token, and signing out ends it; numbers
  from run files are escaped on the page; the export file name comes from the checked run id; key fragments that
  a provider echoes in an error are hidden.
- The API returns an error (502) when a run fails, instead of an empty answer; JSON mode (`response_format`) gets a
  clear error; content parts without a type, bare strings and assistant refusals are handled.
- A saved run file that is broken or of the wrong shape no longer breaks the list of runs; runs are listed by file
  name, and a user's own `demo-run.json` is never mixed up with the example.
- Budgets: `max_cost: 0` is refused like any other non-positive budget, and `--max-cost` must be a positive number.
- Benchmark: `--resume` reads results from before the rename; `summarize.py` prints the list-price figures by
  route too and skips empty rows. The blind check (`results/2026-09-25-blind20.json`) and the provider-key test
  (`results/2026-09-26-direct-keys.md`) are in the repository.
- Packaging: Python 3.11 in the classifiers and CI; the source package leaves out the benchmark data and media;
  a release runs the tests before publishing; hatchling 1.26 or later.

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
