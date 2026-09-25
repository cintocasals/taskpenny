"""Every closed decision in Taskpenny goes through Jev: split or not, which tier, is a result good enough.

Jev also *solves* tasks whose answer is a choice, a yes/no or a score (idea 18): no language model needed.
Questions and criteria are written in English, which is where Jev is most accurate, whatever the
language of the request.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from .catalog import Catalog
from .gateway import EvalResult, GatewayLike, Usage

ANSWER_TYPES = {
    "text": "The answer has to be written: an explanation, a draft, code, an analysis, a translation, a list, "
            "a calculation, or several things at once.",
    "choice": "The whole answer is picking ONE option from a small set of options that the request gives or "
              "clearly implies (a category, a label, one of several named alternatives).",
    "yesno": "The whole answer is yes or no about a text or fact given in the request.",
    "score": "The whole answer is a rating on an ordered scale (for example low to critical, or 1 to 5).",
}

SPLIT_CRITERIA = {
    "true": "The request asks for several distinct deliverables or steps (for example a list of numbered parts, "
            "or 'do X, then Y, then Z') that different people could each do separately and then combine.",
    "false": "The request is one single piece of work: a question, one text that needs a single voice (a poem, "
             "an email, a story, a post), one piece of code, one analysis, or anything short.",
}

MAX_STATE_CHARS = 60_000  # Jev reads up to about 32k tokens of state; keep a wide margin.


LIST_MARK = re.compile(r"(?:^|\s)(?:\d{1,2}[).]|[a-z][).]|[-*\u2022])\s+\S", re.M)


def has_parts(text: str) -> bool:
    """At least two numbered, lettered or bulleted items: a visible list of separate parts."""
    return len(LIST_MARK.findall(text)) >= 2


def clip(text: str, limit: int = MAX_STATE_CHARS) -> str:
    return text if len(text) <= limit else text[: limit // 2] + "\n[...]\n" + text[-limit // 2:]


@dataclass
class Gate:
    split_probability: float
    tier: int
    tier_raw: int
    tier_confidence: float | None
    task_type: str
    answer_type: str
    answer_confidence: float | None
    usage: Usage
    latency_ms: int
    raised: bool = False  # tier went up one level because Jev was unsure
    floored: bool = False  # tier went up to the minimum for this kind of task (models.yaml min_tier)
    probabilities: dict[str, Any] = field(default_factory=dict)

    @property
    def split(self) -> bool:
        return self.split_probability >= 0.5


@dataclass
class Settings:
    """Thresholds. Starting values from TypeSafe and Vercel guidance; the benchmark calibrates them."""
    tier_confidence_min: float = 0.6    # below this, go one tier up...
    raise_upward_min: float = 0.25      # ...if at least this much probability sits on higher tiers
    decision_confidence_min: float = 0.6  # below this, a choice task goes to a language model instead
    verify_pass: float = 0.6            # probability that a result meets its criteria to accept it
    min_split_chars: int = 160          # shorter requests are never split
    split_min: float = 0.7              # Jev's probability needed to split a request with a list of parts
    split_sure: float = 0.9             # ... or to split one without a visible list of parts
    final_pass: float = 0.4             # the final answer only gets a gap-filling pass below this
    split_overhead: float = 1.3         # splitting must make the typical call this much cheaper to go ahead


class Decider:
    def __init__(self, gateway: GatewayLike, catalog: Catalog, settings: Settings | None = None):
        self.gw, self.catalog = gateway, catalog
        self.s = settings or Settings()

    # ----------------------------------------------------------------- the gate
    async def gate(self, request: str) -> Gate:
        tiers = {str(k): f"{v['name']}: {v['description']}" for k, v in sorted(self.catalog.tiers.items())}
        questions = {
            "split": {
                "type": "boolean",
                "instructions": "Would it help to split this request into separate sub-tasks, each done on its own "
                                "and then combined into one answer?",
                "criteria": SPLIT_CRITERIA,
            },
            "tier": {
                "type": "choice",
                "instructions": "What level of AI model does this request need to be answered well, "
                                "if it were done in one go?",
                "criteria": tiers,
            },
            "task_type": {
                "type": "choice",
                "instructions": "What kind of work is this request mainly?",
                "criteria": self.catalog.task_types,
            },
            "answer": {
                "type": "choice",
                "instructions": "What form does the answer to this request take?",
                "criteria": ANSWER_TYPES,
            },
        }
        r: EvalResult = await self.gw.evaluate({"request": clip(request)}, questions)
        split_p = r.boolean("split") or 0.0
        tier_s, tier_c, tier_p = r.choice("tier")
        tier_raw = int(tier_s) if tier_s and tier_s.isdigit() else 3
        tier, raised = tier_raw, False
        # Go one tier up only when Jev is unsure AND a real share of its doubt points to a higher tier.
        # Doubt towards lower tiers is not a reason to pay for a stronger model.
        upward = sum(float(v) for k, v in (tier_p or {}).items() if str(k).isdigit() and int(k) > tier_raw)
        if (tier_c is None or tier_c < self.s.tier_confidence_min) and (not tier_p or upward >= self.s.raise_upward_min):
            tier, raised = min(4, tier_raw + 1), True
        ttype, _, _ = r.choice("task_type")
        ans, ans_c, _ = r.choice("answer")
        floor = self.catalog.min_tier.get(ttype or "", 1)
        floored = tier < floor
        tier = max(tier, floor)
        return Gate(split_probability=split_p, tier=tier, tier_raw=tier_raw, tier_confidence=tier_c,
                    task_type=ttype or "analysis", answer_type=ans or "text", answer_confidence=ans_c,
                    usage=r.usage, latency_ms=r.latency_ms, raised=raised, floored=floored,
                    probabilities={"tier": tier_p})

    def should_split(self, gate: Gate, request: str, depth: int, max_depth: int) -> bool:
        """Split only when it can pay off: never deep, short or trivial requests; and Jev must be clearly sure,
        or fairly sure and the request shows a list of separate parts (numbered items or bullets)."""
        if depth >= max_depth:
            return False
        if len(request) < self.s.min_split_chars:
            return False
        if gate.tier_raw <= 1:  # a trivial task costs more to plan than to do
            return False
        p = gate.split_probability
        return p >= self.s.split_sure or (p >= self.s.split_min and has_parts(request))

    # ------------------------------------------------- Jev as executor (idea 18)
    async def solve(self, spec: dict) -> dict:
        """Answer a choice / yesno / score task directly.

        spec = {"kind": "choice"|"yesno"|"score", "question": str, "options": {label: description} (choice),
                "scale": [low, ..., high] (score), "items": [{"id", "text"}] or "text": str}
        Returns {"answers": [{"id", "answer", "confidence"}], "usage": Usage, "latency_ms": int,
                 "unsure": [ids]} where unsure items fell below the confidence threshold.
        """
        kind = spec["kind"]
        q: dict[str, Any] = {"instructions": spec["question"]}
        if kind == "choice":
            q.update(type="choice", criteria=spec["options"])
        elif kind == "score":
            q.update(type="score", criteria=list(spec["scale"]))
        else:
            q.update(type="boolean")
        items = spec.get("items") or [{"id": "1", "text": spec.get("text", "")}]
        results, total, ms, unsure = [], Usage(), 0, []
        # One call per item: each item is its own state, so Jev never judges one item by another.
        outs = await asyncio.gather(*(self.gw.evaluate({"text": clip(it["text"])}, {"a": q}) for it in items))
        for it, r in zip(items, outs):
            total.tokens_in += r.usage.tokens_in
            total.cost += r.usage.cost
            ms = max(ms, r.latency_ms)
            if kind == "choice":
                ans, conf, _ = r.choice("a")
            elif kind == "score":
                val, conf = r.score("a")
                scale = list(spec["scale"])
                ans = scale[max(0, min(len(scale) - 1, round(val or 0)))] if val is not None else None
            else:
                p = r.boolean("a")
                ans = None if p is None else ("yes" if p >= 0.5 else "no")
                conf = None if p is None else abs(p - 0.5) * 2
            if ans is None or conf is None or conf < self.s.decision_confidence_min:
                unsure.append(it["id"])
            results.append({"id": it["id"], "answer": ans, "confidence": conf})
        return {"answers": results, "usage": total, "latency_ms": ms, "unsure": unsure}

    # ------------------------------------------------------------ verification
    async def verify(self, task: str, criteria: str, result: str) -> tuple[float, Usage, int]:
        r = await self.gw.evaluate(
            {"task": clip(task, 12_000), "success_criteria": criteria or "Does what the task asks.",
             "result": clip(result, 40_000)},
            {"ok": {"type": "boolean",
                    "instructions": "Does the result do what the task asks and meet the success criteria?",
                    "criteria": {"true": "It meets the criteria: complete, on topic, in the requested form.",
                                 "false": "It misses part of the task, is off topic, is empty, refuses, or ignores "
                                          "the requested form."}}},
        )
        return (r.boolean("ok") or 0.0), r.usage, r.latency_ms

    async def final_check(self, request: str, answer: str) -> tuple[float, Usage, int]:
        return await self.verify(request, "Answers everything the request asks for, in the language of the request.",
                                 answer)
