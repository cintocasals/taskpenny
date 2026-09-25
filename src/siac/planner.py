"""The planner: a strong model that turns one request into a small set of well defined sub-tasks.

Planning quality is the bottleneck of task decomposition, so this is where SIAC spends on a strong model.
The planner writes decision sub-tasks (choose / yes-no / score) as closed questions so Jev can solve them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .gateway import GatewayLike, Usage

SYSTEM = """You are the planner of SIAC, a system that splits a request into sub-tasks so that each one can be
done by the cheapest AI model able to do it well, and then combined into one answer.

Write a plan as JSON, nothing else:
{
  "subtasks": [
    {
      "id": "t1",
      "title": "short title",
      "prompt": "a complete, self-contained instruction for this sub-task. Include every fact from the request that the sub-task needs. The worker will NOT see the original request.",
      "success_criteria": "how to tell the result is good, in one or two sentences",
      "answer_type": "text" | "choice" | "yesno" | "score",
      "decision": null,
      "depends_on": []
    }
  ],
  "assembly": "how to combine the sub-task results into the final answer (order, format, what to keep)"
}

Rules:
- Use the FEWEST sub-tasks that let cheap models do most of the work. {max_subtasks} at most. Never split a
  single piece of writing that needs one voice.
- "depends_on" lists the ids whose results this sub-task needs. Sub-tasks with no dependencies run in parallel.
- When a sub-task is only choosing an option, answering yes or no, or giving a score, set "answer_type" to
  "choice", "yesno" or "score" and fill "decision" so a decision model can answer it without writing:
    {"question": "the closed question, in English",
     "options": {"label": "what this option means", ...},      (choice only, 2 to 20 options)
     "scale": ["lowest", "...", "highest"],                      (score only, 2 to 10 steps)
     "labels": {"label or scale step": "how the reader sees it, in the language of the request", ...},
     "items": [{"id": "1", "text": "the exact text to judge"}, ...]}
  Put each item to classify as its own entry in "items", copied word for word from the request.
- Calculations, counting and dates are "text" tasks (a decision model cannot do arithmetic).
- Keep the language of the request for everything the user will read. Decision questions and options go in English.
"""


class PlanError(RuntimeError):
    """No usable plan. `usage` holds what the failed attempts cost, so the receipt can still count it."""

    def __init__(self, message: str, usage: Usage | None = None, model: str = ""):
        super().__init__(message)
        self.usage, self.model = usage or Usage(), model


RESERVED_IDS = {"__proto__", "constructor", "prototype", "root"}


def clean_id(raw: str, i: int) -> str:
    """Sub-task ids become short, safe tokens (letters, digits, - and _), never a special name."""
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", raw.strip())[:24] or f"t{i}"
    return f"t{i}_{sid}" if sid in RESERVED_IDS else sid


@dataclass
class SubtaskSpec:
    id: str
    title: str
    prompt: str
    success_criteria: str
    answer_type: str = "text"
    decision: dict | None = None
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Plan:
    subtasks: list[SubtaskSpec]
    assembly: str
    model: str
    usage: Usage
    latency_ms: int
    raw_text: str = ""


def _extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise PlanError("the planner did not return JSON")
    return json.loads(text[start:end + 1])


def parse_plan(data: dict, max_subtasks: int) -> tuple[list[SubtaskSpec], str]:
    raw = data.get("subtasks") or []
    if not isinstance(raw, list) or not raw:
        raise PlanError("the plan has no sub-tasks")
    subs: list[SubtaskSpec] = []
    seen: set[str] = set()
    rename: dict[str, str] = {}
    for i, s in enumerate(raw[:max_subtasks], 1):
        if not isinstance(s, dict):
            raise PlanError(f"sub-task {i} is not an object")
        orig = str(s.get("id") or f"t{i}")
        sid = clean_id(orig, i)
        if sid in seen:
            sid = f"{sid}_{i}"
        seen.add(sid)
        rename.setdefault(orig, sid)
        at = str(s.get("answer_type") or "text").lower()
        dec = s.get("decision") if isinstance(s.get("decision"), dict) else None
        if at not in ("text", "choice", "yesno", "score"):
            at = "text"
        if at != "text":
            dec = _clean_decision(at, dec)
            if dec is None:  # not usable by Jev: a language model will do it
                at = "text"
        subs.append(SubtaskSpec(
            id=sid, title=str(s.get("title") or sid)[:120], prompt=str(s.get("prompt") or "").strip(),
            success_criteria=str(s.get("success_criteria") or "").strip(), answer_type=at, decision=dec,
            depends_on=[str(x) for x in (s.get("depends_on") or []) if str(x)],
        ))
    for s in subs:
        s.depends_on = [rename.get(d, d) for d in s.depends_on]
    ids = {s.id for s in subs}
    for s in subs:  # drop unknown or self dependencies, then break cycles by order
        s.depends_on = [d for d in s.depends_on if d in ids and d != s.id]
    order = {s.id: i for i, s in enumerate(subs)}
    for s in subs:
        s.depends_on = [d for d in s.depends_on if order[d] < order[s.id]]
    for s in subs:
        if not s.prompt:
            raise PlanError(f"sub-task {s.id} has no prompt")
    return subs, str(data.get("assembly") or "Combine the results in order into one clear answer.")


def _clean_decision(kind: str, dec: dict | None) -> dict | None:
    if not dec or not str(dec.get("question") or "").strip():
        return None
    items = dec.get("items")
    if not isinstance(items, list) or not items:
        return None
    items = [{"id": str(it.get("id") or i), "text": str(it.get("text") or "").strip()}
             for i, it in enumerate(items, 1) if isinstance(it, dict) and str(it.get("text") or "").strip()]
    if not items:
        return None
    out = {"kind": kind, "question": str(dec["question"]).strip(), "items": items}
    labels = dec.get("labels")
    if isinstance(labels, dict):
        out["labels"] = {str(k): str(v) for k, v in labels.items() if str(v).strip()}
    if kind == "choice":
        opts = dec.get("options")
        if isinstance(opts, list):
            opts = {str(o): str(o) for o in opts}
        if not isinstance(opts, dict) or not (2 <= len(opts) <= 255):
            return None
        out["options"] = {str(k): str(v) for k, v in opts.items()}
    if kind == "score":
        scale = dec.get("scale")
        if not isinstance(scale, list) or not (2 <= len(scale) <= 10):
            return None
        out["scale"] = [str(x) for x in scale]
    return out


class Planner:
    def __init__(self, gateway: GatewayLike, model: str, max_subtasks: int = 12, fallbacks: list[str] | None = None,
                 reasoning: str | None = "low"):
        self.gw, self.model, self.max_subtasks, self.reasoning = gateway, model, max_subtasks, reasoning
        self.fallbacks = [m for m in (fallbacks or []) if m != model]

    async def plan(self, request: str, context: str = "", can_retry=None) -> Plan:
        """Ask for a plan; one more try if the first is not valid JSON and `can_retry()` (the budget) allows it."""
        from .gateway import GatewayError
        system = SYSTEM.replace("{max_subtasks}", str(self.max_subtasks))
        user = request if not context else f"{request}\n\nContext from earlier steps:\n{context}"
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        total, ms, last_err, model = Usage(), 0, None, self.model
        for attempt in range(2):
            if attempt and can_retry is not None and not can_retry():
                break
            try:
                r = await self._call(messages)
            except GatewayError as e:
                if not attempt:
                    raise
                last_err = e
                break
            model = r.model
            total.tokens_in += r.usage.tokens_in
            total.tokens_out += r.usage.tokens_out
            total.cost += r.usage.cost
            ms += r.latency_ms
            try:
                subs, assembly = parse_plan(_extract_json(r.text), self.max_subtasks)
                return Plan(subs, assembly, r.model, total, ms, r.text)
            except (PlanError, ValueError) as e:
                last_err = e
                messages += [{"role": "assistant", "content": r.text},
                             {"role": "user", "content": f"That was not a valid plan ({e}). Reply with the JSON plan only."}]
        raise PlanError(str(last_err), usage=total, model=model)

    async def _call(self, messages):
        from .gateway import GatewayError
        models = [self.model] + self.fallbacks
        for i, m in enumerate(models):
            try:
                return await self.gw.chat(m, messages, max_tokens=6000, temperature=0.2, json_mode=True,
                                          reasoning=self.reasoning)
            except GatewayError as e:
                if i + 1 == len(models) or e.status not in (0, 402, 403, 404, 408, 429, 500, 502, 503, 504, 529):
                    raise
