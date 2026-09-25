# SIAC

**Split a big prompt into small tasks and send each one to the cheapest model that can do it well.**

> Status: work in progress. v0.1 (the core loop, from the terminal) is being built. Not ready for use yet.

Most prompts don't need your most expensive model for every part of the job. SIAC works like a good project lead: it decides whether a request is worth splitting, breaks it into small, well defined tasks, gives each task to the cheapest model that can handle it, checks every result before moving on, and puts everything together into one answer. You watch the whole process live, and at the end you get a receipt that compares what it cost with what it would have cost to send everything to the strongest model.

The decisions (split or not, which tier of model, is this result good enough) are made by [Jev](https://typesafe.ai), TypeSafe AI's decision model, which costs about 3 cents per thousand decisions. When a task is simply a choice (pick an option, answer yes or no, give a score), Jev solves it directly: no language model needed.

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

## Roadmap

| Version | What |
|---|---|
| v0.1 | Core loop from the terminal: gate, planner, router, executor, verifier, aggregator, cost receipt |
| v0.2 | Live task tree in the browser, run replay, English, Catalan and Spanish UI |
| v0.3 | Public benchmark: SIAC against a single strong model |
| v0.4 | OpenAI-compatible endpoint, direct provider keys, local models with Ollama, Docker |
| v1.0 | Public release |

## Models

The catalog lives in [`src/siac/models.yaml`](src/siac/models.yaml): one entry per model with its tiers, prices and context. Adding a model is adding a few lines.

## License

MIT. Made by [Cinto Casals](https://cintocasals.com) · SerIA Nativa.
