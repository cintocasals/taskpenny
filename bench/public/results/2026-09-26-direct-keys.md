# Provider keys without Vercel · 26 September 2026

> **Historical.** Since v0.7.0 Taskpenny requires Jev, reached through Vercel AI Gateway, and the stand-in decider
> tested here is gone. Provider keys still work next to the Vercel key (`TASKPENNY_DIRECT`).

One request, run three times, each time with a single provider key and no Vercel AI Gateway key, so a small model
of that provider stood in for Jev. Costs are worked out from the catalog prices (direct calls do not report a cost).

Request: *Write a two-sentence email moving tomorrow meeting to Thursday, and label this comment as positive,
negative or neutral: "Too expensive".*

| Key set | Decider (stand-in for Jev) | Done by | Calls | Cost | Time | Both parts answered |
|---|---|---|---|---|---|---|
| `OPENAI_API_KEY` | openai/gpt-6-luna | openai/gpt-6-sol (tier 2) | 3 | $0.00136 | 9.5 s | yes |
| `ANTHROPIC_API_KEY` | anthropic/claude-haiku-4.5 | anthropic/claude-haiku-4.5 (tier 2), one repair after a failed check | 5 | $0.00415 | 5.8 s | yes |
| `GEMINI_API_KEY` | google/gemini-3.5-flash-lite | google/gemini-3.5-flash-lite (tier 2) | 3 | $0.00086 | 3.3 s | yes |

With Anthropic alone the run cost more than the estimate for one Claude Opus call: for a request this small, the
stand-in decider's calls cost more than the work. `taskpenny doctor` was run first with each key.
