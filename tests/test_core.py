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
            ans["tier"] = {"type": "choice", "choice": g["tier"][0],
                           "probabilities": g.get("tier_probs") or {g["tier"][0]: 0.9}}
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

    async def chat(self, model, messages, *, max_tokens=None, temperature=None, json_mode=False, reasoning=None):
        self.chats.append(model)
        system = messages[0]["content"]
        if system.startswith("You are the planner"):
            out = json.dumps(self.plan)
        elif system.startswith("You assemble"):
            out = self.outline if hasattr(self, "outline") else '{"intro": "", "sections": [], "outro": ""}'
        elif system.startswith("You check a finished answer"):
            out = getattr(self, "gap", "NOTHING MISSING")
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
    gw = ScriptedGateway(catalog, gate={"split": 0.2, "tier": ("2", 0.4), "task_type": "code", "answer": "text",
                                        "tier_probs": {"1": 0.05, "2": 0.55, "3": 0.35, "4": 0.05}})
    g = await Decider(gw, catalog).gate("write code")
    assert g.tier_raw == 2 and g.tier == 3 and g.raised


async def test_gate_keeps_tier_when_doubt_points_down(catalog):
    gw = ScriptedGateway(catalog, gate={"split": 0.2, "tier": ("2", 0.4), "task_type": "code", "answer": "text",
                                        "tier_probs": {"1": 0.4, "2": 0.5, "3": 0.08, "4": 0.02}})
    g = await Decider(gw, catalog).gate("write code")
    assert g.tier == 2 and not g.raised


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
    gw = ScriptedGateway(catalog, gate={"split": 0.95, "tier": ("3", 0.9), "task_type": "writing", "answer": "text"},
                         plan=plan, solve={"second": ("b", 0.3)})
    gw.outline = json.dumps({"intro": "Here it is.", "sections": [{"from": "t3", "heading": "Final draft"},
                                                                  {"from": "t2", "heading": "Types"}], "outro": ""})
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
    assert gw.chats[0] == "google/gemini-3.8-flash"  # the light planner: the request is tier 3, not critical
    # the answer is stitched from the untouched results, in the outline's order
    ans = res.answer
    assert ans.startswith("Here it is.") and ans.index("## Final draft") < ans.index("## Types")
    assert "## Draft" not in ans  # t1 left out by the outline


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


async def test_refused_model_falls_back_to_next_in_tier(catalog):
    from siac.gateway import GatewayError

    class Refusing(ScriptedGateway):
        async def chat(self, model, messages, **kw):
            if model == "anthropic/claude-opus-5.5":
                self.chats.append(model)
                raise GatewayError(429, "No access to this model at this time.")
            return await super().chat(model, messages, **kw)

    gw = Refusing(catalog, gate={"split": 0.1, "tier": ("4", 0.9), "task_type": "analysis", "answer": "text"})
    events = []
    res = await Engine(gw, catalog, on_event=events.append).run("A high-stakes decision")
    assert res.status == "done"
    assert gw.chats == ["anthropic/claude-opus-5.5", "anthropic/claude-opus-5"]
    assert res.nodes["root"]["model"] == "anthropic/claude-opus-5"
    assert any(e["type"] == "model_fallback" for e in events)


def test_chain_order(catalog):
    # A fallback never costs much more than the first choice: Opus 5.5 -> Opus 5 (1.25x) -> Sonnet (tier below),
    # never GPT-6 Astra (2.5x).
    assert [m.id for m in catalog.chain(4)] == ["anthropic/claude-opus-5.5", "anthropic/claude-opus-5",
                                                "anthropic/claude-sonnet-5"]
    assert [m.id for m in catalog.chain(3)][:2] == ["anthropic/claude-sonnet-5", "openai/gpt-6-sol"]


def test_stitch_falls_back_to_plan_order():
    from siac.engine import Node, stitch
    kids = [Node(id="t1", title="One", prompt="", depth=1, result="first"),
            Node(id="t2", title="Two", prompt="", depth=1, result="second")]
    assert stitch("not json", kids) == "## One\n\nfirst\n\n## Two\n\nsecond"


async def test_split_needs_a_list_or_high_confidence(catalog):
    from siac.decider import Decider
    d = Decider(ScriptedGateway(catalog), catalog)
    g = await d.gate("x")
    g.tier_raw, g.split_probability = 3, 0.8
    one_piece = "Analyse why our trading bot lost money for four weeks and tell me whether to stop it. " * 3
    listed = "Do these: 1) write the plan, 2) write the email, 3) list the risks. " * 3
    assert not d.should_split(g, one_piece, 0, 3)
    assert d.should_split(g, listed, 0, 3)
    g.split_probability = 0.95
    assert d.should_split(g, one_piece, 0, 3)


async def test_gap_filler_appends_missing_part(catalog):
    plan = {"subtasks": [{"id": "t1", "title": "A", "prompt": "do a"}, {"id": "t2", "title": "B", "prompt": "do b"}]}
    gw = ScriptedGateway(catalog, gate={"split": 0.95, "tier": ("3", 0.9), "task_type": "writing", "answer": "text"},
                         plan=plan, verify=[0.95, 0.95, 0.1])
    gw.gap = "## Missing\nThe part that was missing."
    res = await Engine(gw, catalog).run("Please do all of this: 1) a thing, 2) another thing. " * 5)
    assert res.answer.endswith("The part that was missing.")


async def test_parallel_calls_cannot_overshoot_the_budget(catalog):
    plan = {"subtasks": [{"id": f"t{i}", "title": str(i), "prompt": f"do {i}"} for i in range(1, 9)]}
    gw = ScriptedGateway(catalog, gate={"split": 0.95, "tier": ("3", 0.9), "task_type": "writing", "answer": "text"},
                         plan=plan)
    res = await Engine(gw, catalog, limits=Limits(max_cost=0.08)).run("List: 1) a, 2) b, 3) c. " * 10)
    assert res.receipt["total_cost"] <= 0.08


def test_export_markdown(tmp_path, capsys):
    from siac.cli import main
    assert main(["demo", "--save-dir", str(tmp_path), "--quiet"]) == 0
    run = next(tmp_path.glob("*.json"))
    assert main(["export", str(run)]) == 0
    md = capsys.readouterr().out
    assert "## Task tree" in md and "| root |" in md and "## Cost receipt" in md


async def test_empty_answer_moves_to_next_model(catalog):
    class Thinker(ScriptedGateway):
        async def chat(self, model, messages, **kw):
            r = await super().chat(model, messages, **kw)
            if model == "anthropic/claude-opus-5.5":
                r.text = ""  # all output tokens went into hidden reasoning
            return r

    gw = Thinker(catalog, gate={"split": 0.1, "tier": ("4", 0.9), "task_type": "analysis", "answer": "text"})
    res = await Engine(gw, catalog).run("A hard question")
    assert res.nodes["root"]["model"] == "anthropic/claude-opus-5" and res.answer == "done"


def test_benchmark_label_scoring():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("bench_run", Path(__file__).parent.parent / "bench/public/run.py")
    run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run)
    gold = {"1": "exchange rate", "2": "change pin", "3": "activate my card"}
    opts = ["activate my card", "change pin", "exchange rate", "apple pay or google pay"]
    strict = run.score_labels("1: exchange rate\n2: change pin\n3: activate my card", gold, opts)
    assert strict["correct"] == 3 and strict["strict"]
    loose = run.score_labels("Labels\n- 1. What is my exchange rate? -> exchange rate\n**2**: Change PIN\n"
                             "3 - Apple Pay or Google Pay", gold, opts)
    assert loose["correct"] == 2 and not loose["strict"]
    assert run.parse_verdict("A is fine but [[B]]") == "B" and run.parse_verdict("no verdict") is None


def test_openai_request_from_messages():
    from siac import openai_api as oai
    assert oai.request_from_messages([{"role": "user", "content": "Hi"}]) == "Hi"
    req = oai.request_from_messages([
        {"role": "system", "content": "Answer in Catalan."},
        {"role": "user", "content": [{"type": "text", "text": "What is 2+2?"}]},
        {"role": "assistant", "content": "4"},
        {"role": "user", "content": "And times 3?"}])
    assert req.startswith("Instructions to follow:\nAnswer in Catalan.")
    assert "User: What is 2+2?" in req and "Assistant: 4" in req and req.endswith("And times 3?")
    assert oai.profile_from_model("siac", ["all", "anthropic"]) == "all"
    assert oai.profile_from_model("siac/anthropic", ["all", "anthropic"]) == "anthropic"
    for bad in ([], [{"role": "assistant", "content": "x"}]):
        with pytest.raises(oai.BadRequest):
            oai.request_from_messages(bad)
    with pytest.raises(oai.BadRequest):
        oai.profile_from_model("gpt-4o", ["all"])


def test_openai_endpoint_end_to_end(tmp_path, monkeypatch):
    import threading
    from http.server import ThreadingHTTPServer

    import httpx

    from siac.catalog import Catalog
    from siac.server import App, make_handler

    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    app = App(Catalog.load(), tmp_path, dry_run_default=True, api_key="secret")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    try:
        auth = {"Authorization": "Bearer secret"}
        assert httpx.get(base + "/models").status_code == 401
        ids = [m["id"] for m in httpx.get(base + "/models", headers=auth).json()["data"]]
        assert ids[0] == "siac" and "siac/anthropic" in ids
        body = {"model": "siac", "messages": [{"role": "user", "content": "Say hello to the team."}]}
        r = httpx.post(base + "/chat/completions", json=body, headers=auth, timeout=30).json()
        assert r["object"] == "chat.completion" and r["choices"][0]["message"]["content"]
        assert r["siac"]["cost_usd"] >= 0 and (tmp_path / f"{r['siac']['run_id']}.json").exists()
        s = httpx.post(base + "/chat/completions", json=body | {"stream": True}, headers=auth, timeout=30).text
        assert s.strip().endswith("data: [DONE]") and '"chat.completion.chunk"' in s
        bad = httpx.post(base + "/chat/completions", json=body | {"tools": [{"type": "function"}]}, headers=auth)
        assert bad.status_code == 400 and "tool" in bad.json()["error"]["message"]
    finally:
        httpd.shutdown()


def _mock_client(handler):
    import httpx
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_direct_anthropic_and_openai_formats(catalog):
    import httpx

    from siac.providers import PROVIDERS, DirectClient
    seen = {}

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        seen[request.url.host] = (body, dict(request.headers))
        if request.url.host == "api.anthropic.com":
            return httpx.Response(200, json={"content": [{"type": "text", "text": "Hola"}],
                                             "usage": {"input_tokens": 100, "output_tokens": 10}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Hi"}}],
                                         "usage": {"prompt_tokens": 50, "completion_tokens": 5}})
    client = _mock_client(handler)
    a = DirectClient(PROVIDERS["anthropic"], "k-a", catalog, client)
    r = await a.chat("anthropic/claude-sonnet-5", [{"role": "system", "content": "Be brief."},
                                                   {"role": "user", "content": "Hi"}], max_tokens=50, reasoning="off")
    body, headers = seen["api.anthropic.com"]
    assert r.text == "Hola" and body["model"] == "claude-sonnet-5" and body["system"] == "Be brief."
    assert body["output_config"] == {"effort": "low"} and headers["x-api-key"] == "k-a"
    assert r.usage.cost == pytest.approx(catalog.get("anthropic/claude-sonnet-5").cost(100, 10))
    o = DirectClient(PROVIDERS["openai"], "k-o", catalog, client)
    r = await o.chat("openai/gpt-6-luna", [{"role": "user", "content": "Hi"}], max_tokens=20, json_mode=True)
    body, headers = seen["api.openai.com"]
    assert r.text == "Hi" and body["model"] == "gpt-6-luna" and body["max_completion_tokens"] == 20
    assert headers["authorization"] == "Bearer k-o" and body["response_format"] == {"type": "json_object"}
    await client.aclose()


async def test_direct_retries_without_refused_controls(catalog):
    import httpx

    from siac.providers import PROVIDERS, DirectClient
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "output_config" in body:
            return httpx.Response(400, json={"error": {"message": "effort not supported"}})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})
    client = _mock_client(handler)
    r = await DirectClient(PROVIDERS["anthropic"], "k", catalog, client).chat(
        "anthropic/claude-opus-5.5", [{"role": "user", "content": "x"}], reasoning="low")
    assert r.text == "ok" and len(bodies) == 2 and bodies[0]["model"] == "claude-opus-5-5"
    await client.aclose()


async def test_llm_decider_answers_jev_questions(catalog):
    from siac.gateway import ChatResult, Usage
    from siac.providers import LLMDecider

    async def chat(model, messages, **kw):
        assert kw["json_mode"] and "QUESTIONS" in messages[-1]["content"]
        return ChatResult(text='{"split": {"probability": 0.8}, "tier": {"probabilities": {"1": 0.1, "2": 0.7, '
                               '"3": 0.2, "4": 0}}, "level": {"probabilities": {"0": 0, "1": 3, "2": 1}}}',
                          model=model, usage=Usage(200, 40, 0.0001, "catalog"), latency_ms=5)
    d = LLMDecider(chat, catalog.pick(1))
    r = await d.evaluate({"request": "x"}, {
        "split": {"type": "boolean", "instructions": "Split?"},
        "tier": {"type": "choice", "instructions": "Tier?", "criteria": {"1": "a", "2": "b", "3": "c", "4": "d"}},
        "level": {"type": "score", "instructions": "How bad?", "criteria": ["low", "mid", "high"]}})
    assert r.boolean("split") == 0.8
    choice, conf, probs = r.choice("tier")
    assert choice == "2" and 0 < conf < 1 and sum(probs.values()) == pytest.approx(1)
    assert r.score("level")[0] == 1.0 and r.usage.cost == 0.0001


def test_connect_picks_routes_from_keys(catalog, monkeypatch):
    from siac.gateway import Gateway, GatewayError
    from siac.providers import MultiGateway, connect
    for v in ("AI_GATEWAY_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
              "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "SIAC_DIRECT", "SIAC_DECIDER"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(GatewayError):
        connect(catalog)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "v")
    gw, cat = connect(catalog)
    assert isinstance(gw, Gateway) and cat is catalog
    monkeypatch.delenv("AI_GATEWAY_API_KEY")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    gw, cat = connect(catalog)
    assert isinstance(gw, MultiGateway) and cat.providers("all") == ["anthropic"]
    assert cat.pick(1).id == "anthropic/claude-haiku-4.5" and cat.decider.id == "anthropic/claude-haiku-4.5"
    assert gw.route("anthropic/claude-sonnet-5") == "anthropic"
    with pytest.raises(GatewayError):
        gw.route("openai/gpt-6-luna")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "v")
    monkeypatch.setenv("SIAC_DIRECT", "anthropic")
    gw, cat = connect(catalog)
    assert gw.route("anthropic/claude-sonnet-5") == "anthropic" and gw.route("openai/gpt-6-luna") == "vercel"
    assert gw.decider is None  # Jev still decides


def test_local_models_go_first_and_keep_cloud_fallbacks(catalog, monkeypatch):
    from siac.providers import connect, local_models
    ms = local_models("qwen3:4b, qwen3:8b@2")
    assert [(m.id, m.tiers, m.direct_id) for m in ms] == [("ollama/qwen3:4b", (1,), "qwen3:4b"),
                                                          ("ollama/qwen3:8b", (1, 2), "qwen3:8b")]
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "DEEPSEEK_API_KEY",
              "DASHSCOPE_API_KEY", "SIAC_DIRECT", "SIAC_DECIDER"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "v")
    monkeypatch.setenv("SIAC_LOCAL", "qwen3:4b")
    gw, cat = connect(catalog)
    chain = [m.id for m in cat.chain(1)]
    assert chain[0] == "ollama/qwen3:4b" and "openai/gpt-6-luna" in chain  # free first, cloud still behind it
    assert gw.route("ollama/qwen3:4b") == "ollama" and gw.route("openai/gpt-6-luna") == "vercel"
    assert gw.decider is None  # Jev keeps deciding
    monkeypatch.delenv("AI_GATEWAY_API_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    gw, cat = connect(catalog)
    assert cat.decider.id == "openai/gpt-6-luna"  # a paid basic model decides, not the local one
