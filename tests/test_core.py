"""Core tests. No network, no key, no cost: scripted gateways stand in for Jev and the models."""

import json

import pytest

from siac.catalog import Catalog, NoModelError
from siac.decider import Decider, Settings
from siac.engine import Engine, Limits, split_notes
from siac.gateway import ChatResult, EvalResult, Usage, confidence_from_probs
from siac.planner import PlanError, _extract_json, parse_plan
from siac.simulate import SimulatedGateway


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load()


class ScriptedGateway:
    """Answers Jev questions and chat calls from small scripts, and records every call."""

    def __init__(self, catalog, *, gate=None, verify=None, plan=None, solve=None, text="done"):
        self.catalog = catalog
        self.gate = gate or {"split": 0.1, "tier": ("2", 0.9), "task_type": "writing", "answer": "text"}
        self.verify = list(verify or [])
        self.plan = plan
        self.solve = solve or {}
        self.text = text
        self.chats: list[str] = []
        self.evals: list[list[str]] = []

    async def aclose(self):
        pass

    async def evaluate(self, state, questions):
        self.evals.append(list(questions))
        ans, conf = {}, {}
        if "tier" in questions:
            g = self.gate
            ans["split"] = {"type": "boolean", "probability": g["split"]}
            ans["tier"] = {"type": "choice", "choice": g["tier"][0], "probabilities": {g["tier"][0]: 0.9}}
            conf["tier"] = g["tier"][1]
            ans["task_type"] = {"type": "choice", "choice": g["task_type"]}
            ans["answer"] = {"type": "choice", "choice": g["answer"]}
        elif "ok" in questions:
            p = self.verify.pop(0) if self.verify else 0.95
            ans["ok"] = {"type": "boolean", "probability": p}
        else:
            text = state["text"]
            label, c = self.solve.get(text, ("a", 0.9))
            ans["a"] = {"type": "choice", "choice": label}
            conf["a"] = c
        return EvalResult(ans, conf, Usage(100, 0, 100 * 0.042 / 1e6), 5)

    async def chat(self, model, messages, *, max_tokens=None, temperature=None, json_mode=False):
        self.chats.append(model)
        system = messages[0]["content"]
        if system.startswith("You are the planner"):
            out = json.dumps(self.plan)
        elif "Allowed answers" in messages[-1]["content"]:
            out = "2: b"
        else:
            out = self.text
        return ChatResult(out, model, Usage(1000, 500, self.catalog.get(model).cost(1000, 500)), 5)


# ------------------------------------------------------------------ catalog
def test_cheapest_model_per_tier(catalog):
    assert catalog.pick(1).id == "openai/gpt-6-luna"
    assert catalog.pick(4).id == "anthropic/claude-opus-5.5"
    assert catalog.pick(1, "anthropic").id == "anthropic/claude-haiku-4.5"
    assert catalog.pick(3, "anthropic").id == "anthropic/claude-sonnet-5"


def test_price_tie_keeps_file_order(catalog):
    # Sonnet 5 and GPT-6 Sol cost the same; Sonnet is listed first.
    assert catalog.pick(3).id == "anthropic/claude-sonnet-5"


def test_unknown_profile_and_empty_catalog(catalog):
    with pytest.raises(KeyError):
        catalog.pick(2, "nope")
    data = {"decider": {"id": "typesafe-ai/jev", "price": {"input": 0.042}}, "planner": "x/a", "baseline": "x/a",
            "tiers": {1: {"name": "b", "description": ""}}, "task_types": {"code": ""},
            "models": [{"id": "x/a", "tiers": [4], "price": {"input": 1, "output": 1}}]}
    c = Catalog.from_dict(data)
    assert c.pick(1).id == "x/a"  # goes up the tiers until something fits
    with pytest.raises(NoModelError):
        Catalog.from_dict({**data, "profiles": {"none": []}}).pick(1, "none")


def test_confidence_approximation():
    assert confidence_from_probs({"a": 1.0, "b": 0.0}) == 1.0
    assert confidence_from_probs({"a": 0.5, "b": 0.5}) == 0.0
    assert round(confidence_from_probs({"a": 0.7, "b": 0.1, "c": 0.1, "d": 0.1}), 2) == 0.6


# ------------------------------------------------------------------ planner
def test_plan_parsing_cleans_bad_input():
    data = {"subtasks": [
        {"id": "t1", "title": "A", "prompt": "do a", "depends_on": ["t9", "t1"]},
        {"id": "t2", "title": "B", "prompt": "do b", "depends_on": ["t1"], "answer_type": "choice",
         "decision": {"question": "Which?", "options": {"x": "", "y": ""}, "items": [{"id": "1", "text": "hi"}]}},
        {"id": "t3", "title": "C", "prompt": "do c", "answer_type": "choice", "decision": {"question": "?"}},
        {"id": "t4", "title": "D", "prompt": "do d", "depends_on": ["t5"]},
        {"id": "t5", "title": "E", "prompt": "do e"},
    ]}
    subs, assembly = parse_plan(data, 12)
    by = {s.id: s for s in subs}
    assert by["t1"].depends_on == []                 # unknown and self dependencies dropped
    assert by["t2"].answer_type == "choice" and by["t2"].decision["kind"] == "choice"
    assert by["t3"].answer_type == "text"            # unusable decision falls back to a language model
    assert by["t4"].depends_on == []                 # forward reference dropped: no cycles
    assert assembly


def test_plan_limits_and_errors():
    subs, _ = parse_plan({"subtasks": [{"prompt": f"p{i}"} for i in range(20)]}, 12)
    assert len(subs) == 12
    with pytest.raises(PlanError):
        parse_plan({"subtasks": []}, 12)
    assert _extract_json('Sure!\n```json\n{"subtasks": [1]}\n```') == {"subtasks": [1]}


def test_split_notes():
    r, n, p = split_notes("The answer.\nHANDOFF NOTES:\n- keep it short")
    assert (r, n, p) == ("The answer.", "- keep it short", "")
    r, n, p = split_notes("Partial.\nPLAN PROBLEM: the data does not exist")
    assert p == "the data does not exist"


# ------------------------------------------------------------------ decider
async def test_gate_raises_tier_when_jev_is_unsure(catalog):
    gw = ScriptedGateway(catalog, gate={"split": 0.2, "tier": ("2", 0.4), "task_type": "code", "answer": "text"})
    g = await Decider(gw, catalog).gate("write code")
    assert g.tier_raw == 2 and g.tier == 3 and g.raised


async def test_trivial_or_short_requests_never_split(catalog):
    d = Decider(ScriptedGateway(catalog), catalog, Settings(min_split_chars=50))
    gw_gate = await d.gate("x")
    gw_gate.split_probability = 0.99
    gw_gate.tier_raw = 1
    assert not d.should_split(gw_gate, "y" * 500, 0, 3)
    gw_gate.tier_raw = 3
    assert not d.should_split(gw_gate, "short", 0, 3)
    assert d.should_split(gw_gate, "y" * 500, 0, 3)
    assert not d.should_split(gw_gate, "y" * 500, 3, 3)


# ------------------------------------------------------------------ engine
async def test_atomic_request_goes_to_cheapest_model(catalog):
    gw = ScriptedGateway(catalog, gate={"split": 0.1, "tier": ("1", 0.95), "task_type": "writing", "answer": "text"})
    res = await Engine(gw, catalog).run("Hello!")
    assert res.status == "done"
    assert gw.chats == ["openai/gpt-6-luna"]
    assert res.nodes["root"]["kind"] == "llm"
    assert res.receipt["total_cost"] > 0 and res.receipt["baseline"]["model"] == "anthropic/claude-opus-5.5"


async def test_repair_then_one_tier_up(catalog):
    gw = ScriptedGateway(catalog, verify=[0.1, 0.2, 0.9])
    res = await Engine(gw, catalog).run("Write an email")
    # tier 2 -> deepseek twice (first try and repair with feedback), then tier 3 -> sonnet
    assert gw.chats == ["deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4.1-flash", "anthropic/claude-sonnet-5"]
    assert [a["tier"] for a in res.nodes["root"]["attempts"]] == [2, 2, 3]


async def test_split_runs_subtasks_and_jev_solves_choices(catalog):
    plan = {"subtasks": [
        {"id": "t1", "title": "Draft", "prompt": "write the draft", "success_criteria": "a draft"},
        {"id": "t2", "title": "Classify", "prompt": "classify", "answer_type": "choice", "depends_on": [],
         "decision": {"question": "Which type?", "options": {"a": "A", "b": "B"},
                      "items": [{"id": "1", "text": "first"}, {"id": "2", "text": "second"}]}},
        {"id": "t3", "title": "Polish", "prompt": "polish the draft", "depends_on": ["t1"]},
    ], "assembly": "join"}
    gw = ScriptedGateway(catalog, gate={"split": 0.9, "tier": ("3", 0.9), "task_type": "writing", "answer": "text"},
                         plan=plan, solve={"second": ("b", 0.3)})
    events = []
    res = await Engine(gw, catalog, on_event=events.append).run("A long request. " * 20)
    assert res.status == "done"
    assert set(res.nodes["root"]["children"]) == {"t1", "t2", "t3"}
    assert res.nodes["t2"]["kind"] == "jev"
    assert "-> a" in res.nodes["t2"]["result"] and "-> b" in res.nodes["t2"]["result"]  # item 2 via fallback model
    types = [e["type"] for e in events]
    assert "plan" in types and "jev_solve" in types and "fallback" in types and "final_check" in types
    # t3 depends on t1: it starts only after t1 is done
    t1_done = next(e["t"] for e in events if e["type"] == "node_done" and e["node"] == "t1")
    t3_start = next(e["t"] for e in events if e["type"] == "node_started" and e["node"] == "t3")
    assert t3_start >= t1_done
    assert gw.chats[0] == "anthropic/claude-sonnet-5"  # the planner


async def test_budget_stops_the_run(catalog):
    gw = ScriptedGateway(catalog)
    res = await Engine(gw, catalog, limits=Limits(max_cost=0.000001)).run("Write an email")
    assert res.status == "partial" and "budget" in res.error


async def test_no_split_flag(catalog):
    gw = ScriptedGateway(catalog, gate={"split": 0.99, "tier": ("3", 0.9), "task_type": "writing", "answer": "text"},
                         plan={"subtasks": [{"prompt": "x"}]})
    res = await Engine(gw, catalog, allow_split=False).run("long " * 100)
    assert res.nodes["root"]["kind"] == "llm"


async def test_dry_run_demo_end_to_end(catalog):
    from siac.cli import DEMO_PROMPT
    gw = SimulatedGateway(catalog, latency=(0, 0.001))
    res = await Engine(gw, catalog).run(DEMO_PROMPT)
    assert res.status == "done"
    assert res.nodes["root"]["kind"] == "split"
    assert any(n["kind"] == "jev" for n in res.nodes.values())
    assert res.events[0]["type"] == "run_started" and res.events[-1]["type"] == "run_done"


def test_cli_models_and_demo(tmp_path, capsys):
    from siac.cli import main
    assert main(["models"]) == 0
    assert "Cheapest per tier" in capsys.readouterr().out
    assert main(["demo", "--save-dir", str(tmp_path), "--quiet"]) == 0
    assert list(tmp_path.glob("*.json"))
