"""The SIAC loop: gate -> (plan -> sub-tasks back to the gate) or (route -> execute -> verify -> repair)
-> aggregate -> final check. Every step emits an event, so the process can be shown live."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .catalog import Catalog
from .decider import Decider, Gate, Settings
from .gateway import GatewayError, GatewayLike, Usage
from .planner import Plan, PlanError, Planner

NOTES_MARK = "HANDOFF NOTES:"
PROBLEM_MARK = "PLAN PROBLEM:"
FALLBACK_STATUS = {0, 402, 403, 404, 408, 429, 500, 502, 503, 504, 529}

WORKER_ROOT = ("Do the task below completely and well, as a careful expert would. Answer in the language of the task. "
               "When it involves reasoning, calculation or code, show the key steps briefly and check the result "
               "before giving it. Match the depth the request needs: short when it asks for something short, "
               "with the explanation a reader needs when it does not. No preamble about yourself.")
WORKER_SUB = ("You are one worker in a team that splits a big request into small tasks. Do exactly the task below, "
              "completely, in the language of the task. Output only the result. Then, on a new line, write "
              f"'{NOTES_MARK}' followed by up to 5 short bullet points with facts, decisions or warnings that later "
              f"tasks must know. If the task cannot be done as planned, write '{PROBLEM_MARK}' and explain why.")
AGGREGATOR = ("You assemble the final answer to a request from results a team has already written. Do not rewrite "
              "the results: choose which ones answer the request, their order and a heading for each. Reply with "
              "JSON only: {\"intro\": \"one or two sentences for the reader, in the language of the request, or empty\", "
              "\"sections\": [{\"from\": \"result id\", \"heading\": \"heading in the language of the request\"}], "
              "\"outro\": \"one closing sentence or empty\"}. Never mention the team, the sub-tasks or the process.")
GAP_FILLER = ("You check a finished answer against the request. If the answer misses something the request asks for, "
              "write ONLY the missing part, with its own short heading, in the language of the request. If nothing is "
              "missing, reply exactly: NOTHING MISSING")
REWRITE = ("You write the final answer to a request, using results a team has already produced. Keep the language "
           "of the request. Do not mention the team, the sub-tasks or the process.")


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Limits:
    max_depth: int = 3
    max_subtasks: int = 12
    max_cost: float = 0.50      # USD per run
    max_repairs: int = 2
    max_parallel: int = 4
    worker_max_tokens: int = 6000
    subtask_max_tokens: int = 4000
    # Hidden reasoning is billed as output. Cheap tiers do not need it; strong tiers get a little.
    reasoning: dict = field(default_factory=lambda: {1: "off", 2: "off", 3: "low", 4: "low"})


@dataclass
class Node:
    id: str
    title: str
    prompt: str
    depth: int
    parent: str | None = None
    criteria: str = ""
    answer_type: str = "text"
    decision: dict | None = None
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"  # pending | running | done | failed
    kind: str = ""           # split | llm | jev
    gate: dict | None = None
    model: str | None = None
    tier: int | None = None
    result: str = ""
    notes: str = ""
    problem: str = ""
    attempts: list[dict] = field(default_factory=list)
    cost: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    children: list[str] = field(default_factory=list)
    started: float | None = None
    ended: float | None = None


@dataclass
class RunResult:
    id: str
    request: str
    answer: str
    status: str  # done | partial | failed
    nodes: dict[str, dict]
    events: list[dict]
    receipt: dict
    started_at: str
    duration_s: float
    error: str = ""


def split_notes(text: str) -> tuple[str, str, str]:
    """Separate the worker's result from its hand-off notes and plan problems."""
    problem = ""
    m = re.search(re.escape(PROBLEM_MARK) + r"(.*)", text, re.S)
    if m:
        problem, text = m.group(1).strip(), text[: m.start()]
    notes = ""
    m = re.search(re.escape(NOTES_MARK) + r"(.*)", text, re.S)
    if m:
        notes, text = m.group(1).strip(), text[: m.start()]
    return text.strip(), notes, problem


def stitch(outline_text: str, kids: list[Node], level: int = 2) -> str:
    """Build the final answer from the aggregator's outline and the untouched sub-task results."""
    by_id = {k.id.split(".")[-1]: k for k in kids}
    by_id.update({k.id: k for k in kids})
    hashes = "#" * level
    try:
        start, end = outline_text.find("{"), outline_text.rfind("}")
        outline = json.loads(outline_text[start:end + 1])
        sections = [s for s in outline.get("sections", []) if isinstance(s, dict) and str(s.get("from")) in by_id]
    except (ValueError, AttributeError):
        outline, sections = {}, []
    if not sections:  # no usable outline: every result, in plan order, under its title
        sections = [{"from": k.id, "heading": k.title} for k in kids]
    used, parts = set(), []
    if str(outline.get("intro") or "").strip():
        parts.append(str(outline["intro"]).strip())
    for s in sections:
        k = by_id[str(s["from"])]
        if k.id in used or not k.result:
            continue
        used.add(k.id)
        heading = str(s.get("heading") or k.title).strip()
        parts.append(f"{hashes} {heading}\n\n{k.result.strip()}")
    if str(outline.get("outro") or "").strip():
        parts.append(str(outline["outro"]).strip())
    return "\n\n".join(parts)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class Engine:
    def __init__(self, gateway: GatewayLike, catalog: Catalog, *, profile: str = "all",
                 limits: Limits | None = None, settings: Settings | None = None,
                 on_event: Callable[[dict], Any] | None = None, allow_split: bool = True):
        self.gw, self.catalog, self.profile = gateway, catalog, profile
        self.limits = limits or Limits()
        self.decider = Decider(gateway, catalog, settings)
        fallbacks = [m.id for m in catalog.chain(3, profile)]
        self.planner = Planner(gateway, catalog.pick_named(catalog.planner, profile, fallback_tier=3).id,
                               self.limits.max_subtasks, fallbacks=fallbacks)
        self.planner_light = Planner(gateway, catalog.pick_named(catalog.planner_light, profile, fallback_tier=2).id,
                                     self.limits.max_subtasks, fallbacks=fallbacks)
        self.on_event = on_event
        self.allow_split = allow_split
        self._sem = asyncio.Semaphore(self.limits.max_parallel)

    # ---------------------------------------------------------------- plumbing
    def _emit(self, type_: str, node: Node | None = None, **data: Any) -> None:
        ev = {"t": round(time.perf_counter() - self._t0, 3), "type": type_}
        if node is not None:
            ev["node"] = node.id
        ev.update(data)
        self.events.append(ev)
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:  # a broken listener never stops the run
                pass

    def _spend(self, node: Node, role: str, usage: Usage, model: str) -> None:
        node.cost += usage.cost
        node.tokens_in += usage.tokens_in
        node.tokens_out += usage.tokens_out
        self.cost += usage.cost
        self.by_role[role] = self.by_role.get(role, 0.0) + usage.cost
        self.calls += 1
        self.tokens_in += usage.tokens_in
        self.tokens_out += usage.tokens_out
        self.by_model[model] = self.by_model.get(model, 0.0) + usage.cost

    def _check_budget(self, model_id: str, tokens_in: int, tokens_out: int) -> float:
        """Reserve the estimated cost of a call before making it, so parallel calls cannot overshoot together."""
        try:
            est = self.catalog.get(model_id).cost(tokens_in, tokens_out)
        except KeyError:
            est = 0.0
        if self.cost + self.reserved + est > self.limits.max_cost:
            raise BudgetExceeded(f"budget of ${self.limits.max_cost:.2f} reached (spent ${self.cost:.4f})")
        self.reserved += est
        return est

    async def _chat(self, node: Node, role: str, model: str | list[str], system: str, user: str,
                    max_tokens: int, reasoning: str | None = "off") -> str:
        """Call a model; if the provider refuses or keeps failing, try the next model in the list."""
        models = [model] if isinstance(model, str) else list(model)
        last: GatewayError | None = None
        for i, m in enumerate(models):
            est = self._check_budget(m, estimate_tokens(system + user), int(max_tokens * 0.75))
            try:
                async with self._sem:
                    r = await self.gw.chat(m, [{"role": "system", "content": system},
                                               {"role": "user", "content": user}], max_tokens=max_tokens,
                                           reasoning=reasoning)
            except GatewayError as e:
                self.reserved -= est
                last = e
                if e.status in FALLBACK_STATUS and i + 1 < len(models):
                    self._emit("model_fallback", node, model=m, next=models[i + 1], status=e.status,
                               message=str(e)[:200])
                    continue
                raise
            self.reserved -= est
            if m != models[0]:
                node.model = m
            self._spend(node, role, r.usage, m)
            self._emit("llm_call", node, role=role, model=m, tokens_in=r.usage.tokens_in,
                       tokens_out=r.usage.tokens_out, cost=r.usage.cost, cost_source=r.usage.cost_source,
                       latency_ms=r.latency_ms)
            if role == "work":
                self.work_out += r.usage.tokens_out
                self.work_visible += estimate_tokens(r.text)
            if not r.text.strip() and i + 1 < len(models):
                # the whole output budget went into hidden reasoning: try the next model, without reasoning
                self._emit("model_fallback", node, model=m, next=models[i + 1], status=0,
                           message="empty answer (output spent on reasoning)")
                reasoning = "off"
                continue
            return r.text
        raise last or GatewayError(0, "no model available")

    # --------------------------------------------------------------- the loop
    async def run(self, request: str) -> RunResult:
        self._t0 = time.perf_counter()
        self.events: list[dict] = []
        self.nodes: dict[str, Node] = {}
        self.cost, self.calls, self.tokens_in, self.tokens_out = 0.0, 0, 0, 0
        self.reserved = 0.0
        self.work_out, self.work_visible = 0, 0
        self.by_role: dict[str, float] = {}
        self.by_model: dict[str, float] = {}
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        root = self._add(Node(id="root", title="Request", prompt=request, depth=0,
                              criteria="Answers everything the request asks for."))
        self._emit("run_started", root, request=request, profile=self.profile,
                   limits=asdict(self.limits))
        status, error = "done", ""
        try:
            await self._process(root, context="")
            if root.status == "done":
                await self._final_check(root, request)
        except BudgetExceeded as e:
            status, error = "partial", str(e)
            self._emit("budget_exceeded", root, message=str(e))
        except GatewayError as e:
            status, error = "failed", str(e)
            self._emit("error", root, message=str(e))
        answer = root.result or self._partial_answer()
        if status == "done" and root.status != "done":
            status = "partial"
        receipt = self._receipt(request, answer)
        self._emit("run_done", root, status=status, cost=round(self.cost, 6), receipt=receipt, answer=answer[:30000],
                   error=error)
        return RunResult(id=run_id, request=request, answer=answer, status=status,
                         nodes={k: asdict(v) for k, v in self.nodes.items()}, events=self.events,
                         receipt=receipt, started_at=started,
                         duration_s=round(time.perf_counter() - self._t0, 2), error=error)

    def _add(self, node: Node) -> Node:
        self.nodes[node.id] = node
        return node

    async def _process(self, node: Node, context: str) -> None:
        node.status, node.started = "running", time.perf_counter() - self._t0
        self._emit("node_started", node, title=node.title, depth=node.depth, parent=node.parent,
                   prompt=node.prompt[:3000], criteria=node.criteria[:500], answer_type=node.answer_type,
                   jev=bool(node.decision), depends_on=node.depends_on)
        try:
            if node.decision:  # a closed question written by the planner: Jev answers it (idea 18)
                await self._solve_with_jev(node)
            else:
                gate = await self.decider.gate(node.prompt)
                self._spend(node, "decisions", gate.usage, self.catalog.decider.id)
                node.gate = {"split": round(gate.split_probability, 3), "tier": gate.tier, "tier_raw": gate.tier_raw,
                             "tier_confidence": gate.tier_confidence, "raised": gate.raised, "floored": gate.floored,
                             "task_type": gate.task_type, "answer_type": gate.answer_type}
                node.tier = gate.tier
                self._emit("gate", node, **node.gate, latency_ms=gate.latency_ms, cost=gate.usage.cost)
                if self.allow_split and self.decider.should_split(gate, node.prompt, node.depth, self.limits.max_depth):
                    await self._split(node, gate, context)
                else:
                    await self._execute(node, gate, context)
            node.status = "done"
        except BudgetExceeded:
            node.status = "failed"
            raise
        except (PlanError, GatewayError) as e:
            node.status = "failed"
            node.problem = str(e)
            self._emit("node_failed", node, message=str(e))
            if node.id == "root":
                raise
        finally:
            node.ended = time.perf_counter() - self._t0
            self._emit("node_done", node, status=node.status, model=node.model, tier=node.tier,
                       cost=round(node.cost, 6), kind=node.kind, result=node.result[:6000],
                       notes=node.notes[:1500], problem=node.problem[:500], attempts=node.attempts)

    # ------------------------------------------------------------ split path
    async def _split(self, node: Node, gate: Gate, context: str) -> None:
        node.kind = "split"
        planner = self.planner if gate.tier >= 4 else self.planner_light
        est = self._check_budget(planner.model, estimate_tokens(node.prompt) + 900, 2000)
        try:
            plan: Plan = await planner.plan(node.prompt, context)
        finally:
            self.reserved -= est
        self._spend(node, "planning", plan.usage, plan.model)
        prefix = "" if node.id == "root" else node.id + "."
        kids = []
        for s in plan.subtasks:
            child = self._add(Node(id=prefix + s.id, title=s.title, prompt=s.prompt, depth=node.depth + 1,
                                   parent=node.id, criteria=s.success_criteria, answer_type=s.answer_type,
                                   decision=s.decision, depends_on=[prefix + d for d in s.depends_on]))
            kids.append(child)
            node.children.append(child.id)
        self._emit("plan", node, model=plan.model, cost=plan.usage.cost, latency_ms=plan.latency_ms,
                   assembly=plan.assembly,
                   subtasks=[{"id": k.id, "title": k.title, "answer_type": k.answer_type,
                              "depends_on": k.depends_on, "jev": bool(k.decision)} for k in kids])
        # Run in waves: every sub-task whose dependencies are done starts at once.
        pending = {k.id: k for k in kids}
        done: set[str] = set()
        while pending:
            ready = [k for k in pending.values() if all(d in done for d in k.depends_on)]
            if not ready:  # cannot happen after parse_plan, but never hang
                ready = list(pending.values())
            outs = await asyncio.gather(*(self._process(k, self._context_for(k)) for k in ready),
                                        return_exceptions=True)
            for o in outs:  # a budget stop ends the run; other errors were already recorded on the node
                if isinstance(o, BudgetExceeded):
                    raise o
                if isinstance(o, BaseException) and not isinstance(o, (PlanError, GatewayError)):
                    raise o
            for k in ready:
                done.add(k.id)
                pending.pop(k.id)
        await self._aggregate(node, gate, kids, plan.assembly)

    def _context_for(self, node: Node) -> str:
        parts = []
        for d in node.depends_on:
            dep = self.nodes[d]
            chunk = f"[{dep.title}]\n{dep.result[:6000]}"
            if dep.notes:
                chunk += f"\nNotes: {dep.notes}"
            parts.append(chunk)
        return "\n\n".join(parts)

    async def _aggregate(self, node: Node, gate: Gate, kids: list[Node], assembly: str) -> None:
        """A cheap model decides order, headings, a short intro and outro; the code stitches the results.
        The results are not rewritten, so assembling costs a few hundred output tokens, not thousands."""
        chain = [m.id for m in self.catalog.chain(2, self.profile)]
        node.model = chain[0]
        listing = []
        for k in kids:
            block = f"[{k.id.split('.')[-1]}] {k.title}\n{k.result[:1500] or '(no result)'}"
            if k.problem:
                block += f"\n(Problem reported: {k.problem})"
            listing.append(block)
        user = (f"REQUEST:\n{node.prompt}\n\nASSEMBLY INSTRUCTIONS:\n{assembly}\n\n"
                "RESULTS (shortened; ids in brackets):\n" + "\n\n".join(listing))
        self._emit("aggregate", node, model=chain[0])
        text = await self._chat(node, "assembly", chain, AGGREGATOR, user, 1500)
        node.result = stitch(text, kids, level=2 if node.id == "root" else 3)

    # ------------------------------------------------------ atomic path (LLM)    # ------------------------------------------------------ atomic path (LLM)
    async def _execute(self, node: Node, gate: Gate, context: str) -> None:
        node.kind = "llm"
        tier = gate.tier
        system = WORKER_ROOT if node.id == "root" else WORKER_SUB
        user = node.prompt if not context else f"{node.prompt}\n\nResults from earlier tasks you can use:\n{context}"
        feedback = ""
        for attempt in range(self.limits.max_repairs + 1):
            if attempt == 2:  # second repair: one tier up
                tier = min(4, tier + 1)
            chain = [m.id for m in self.catalog.chain(tier, self.profile)]
            model = chain[0]
            node.model, node.tier = model, tier
            self._emit("route", node, model=model, tier=tier, attempt=attempt + 1)
            max_out = self.limits.worker_max_tokens if node.id == "root" else self.limits.subtask_max_tokens
            if tier >= 4:  # strong models think before they write: leave room for the answer
                max_out = max(max_out, 8000)
            text = await self._chat(node, "work", chain, system, user + feedback, max_out,
                                    reasoning=self.limits.reasoning.get(tier))
            model = node.model
            result, notes, problem = split_notes(text)
            node.result, node.notes, node.problem = result, notes, problem
            p, usage, ms = await self.decider.verify(node.prompt, node.criteria, result)
            self._spend(node, "checks", usage, self.catalog.decider.id)
            ok = p >= self.decider.s.verify_pass
            node.attempts.append({"model": model, "tier": tier, "ok_probability": round(p, 3)})
            self._emit("verify", node, ok=ok, probability=round(p, 3), attempt=attempt + 1, cost=usage.cost)
            if ok or problem:
                return
            feedback = ("\n\nA reviewer found that the previous attempt did not fully meet the task. "
                        f"Previous attempt:\n{result[:4000]}\n\nDo the task again, completely.")
            if attempt < self.limits.max_repairs:
                self._emit("repair", node, next_attempt=attempt + 2, tier_up=(attempt + 1 == 2))

    # ------------------------------------------------ atomic path (Jev solves)
    async def _solve_with_jev(self, node: Node) -> None:
        node.kind = "jev"
        node.model = self.catalog.decider.id
        spec = node.decision
        out = await self.decider.solve(spec)
        self._spend(node, "decisions", out["usage"], self.catalog.decider.id)
        answers = {a["id"]: a for a in out["answers"]}
        self._emit("jev_solve", node, items=len(answers), unsure=out["unsure"], cost=out["usage"].cost,
                   latency_ms=out["latency_ms"])
        if out["unsure"]:  # items Jev was not sure about: a cheap model answers them with the same options
            chain = [m.id for m in self.catalog.chain(1, self.profile)]
            model = chain[0]
            items = [it for it in spec["items"] if it["id"] in out["unsure"]]
            allowed = ", ".join(spec.get("options") or spec.get("scale") or ["yes", "no"])
            user = (f"{spec['question']}\nAllowed answers: {allowed}.\nAnswer each item with its id and one allowed "
                    "answer, one per line, like '3: label'.\n\n" + "\n".join(f"{it['id']}: {it['text']}" for it in items))
            text = await self._chat(node, "work", chain, "Answer with the allowed labels only.", user, 2000)
            for line in text.splitlines():
                m = re.match(r"\s*([\w.-]+)\s*[:\-]\s*(.+)", line)
                if m and m.group(1) in answers:
                    answers[m.group(1)].update(answer=m.group(2).strip(), confidence=None, by=model)
            self._emit("fallback", node, model=model, items=len(items))
        lines = []
        for it in spec["items"]:
            a = answers.get(it["id"], {})
            text = it["text"] if len(it["text"]) <= 80 else it["text"][:77] + "..."
            lines.append(f"- {it['id']}. {text} -> {a.get('answer', '?')}")
        node.result = f"{spec['question']}\n" + "\n".join(lines)

    # ------------------------------------------------------------ final check
    async def _final_check(self, root: Node, request: str) -> None:
        if root.kind != "split":
            return  # an atomic answer was already verified against the request
        p, usage, ms = await self.decider.final_check(request, root.result)
        self._spend(root, "checks", usage, self.catalog.decider.id)
        ok = p >= self.decider.s.final_pass
        self._emit("final_check", root, ok=ok, probability=round(p, 3), cost=usage.cost)
        if ok:
            return
        chain = [m.id for m in self.catalog.chain(2, self.profile)]
        self._emit("repair", root, next_attempt=2, tier_up=False, model=chain[0], gap_fill=True)
        user = f"REQUEST:\n{request}\n\nANSWER:\n{root.result}"
        extra = (await self._chat(root, "assembly", chain, GAP_FILLER, user, 2500)).strip()
        if extra and "NOTHING MISSING" not in extra.upper():
            root.result = root.result.rstrip() + "\n\n" + extra

    # ---------------------------------------------------------------- receipt
    def _partial_answer(self) -> str:
        done = [n for n in self.nodes.values() if n.id != "root" and n.result]
        if not done:
            return ""
        return "\n\n".join(f"## {n.title}\n{n.result}" for n in done)

    def _receipt(self, request: str, answer: str) -> dict:
        base = self.catalog.get(self.catalog.baseline)
        # The strong model would also spend hidden reasoning tokens: scale the answer length by the ratio of
        # billed output to visible output that SIAC's own workers showed in this run (never below 1).
        factor = max(1.0, self.work_out / self.work_visible) if self.work_visible else 1.0
        tin, tout = estimate_tokens(request) + 40, int(estimate_tokens(answer) * factor)
        baseline_cost = base.cost(tin, tout)
        saving = (1 - self.cost / baseline_cost) * 100 if baseline_cost > 0 else None
        return {
            "total_cost": round(self.cost, 6),
            "calls": self.calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "by_role": {k: round(v, 6) for k, v in sorted(self.by_role.items())},
            "by_model": {k: round(v, 6) for k, v in sorted(self.by_model.items(), key=lambda kv: -kv[1])},
            "baseline": {
                "model": base.id,
                "estimated_cost": round(baseline_cost, 6),
                "method": "one call to the baseline model with this request and an answer as long as SIAC's, "
                          "scaled by the hidden reasoning SIAC's own models used in this run, with no retries",
                "reasoning_factor": round(factor, 2),
            },
            "saving_pct": round(saving, 1) if saving is not None else None,
        }
