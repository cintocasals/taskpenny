"""A simulated gateway for `--dry-run`: shows the whole SIAC process without a key and without spending.

Answers are placeholders and decisions come from simple rules, but the flow, the events, the models chosen
and the cost receipt (at catalog prices) are the real ones. The test suite uses it too.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Any

from .catalog import Catalog
from .gateway import ChatResult, EvalResult, Usage

NUMBERED = re.compile(r"(?:^|\s)(\d{1,2})[).]\s+(.+?)(?=(?:\s\d{1,2}[).]\s)|$)", re.S)
CHOICE_HINTS = ("positive, negative", "positiva, negativa", "queja, una pregunta", "bug report", " o ", " or ")
FILLER = "This simulated text stands in for the real answer, so the cost receipt has a realistic length. "
T4 = ("security", "seguridad", "legal", "préstamo", "loan", "architecture", "trading", "bot", "risk", "riesgo",
      "database for", "servidor")


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


class SimulatedGateway:
    def __init__(self, catalog: Catalog, *, latency: tuple[float, float] = (0.05, 0.3), seed: int = 7):
        self.catalog = catalog
        self.latency = latency
        self.rng = random.Random(seed)
        self.calls: list[dict] = []

    async def aclose(self) -> None:
        return None

    async def _sleep(self) -> int:
        s = self.rng.uniform(*self.latency)
        await asyncio.sleep(s)
        return int(s * 1000)

    # ------------------------------------------------------------- decisions
    async def evaluate(self, state: Any, questions: dict[str, dict]) -> EvalResult:
        ms = await self._sleep()
        text = json.dumps(state, ensure_ascii=False)
        low = text.lower()
        answers: dict[str, dict] = {}
        conf: dict[str, float] = {}
        for name, q in questions.items():
            if name == "split":
                parts = len(NUMBERED.findall(text))
                answers[name] = {"type": "boolean", "probability": 0.9 if parts >= 3 and len(text) > 300 else 0.1}
            elif name == "tier":
                tier = "4" if any(k in low for k in T4) else "3" if len(text) > 700 else "2" if len(text) > 160 else "1"
                answers[name] = {"type": "choice", "choice": tier, "probabilities": {tier: 0.85}}
                conf[name] = 0.8
            elif name == "task_type":
                t = ("code" if any(k in low for k in ("python", "sql", "function", "node.js", "código", "excel"))
                     else "translation" if any(k in low for k in ("translate", "tradueix", "traduce"))
                     else "writing" if any(k in low for k in ("write", "escriu", "escribe", "redacta", "email", "post"))
                     else "analysis")
                answers[name] = {"type": "choice", "choice": t, "probabilities": {t: 0.8}}
                conf[name] = 0.7
            elif name == "answer":
                a = "choice" if any(k in low for k in CHOICE_HINTS) and "?" in text and len(text) < 400 else "text"
                answers[name] = {"type": "choice", "choice": a, "probabilities": {a: 0.8}}
                conf[name] = 0.7
            elif name == "ok":
                answers[name] = {"type": "boolean", "probability": 0.93 if "(empty)" not in low else 0.1}
            else:  # Jev solving a decision task
                if q["type"] == "choice":
                    opts = list(q["criteria"])
                    pick = opts[self.rng.randrange(len(opts))]
                    answers[name] = {"type": "choice", "choice": pick, "probabilities": {pick: 0.9}}
                    conf[name] = self.rng.choice([0.92, 0.88, 0.81, 0.45])
                elif q["type"] == "score":
                    n = len(q["criteria"])
                    answers[name] = {"type": "score", "score": float(self.rng.randrange(n)), "probabilities": {}}
                    conf[name] = 0.8
                else:
                    answers[name] = {"type": "boolean", "probability": self.rng.choice([0.95, 0.1, 0.9])}
        tin = _tokens(text) + 60 * len(questions)
        self.calls.append({"kind": "evaluate", "questions": list(questions)})
        return EvalResult(answers=answers, confidence=conf,
                          usage=Usage(tin, 0, self.catalog.decider.cost(tin, 0), "simulated"), latency_ms=ms)

    # ------------------------------------------------------------ chat models
    async def chat(self, model: str, messages: list[dict], *, max_tokens: int | None = None,
                   temperature: float | None = None, json_mode: bool = False,
                   reasoning: str | None = None) -> ChatResult:
        ms = await self._sleep()
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        user = messages[-1]["content"]
        if system.startswith("Please act as an impartial judge"):
            out = "Simulated verdict: both answers are placeholders.\n[[" + self.rng.choice("ABC") + "]]"
        elif system.startswith("You are the planner"):
            out = self._plan(user)
        elif system.startswith("You write the final answer"):
            heads = [line[4:] for line in user.splitlines() if line.startswith("### ")] or ["Answer"]
            out = "[Simulated final answer written by " + model + "]\n\n" + "\n\n".join(
                f"## {h}\n{FILLER * 6}" for h in heads)
        elif "Allowed answers" in user:
            out = "\n".join(f"{m.group(1)}: " + user.split("Allowed answers: ")[1].split(",")[0].strip(" .")
                            for m in re.finditer(r"^([\w.-]+): ", user, re.M))
        else:
            first = user.strip().splitlines()[0][:90] if user.strip() else ""
            out = f"[Simulated answer from {model}] {first}\n{FILLER * 8}"
            if system.startswith("You are one worker"):
                out += "\nHANDOFF NOTES:\n- simulated note"
        tin = sum(_tokens(m["content"]) for m in messages)
        tout = _tokens(out) + 150
        try:
            cost = self.catalog.get(model).cost(tin, tout)
        except KeyError:
            cost = 0.0
        self.calls.append({"kind": "chat", "model": model})
        return ChatResult(text=out, model=model, usage=Usage(tin, tout, cost, "simulated"), latency_ms=ms)

    def _plan(self, request: str) -> str:
        parts = [p.strip().rstrip(",;") for _, p in NUMBERED.findall(request)] or [request[:200]]
        subs = []
        for i, p in enumerate(parts[:12], 1):
            decide = any(k in p.lower() for k in ("decide", "whether", "classify", "clasifica", "digues si", "for each"))
            sub = {"id": f"t{i}", "title": p, "prompt": p, "success_criteria": "Does what the step asks.",
                   "answer_type": "choice" if decide else "text", "decision": None,
                   "depends_on": [f"t{i - 1}"] if i > 1 and decide else []}
            if decide:
                sub["decision"] = {"question": "(simulated) Which category fits each item?",
                                   "options": {"a": "first kind", "b": "second kind", "c": "third kind"},
                                   "items": [{"id": str(j), "text": f"item {j} of {p[:40]}"} for j in range(1, 4)]}
            subs.append(sub)
        return json.dumps({"subtasks": subs, "assembly": "Put the parts in order under clear headings."})
