# How Taskpenny works

Taskpenny turns one request into an answer in five stages. Every decision in it is a closed question (split or
not, which tier, is this result good enough), and closed questions are what Jev, TypeSafe AI's decision model,
answers well and cheaply: about 3 cents per thousand decisions, with a probability and a confidence for each.
Language models only write.

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

- never deeper than three levels, never a request under 160 characters, never a tier 1 request;
- Jev must be clearly sure (probability 0.9 or more), or fairly sure (0.7 or more) when the request shows a
  list of separate parts.

## 3. The planner

A planner model writes the subtasks as JSON: goal, prompt, success criteria, the form of the answer and what
each one depends on. Critical (tier 4) requests get the strong planner; the rest get a lighter one, because
their parts are usually visible in the request itself. When a part is a closed question over a list of items
(label each message, answer yes or no for each case), the planner marks it so Jev can answer it directly.

Before any part runs, Jev gates every part (it costs almost nothing) and Taskpenny compares the typical cost of the
parts' tiers with the whole request's tier. If the parts would not be at least 1.3 times cheaper on average, the
plan is dropped and the request is done in one go: at advanced level the parts tend to need the same models as
the whole, and splitting then only adds cost.

Subtasks run in waves: everything whose dependencies are done runs in parallel (four at a time by default),
and each result passes its hand-off notes to the tasks that need them. Every subtask goes back through the gate,
so it gets its own tier.

## 4. Doing the work

- **Closed questions go to Jev.** One call per item, so no item is judged by the others. Items where Jev's
  confidence is under 0.6 go to a basic language model instead.
- **Everything else goes to the cheapest model of its tier** that your keys and profile allow.
- **Every result is checked.** Jev estimates how likely the result meets its success criteria. Under 0.6 it is
  repaired with that feedback; the second repair goes one tier up. Two repairs at most.
- **Refusals and errors fall back** to the next model of the same tier that costs at most 1.5 times as much,
  then to the best model of the tiers below. A fallback never costs much more than the first choice.
- **Hidden reasoning is billed as output**, so it is off for tiers 1 and 2 and low for tiers 3 and 4. A model
  that spends its whole output on reasoning and returns nothing moves on to the next model.

## 5. Assembly and the final check

The parts are not rewritten. A model writes a short outline (an intro, the order of the parts, a heading for
each, a closing line) and Taskpenny stitches the parts in under it. Jev then checks the whole answer against the
request; if it scores under 0.4, a cheap model writes only what is missing.

## Money

- A budget per run (0.50 USD by default). Before each call Taskpenny reserves its worst case; a call that does not fit
  is not made, and the run ends as partial with what it has.
- The receipt adds up planning, decisions, work, checks and assembly. Through Vercel AI Gateway every cost is
  the one the gateway reports; with direct keys it is worked out from the catalog prices.
- The comparison on the receipt is an estimate: the same request sent once to the baseline model, with an answer
  as long as Taskpenny's, scaled by the hidden reasoning Taskpenny's own models used, and no retries. The public benchmark
  runs the baseline for real instead.

## What you can change

Every threshold above lives in `Settings` (`src/taskpenny/decider.py`) and `Limits` (`src/taskpenny/engine.py`), and the
models, tiers, prices, planners, floors and profiles live in `models.yaml`. See [configuration](configuration.md).
