# How Taskpenny works

Taskpenny turns one request into an answer in five stages. Every decision in it is a closed question (split or
not, which tier, is this result good enough), and closed questions are what Jev, TypeSafe AI's decision model,
answers well and cheaply: about 4 cents per thousand decisions at its list price (about 1,000 tokens each in our
benchmark), with a probability and a confidence for each. Language models only write.

Jev is required. It is reached through Vercel AI Gateway, so a real run needs `AI_GATEWAY_API_KEY`; without it,
Taskpenny only simulates (`--dry-run`) or replays (`taskpenny demo`). Provider keys and local models can be added
next to it for the work itself, never instead of it.

## 1. The gate

Jev reads the request and answers four questions at once:

| Question | Answers |
|---|---|
| Would splitting help? | a probability |
| Which tier does it need, done in one go? | 1 basic, 2 standard, 3 advanced, 4 critical (descriptions in `models.yaml`) |
| What kind of work is it? | code, writing, analysis, research, maths, translation |
| What form does the answer take? | text, choice, yes/no, score |

Two rules adjust the tier:

- **Doubt that points up raises it.** If Jev's confidence in the tier is under 0.6 and at least a quarter of its
  probability sits on higher tiers, Taskpenny goes one tier up. Doubt towards lower tiers is not a reason to pay more.
- **Some work has a floor.** Maths and code start at tier 2 at least (`min_tier` in `models.yaml`): the
  cheapest models slip on arithmetic, logic and code.

## 2. Split or not

Taskpenny splits only when it can pay off:

- never deeper than three levels, never a request under 160 characters, never a request Jev first judged tier 1
  (the tier before any raise for doubt or floor for maths and code), and never more than 12 subtasks in a whole
  run, counting every level;
- Jev must be clearly sure (probability 0.9 or more), or fairly sure (0.7 or more) when the request shows a
  list of separate parts.

## 3. The planner

A planner model writes the subtasks as JSON: goal, prompt, success criteria, the form of the answer and what
each one depends on. Critical (tier 4) requests get the strong planner; the rest get a lighter one, because
their parts are usually visible in the request itself. When a part is a closed question over a list of items
(label each message, answer yes or no for each case, 50 items at most), the planner marks it so Jev can answer
it directly. A plan with a single part, or one that cannot be read or paid for, is dropped: the request is done
in one go.

Before any part runs, Jev gates every part (it costs almost nothing) and Taskpenny compares the typical cost of the
parts' tiers with the whole request's tier. If the parts would not be at least 1.3 times cheaper on average, the
plan is dropped and the request is done in one go: at advanced level the parts tend to need the same models as
the whole, and splitting then only adds cost.

Subtasks run in waves: everything whose dependencies are done runs in parallel (four at a time by default),
and each result passes its hand-off notes to the tasks that need them. Every subtask goes back through the gate,
so it gets its own tier.

## 4. Doing the work

- **Closed questions go to Jev.** One call per item, so no item is judged by the others; when several decisions
  are about the same items, one call per item answers all of them. Items where Jev's confidence is under 0.6 go
  to a basic language model instead.
- **A request that is itself a closed question goes to Jev too.** When the gate says, with confidence 0.8 or
  more, that the answer is a choice, a yes/no or a score, the request is tier 1 or 2 as first judged, and it is
  not maths or code, the cheapest model writes the closed question and points at the lines of the request that hold the items (it does
  not copy them, so nothing is changed on the way). Jev answers each item. If the request does not fit (the
  options are not given, it is a quiz question whose answer needs knowledge or reasoning, the answer needs
  arithmetic), it goes to a language model as usual. Jev judges texts against criteria; it does not solve problems.
- **Everything else goes to the cheapest model of its tier** that your keys and profile allow.
- **Every result is checked.** Jev estimates how likely the result meets its success criteria. Under 0.6 it is
  repaired with that feedback; the second repair goes one tier up. Two repairs at most. If no attempt passes,
  the best one is kept and the task is marked **unverified**. A verdict Jev cannot give (its answer is missing
  twice, or the call fails) never pays for a repair: the result is kept and marked unverified too.
- **Refusals and errors fall back** to the next model of the same tier that costs at most 1.5 times as much,
  then to the cheapest model of each tier below. A fallback never costs much more than the first choice.
- **Hidden reasoning is billed as output**, so it is off for tiers 1 and 2 and low for tiers 3 and 4. A model
  that spends its whole output on reasoning and returns nothing moves on to the next model.

## 5. Assembly and the final check

The parts are not rewritten. A model writes a short outline (an intro, the order of the parts, a heading for
each, a closing line) and Taskpenny stitches the parts in under it; if that model fails, the parts are stitched
in plan order under their own titles. Jev then checks the whole answer against the request; if it scores under
0.4, a cheap model writes only what is missing.

## Nothing fails in silence

Every run ends with one of four statuses, and a list of warnings that explains anything but the first:

| Status | Meaning | Through the API |
|---|---|---|
| `done` | Every result passed Jev's check. | `finish_reason: "stop"` |
| `unverified` | The answer is complete, but some result did not pass a check, or a check could not run. | `"stop"`, with `taskpenny.status` and `taskpenny.warnings` |
| `partial` | Something is missing: a part failed, the budget ran out, or the client went away. | `"length"` (Responses: `incomplete`) |
| `failed` | No usable answer. | HTTP 502 with the reason |

The status is also in the `X-Taskpenny-Status` header, on the live page (with a visible list of warnings) and in
the run file.

## Money

- A budget per run (0.50 USD by default), and it is a ceiling. Before every call, Jev's included, Taskpenny
  reserves the most it can cost: all the output it allows, and the input counted cautiously (3 characters per
  token for plain ASCII, one token per character for anything else, such as accents, Chinese or Japanese). Calls
  reserve inside their slot (four language model calls and eight Jev calls at a time), so waiting calls hold
  nothing. A call that does not fit waits for running calls to settle; when nothing else is running and it still
  does not fit, its answer is shortened to what the budget can pay (if that still leaves 1,000 tokens), or the
  call is not made and the run ends as partial with what it has. The message says what the run really spent.
- The receipt adds up planning, decisions, work, checks and assembly. Through Vercel AI Gateway every cost is
  the one the gateway reports; with direct keys it is worked out from the catalog prices.
- The comparison on the receipt is an estimate: the same request sent once to the baseline model, with an answer
  as long as Taskpenny's, scaled by the hidden reasoning Taskpenny's own models used, and no retries. The public benchmark
  runs the baseline for real instead.

## What you can change

Every threshold above lives in `Settings` (`src/taskpenny/decider.py`) and `Limits` (`src/taskpenny/engine.py`), and the
models, tiers, prices, planners, floors and profiles live in `models.yaml`. See [configuration](configuration.md).
