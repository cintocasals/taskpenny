"""`siac ui` and `siac serve`: a local page that shows every run live as a task tree, and an
OpenAI-compatible endpoint (/v1/chat/completions) so any tool can send its prompts through SIAC.

Standard library only. Each run executes in its own thread with its own event loop; the page follows it
through Server-Sent Events, and can replay any saved run from the runs folder. Runs that arrive through the
API show up on the page while they work.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import threading
import time
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .catalog import Catalog
from .engine import Engine, Limits
from . import openai_api as oai
from .gateway import GatewayError
from .providers import connect, has_any_key
from .runlog import save
from .simulate import SimulatedGateway

RUN_ID = re.compile(r"^[\w.-]{1,80}$")


class LiveRun:
    def __init__(self, run_id: str, request: str, source: str = "page"):
        self.id, self.request, self.source = run_id, request, source
        self.started = time.time()
        self.events: list[dict] = []
        self.done = False
        self.result: dict | None = None
        self.cond = threading.Condition()

    def push(self, ev: dict) -> None:
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()

    def finish(self, result: dict | None) -> None:
        with self.cond:
            self.result, self.done = result, True
            self.cond.notify_all()


class App:
    def __init__(self, catalog: Catalog, runs_dir: Path, dry_run_default: bool, api_key: str | None = None):
        self.catalog, self.runs_dir = catalog, runs_dir
        self.dry_run_default = dry_run_default
        self.live: dict[str, LiveRun] = {}
        self.has_key = has_any_key()
        self.api_key = api_key or None
        self.decider_label = ""
        if self.has_key:
            try:
                _, cat = connect(catalog)
                self.decider_label = cat.decider_label or cat.decider.id
            except GatewayError:
                pass
        self._lock = threading.Lock()
        self._count = 0

    def start(self, request: str, *, dry_run: bool, profile: str, max_cost: float, allow_split: bool,
              source: str = "page") -> str:
        with self._lock:
            self._count += 1
            run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{self._count:03d}"
        live = LiveRun(run_id, request, source)
        self.live[run_id] = live

        def work():
            async def go():
                gw, cat = ((SimulatedGateway(self.catalog, latency=(0.2, 0.9)), self.catalog) if dry_run
                           else connect(self.catalog))
                try:
                    eng = Engine(gw, cat, profile=profile, limits=Limits(max_cost=max_cost),
                                 on_event=live.push, allow_split=allow_split)
                    res = await eng.run(request)
                    res.id = run_id
                    save(res, self.runs_dir)
                    return asdict(res)
                finally:
                    await gw.aclose()
            try:
                result = asyncio.run(go())
            except GatewayError as e:
                live.push({"t": 0, "type": "error", "node": "root", "message": str(e)})
                result = None
            live.finish(result)

        threading.Thread(target=work, name=f"run-{run_id}", daemon=True).start()
        return run_id

    def wait(self, run_id: str, timeout: float) -> LiveRun:
        live = self.live[run_id]
        with live.cond:
            live.cond.wait_for(lambda: live.done, timeout=timeout)
        return live

    def running(self) -> list[dict]:
        return [{"id": r.id, "request": r.request[:160], "source": r.source, "started": r.started}
                for r in list(self.live.values()) if not r.done]

    def saved_runs(self) -> list[dict]:
        out = []
        for p in sorted(self.runs_dir.glob("*.json"), reverse=True)[:50]:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"id": d.get("id", p.stem), "request": d.get("request", "")[:160],
                        "status": d.get("status"), "cost": d.get("receipt", {}).get("total_cost"),
                        "saving_pct": d.get("receipt", {}).get("saving_pct"), "started_at": d.get("started_at")})
        return out

    def load_run(self, run_id: str) -> dict | None:
        if not RUN_ID.match(run_id):
            return None
        p = self.runs_dir / f"{run_id}.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))


def make_handler(app: App):
    page = resources.files("siac").joinpath("web/index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "siac"

        def log_message(self, *args):  # quiet
            pass

        def _json(self, obj, status=HTTPStatus.OK):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            elif u.path == "/api/info":
                self._json({"has_key": app.has_key, "dry_run_default": app.dry_run_default or not app.has_key,
                            "profiles": list(app.catalog.profiles) or ["all"],
                            "baseline": app.catalog.baseline, "decider": app.catalog.decider.id,
                            "decider_label": app.decider_label})
            elif u.path == "/api/runs":
                self._json(app.saved_runs())
            elif u.path == "/api/live":
                self._json(app.running())
            elif u.path in ("/v1/models", "/models"):
                if self._api_auth():
                    self._json(oai.model_list(list(app.catalog.profiles) or ["all"]))
            elif u.path.startswith("/api/runs/"):
                run = app.load_run(u.path.rsplit("/", 1)[-1])
                self._json(run if run else {"error": "not found"}, HTTPStatus.OK if run else HTTPStatus.NOT_FOUND)
            elif u.path.startswith("/api/export/"):
                from .runlog import to_markdown
                run = app.load_run(u.path.rsplit("/", 1)[-1])
                if not run:
                    return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                body = to_markdown(run).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="siac-{run.get("id", "run")}.md"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path.startswith("/api/events/"):
                self._stream(u.path.rsplit("/", 1)[-1], int((parse_qs(u.query).get("from") or ["0"])[0]))
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def _api_auth(self) -> bool:
            if not app.api_key:
                return True
            got = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            if hmac.compare_digest(got.encode(), app.api_key.encode()):
                return True
            self._json(oai.error("wrong or missing API key for this SIAC server", "authentication_error"),
                       HTTPStatus.UNAUTHORIZED)
            return False

        def _chat(self):
            if not self._api_auth():
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 4_000_000)) or b"{}")
                if not isinstance(body, dict):
                    raise oai.BadRequest("the body must be a JSON object")
                if body.get("tools") or body.get("functions"):
                    raise oai.BadRequest("SIAC does not support tool calling yet: send plain messages")
                profiles = list(app.catalog.profiles) or ["all"]
                model = str(body.get("model") or "siac")
                profile = oai.profile_from_model(model, profiles)
                request = oai.request_from_messages(body.get("messages"))
                opts = body.get("siac") if isinstance(body.get("siac"), dict) else {}
                max_cost = float(opts.get("max_cost") or self.headers.get("X-SIAC-Max-Cost") or 0.5)
            except (ValueError, TypeError) as e:
                return self._json(oai.error(str(e)), HTTPStatus.BAD_REQUEST)
            dry = bool(opts.get("dry_run")) or not app.has_key or app.dry_run_default
            run_id = app.start(request, dry_run=dry, profile=profile, max_cost=max_cost,
                               allow_split=not opts.get("no_split"), source="api")
            live = app.wait(run_id, timeout=900)
            if not live.done:
                return self._json(oai.error("SIAC is still working on this request after 15 minutes", "timeout"),
                                  HTTPStatus.GATEWAY_TIMEOUT)
            if live.result is None:
                msg = next((e.get("message") for e in reversed(live.events) if e.get("type") == "error"),
                           "the run failed")
                return self._json(oai.error(str(msg), "upstream_error"), HTTPStatus.BAD_GATEWAY)
            run = live.result
            if body.get("stream"):
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-SIAC-Run-Id", run_id)
                self.end_headers()
                for chunk in oai.stream_chunks(run, model):
                    self.wfile.write(chunk)
                self.wfile.flush()
                return
            self._json(oai.completion(run, model))

        def do_POST(self):
            path = urlparse(self.path).path
            if path in ("/v1/chat/completions", "/chat/completions"):
                return self._chat()
            if path != "/api/run":
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 2_000_000)) or b"{}")
            except ValueError:
                return self._json({"error": "bad json"}, HTTPStatus.BAD_REQUEST)
            request = str(body.get("request") or "").strip()
            if not request:
                return self._json({"error": "empty request"}, HTTPStatus.BAD_REQUEST)
            dry = bool(body.get("dry_run")) or not app.has_key
            run_id = app.start(request, dry_run=dry, profile=str(body.get("profile") or "all"),
                               max_cost=float(body.get("max_cost") or 0.5), allow_split=not body.get("no_split"))
            self._json({"id": run_id, "dry_run": dry})

        def _stream(self, run_id: str, start: int):
            live = app.live.get(run_id)
            if not live:
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            i = max(0, start)
            try:
                while True:
                    with live.cond:
                        while i >= len(live.events) and not live.done:
                            live.cond.wait(timeout=15)
                            if i >= len(live.events) and not live.done:
                                self.wfile.write(b": ping\n\n")
                                self.wfile.flush()
                        batch = live.events[i:]
                        i += len(batch)
                        finished = live.done and i >= len(live.events)
                    for ev in batch:
                        self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                    if finished:
                        self.wfile.write(f"event: result\ndata: {json.dumps(live.result, ensure_ascii=False)}\n\n"
                                         .encode("utf-8"))
                        self.wfile.flush()
                        return
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, *, runs_dir: str = "runs", dry_run: bool = False,
          models: str | None = None, open_browser: bool = True, api_key: str | None = None) -> None:
    catalog = Catalog.load(models)
    app = App(catalog, Path(runs_dir), dry_run, api_key=api_key)
    Path(runs_dir).mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    url = f"http://{host}:{port}/"
    mode = "dry run (no key found)" if not app.has_key else ("dry run by default" if dry_run else "live")
    print(f"SIAC is running at {url}  ·  {mode}  ·  Ctrl+C to stop")
    print(f"OpenAI-compatible API: base URL {url}v1  ·  model \"siac\""
          + ("  ·  needs the API key you set" if api_key else ""))
    if host not in ("127.0.0.1", "localhost", "::1") and not api_key:
        print("Warning: this server is reachable from other machines and has no API key. "
              "Anyone who can reach it can spend your credit. Set --api-key or SIAC_API_KEY.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
