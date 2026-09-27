"""Tests for the cases the external review of 27 September 2026 found untested. No network, no key, no cost."""

import asyncio
import json
import threading

import httpx
import pytest

from taskpenny.catalog import Catalog
from taskpenny.engine import Engine, Limits, Node, reserve_tokens, stitch
from taskpenny.gateway import ChatResult, EvalResult, Gateway, GatewayError, Usage

from test_core import ScriptedGateway, _serve


@pytest.fixture(scope="module")
def catalog():
    return Catalog.load()


TEXT = {"split": 0.1, "tier": ("2", 0.9), "task_type": "writing", "answer": "text"}
SPLIT = {"split": 0.95, "tier": ("3", 0.9), "task_type": "writing", "answer": "text"}
TWO_PARTS = {"subtasks": [{"id": "t1", "title": "A", "prompt": "do a"}, {"id": "t2", "title": "B", "prompt": "do b"}]}
LISTED = "Please do all of this: 1) a thing, 2) another thing. " * 5


# ------------------------------------------------------------------ C1: nothing fails in silence
async def test_checks_that_never_pass_leave_the_run_unverified(catalog):
    class Refuser(ScriptedGateway):
        async def chat(self, model, messages, **kw):
            r = await super().chat(model, messages, **kw)
            r.text = "I cannot help with that." if len(self.chats) < 3 else "Still no."
            return r
    gw = Refuser(catalog, gate=TEXT, verify=[0.05, 0.3, 0.05])
    res = await Engine(gw, catalog).run("Write an email")
    root = res.nodes["root"]
    assert res.status == "unverified" and root["status"] == "unverified" and root["checked"] is False
    assert res.warnings and "no attempt passed" in res.warnings[0]
    assert [a["ok_probability"] for a in root["attempts"]] == [0.05, 0.3, 0.05]
    assert res.answer == "I cannot help with that."  # the best attempt (0.3) is the one kept...
    assert root["model"] == "deepseek/deepseek-v4.1-flash" and root["tier"] == 2  # ...with who made it


async def test_a_failed_part_makes_the_run_partial_and_is_named(catalog):
    class OnePartFails(ScriptedGateway):
        async def chat(self, model, messages, **kw):
            if messages[-1]["content"].startswith("do b"):
                raise GatewayError(400, "the provider refused this part")
            return await super().chat(model, messages, **kw)
    gw = OnePartFails(catalog, gate=SPLIT, plan=TWO_PARTS)
    res = await Engine(gw, catalog).run(LISTED)
    assert res.status == "partial" and res.nodes["t2"]["status"] == "failed"
    assert res.nodes["root"]["status"] == "partial"
    assert any("t2" in w and "missing" in w for w in res.warnings)
    assert "done" in res.answer  # the part that worked is still there


async def test_an_unreadable_verdict_is_not_paid_for_with_repairs(catalog):
    class NoVerdict(ScriptedGateway):
        async def evaluate(self, state, questions):
            r = await super().evaluate(state, questions)
            if "ok" in questions:
                r.answers["ok"] = {"type": "boolean", "probability": None}
            return r
    gw = NoVerdict(catalog, gate=TEXT)
    res = await Engine(gw, catalog).run("Write an email")
    assert len(gw.chats) == 1  # no repair, no tier up
    assert gw.evals.count(["ok"]) == 2  # asked again once
    assert res.status == "unverified" and "could not be read" in res.warnings[0]


async def test_a_check_that_errors_keeps_the_answer(catalog):
    class CheckDown(ScriptedGateway):
        async def evaluate(self, state, questions):
            if "ok" in questions:
                raise GatewayError(503, "Jev is down")
            return await super().evaluate(state, questions)
    res = await Engine(CheckDown(catalog, gate=TEXT), catalog).run("Write an email")
    assert res.status == "unverified" and res.answer == "done"


# ------------------------------------------------------------------ C2: the budget is a ceiling
async def test_many_items_respect_the_budget_and_the_decision_slots(catalog):
    from dataclasses import replace
    # a decider as dear as the review's test (about $0.0026 a call), billed for the text it reads
    dear = replace(catalog, decider=replace(catalog.decider, price_in=13.0))
    items = [{"id": str(i), "text": f"item {i}"} for i in range(1, 51)]
    plan = {"subtasks": [{"id": "t1", "title": "Label", "prompt": "label them", "answer_type": "choice",
                          "decision": {"question": "Which?", "options": {"a": "A", "b": "B"}, "items": items}},
                         {"id": "t2", "title": "Write", "prompt": "write"}]}

    class Counting(ScriptedGateway):
        now = peak = 0

        async def evaluate(self, state, questions):
            Counting.now += 1
            Counting.peak = max(Counting.peak, Counting.now)
            await asyncio.sleep(0.001)
            Counting.now -= 1
            r = await super().evaluate(state, questions)
            tin = len(json.dumps(state) + json.dumps(questions)) // 4
            r.usage = Usage(tin, 0, dear.decider.cost(tin, 0), "gateway")
            return r
    gw = Counting(dear, gate=SPLIT, plan=plan)
    res = await Engine(gw, dear, limits=Limits(max_cost=0.02)).run(LISTED)
    assert res.receipt["total_cost"] <= 0.02 + 1e-9
    assert Counting.peak <= Limits().max_parallel_decisions
    assert res.status in ("partial", "failed") and "budget" in res.error


def test_too_many_items_go_to_a_language_model():
    from taskpenny.planner import clean_decision
    items = [{"id": str(i), "text": f"item {i}"} for i in range(1, 52)]
    assert clean_decision("choice", {"question": "Which?", "options": {"a": "", "b": ""}, "items": items}) is None
    assert clean_decision("choice", {"question": "Which?", "options": {"a": "", "b": ""}, "items": items[:50]})


def test_non_latin_text_is_reserved_at_one_token_per_character():
    assert reserve_tokens("hello world " * 100) == 400
    assert reserve_tokens("中文" * 1000) == 2000
    assert reserve_tokens("Això és una prova") >= len("Això és una prova") // 3


async def test_budget_reserves_the_whole_output_and_shortens_the_last_answer(catalog):
    class Big(ScriptedGateway):
        asked = []

        async def chat(self, model, messages, **kw):
            Big.asked.append(kw["max_tokens"])
            r = await super().chat(model, messages, **kw)
            tin = sum(len(m["content"]) for m in messages) // 4  # what a provider would bill for this input
            r.usage = Usage(tin, kw["max_tokens"], self.catalog.get(model).cost(tin, kw["max_tokens"]), "gateway")
            return r
    gw = Big(catalog, gate={"split": 0.1, "tier": ("4", 0.9), "task_type": "analysis", "answer": "text"})
    res = await Engine(gw, catalog, limits=Limits(max_cost=0.10)).run("A hard question")
    # Opus 5.5 at $20/M out: 8000 tokens would cost $0.16, so the answer is limited to what $0.10 still pays
    assert Big.asked and Big.asked[0] < 8000 and res.receipt["total_cost"] <= 0.10 + 1e-9


async def test_the_budget_message_says_what_the_run_spent(catalog):
    gw = ScriptedGateway(catalog, gate=SPLIT, plan=TWO_PARTS, verify=[0.1] * 10)
    res = await Engine(gw, catalog, limits=Limits(max_cost=0.006)).run(LISTED)
    assert res.status in ("partial", "failed")
    assert f"spent ${res.receipt['total_cost']:.4f}" in res.error


# ------------------------------------------------------------------ I1: Jev answers a request that is a choice
async def test_jev_answers_a_request_that_is_itself_a_choice(catalog):
    gate = {"split": 0.1, "tier": ("1", 0.95), "task_type": "analysis", "answer": "choice", "answer_conf": 0.95}
    request = "Label each message as complaint or praise.\n1) The app crashes.\n2) Love it!"
    gw = ScriptedGateway(catalog, gate=gate, solve={"1) The app crashes.": ("complaint", 0.95),
                                                    "2) Love it!": ("praise", 0.9)})
    gw.extract = json.dumps({"fits": True, "kind": "choice", "question": "Is this message a complaint or praise?",
                             "options": {"complaint": "a problem", "praise": "a compliment"},
                             "labels": {"complaint": "complaint", "praise": "praise"}, "items": [[2, 2], [3, 3]]})
    res = await Engine(gw, catalog).run(request)
    root = res.nodes["root"]
    assert root["kind"] == "jev" and res.status == "done"
    assert res.answer == "- 1) The app crashes.: **complaint**\n- 2) Love it!: **praise**"
    assert gw.chats == ["openai/gpt-6-luna"]  # only the cheap call that wrote the question; no worker


async def test_a_multiple_choice_sum_is_never_given_to_jev(catalog):
    gate = {"split": 0.1, "tier": ("1", 0.9), "task_type": "math", "answer": "choice", "answer_conf": 1.0}
    gw = ScriptedGateway(catalog, gate=gate)
    gw.extract = '{"fits": true, "kind": "choice", "question": "Q", "options": {"a": "", "b": ""}, "items": [[1, 1]]}'
    res = await Engine(gw, catalog).run("Liabilities fell by 25,000 and equity rose by 5,000. Assets? a) -20,000 b) +30,000")
    assert res.nodes["root"]["kind"] == "llm" and gw.chats == [catalog.pick(2).id]  # no extraction call either


async def test_a_request_that_does_not_fit_goes_to_a_model(catalog):
    gate = {"split": 0.1, "tier": ("1", 0.95), "task_type": "analysis", "answer": "choice", "answer_conf": 0.95}
    gw = ScriptedGateway(catalog, gate=gate)  # the extraction answers {"fits": false}
    res = await Engine(gw, catalog).run("Which is better for me, tea or coffee?")
    assert res.nodes["root"]["kind"] == "llm" and res.answer == "done"


def test_extraction_uses_the_request_lines_and_refuses_bad_ranges():
    from taskpenny.decider import parse_extraction
    lines = ["Classify:", "first", "second"]
    ok = parse_extraction('{"fits": true, "kind": "yesno", "question": "Is it spam?", "items": [[2, 2], 3]}', lines)
    assert [it["text"] for it in ok["items"]] == ["first", "second"]
    for bad in ('{"fits": true, "kind": "yesno", "question": "Q", "items": [[2, 9]]}',
                '{"fits": true, "kind": "yesno", "question": "Q", "items": [[2, 3], [3, 3]]}',
                '{"fits": false}', "not json"):
        assert parse_extraction(bad, lines) is None


# ------------------------------------------------------------------ proposal 15 and proposal 7
async def test_two_decisions_on_the_same_items_share_one_call_per_item(catalog):
    items = [{"id": "1", "text": "post one"}, {"id": "2", "text": "post two"}]
    plan = {"subtasks": [
        {"id": "t1", "title": "Kind", "prompt": "kind", "answer_type": "choice",
         "decision": {"question": "Which kind?", "options": {"a": "", "b": ""}, "items": items}},
        {"id": "t2", "title": "Tone", "prompt": "tone", "answer_type": "choice",
         "decision": {"question": "Which tone?", "options": {"a": "", "b": ""}, "items": items}}]}
    gw = ScriptedGateway(catalog, gate=SPLIT, plan=plan)
    res = await Engine(gw, catalog).run(LISTED)
    per_item = [q for q in gw.evals if q and q[0].startswith("q")]
    assert per_item == [["q0", "q1"], ["q0", "q1"]]  # two calls, not four
    assert res.nodes["t1"]["kind"] == res.nodes["t2"]["kind"] == "jev"


async def test_a_single_part_plan_is_done_in_one_go(catalog):
    gw = ScriptedGateway(catalog, gate=SPLIT, plan={"subtasks": [{"id": "t1", "title": "A", "prompt": "do it"}]})
    events = []
    res = await Engine(gw, catalog, on_event=events.append).run(LISTED)
    assert res.nodes["root"]["kind"] == "llm" and set(res.nodes) == {"root"}
    assert any(e["type"] == "plan_failed" and "single part" in e["message"] for e in events)


async def test_the_run_never_has_more_than_twelve_subtasks(catalog):
    plan = {"subtasks": [{"id": f"t{i}", "title": str(i), "prompt": f"do {i}. " * 40} for i in range(1, 8)]}
    gw = ScriptedGateway(catalog, gate=SPLIT, plan=plan, part_gate=SPLIT)
    res = await Engine(gw, catalog, limits=Limits(max_cost=5)).run(LISTED)
    assert len([n for n in res.nodes if n != "root"]) <= 12


# ------------------------------------------------------------------ I9 and I10: odd answers and failing steps
@pytest.mark.parametrize("payload,where", [
    ("<html>proxy error</html>", "chat"), ({"choices": ["x"]}, "chat"),
    ({"choices": [{"message": {"content": "x"}}], "usage": "lots"}, "chat"),
    ({"answers": None}, "evaluate"), ({"answers": ["x"]}, "evaluate"),
])
async def test_malformed_answers_become_gateway_errors(catalog, payload, where):
    def handler(request):
        if isinstance(payload, str):
            return httpx.Response(200, text=payload)
        return httpx.Response(200, json=payload)
    gw = Gateway("k", catalog=catalog, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(GatewayError) as e:
        if where == "chat":
            await gw.chat("openai/gpt-6-luna", [{"role": "user", "content": "x"}], max_tokens=10)
        else:
            await gw.evaluate({"x": 1}, {"ok": {"type": "boolean", "instructions": "?"}})
    assert e.value.status == 502
    await gw.aclose()


async def test_odd_but_readable_jev_answers(catalog):
    def handler(request):
        return httpx.Response(200, json={"answers": {
            "tier": {"type": "choice", "choice": 2, "probabilities": {"2": "0.9", "3": None}},
            "split": {"type": "boolean", "probability": None}}, "providerMetadata": {"typesafe": {"confidence": {
                "tier": None}}}})
    gw = Gateway("k", catalog=catalog, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    r = await gw.evaluate({"x": 1}, {})
    assert r.choice("tier")[0] == "2" and r.boolean("split") is None
    await gw.aclose()


def test_stitch_survives_odd_outlines():
    kids = [Node(id="t1", title="One", prompt="", depth=1, result="first")]
    for outline in ('{"sections": null}', '{"sections": "x"}', '[1, 2]', '{"intro": null, "sections": [5]}', ""):
        assert stitch(outline, kids) == "## One\n\nfirst"


async def test_a_crash_inside_the_run_keeps_what_was_paid(catalog):
    class Crashing(ScriptedGateway):
        async def evaluate(self, state, questions):
            if "ok" in questions:
                raise TypeError("something no one expected")
            return await super().evaluate(state, questions)
    res = await Engine(Crashing(catalog, gate=TEXT), catalog).run("Write an email")
    assert res.status in ("partial", "failed") and "TypeError" in res.error
    assert res.receipt["total_cost"] > 0  # the paid work call is in the receipt


async def test_assembly_failure_falls_back_to_plan_order(catalog):
    class NoAssembly(ScriptedGateway):
        async def chat(self, model, messages, **kw):
            if messages[0]["content"].startswith("You assemble"):
                raise GatewayError(503, "busy")
            return await super().chat(model, messages, **kw)
    res = await Engine(NoAssembly(catalog, gate=SPLIT, plan=TWO_PARTS), catalog).run(LISTED)
    assert res.answer.startswith("## A\n\ndone\n\n## B\n\ndone")
    assert res.status == "done" and any("assembly" in w for w in res.warnings)


async def test_planner_error_does_the_request_in_one_go(catalog):
    class NoPlanner(ScriptedGateway):
        async def chat(self, model, messages, **kw):
            if messages[0]["content"].startswith("You are the planner"):
                raise GatewayError(400, "context too long")
            return await super().chat(model, messages, **kw)
    res = await Engine(NoPlanner(catalog, gate=SPLIT, plan=TWO_PARTS), catalog).run(LISTED)
    assert res.status == "done" and res.nodes["root"]["kind"] == "llm" and res.answer == "done"


async def test_chat_504_is_not_sent_again(catalog):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(504, json={"error": {"message": "gateway timeout"}})
    gw = Gateway("k", catalog=catalog, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(GatewayError):
        await gw.chat("openai/gpt-6-luna", [{"role": "user", "content": "x"}], max_tokens=10)
    assert len(calls) == 1  # it may have been billed
    await gw.aclose()


# ------------------------------------------------------------------ M3: damaged run files
def test_export_of_a_damaged_run_file():
    from taskpenny.runlog import to_markdown
    run = {"id": "x", "status": None, "receipt": {"total_cost": None, "by_role": {"work": None}, "baseline": None},
           "nodes": {"root": {"children": ["a"], "cost": None}, "a": {"children": ["root", "a"], "title": "A|B"}}}
    md = to_markdown(run)
    assert "| a |" in md and md.count("| root |") == 1 and "A/B" in md
    assert to_markdown([1, 2]).startswith("# Taskpenny run")


# ------------------------------------------------------------------ the API
def test_api_says_when_an_answer_is_not_verified_and_refuses_empty_ones(tmp_path, monkeypatch):
    from taskpenny import server
    from taskpenny.simulate import SimulatedGateway

    class Doubtful(SimulatedGateway):
        async def evaluate(self, state, questions):
            r = await super().evaluate(state, questions)
            if "ok" in questions:
                r.answers["ok"]["probability"] = 0.1
            return r
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.setattr(server, "SimulatedGateway", Doubtful)
    httpd, base = _serve(server.App(Catalog.load(), tmp_path, dry_run_default=True))
    try:
        body = {"model": "taskpenny", "messages": [{"role": "user", "content": "Write a haiku about rain."}],
                "temperature": 0.2, "stream": "false"}
        r = httpx.post(base + "/v1/chat/completions", json=body, timeout=60)
        j = r.json()
        assert r.status_code == 200 and r.headers["X-Taskpenny-Status"] == "unverified"
        assert j["taskpenny"]["status"] == "unverified" and j["taskpenny"]["warnings"]
        assert j["taskpenny"]["ignored"] == ["temperature"] and j["object"] == "chat.completion"  # "false" is no stream
        assert httpx.post(base + "/v1/chat/completions", json=body | {"n": 2}).status_code == 400
        empty = httpx.post(base + "/v1/chat/completions", json=body | {"taskpenny": {"max_cost": 1e-9}}, timeout=60)
        assert empty.status_code == 502 and "budget" in empty.json()["error"]["message"]
    finally:
        httpd.shutdown()


def test_idempotency_key_runs_once(tmp_path, monkeypatch):
    from taskpenny import server
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    app = server.App(Catalog.load(), tmp_path, dry_run_default=True)
    httpd, base = _serve(app)
    try:
        body = {"model": "taskpenny", "messages": [{"role": "user", "content": "Say hello."}]}
        h = {"Idempotency-Key": "abc-123"}
        a = httpx.post(base + "/v1/chat/completions", json=body, headers=h, timeout=60).json()
        b = httpx.post(base + "/v1/chat/completions", json=body, headers=h, timeout=60).json()
        assert a["taskpenny"]["run_id"] == b["taskpenny"]["run_id"] and len(list(tmp_path.glob("*.json"))) == 1
    finally:
        httpd.shutdown()


def test_a_client_that_leaves_cancels_its_run(tmp_path, monkeypatch):
    import socket as _socket

    from taskpenny import server
    from taskpenny.simulate import SimulatedGateway

    class Slow(SimulatedGateway):
        def __init__(self, catalog, **kw):
            super().__init__(catalog, latency=(1.5, 1.6))
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.setattr(server, "SimulatedGateway", Slow)
    app = server.App(Catalog.load(), tmp_path, dry_run_default=True)
    httpd, base = _serve(app)
    try:
        port = httpd.server_address[1]
        body = json.dumps({"model": "taskpenny", "messages": [{"role": "user", "content": "Write an email."}]})
        s = _socket.create_connection(("127.0.0.1", port))
        s.sendall(f"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                  f"Content-Length: {len(body)}\r\n\r\n{body}".encode())
        import time
        time.sleep(0.8)
        s.close()  # the client gives up
        for _ in range(60):
            runs = list(app.live.values())
            if runs and runs[0].done:
                break
            time.sleep(0.1)
        assert runs and runs[0].cancelled and runs[0].done
        assert runs[0].result is not None and "cancelled" in runs[0].result["error"]  # saved, and says why
        assert (tmp_path / f"{runs[0].id}.json").exists()
    finally:
        httpd.shutdown()


def test_streams_start_at_once_and_errors_arrive_as_events(tmp_path, monkeypatch):
    from taskpenny import server
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    httpd, base = _serve(server.App(Catalog.load(), tmp_path, dry_run_default=True))
    try:
        body = {"model": "taskpenny", "messages": [{"role": "user", "content": "Hi"}], "stream": True,
                "taskpenny": {"max_cost": 1e-9}}
        with httpx.stream("POST", base + "/v1/chat/completions", json=body, timeout=30) as r:
            assert r.status_code == 200 and r.headers["content-type"] == "text/event-stream"
            text = "".join(r.iter_text())
        assert '"error"' in text and "budget" in text and text.strip().endswith("data: [DONE]")
    finally:
        httpd.shutdown()


def test_wrong_keys_are_throttled_for_the_whole_server(tmp_path, monkeypatch):
    from taskpenny import server
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.setattr(server, "MAX_WRONG_KEYS", 3)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    httpd, base = _serve(server.App(Catalog.load(), tmp_path, dry_run_default=True, api_key="secret"))
    try:
        codes = [httpx.get(base + "/v1/models", headers={"Authorization": f"Bearer nope{i}"}).status_code
                 for i in range(5)]
        assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]
        assert httpx.post(base + "/api/login", json={"key": "secret"}).status_code == 429  # even the right key waits
    finally:
        httpd.shutdown()


def test_sessions_survive_a_restart_and_expire(tmp_path, monkeypatch):
    from taskpenny import server
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    a = server.App(Catalog.load(), tmp_path, dry_run_default=True, api_key="secret")
    token = a.new_session()
    b = server.App(Catalog.load(), tmp_path, dry_run_default=True, api_key="secret")  # the server restarted
    assert b.session_ok(token) and token not in (tmp_path / ".sessions.json").read_text()
    assert ".sessions" not in [r["id"] for r in b.saved_runs()] and b.load_run(".sessions") is None
    b.sessions = {k: 0 for k in b.sessions}  # expired
    assert not b.session_ok(token)


def test_page_fonts_are_served_locally(tmp_path, monkeypatch):
    from taskpenny import server
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    httpd, base = _serve(server.App(Catalog.load(), tmp_path, dry_run_default=True, api_key="secret"))
    try:
        page = httpx.get(base + "/").text
        assert "googleapis" not in page and "fonts/IBMPlexMono-Regular.woff2" in page
        f = httpx.get(base + "/fonts/IBMPlexMono-Regular.woff2")
        assert f.status_code == 200 and f.headers["content-type"] == "font/woff2" and f.content[:4] == b"wOF2"
        import socket as _socket
        port = int(base.rsplit(":", 1)[1])
        for path in ("/fonts/../server.py", "/fonts/%2e%2e/server.py", "/fonts/OFL.txt"):
            with _socket.create_connection(("127.0.0.1", port)) as c:  # sent as is, not normalised by a client
                c.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
                head = c.recv(200).decode(errors="replace")
            assert " 200 " not in head.split("\r\n")[0], path
    finally:
        httpd.shutdown()


def test_run_ids_do_not_collide(tmp_path, monkeypatch):
    from taskpenny import server
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    a = server.App(Catalog.load(), tmp_path, dry_run_default=True)
    b = server.App(Catalog.load(), tmp_path, dry_run_default=True)  # `ui` and `serve` sharing a runs folder
    ids = {a.start("hi", dry_run=True, profile="all", max_cost=0.1, allow_split=False),
           b.start("hi", dry_run=True, profile="all", max_cost=0.1, allow_split=False)}
    assert len(ids) == 2
    for app in (a, b):
        for live in list(app.live.values()):
            app.wait(live.id, timeout=30)


def test_cli_rejects_an_unknown_profile(capsys):
    from taskpenny.cli import main
    assert main(["run", "--dry-run", "--profile", "nope", "hello"]) == 1
    assert "unknown profile" in capsys.readouterr().err


# ------------------------------------------------------------------ second review: 15 confirmed bugs
def test_keyless_api_refuses_instead_of_simulating(tmp_path, monkeypatch):
    from taskpenny import server
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    httpd, base = _serve(server.App(Catalog.load(), tmp_path, dry_run_default=False))
    try:
        body = {"model": "taskpenny", "messages": [{"role": "user", "content": "Hi"}]}
        r = httpx.post(base + "/v1/chat/completions", json=body, timeout=30)
        assert r.status_code == 503 and "AI_GATEWAY_API_KEY" in r.json()["error"]["message"]
        d = httpx.post(base + "/v1/chat/completions", json=body | {"taskpenny": {"dry_run": True}}, timeout=30)
        assert d.status_code == 200 and d.json()["taskpenny"]["simulated"] is True
        assert d.headers["X-Taskpenny-Simulated"] == "true"
    finally:
        httpd.shutdown()


@pytest.mark.parametrize("bad", [-0.05, "NaN", "Infinity", "1e400"])
async def test_impossible_costs_from_the_gateway_are_not_trusted(catalog, bad):
    def handler(request):
        return httpx.Response(200, text=json.dumps({"choices": [{"message": {"content": "hi"}}],
                                                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": bad},
                                                    }).replace('"Infinity"', "Infinity"))
    gw = Gateway("k", catalog=catalog, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    r = await gw.chat("openai/gpt-6-luna", [{"role": "user", "content": "x"}], max_tokens=10)
    assert r.usage.cost == catalog.get("openai/gpt-6-luna").cost(10, 5) and r.usage.cost_source == "catalog"
    await gw.aclose()


def test_token_counts_and_scores_that_cannot_be_numbers():
    from taskpenny.gateway import as_int
    assert as_int(float("inf")) == 0 and as_int("x") == 0 and as_int(-3) == 0 and as_int("12") == 12
    r = EvalResult({"s": {"type": "score", "score": float("inf")}}, {}, Usage(), 1)
    assert r.score("s") == (None, None)


async def test_a_repair_that_fails_keeps_the_best_attempt_and_says_so(catalog):
    class RepairFails(ScriptedGateway):
        async def chat(self, model, messages, **kw):
            if len(self.chats) >= 1 and "previous attempt" in messages[-1]["content"]:
                self.chats.append(model)
                raise GatewayError(400, "refused")
            return await super().chat(model, messages, **kw)
    res = await Engine(RepairFails(catalog, gate=TEXT, verify=[0.05]), catalog).run("Write an email")
    assert res.status == "unverified" and res.answer == "done"
    assert any("repair could not be made" in w for w in res.warnings)


async def test_when_the_fallback_model_fails_jev_unsure_answers_are_flagged(catalog):
    items = [{"id": "1", "text": "first"}, {"id": "2", "text": "second"}]
    plan = {"subtasks": [{"id": "t1", "title": "Label", "prompt": "label", "answer_type": "choice",
                          "decision": {"question": "Which?", "options": {"a": "", "b": ""}, "items": items}},
                         {"id": "t2", "title": "Write", "prompt": "write"}]}

    class NoFallback(ScriptedGateway):
        async def chat(self, model, messages, **kw):
            if "Allowed answers" in messages[-1]["content"]:
                raise GatewayError(503, "busy")
            return await super().chat(model, messages, **kw)
    gw = NoFallback(catalog, gate=SPLIT, plan=plan, solve={"second": ("b", 0.2)})
    res = await Engine(gw, catalog).run(LISTED)
    assert res.nodes["t1"]["status"] == "unverified" and res.status == "unverified"
    assert any("unsure answers were kept" in w for w in res.warnings)


async def test_one_jev_item_that_fails_goes_to_the_fallback(catalog):
    items = [{"id": str(i), "text": f"item {i}"} for i in range(1, 6)]
    plan = {"subtasks": [{"id": "t1", "title": "Label", "prompt": "label", "answer_type": "choice",
                          "decision": {"question": "Which?", "options": {"a": "", "b": ""}, "items": items}},
                         {"id": "t2", "title": "Write", "prompt": "write"}]}

    class ItemFails(ScriptedGateway):
        async def evaluate(self, state, questions):
            if state.get("text") == "item 2":
                raise GatewayError(400, "bad item")
            return await super().evaluate(state, questions)
    res = await Engine(ItemFails(catalog, gate=SPLIT, plan=plan), catalog).run(LISTED)
    assert res.status == "done" and "- 2. item 2: **b**" in res.nodes["t1"]["result"]  # answered by the fallback


async def test_nested_splits_share_twelve_subtasks_even_when_planning_overlaps(catalog):
    kids = {"subtasks": [{"id": f"k{i}", "title": str(i), "prompt": ("do part %d. " % i) * 40} for i in range(1, 4)]}
    five = {"subtasks": [{"id": f"p{i}", "title": str(i), "prompt": f"small {i}"} for i in range(1, 6)]}

    class Slow(ScriptedGateway):
        async def chat(self, model, messages, **kw):
            await asyncio.sleep(0.01)
            if messages[0]["content"].startswith("You are the planner"):
                self.plan = kids if len(self.chats) == 0 else five
            return await super().chat(model, messages, **kw)
    part = {"split": 0.95, "tier": ("2", 0.9), "task_type": "writing", "answer": "text"}
    gw = Slow(catalog, gate=SPLIT, plan=kids, part_gate=part)
    gw.part_gate = part
    res = await Engine(gw, catalog, limits=Limits(max_cost=5)).run(LISTED)
    assert len([n for n in res.nodes if n != "root"]) <= 12


async def test_jev_failing_after_the_question_was_written_falls_back_to_a_model(catalog):
    gate = {"split": 0.1, "tier": ("1", 0.95), "task_type": "analysis", "answer": "choice", "answer_conf": 0.95}

    class JevDown(ScriptedGateway):
        async def evaluate(self, state, questions):
            if "text" in state:
                raise GatewayError(503, "Jev is down")
            return await super().evaluate(state, questions)

        async def chat(self, model, messages, **kw):
            if "Allowed answers" in messages[-1]["content"]:
                raise GatewayError(503, "busy")
            return await super().chat(model, messages, **kw)
    gw = JevDown(catalog, gate=gate)
    gw.extract = '{"fits": true, "kind": "yesno", "question": "Spam?", "items": [[2, 2]]}'
    res = await Engine(gw, catalog).run("Is this spam?\nWIN A PRIZE NOW")
    assert res.nodes["root"]["kind"] == "llm" and res.answer == "done" and res.status == "done"


async def test_a_cancelled_run_stops_its_caller_and_carries_its_result(catalog):
    from taskpenny.engine import RunCancelled

    class Slow(ScriptedGateway):
        async def chat(self, model, messages, **kw):
            await asyncio.sleep(5)
            return await super().chat(model, messages, **kw)
    task = asyncio.ensure_future(Engine(Slow(catalog, gate=TEXT), catalog).run("Write an email"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(RunCancelled) as e:
        await task
    assert e.value.result.status == "failed" and "cancelled" in e.value.result.error
    assert task.cancelled()


async def test_an_unexpected_error_never_leaves_a_task_running(catalog):
    class Broken(ScriptedGateway):
        async def chat(self, model, messages, **kw):
            raise KeyError("surprise")
    res = await Engine(Broken(catalog, gate=TEXT), catalog).run("Write an email")
    assert res.nodes["root"]["status"] == "failed" and "KeyError" in res.error


def test_cli_local_profile_needs_local_models(monkeypatch, capsys):
    from taskpenny.cli import main
    monkeypatch.delenv("TASKPENNY_LOCAL", raising=False)
    assert main(["run", "--dry-run", "--profile", "local", "hello"]) == 1
    assert "unknown profile 'local'" in capsys.readouterr().err


def test_an_idempotency_key_cannot_be_reused_for_another_request(tmp_path, monkeypatch):
    from taskpenny import server
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    httpd, base = _serve(server.App(Catalog.load(), tmp_path, dry_run_default=True))
    try:
        h = {"Idempotency-Key": "k-1"}
        a = {"model": "taskpenny", "messages": [{"role": "user", "content": "Say hello."}]}
        b = {"model": "taskpenny", "messages": [{"role": "user", "content": "Say goodbye."}]}
        assert httpx.post(base + "/v1/chat/completions", json=a, headers=h, timeout=60).status_code == 200
        r = httpx.post(base + "/v1/chat/completions", json=b, headers=h, timeout=60)
        assert r.status_code == 400 and "Idempotency-Key" in r.json()["error"]["message"]
    finally:
        httpd.shutdown()
