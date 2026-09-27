"""The Taskpenny loop: gate -> (plan -> sub-tasks back to the gate) or (Jev answers a closed question) or
(route -> execute -> verify -> repair) -> aggregate -> final check. Every step emits an event, so the process
can be shown live.

Nothing fails in silence. A result that never passed Jev's check is kept but marked "unverified", a part that
failed is named, and the run's status says which: done, unverified, partial or failed, with the reasons in
`warnings`. The budget is a ceiling: every call, Jev's included, waits for a slot and reserves its full price
(all the input and all the output it may bill) before it is made.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .catalog import Catalog, Model
from .decider import EXTRACT, Decider, Gate, Settings, parse_extraction
from .gateway import BudgetExceeded, ChatResult, EvalResult, GatewayError, GatewayLike, Usage
from .planner import Plan, PlanError, Planner

__all__ = ["BudgetExceeded", "Engine", "Limits", "Node", "RunCancelled", "RunResult", "reserve_tokens", "split_notes",
           "stitch"]

NOTES_MARK = "HANDOFF NOTES:"
PROBLEM_MARK = "PLAN PROBLEM:"
FALLBACK_STATUS = {0, 402, 403, 404, 408, 429, 500, 502, 503, 504, 529}
OWN_MARK = re.compile(r"\s*(?:\d{1,3}[.)]|[A-Za-z][.)]|[-*\u2022])\s")  # an item that brings its own number
MIN_USEFUL_OUT = 1000  # when the budget is nearly spent, an answer shorter than this is not worth paying for

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


class RunCancelled(asyncio.CancelledError):
    """The run was cancelled. It still carries what was done (`result`), so a caller can save it; anything that does
    not know about it sees an ordinary cancellation and stops."""

    def __init__(self, result: "RunResult"):
        super().__init__("run cancelled")
        self.result = result


@dataclass
class Limits:
    max_depth: int = 3
    max_subtasks: int = 12      # per run, across every level of splitting
    max_cost: float = 0.50      # USD per run: a ceiling, never passed
    max_repairs: int = 2
    max_parallel: int = 4       # language model calls at once
    max_parallel_decisions: int = 8  # Jev calls at once
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
    status: str = "pending"  # pending | running | done | unverified | partial | failed
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
    checked: bool | None = None  # True: passed Jev's check; False: kept without passing it; None: no check
    warning: str = ""
    missing: bool = False  # a split whose answer lacks a failed part


@dataclass
class RunResult:
    id: str
    request: str
    answer: str
    status: str  # done | unverified | partial | failed
    nodes: dict[str, dict]
    events: list[dict]
    receipt: dict
    started_at: str
    duration_s: float
    error: str = ""
    warnings: list[str] = field(default_factory=list)


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
    """Build the final answer from the aggregator's outline and the untouched sub-task results. The outline may
    leave out results that only fed later parts (a first draft, say). An outline that cannot be used (missing,
    not JSON, wrong shape) gives every result in plan order, under its own title."""
    by_id = {k.id.split(".")[-1]: k for k in kids}
    by_id.update({k.id: k for k in kids})
    hashes = "#" * level
    outline: dict = {}
    sections: list[dict] = []
    try:
        start, end = outline_text.find("{"), outline_text.rfind("}")
        data = json.loads(outline_text[start:end + 1]) if start >= 0 and end > start else {}
        outline = data if isinstance(data, dict) else {}
        raw = outline.get("sections")
        if isinstance(raw, list):
            sections = [s for s in raw if isinstance(s, dict) and str(s.get("from")) in by_id]
    except (ValueError, RecursionError):
        outline, sections = {}, []
    if not sections:
        sections = [{"from": k.id, "heading": k.title} for k in kids]
    used, parts = set(), []
    intro, outro = str(outline.get("intro") or "").strip(), str(outline.get("outro") or "").strip()
    if intro:
        parts.append(intro)
    for s in sections:
        k = by_id[str(s["from"])]
        if k.id in used or not k.result:
            continue
        used.add(k.id)
        heading = str(s.get("heading") or k.title).strip()
        parts.append(f"{hashes} {heading}\n\n{k.result.strip()}")
    if outro:
        parts.append(outro)
    return "\n\n".join(parts)


def estimate_tokens(text: str) -> int:
    """About 4 characters per token: used for the receipt's estimate of the baseline, as published."""
    return max(1, len(text) // 4)


def reserve_tokens(text: str) -> int:
    """A cautious token count for reserving budget: 3 characters per token for ASCII letters and spaces (English
    prose is about 4), 2 for ASCII digits and punctuation (numbers, IDs and code split finely), and one token per
    character for everything else (accents, Chinese, Japanese, Cyrillic...)."""
    ascii_ = text.encode("ascii", "ignore").decode()
    other = len(text) - len(ascii_)
    words = sum(1 for ch in ascii_ if ch.isalpha() or ch == " ")
    return max(1, words // 3 + (len(ascii_) - words) // 2 + other)


class _Meter:
    """The gateway as the planner and the decider see it: each call waits for a slot, reserves its full price
    before it is made, and is booked on one task under one role."""

    def __init__(self, engine: "Engine", node: Node, role: str):
        self.engine, self.node, self.role = engine, node, role

    async def evaluate(self, state: Any, questions: dict[str, dict]) -> EvalResult:
        return await self.engine._evaluate(self.node, self.role, state, questions)

    async def chat(self, model: str, messages: list[dict], **kw: Any) -> ChatResult:
        return await self.engine._call_chat(self.node, self.role, model, messages, **kw)

    async def aclose(self) -> None:
        return None


class Engine:
    def __init__(self, gateway: GatewayLike, catalog: Catalog, *, profile: str = "all",
                 limits: Limits | None = None, settings: Settings | None = None,
                 on_event: Callable[[dict], Any] | None = None, allow_split: bool = True):
        self.gw, self.catalog, self.profile = gateway, catalog, profile
        catalog.providers(profile)  # an unknown profile fails here, before anything is spent
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
        self._eval_sem = asyncio.Semaphore(self.limits.max_parallel_decisions)

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

    def _meter(self, node: Node, role: str) -> _Meter:
        return _Meter(self, node, role)

    def _spend(self, node: Node, role: str, usage: Usage, model: str) -> None:
        if usage.cost_source == "simulated":
            self.simulated = True
        node.cost += usage.cost
        node.tokens_in += usage.tokens_in
        node.tokens_out += usage.tokens_out
        self.cost += usage.cost
        self.by_role[role] = self.by_role.get(role, 0.0) + usage.cost
        self.calls += 1
        self.tokens_in += usage.tokens_in
        self.tokens_out += usage.tokens_out
        self.by_model[model] = self.by_model.get(model, 0.0) + usage.cost

    def _warn(self, node: Node, message: str) -> None:
        label = "the answer" if node.id == "root" else f"part {node.id} ({node.title[:60]})"
        self.warnings.append(f"{label}: {message}")
        self._emit("warning", node, message=message)

    def _unchecked(self, node: Node, message: str) -> None:
        """The result stays, but the run says it did not pass Jev's check (idea 10: carry on, visibly)."""
        node.checked, node.warning, self.unchecked = False, message, True
        self._warn(node, message)

    # ------------------------------------------------------------------ budget
    def _room(self) -> float:
        return self.limits.max_cost - self.cost - self.reserved

    def _release(self, est: float) -> None:
        self.reserved = max(0.0, self.reserved - est)
        self._freed.set()

    async def _reserve(self, est: float) -> float:
        """Hold `est` USD of the budget for a call about to be made. If other calls are running, wait for them to
        settle; if nothing is running and it still does not fit, the budget is spent."""
        while est > self._room() + 1e-12:
            if self.reserved <= 1e-12:
                raise BudgetExceeded(f"budget of ${self.limits.max_cost:.2f} reached")
            self._freed.clear()
            await self._freed.wait()
        self.reserved += est
        return est

    async def _reserve_chat(self, m: Model | None, tokens_in: int, max_tokens: int) -> tuple[int, float]:
        """Reserve all the input and all the output a chat call may bill. When the budget is nearly spent and
        nothing else is running, the answer is shortened to what the budget can still pay, if that is enough
        for a useful answer."""
        if m is None:  # a model outside the catalog: its price is unknown, so it cannot be reserved
            return max_tokens, await self._reserve(0.0)
        while True:
            full = m.cost(tokens_in, max_tokens)
            if full <= self._room() + 1e-12:
                self.reserved += full
                return max_tokens, full
            if self.reserved > 1e-12:
                self._freed.clear()
                await self._freed.wait()
                continue
            if m.price_out > 0:
                fit = int((self._room() - m.cost(tokens_in, 0)) / m.price_out * 1e6)
                if fit >= min(MIN_USEFUL_OUT, max_tokens):
                    est = m.cost(tokens_in, fit)
                    self.reserved += est
                    return fit, est
            raise BudgetExceeded(f"budget of ${self.limits.max_cost:.2f} reached")

    # ------------------------------------------------------------------- calls
    async def _evaluate(self, node: Node, role: str, state: Any, questions: dict[str, dict]) -> EvalResult:
        """One Jev call: a slot among the decision slots, its price reserved, its cost booked on `node`."""
        size = reserve_tokens(json.dumps(state, ensure_ascii=False) + json.dumps(questions, ensure_ascii=False))
        async with self._eval_sem:
            est = await self._reserve(self.catalog.decider.cost(size + 100, 0))
            try:
                r = await self.gw.evaluate(state, questions)
            finally:
                self._release(est)
            self._spend(node, role, r.usage, self.catalog.decider.id)
        return r

    async def _call_chat(self, node: Node, role: str, model: str, messages: list[dict], *, max_tokens: int,
                         reasoning: str | None = None, temperature: float | None = None,
                         json_mode: bool = False) -> ChatResult:
        """One language model call: a slot, its full price reserved, its cost booked on `node`."""
        try:
            m: Model | None = self.catalog.get(model)
        except KeyError:
            m = None
        tin = sum(reserve_tokens(str(x.get("content") or "")) + 8 for x in messages)
        async with self._sem:
            allowed, est = await self._reserve_chat(m, tin, max_tokens)
            try:
                r = await self.gw.chat(model, messages, max_tokens=allowed, reasoning=reasoning,
                                       temperature=temperature, json_mode=json_mode)
            except GatewayError as e:
                if e.status == 408:
                    self.timeouts += 1
                raise
            finally:
                self._release(est)
            self._spend(node, role, r.usage, model)
        self._emit("llm_call", node, role=role, model=model, tokens_in=r.usage.tokens_in,
                   tokens_out=r.usage.tokens_out, cost=r.usage.cost, cost_source=r.usage.cost_source,
                   latency_ms=r.latency_ms, **({"max_tokens": allowed} if allowed < max_tokens else {}),
                   **({"cut": True} if r.finish_reason == "length" else {}))
        if allowed < max_tokens and r.finish_reason == "length":
            self._warn(node, f"the budget allowed only {allowed} tokens of output, and the answer was cut there")
        if role == "work":
            self.work_out += r.usage.tokens_out
            self.work_visible += estimate_tokens(r.text)
        return r

    async def _chat(self, node: Node, role: str, model: str | list[str], system: str, user: str,
                    max_tokens: int, reasoning: str | None = "off") -> ChatResult:
        """Call a model; if the provider refuses or keeps failing, try the next model in the list."""
        models = [model] if isinstance(model, str) else list(model)
        last: GatewayError | None = None
        for i, m in enumerate(models):
            try:
                r = await self._call_chat(node, role, m, [{"role": "system", "content": system},
                                                          {"role": "user", "content": user}],
                                          max_tokens=max_tokens, reasoning=reasoning)
            except GatewayError as e:
                last = e
                if e.status in FALLBACK_STATUS and i + 1 < len(models):
                    self._emit("model_fallback", node, model=m, next=models[i + 1], status=e.status,
                               message=str(e)[:200])
                    continue
                raise
            if m != models[0]:
                node.model = m
            if not r.text.strip() and i + 1 < len(models):
                # the whole output budget went into hidden reasoning: try the next model, without reasoning
                self._emit("model_fallback", node, model=m, next=models[i + 1], status=0,
                           message="empty answer (output spent on reasoning)")
                reasoning = "off"
                continue
            return r
        raise last or GatewayError(0, "no model available")

    # --------------------------------------------------------------- the loop
    async def run(self, request: str) -> RunResult:
        self._t0 = time.perf_counter()
        self.events: list[dict] = []
        self.nodes: dict[str, Node] = {}
        self.cost, self.calls, self.tokens_in, self.tokens_out = 0.0, 0, 0, 0
        self.simulated = False
        self.timeouts = 0  # calls that got no answer in time: the provider may still bill them
        self._pre_gates: dict[str, Gate] = {}  # sub-task gates already asked while deciding whether a split pays
        self._jev_shared: dict[str, tuple[asyncio.Future, int]] = {}  # decisions answered together (proposal 15)
        self.reserved = 0.0
        self._freed = asyncio.Event()
        self.work_out, self.work_visible = 0, 0
        self.by_role: dict[str, float] = {}
        self.by_model: dict[str, float] = {}
        self.warnings: list[str] = []
        self.unchecked = False   # some result was kept without passing Jev's check
        self.incomplete = False  # some part failed and is missing from the answer
        self.subtask_count = 0
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        root = self._add(Node(id="root", title="Request", prompt=request, depth=0,
                              criteria="Answers everything the request asks for."))
        self._emit("run_started", root, request=request, profile=self.profile,
                   limits=asdict(self.limits))
        stop, error = "", ""
        try:
            await self._process(root, context="")
        except BudgetExceeded:
            stop = "budget"
        except asyncio.CancelledError:
            stop, error = "cancelled", "cancelled before the answer was ready"
            self._emit("error", root, message=error)
        except (GatewayError, PlanError) as e:
            stop, error = "error", str(e)
            self._emit("error", root, message=error)
        except Exception as e:  # noqa: BLE001 - an unexpected answer must not lose what was already paid
            stop, error = "error", f"{type(e).__name__}: {e}"[:500]
            self._emit("error", root, message=error)
        if stop == "budget":  # said once every call has settled, with what the run really spent
            error = f"budget of ${self.limits.max_cost:.2f} reached; this run spent ${self.cost:.4f}"
            self._emit("budget_exceeded", root, message=error)
        answer = root.result or self._partial_answer()
        if not answer.strip():
            status = "failed"
        elif stop or self.incomplete:
            status = "partial"
        elif self.unchecked:
            status = "unverified"
        else:
            status = "done"
        receipt = self._receipt(request, answer)
        self._emit("run_done", root, status=status, cost=round(self.cost, 6), receipt=receipt, answer=answer[:30000],
                   answer_chars=len(answer), error=error, warnings=self.warnings)
        result = RunResult(id=run_id, request=request, answer=answer, status=status,
                           nodes={k: asdict(v) for k, v in self.nodes.items()}, events=self.events,
                           receipt=receipt, started_at=started,
                           duration_s=round(time.perf_counter() - self._t0, 2), error=error,
                           warnings=list(self.warnings))
        if stop == "cancelled":
            raise RunCancelled(result)  # the caller stops; the server catches it to save what was done
        return result

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
                gate = self._pre_gates.pop(node.id, None)
                if gate is None:
                    gate = await self.decider.gate(node.prompt, gw=self._meter(node, "decisions"))
                node.gate = {"split": round(gate.split_probability, 3), "tier": gate.tier, "tier_raw": gate.tier_raw,
                             "tier_confidence": gate.tier_confidence, "raised": gate.raised, "floored": gate.floored,
                             "task_type": gate.task_type, "answer_type": gate.answer_type,
                             "answer_confidence": gate.answer_confidence}
                node.tier = gate.tier
                self._emit("gate", node, **node.gate, latency_ms=gate.latency_ms, cost=gate.usage.cost)
                room = self.limits.max_subtasks - self.subtask_count
                if self.allow_split and self.decider.should_split(gate, node.prompt, node.depth,
                                                                  self.limits.max_depth, room):
                    await self._split(node, gate, context)
                elif not context and self.decider.jev_can_answer(gate) and await self._answer_with_jev(node):
                    pass  # Jev answered it (idea 18)
                else:
                    await self._execute(node, gate, context)
            if node.id == "root" and node.kind == "split":
                await self._final_check(node)
            node.status = "partial" if node.missing else "unverified" if node.checked is False else "done"
        except (BudgetExceeded, asyncio.CancelledError):
            node.status = "failed"
            raise
        except (PlanError, GatewayError) as e:
            node.status = "failed"
            node.problem = str(e)
            self._emit("node_failed", node, message=str(e))
            if node.id == "root":
                raise
        except Exception as e:  # noqa: BLE001 - never leave a node "running"; the run records the error
            node.status, node.problem = "failed", f"{type(e).__name__}: {e}"[:500]
            raise
        finally:
            node.ended = time.perf_counter() - self._t0
            self._emit("node_done", node, status=node.status, model=node.model, tier=node.tier,
                       cost=round(node.cost, 6), tokens_in=node.tokens_in, tokens_out=node.tokens_out,
                       seconds=round(node.ended - (node.started or 0.0), 2), kind=node.kind, result=node.result[:6000],
                       notes=node.notes[:1500], problem=node.problem[:500], attempts=node.attempts,
                       warning=node.warning[:500])

    # ------------------------------------------------------------ split path
    async def _split(self, node: Node, gate: Gate, context: str) -> None:
        node.kind = "split"
        planner = self.planner if gate.tier >= 4 else self.planner_light
        # Claim the room left for sub-tasks now, before planning: splits planning at the same time must not
        # all count on the same room. What the plan does not use is given back.
        room = self.limits.max_subtasks - self.subtask_count
        if room < 2:
            node.kind = "llm"
            await self._execute(node, gate, context)
            return
        self.subtask_count += room
        try:
            plan: Plan = await planner.plan(node.prompt, context, gw=self._meter(node, "planning"), max_subtasks=room)
            if len(plan.subtasks) < 2:
                raise PlanError("the plan has a single part: nothing to split")
        except (PlanError, GatewayError, BudgetExceeded) as e:
            self.subtask_count -= room
            # no usable plan, or none the budget can pay for: do the request in one go rather than fail it
            self._emit("plan_failed", node, message=str(e)[:300])
            node.kind = "llm"
            await self._execute(node, gate, context)
            return
        except BaseException:
            self.subtask_count -= room
            raise
        self.subtask_count -= room - len(plan.subtasks)
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
        # Does splitting pay? Jev gates every part now (it costs almost nothing); if the parts would not go to
        # clearly cheaper models than the whole, do the whole in one go instead.
        gated = [k for k in kids if not k.decision]
        meter = self._meter(node, "decisions")
        results = await asyncio.gather(*(self.decider.gate(k.prompt, gw=meter) for k in gated),
                                       return_exceptions=True)
        tiers = []
        for k, g in zip(gated, results):
            if isinstance(g, BaseException):
                if not isinstance(g, GatewayError):
                    raise g
                continue  # this part will ask its own gate when it runs
            self._pre_gates[k.id] = g
            tiers.append(g.tier)
        pays, parts, whole = self._split_pays(gate, tiers)
        if not pays:
            for k in kids:
                self.nodes.pop(k.id, None)
                self._pre_gates.pop(k.id, None)
            node.children = []
            self.subtask_count -= len(kids)
            self._emit("split_rejected", node, parts_tiers=tiers, parts_typical=round(parts, 6),
                       whole_typical=round(whole, 6))
            node.kind = "llm"
            await self._execute(node, gate, context)
            return
        # Run in waves: every sub-task whose dependencies are done starts at once.
        pending = {k.id: k for k in kids}
        done: set[str] = set()
        while pending:
            ready = [k for k in pending.values() if all(d in done for d in k.depends_on)]
            if not ready:  # cannot happen after parse_plan, but never hang
                ready = list(pending.values())
            self._group_decisions(ready)
            outs = await asyncio.gather(*(self._process(k, self._context_for(k)) for k in ready),
                                        return_exceptions=True)
            for o in outs:  # a budget stop ends the run; other errors were already recorded on the node
                if isinstance(o, BaseException) and not isinstance(o, (PlanError, GatewayError)):
                    raise o
            for k in ready:
                done.add(k.id)
                pending.pop(k.id)
        failed = [k for k in kids if k.status == "failed"]
        if len(failed) == len(kids):
            raise GatewayError(0, "every part of the plan failed: " + (failed[0].problem[:200] if failed else ""))
        for k in failed:
            node.missing, self.incomplete = True, True
            k.result = ""  # a failed part is left out, and its draft is not passed on as if it were good
            self._warn(k, f"this part failed and is missing from the answer ({k.problem[:200]})")
        await self._aggregate(node, kids, plan.assembly)

    def _group_decisions(self, ready: list[Node]) -> None:
        """Decisions about the same items that can run now are answered in one Jev call per item (proposal 15)."""
        groups: dict[tuple, list[Node]] = {}
        for k in ready:
            if k.decision and k.id not in self._jev_shared:
                key = tuple((it["id"], it["text"]) for it in k.decision.get("items") or [])
                groups.setdefault(key, []).append(k)
        for group in groups.values():
            if len(group) < 2:
                continue
            task = asyncio.ensure_future(self.decider.solve_many([k.decision for k in group],
                                                                 gw=self._meter(group[0], "decisions")))
            for i, k in enumerate(group):
                self._jev_shared[k.id] = (task, i)

    def _context_for(self, node: Node) -> str:
        parts = []
        for d in node.depends_on:
            dep = self.nodes[d]
            chunk = f"[{dep.title}]\n{dep.result[:6000] or '(no result: this part failed)'}"
            if dep.notes:
                chunk += f"\nNotes: {dep.notes}"
            parts.append(chunk)
        return "\n\n".join(parts)

    async def _aggregate(self, node: Node, kids: list[Node], assembly: str) -> None:
        """A cheap model decides order, headings, a short intro and outro; the code stitches the results.
        The results are not rewritten, so assembling costs a few hundred output tokens, not thousands. If the
        assembly model fails, the results are stitched in plan order: nothing already paid is lost."""
        chain = [m.id for m in self.catalog.chain(2, self.profile)]
        node.model = chain[0]
        listing = []
        for k in kids:
            block = f"[{k.id.split('.')[-1]}] {k.title}\n{k.result[:1500] or '(no result)'}"
            if k.problem and k.status != "failed":
                block += f"\n(Problem reported: {k.problem})"
            listing.append(block)
        user = (f"REQUEST:\n{node.prompt}\n\nASSEMBLY INSTRUCTIONS:\n{assembly}\n\n"
                "RESULTS (shortened; ids in brackets):\n" + "\n\n".join(listing))
        self._emit("aggregate", node, model=chain[0])
        try:
            text = (await self._chat(node, "assembly", chain, AGGREGATOR, user, 1500)).text
        except (GatewayError, BudgetExceeded) as e:
            text = ""
            self._warn(node, f"the assembly step failed ({str(e)[:150]}), so the parts are shown in plan order")
        node.result = stitch(text, kids, level=2 if node.id == "root" else 3)

    def _split_pays(self, gate: Gate, part_tiers: list[int]) -> tuple[bool, float, float]:
        """Compare the typical call of the whole request's tier with the average written part. Splitting goes ahead
        only if no written part needs a higher tier than the whole and the parts are cheaper by the overhead
        factor. Parts that Jev answers are not counted either way; a plan made only of them always goes ahead."""
        whole = self.catalog.pick(gate.tier, self.profile).typical_cost
        if not part_tiers:
            return True, 0.0, whole
        if max(part_tiers) > gate.tier:
            return False, float("inf"), whole
        costs = [self.catalog.pick(t, self.profile).typical_cost for t in part_tiers]
        parts = sum(costs) / len(costs)
        return parts * self.decider.s.split_overhead < whole, parts, whole

    # ------------------------------------------------------ atomic path (LLM)
    async def _execute(self, node: Node, gate: Gate, context: str) -> None:
        """Route to the cheapest model of the tier, have Jev check the result, repair: once with feedback, then one
        tier up. A result that never passes is kept (the best one) and marked unverified."""
        node.kind = "llm"
        tier = gate.tier
        system = WORKER_ROOT if node.id == "root" else WORKER_SUB
        user = node.prompt if not context else f"{node.prompt}\n\nResults from earlier tasks you can use:\n{context}"
        feedback = ""
        best: float | None = None
        kept: tuple = ()
        for attempt in range(self.limits.max_repairs + 1):
            if attempt == 2:  # second repair: one tier up
                tier = min(4, tier + 1)
            chain = [m.id for m in self.catalog.chain(tier, self.profile)]
            node.model, node.tier = chain[0], tier
            self._emit("route", node, model=chain[0], tier=tier, attempt=attempt + 1)
            max_out = self.limits.worker_max_tokens if node.id == "root" else self.limits.subtask_max_tokens
            if tier >= 4:  # strong models think before they write: leave room for the answer
                max_out = max(max_out, 8000)
            try:
                r = await self._chat(node, "work", chain, system, user + feedback, max_out,
                                     reasoning=self.limits.reasoning.get(tier))
            except (GatewayError, BudgetExceeded) as e:
                if best is None:
                    raise  # nothing to keep: the task failed
                node.result, node.notes, node.problem, node.model, node.tier = kept
                self._unchecked(node, f"the repair could not be made ({str(e)[:150]}), so the best attempt was "
                                      f"kept although it did not pass Jev's check (best {best:.2f})")
                if isinstance(e, BudgetExceeded):
                    raise
                return
            model = node.model
            result, notes, problem = split_notes(r.text)
            if best is None:  # until a check says otherwise, the first result is the one to keep
                node.result, node.notes, node.problem = result, notes, problem
            try:
                p, usage, ms = await self.decider.verify(node.prompt, node.criteria, result,
                                                         gw=self._meter(node, "checks"))
            except GatewayError as e:
                node.result, node.notes, node.problem, node.model, node.tier = result, notes, problem, model, tier
                node.attempts.append({"model": model, "tier": tier, "ok_probability": None})
                self._emit("verify", node, ok=None, probability=None, attempt=attempt + 1, cost=0.0)
                self._unchecked(node, f"Jev could not check this result ({str(e)[:150]}), so it was kept unchecked")
                return
            node.attempts.append({"model": model, "tier": tier, "ok_probability": None if p is None else round(p, 3)})
            if p is None:
                node.result, node.notes, node.problem, node.model, node.tier = result, notes, problem, model, tier
                self._emit("verify", node, ok=None, probability=None, attempt=attempt + 1, cost=usage.cost)
                self._unchecked(node, "Jev's verdict could not be read, so the result was kept unchecked")
                return
            ok = p >= self.decider.s.verify_pass
            self._emit("verify", node, ok=ok, probability=round(p, 3), attempt=attempt + 1, cost=usage.cost)
            if best is None or p > best:
                best = p
                kept = (result, notes, problem, model, tier)
                node.result, node.notes, node.problem, node.model, node.tier = kept
            if ok:
                node.result, node.notes, node.problem, node.model, node.tier = result, notes, problem, model, tier
                node.checked = True
                return
            if problem:
                self._unchecked(node, f"the worker says the task cannot be done as planned: {problem[:200]}")
                return
            feedback = ("\n\nA reviewer found that the previous attempt did not fully meet the task. "
                        f"Previous attempt:\n{result[:4000]}\n\nDo the task again, completely.")
            if attempt < self.limits.max_repairs:
                self._emit("repair", node, next_attempt=attempt + 2, tier_up=(attempt + 1 == 2))
        node.result, node.notes, node.problem, node.model, node.tier = kept  # the best attempt, and who made it
        self._unchecked(node, f"no attempt passed Jev's check ({len(node.attempts)} tries, best {best:.2f}); "
                              "the best one was kept")

    # ------------------------------------------------ atomic path (Jev answers)
    async def _answer_with_jev(self, node: Node) -> bool:
        """A request that is itself a choice, a yes/no or a score (I1, idea 18): the cheapest model writes the closed
        question and points at the lines that hold the items, and Jev answers each item. False when it does not
        fit, and the task goes to a language model as usual."""
        lines = node.prompt.splitlines()
        if not lines or len(lines) > 400:
            return False
        numbered = "\n".join(f"L{i}: {line}" for i, line in enumerate(lines, 1))
        chain = [m.id for m in self.catalog.chain(1, self.profile)]
        try:
            r = await self._chat(node, "planning", chain, EXTRACT, numbered, 1500)
        except GatewayError as e:
            self._emit("jev_declined", node, reason=f"the question could not be written: {e}"[:300])
            return False
        spec = parse_extraction(r.text, lines)
        if not spec:
            self._emit("jev_declined", node, reason="not a closed question over items given in the request")
            return False
        node.decision, node.answer_type = spec, spec["kind"]
        self._emit("jev_extract", node, kind=spec["kind"], items=len(spec["items"]), question=spec["question"][:300])
        try:
            await self._solve_with_jev(node)
        except GatewayError as e:  # Jev and the fallback could not answer: a language model does the request
            node.decision, node.kind, node.answer_type, node.result = None, "", "text", ""
            node.checked, node.warning, node.missing = None, "", False
            self._emit("jev_declined", node, reason=f"the items could not be answered: {e}"[:300])
            return False
        return True

    async def _solve_with_jev(self, node: Node) -> None:
        node.kind = "jev"
        node.model = self.catalog.decider.id
        spec = node.decision
        shared = self._jev_shared.pop(node.id, None)
        if shared:
            task, i = shared
            out = (await task)[i]
        else:
            out = await self.decider.solve(spec, gw=self._meter(node, "decisions"))
        answers = {a["id"]: a for a in out["answers"]}
        self._emit("jev_solve", node, items=len(answers), unsure=out["unsure"], cost=out["usage"].cost,
                   latency_ms=out["latency_ms"])
        if out["unsure"]:  # items Jev was not sure about: a cheap model answers them with the same options
            chain = [m.id for m in self.catalog.chain(1, self.profile)]
            items = [it for it in spec["items"] if it["id"] in out["unsure"]]
            allowed = ", ".join(spec.get("options") or spec.get("scale") or ["yes", "no"])
            user = (f"{spec['question']}\nAllowed answers: {allowed}.\nAnswer each item with its id and one allowed "
                    "answer, one per line, like '3: label'.\n\n" + "\n".join(f"{it['id']}: {it['text']}" for it in items))
            try:
                r = await self._chat(node, "work", chain, "Answer with the allowed labels only.", user, 2000)
                for line in r.text.splitlines():
                    m = re.match(r"\s*([\w.-]+)\s*[:\-]\s*(.+)", line)
                    if m and m.group(1) in answers:
                        answers[m.group(1)].update(answer=m.group(2).strip().strip("*").strip(), confidence=None,
                                                   by=node.model if node.model != self.catalog.decider.id else chain[0])
                self._emit("fallback", node, model=chain[0], items=len(items))
            except GatewayError as e:
                self._emit("fallback", node, model=chain[0], items=len(items), failed=str(e)[:200])
                guessed = [it["id"] for it in items if answers.get(it["id"], {}).get("answer") is not None]
                if guessed:
                    self._unchecked(node, f"the model for the {len(items)} item(s) Jev was unsure about failed, so "
                                          f"Jev's unsure answers were kept for {', '.join(guessed[:10])}")
        unanswered = [a["id"] for a in answers.values() if a.get("answer") is None]
        if unanswered and len(unanswered) == len(answers):
            raise GatewayError(0, "no item could be answered, by Jev or by the fallback model")
        if unanswered:
            node.missing, self.incomplete = True, True
            self._warn(node, f"{len(unanswered)} item(s) got no answer: {', '.join(unanswered[:10])}")
        node.model = self.catalog.decider.id
        # The question and options are in English for Jev; the reader sees each item with its label in the
        # language of the request (the planner's "labels"), and no English question.
        labels = spec.get("labels") or {}
        if len(spec["items"]) == 1 and node.id == "root":
            a = answers.get(spec["items"][0]["id"], {})
            label = str(a.get("answer") or "?")
            node.result = labels.get(label, label)
            return
        lines = []
        for it in spec["items"]:
            a = answers.get(it["id"], {})
            text = it["text"] if len(it["text"]) <= 80 else it["text"][:77] + "..."
            label = str(a.get("answer") or "?")
            mark = "" if OWN_MARK.match(text) else f"{it['id']}. "  # "1) ..." keeps its own number
            lines.append(f"- {mark}{text}: **{labels.get(label, label)}**")
        node.result = "\n".join(lines)

    # ------------------------------------------------------------ final check
    async def _final_check(self, root: Node) -> None:
        """Jev checks the assembled answer against the request; below `final_pass`, a cheap model adds only what is
        missing. If the check cannot run or be read, the answer stands and is marked unverified."""
        request = root.prompt
        try:
            p, usage, ms = await self.decider.final_check(request, root.result, gw=self._meter(root, "checks"))
        except (GatewayError, BudgetExceeded) as e:
            self._emit("final_check", root, ok=None, probability=None, cost=0.0, message=str(e)[:200])
            self._unchecked(root, f"the final check could not run ({str(e)[:150]})")
            return
        if p is None:
            self._emit("final_check", root, ok=None, probability=None, cost=usage.cost)
            self._unchecked(root, "the final check's verdict could not be read")
            return
        ok = p >= self.decider.s.final_pass
        self._emit("final_check", root, ok=ok, probability=round(p, 3), cost=usage.cost)
        if ok:
            return
        chain = [m.id for m in self.catalog.chain(2, self.profile)]
        self._emit("repair", root, next_attempt=2, tier_up=False, model=chain[0], gap_fill=True)
        user = f"REQUEST:\n{request}\n\nANSWER:\n{root.result}"
        try:
            extra = (await self._chat(root, "assembly", chain, GAP_FILLER, user, 2500)).text.strip()
        except (GatewayError, BudgetExceeded) as e:
            self._unchecked(root, f"the final check found something missing, and it could not be added ({str(e)[:120]})")
            return
        if extra and "NOTHING MISSING" not in extra.upper():
            root.result = root.result.rstrip() + "\n\n" + extra

    # ---------------------------------------------------------------- receipt
    def _partial_answer(self) -> str:
        done = [n for n in self.nodes.values() if n.id != "root" and n.result and not n.children]
        if not done:
            return ""
        return "\n\n".join(f"## {n.title}\n{n.result}" for n in done)

    def _receipt(self, request: str, answer: str) -> dict:
        base = self.catalog.get(self.catalog.baseline)
        # The strong model would also spend hidden reasoning tokens: scale the answer length by the ratio of
        # billed output to visible output that Taskpenny's own workers showed in this run (never below 1).
        factor = max(1.0, self.work_out / self.work_visible) if self.work_visible else 1.0
        tin, tout = estimate_tokens(request) + 40, int(estimate_tokens(answer) * factor)
        baseline_cost = base.cost(tin, tout)
        # a simulated run measured nothing, so it claims no saving
        saving = (1 - self.cost / baseline_cost) * 100 if baseline_cost > 0 and not self.simulated else None
        return {
            "simulated": self.simulated,
            "total_cost": round(self.cost, 6),
            "calls": self.calls,
            "timeouts": self.timeouts,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "by_role": {k: round(v, 6) for k, v in sorted(self.by_role.items())},
            "by_model": {k: round(v, 6) for k, v in sorted(self.by_model.items(), key=lambda kv: -kv[1])},
            "baseline": {
                "model": base.id,
                "estimated_cost": round(baseline_cost, 6),
                "method": "one call to the baseline model with this request and an answer as long as Taskpenny's, "
                          "scaled by the hidden reasoning Taskpenny's own models used in this run, with no retries",
                "reasoning_factor": round(factor, 2),
            },
            "saving_pct": round(saving, 1) if saving is not None else None,
        }
