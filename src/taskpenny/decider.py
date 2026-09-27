"""Every closed decision in Taskpenny goes through Jev: split or not, which tier, is a result good enough.

Jev also *solves* tasks whose answer is a choice, a yes/no or a score (idea 18): the planner writes them as
closed questions, and a request that is itself such a task gets its question and items pulled out by the
cheapest model (EXTRACT) so that Jev can answer it. Items Jev is unsure about go to a cheap language model.
Questions and criteria are written in English, which is where Jev is most accurate, whatever the
language of the request.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .catalog import Catalog
from .gateway import EvalResult, GatewayError, GatewayLike, Usage

# "For each item of a list" lets Jev see that labelling ten messages is a choice, not writing (checked on
# 27 September 2026: the 42 development cases are typed exactly as before, and lists of labels, yes/no or scores
# go from "text" to their own type while mixed requests stay "text").
ANSWER_TYPES = {
    "text": "The answer has to be written: an explanation, a draft, code, an analysis, a translation, a summary, "
            "a calculation, or several different things at once.",
    "choice": "The whole answer is picking one option from a small set of options that the request gives or clearly "
              "implies (a category, a label, one of several named alternatives), for one item or for each item of a "
              "list given in the request, and nothing else.",
    "yesno": "The whole answer is yes or no about a text or fact given in the request, or about each item of a list "
             "given in the request, and nothing else.",
    "score": "The whole answer is a rating on an ordered scale (for example low to critical, or 1 to 5), for one item "
             "or for each item of a list given in the request, and nothing else.",
}

SPLIT_CRITERIA = {
    "true": "The request asks for several distinct deliverables or steps (for example a list of numbered parts, "
            "or 'do X, then Y, then Z') that different people could each do separately and then combine.",
    "false": "The request is one single piece of work: a question, one text that needs a single voice (a poem, "
             "an email, a story, a post), one piece of code, one analysis, or anything short.",
}

MAX_STATE_CHARS = 60_000  # Jev reads up to about 32k tokens of state; keep a wide margin.
NO_JEV_TYPES = ("math", "code")  # kinds of work Jev never answers itself
MAX_JEV_ITEMS = 50  # more items than this go to a language model: one Jev call per item adds up

EXTRACT = (
    "A decision model will answer the request below item by item, without writing: for each item it can only "
    "pick one option from a list, answer yes or no, or give a score on a scale. Decide whether the request fits "
    "that, and if it does, write the closed question for it.\n\n"
    "The request is shown with numbered lines (L1, L2, ...). Reply with JSON only:\n"
    '{"fits": true or false,\n'
    ' "kind": "choice" or "yesno" or "score",\n'
    ' "question": "the closed question in English, as it applies to ONE item",\n'
    ' "options": {"label": "what this option means"},\n'
    ' "scale": ["lowest", "...", "highest"],\n'
    ' "labels": {"label or scale step": "how the reader sees it, in the language of the request"},\n'
    ' "items": [[first line, last line], ...]}\n'
    '"options" only for choice (2 to 20, with the labels the request uses); "scale" only for score (2 to 10 '
    'steps). "items" gives the lines that hold each item to judge, one pair per item, never the instruction '
    "lines. It fits when each item is a text given in the request, to be labelled, answered yes or no, or "
    "scored against criteria the request gives. It does NOT fit a quiz, exam or multiple-choice question whose "
    "right answer needs knowledge, calculation or reasoning (the decision model judges texts, it does not solve "
    "problems), nor when the answer needs writing, shown reasoning, counting, arithmetic, dates or comparing "
    "numbers, when the options are not given or clearly implied, or when there is nothing specific to judge."
)

LIST_MARK = re.compile(r"(?:^|\s)(?:\d{1,2}[).]|[a-z][).]|[-*•])\s+\S", re.M)


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
    decision_confidence_min: float = 0.6  # below this, a choice item goes to a language model instead
    verify_pass: float = 0.6            # probability that a result meets its criteria to accept it
    min_split_chars: int = 160          # shorter requests are never split
    split_min: float = 0.7              # Jev's probability needed to split a request with a list of parts
    split_sure: float = 0.9             # ... or to split one without a visible list of parts
    final_pass: float = 0.4             # the final answer only gets a gap-filling pass below this
    split_overhead: float = 1.3         # splitting must make the typical call this much cheaper to go ahead
    jev_answer_min: float = 0.8         # Jev's confidence that a request is a choice / yes-no / score task...
    jev_answer_max_tier: int = 2        # ...at most this tier, for Jev to answer it instead of a language model


class Decider:
    """Every method takes an optional gateway: the engine passes one that waits for a slot, reserves the budget
    and books the cost of each Jev call on the right task."""

    def __init__(self, gateway: GatewayLike, catalog: Catalog, settings: Settings | None = None):
        self.gw, self.catalog = gateway, catalog
        self.s = settings or Settings()

    # ----------------------------------------------------------------- the gate
    async def gate(self, request: str, gw: GatewayLike | None = None) -> Gate:
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
        r: EvalResult = await (gw or self.gw).evaluate({"request": clip(request)}, questions)
        split_p = r.boolean("split") or 0.0
        tier_s, tier_c, tier_p = r.choice("tier")
        tier_s = str(tier_s or "").strip()
        tier_raw = int(tier_s) if tier_s.isdigit() and 1 <= int(tier_s) <= 4 else 3
        tier, raised = tier_raw, False
        # Go one tier up only when Jev is unsure AND a real share of its doubt points to a higher tier.
        # Doubt towards lower tiers is not a reason to pay for a stronger model.
        upward = sum(v for k, v in (tier_p or {}).items() if str(k).isdigit() and int(k) > tier_raw)
        if (tier_c is None or tier_c < self.s.tier_confidence_min) and (not tier_p or upward >= self.s.raise_upward_min):
            tier, raised = min(4, tier_raw + 1), True
        ttype, _, _ = r.choice("task_type")
        ans, ans_c, _ = r.choice("answer")
        ttype = ttype if ttype in self.catalog.task_types else None
        ans = ans if ans in ANSWER_TYPES else None
        floor = self.catalog.min_tier.get(ttype or "", 1)
        floored = tier < floor
        tier = max(tier, floor)
        return Gate(split_probability=split_p, tier=tier, tier_raw=tier_raw, tier_confidence=tier_c,
                    task_type=ttype or "analysis", answer_type=ans or "text", answer_confidence=ans_c,
                    usage=r.usage, latency_ms=r.latency_ms, raised=raised, floored=floored,
                    probabilities={"tier": tier_p})

    def jev_can_answer(self, gate: Gate) -> bool:
        """The request is itself a choice, a yes/no or a score, simple enough for Jev to answer (idea 18). Never
        maths or code: a multiple-choice sum is still a sum, and Jev does not calculate."""
        return (gate.answer_type in ("choice", "yesno", "score")
                and (gate.answer_confidence or 0.0) >= self.s.jev_answer_min
                and gate.tier_raw <= self.s.jev_answer_max_tier
                and gate.task_type not in NO_JEV_TYPES)

    def should_split(self, gate: Gate, request: str, depth: int, max_depth: int, room: int = 12) -> bool:
        """Split only when it can pay off: never deep, short or trivial requests, nor when the run has no room
        for two more sub-tasks; and Jev must be clearly sure, or fairly sure and the request shows a list of
        separate parts (numbered items or bullets). Trivial means tier 1 as Jev first judged it, before a raise
        or a minimum tier for the kind of task."""
        if depth >= max_depth or room < 2:
            return False
        if len(request) < self.s.min_split_chars:
            return False
        if gate.tier_raw <= 1:  # a trivial task costs more to plan than to do
            return False
        p = gate.split_probability
        return p >= self.s.split_sure or (p >= self.s.split_min and has_parts(request))

    # ------------------------------------------------- Jev as executor (idea 18)
    @staticmethod
    def _question(spec: dict) -> dict[str, Any]:
        q: dict[str, Any] = {"instructions": spec["question"]}
        if spec["kind"] == "choice":
            q.update(type="choice", criteria=spec["options"])
        elif spec["kind"] == "score":
            q.update(type="score", criteria=list(spec["scale"]))
        else:
            q.update(type="boolean")
        return q

    async def solve(self, spec: dict, gw: GatewayLike | None = None) -> dict:
        """Answer a choice / yesno / score task directly.

        spec = {"kind": "choice"|"yesno"|"score", "question": str, "options": {label: description} (choice),
                "scale": [low, ..., high] (score), "items": [{"id", "text"}] or "text": str}
        Returns {"answers": [{"id", "answer", "confidence"}], "usage": Usage, "latency_ms": int,
                 "unsure": [ids]} where unsure items fell below the confidence threshold.
        """
        return (await self.solve_many([spec], gw))[0]

    async def solve_many(self, specs: list[dict], gw: GatewayLike | None = None) -> list[dict]:
        """Several decisions about the same items: one Jev call per item answers all of them (proposal 15).
        Each item is its own state, so Jev never judges one item by another. The calls are booked on the
        first decision of the group."""
        g = gw or self.gw
        items = specs[0].get("items") or [{"id": "1", "text": specs[0].get("text", "")}]
        questions = {f"q{i}": self._question(s) for i, s in enumerate(specs)}
        outs = await asyncio.gather(*(g.evaluate({"text": clip(it["text"])}, questions) for it in items),
                                    return_exceptions=True)
        for o in outs:  # a budget stop or a cancellation stops everything; a failed item is only unsure
            if isinstance(o, BaseException) and not isinstance(o, GatewayError):
                raise o
        results = [{"answers": [], "usage": Usage(), "latency_ms": 0, "unsure": []} for _ in specs]
        for it, r in zip(items, outs):
            if isinstance(r, GatewayError):  # Jev could not answer this item: a language model will
                for res in results:
                    res["unsure"].append(it["id"])
                    res["answers"].append({"id": it["id"], "answer": None, "confidence": None})
                continue
            for i, spec in enumerate(specs):
                name, res = f"q{i}", results[i]
                if spec["kind"] == "choice":
                    ans, conf, _ = r.choice(name)
                    if ans is not None and ans not in spec["options"]:
                        ans = None  # not one of the options: a language model answers it
                elif spec["kind"] == "score":
                    val, conf = r.score(name)
                    scale = list(spec["scale"])
                    ans = scale[max(0, min(len(scale) - 1, round(val)))] if val is not None else None
                else:
                    p = r.boolean(name)
                    ans = None if p is None else ("yes" if p >= 0.5 else "no")
                    conf = None if p is None else abs(p - 0.5) * 2
                if ans is None or conf is None or conf < self.s.decision_confidence_min:
                    res["unsure"].append(it["id"])
                res["answers"].append({"id": it["id"], "answer": ans, "confidence": conf})
                res["latency_ms"] = max(res["latency_ms"], r.latency_ms)
            results[0]["usage"].tokens_in += r.usage.tokens_in
            results[0]["usage"].cost += r.usage.cost
        return results

    # ------------------------------------------------------------ verification
    async def verify(self, task: str, criteria: str, result: str,
                     gw: GatewayLike | None = None) -> tuple[float | None, Usage, int]:
        """The probability that the result meets the criteria, or None when Jev's verdict could not be read
        twice. An unknown verdict is not a failed one: it must not pay for repairs."""
        g = gw or self.gw
        state = {"task": clip(task, 12_000), "success_criteria": criteria or "Does what the task asks.",
                 "result": clip(result, 40_000)}
        q = {"ok": {"type": "boolean",
                    "instructions": "Does the result do what the task asks and meet the success criteria?",
                    "criteria": {"true": "It meets the criteria: complete, on topic, in the requested form.",
                                 "false": "It misses part of the task, is off topic, is empty, refuses, or ignores "
                                          "the requested form."}}}
        total, ms = Usage(), 0
        for _ in range(2):
            r = await g.evaluate(state, q)
            total.tokens_in += r.usage.tokens_in
            total.cost += r.usage.cost
            ms += r.latency_ms
            p = r.boolean("ok")
            if p is not None:
                return p, total, ms
        return None, total, ms

    async def final_check(self, request: str, answer: str,
                          gw: GatewayLike | None = None) -> tuple[float | None, Usage, int]:
        return await self.verify(request, "Answers everything the request asks for, in the language of the request.",
                                 answer, gw)


def parse_extraction(text: str, lines: list[str]) -> dict | None:
    """Turn the EXTRACT answer into a decision spec over the request's own lines, or None when it does not fit.
    Items are the request's lines, never text the model wrote, so nothing is lost or changed on the way."""
    from .planner import clean_decision
    try:
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("fits") is not True:
        return None
    kind = str(data.get("kind") or "").lower()
    if kind not in ("choice", "yesno", "score"):
        return None
    raw = data.get("items")
    if not isinstance(raw, list):
        return None
    items: list[dict] = []
    used: set[int] = set()
    for pair in raw:
        if isinstance(pair, int):
            pair = [pair, pair]
        if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, int) for x in pair)):
            return None
        a, b = pair
        if not 1 <= a <= b <= len(lines) or used & set(range(a, b + 1)):
            return None
        used |= set(range(a, b + 1))
        chunk = "\n".join(lines[a - 1:b]).strip()
        if chunk:
            items.append({"id": str(len(items) + 1), "text": chunk})
    if not items or len(items) > MAX_JEV_ITEMS:
        return None
    return clean_decision(kind, {**data, "items": items})
