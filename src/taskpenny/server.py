"""`taskpenny ui` and `taskpenny serve`: a local page that shows every run live as a task tree, and an
OpenAI-compatible endpoint (/v1/chat/completions) so any tool can send its prompts through Taskpenny.

Standard library only. Each run executes in its own thread with its own event loop; the page follows it
through Server-Sent Events, and can replay any saved run from the runs folder. Runs that arrive through the
API show up on the page while they work.

With an API key set, everything except the page itself needs it: API clients send it as a Bearer token, and
the page signs in once and gets a session cookie. POST bodies must be JSON, which keeps other websites from
starting runs through a visitor's browser.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import openai_api as oai
from .demo import load_demo
from .catalog import Catalog
from .engine import Engine, Limits
from .gateway import GatewayError
from .providers import connect, has_any_key
from .runlog import save
from .simulate import SimulatedGateway

RUN_ID = re.compile(r"^[\w.-]{1,80}$")
COOKIE = "taskpenny_session"
MAX_BODY = 4_000_000
KEEP_FINISHED_S = 600  # finished runs stay followable this long, then only their saved file remains


class BadInput(ValueError):
    pass


def host_allowed(host_header: str | None, bind_host: str, extra: set[str]) -> bool:
    """Without a key, only names that cannot be a stranger's website may reach the server. This stops DNS
    rebinding: a web page whose domain is made to point at 127.0.0.1 still sends its own domain as Host."""
    host = (host_header or "").strip().lower()
    if host.startswith("["):  # [::1]:8765
        name = host[1:].split("]", 1)[0]
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if not name or name in ("localhost", "127.0.0.1", "::1", bind_host.lower()) or name in extra:
        return True
    if ":" in name or re.fullmatch(r"[\d.]+", name):  # an IP address typed directly, not a domain
        return True
    return "." not in name  # a one-word name such as a Docker service ("taskpenny"), never a public domain


class LiveRun:
    def __init__(self, run_id: str, request: str, source: str = "page"):
        self.id, self.request, self.source = run_id, request, source
        self.started = time.time()
        self.finished_at: float | None = None
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
            self.result, self.done, self.finished_at = result, True, time.time()
            self.cond.notify_all()


class App:
    def __init__(self, catalog: Catalog, runs_dir: Path, dry_run_default: bool, api_key: str | None = None,
                 max_cost_cap: float | None = None, force_dry: bool = False, bind_host: str = "127.0.0.1"):
        self.catalog, self.runs_dir = catalog, runs_dir
        self.dry_run_default = dry_run_default or force_dry
        self.force_dry = force_dry  # `serve --dry-run`: no request may spend
        self.bind_host = bind_host
        self.allowed_hosts = {h.strip().lower() for h in os.environ.get("TASKPENNY_ALLOWED_HOSTS", "").split(",")
                              if h.strip()}
        self.sessions: set[str] = set()  # one random token per sign-in; signing out forgets it
        self.live: dict[str, LiveRun] = {}
        self.has_key = has_any_key()
        self.api_key = api_key or None
        cap = max_cost_cap if max_cost_cap is not None else float(os.environ.get("TASKPENNY_MAX_COST", "2") or 2)
        self.max_cost_cap = cap if math.isfinite(cap) and cap > 0 else 2.0
        self.profiles = list(catalog.profiles) or ["all"]
        self.decider_label = ""
        if self.has_key:
            try:
                _, cat = connect(catalog)
                self.decider_label = cat.decider_label or cat.decider.id
            except GatewayError:
                pass
        self._lock = threading.Lock()
        self._count = 0

    def budget(self, value) -> float:
        """A per-run budget from a request: a finite number above zero, never above the server's cap."""
        if value in (None, ""):
            return min(0.5, self.max_cost_cap)
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise BadInput("max_cost must be a number of USD") from None
        if not math.isfinite(v) or v <= 0:
            raise BadInput("max_cost must be a positive number of USD")
        return min(v, self.max_cost_cap)

    def check_profile(self, profile: str) -> str:
        if profile != "all" and profile not in self.catalog.profiles:
            raise BadInput(f"unknown profile {profile!r}; known: {', '.join(self.profiles)}")
        return profile

    def start(self, request: str, *, dry_run: bool, profile: str, max_cost: float, allow_split: bool,
              source: str = "page") -> str:
        with self._lock:
            self._count += 1
            run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{self._count:03d}"
            now = time.time()
            for k in [k for k, r in self.live.items() if r.done and now - (r.finished_at or now) > KEEP_FINISHED_S]:
                del self.live[k]
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
            result = None
            try:
                result = asyncio.run(go())
            except Exception as e:  # noqa: BLE001 - whatever happens, the run must end for its watchers
                live.push({"t": 0, "type": "error", "node": "root", "message": f"{type(e).__name__}: {e}"[:500]})
            finally:
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
        def entry(run_id: str, d: dict) -> dict:
            r = d.get("receipt") if isinstance(d.get("receipt"), dict) else {}
            return {"id": run_id, "request": str(d.get("request") or "")[:160], "status": d.get("status"),
                    "cost": r.get("total_cost"), "saving_pct": r.get("saving_pct"), "started_at": d.get("started_at")}
        out = []
        for p in sorted(self.runs_dir.glob("*.json"), reverse=True)[:50]:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(d, dict) and RUN_ID.match(p.stem):
                out.append(entry(p.stem, d))  # the file name is the id that load_run opens
        demo = load_demo()  # the real run shipped with the package, so there is always something to replay
        if not (self.runs_dir / f"{demo['id']}.json").exists():
            out.append(entry(demo["id"], demo) | {"example": True})
        return out

    def load_run(self, run_id: str) -> dict | None:
        if not RUN_ID.match(run_id):
            return None
        p = self.runs_dir / f"{run_id}.json"
        if not p.exists():
            demo = load_demo()
            return demo if run_id == demo["id"] else None
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return d if isinstance(d, dict) else None


def make_handler(app: App):
    page = resources.files("taskpenny").joinpath("web/index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "taskpenny"

        def log_message(self, *args):  # quiet
            pass

        def _json(self, obj, status=HTTPStatus.OK, headers: dict | None = None):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        # --------------------------------------------------------------- access
        def _authorized(self) -> bool:
            if not app.api_key:
                return True
            bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            if bearer and hmac.compare_digest(bearer.encode(), app.api_key.encode()):
                return True
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie") or "")
            except Exception:  # noqa: BLE001 - a malformed cookie header is just no cookie
                return False
            got = jar[COOKIE].value if COOKIE in jar else ""
            return bool(got) and any(hmac.compare_digest(got.encode(), s.encode()) for s in list(app.sessions))

        def _host_ok(self) -> bool:
            if app.api_key or host_allowed(self.headers.get("Host"), app.bind_host, app.allowed_hosts):
                return True
            self._json({"error": "this Taskpenny server has no key, so it only answers to localhost; set "
                                 "TASKPENNY_API_KEY, or list this name in TASKPENNY_ALLOWED_HOSTS"},
                       HTTPStatus.FORBIDDEN)
            return False

        def _require_auth(self, api_style: bool = False) -> bool:
            if self._authorized():
                return True
            msg = "wrong or missing API key for this Taskpenny server"
            self._json(oai.error(msg, "authentication_error") if api_style else {"error": "login required"},
                       HTTPStatus.UNAUTHORIZED)
            return False

        def _body(self) -> dict:
            """The JSON object sent with a POST. Anything else is refused before a run can start."""
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise BadInput("send the body as JSON (Content-Type: application/json)")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise BadInput("bad Content-Length") from None
            if n < 0 or n > MAX_BODY:
                raise BadInput("the body is too large" if n > 0 else "bad Content-Length")
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                raise BadInput("the body is not valid JSON") from None
            if not isinstance(body, dict):
                raise BadInput("the body must be a JSON object")
            return body

        def _secure(self) -> bool:
            return (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

        # ------------------------------------------------------------------ GET
        def do_GET(self):
            u = urlparse(self.path)
            if u.path != "/health" and not self._host_ok():
                return
            if u.path in ("/", "/index.html"):
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(page)
                return
            if u.path == "/health":  # for container health checks: says nothing about runs or keys
                return self._json({"ok": True})
            if u.path in ("/v1/models", "/models"):
                if self._require_auth(api_style=True):
                    self._json(oai.model_list(app.profiles))
                return
            if not u.path.startswith("/api/"):
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            if not self._require_auth():
                return
            if u.path == "/api/info":
                self._json({"has_key": app.has_key, "dry_run_default": app.dry_run_default or not app.has_key,
                            "force_dry": app.force_dry or not app.has_key,
                            "profiles": app.profiles, "baseline": app.catalog.baseline,
                            "decider": app.catalog.decider.id, "decider_label": app.decider_label,
                            "max_cost_cap": app.max_cost_cap, "login": bool(app.api_key)})
            elif u.path == "/api/runs":
                self._json(app.saved_runs())
            elif u.path == "/api/live":
                self._json(app.running())
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
                run_id = u.path.rsplit("/", 1)[-1]  # already checked by load_run: letters, digits, dot, dash
                self.send_header("Content-Disposition", f'attachment; filename="taskpenny-{run_id}.md"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path.startswith("/api/events/"):
                try:
                    start = int((parse_qs(u.query).get("from") or ["0"])[0])
                except ValueError:
                    start = 0
                self._stream(u.path.rsplit("/", 1)[-1], start)
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        # ----------------------------------------------------------------- POST
        def do_POST(self):
            path = urlparse(self.path).path
            if not self._host_ok():
                return
            try:
                if path in ("/v1/chat/completions", "/chat/completions"):
                    return self._chat()
                if path in ("/v1/responses", "/responses"):
                    return self._chat(responses=True)
                if path == "/api/login":
                    return self._login()
                if path == "/api/logout":
                    return self._logout()
                if path == "/api/run":
                    return self._run()
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except BadInput as e:
                api = path.endswith("/chat/completions") or path.endswith("/responses")
                return self._json(oai.error(str(e)) if api else {"error": str(e)}, HTTPStatus.BAD_REQUEST)

        def _login(self):
            body = self._body()
            if not app.api_key:
                return self._json({"ok": True})
            key = str(body.get("key") or "")
            if not hmac.compare_digest(key.encode(), app.api_key.encode()):
                time.sleep(1.0)  # slow down guessing
                return self._json({"error": "wrong key"}, HTTPStatus.UNAUTHORIZED)
            token = secrets.token_urlsafe(32)
            app.sessions.add(token)
            flags = "; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000" + ("; Secure" if self._secure() else "")
            self._json({"ok": True}, headers={"Set-Cookie": f"{COOKIE}={token}{flags}"})

        def _logout(self):
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie") or "")
            except Exception:  # noqa: BLE001
                pass
            if COOKIE in jar:
                app.sessions.discard(jar[COOKIE].value)  # the token stops working everywhere, not just here
            self._json({"ok": True}, headers={"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0"})

        def _run(self):
            if not self._require_auth():
                return
            body = self._body()
            request = str(body.get("request") or "").strip()
            if not request:
                raise BadInput("empty request")
            profile = app.check_profile(str(body.get("profile") or "all"))
            max_cost = app.budget(body.get("max_cost"))
            dry = bool(body.get("dry_run")) or not app.has_key or app.force_dry
            run_id = app.start(request, dry_run=dry, profile=profile, max_cost=max_cost,
                               allow_split=not body.get("no_split"))
            self._json({"id": run_id, "dry_run": dry, "max_cost": max_cost})

        def _chat(self, responses: bool = False):
            """Chat Completions, or the Responses API with responses=True: same run, different wrapping."""
            if not self._require_auth(api_style=True):
                return
            body = self._body()
            if body.get("tools") or body.get("functions"):
                raise BadInput("Taskpenny does not support tool calling yet: send plain messages")
            fmt = body.get("response_format") or ((body.get("text") or {}).get("format")
                                                  if isinstance(body.get("text"), dict) else None)
            if isinstance(fmt, dict) and fmt.get("type") not in (None, "text"):
                raise BadInput("Taskpenny does not support JSON mode (response_format) yet: ask for JSON in the "
                               "message instead")
            model = str(body.get("model") or "taskpenny")
            try:
                profile = oai.profile_from_model(model, app.profiles)
                request = (oai.request_from_responses(body) if responses
                           else oai.request_from_messages(body.get("messages")))
            except oai.BadRequest as e:
                raise BadInput(str(e)) from None
            opts = body.get("taskpenny") if isinstance(body.get("taskpenny"), dict) else {}
            max_cost = app.budget(opts["max_cost"] if "max_cost" in opts else self.headers.get("X-Taskpenny-Max-Cost"))
            dry = bool(opts.get("dry_run")) or not app.has_key or app.dry_run_default
            run_id = app.start(request, dry_run=dry, profile=profile, max_cost=max_cost,
                               allow_split=not opts.get("no_split"), source="api")
            live = app.wait(run_id, timeout=900)
            if not live.done:
                return self._json(oai.error("Taskpenny is still working on this request after 15 minutes", "timeout"),
                                  HTTPStatus.GATEWAY_TIMEOUT)
            if live.result is None:
                msg = next((e.get("message") for e in reversed(live.events) if e.get("type") == "error"),
                           "the run failed")
                return self._json(oai.error(str(msg), "upstream_error"), HTTPStatus.BAD_GATEWAY)
            run = live.result
            if run.get("status") == "failed":  # nothing usable came back: say so, do not send an empty answer
                return self._json(oai.error(str(run.get("error") or "the run failed"), "upstream_error"),
                                  HTTPStatus.BAD_GATEWAY)
            if body.get("stream"):
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Taskpenny-Run-Id", run_id)
                self.end_headers()
                for chunk in (oai.response_events(run, model) if responses else oai.stream_chunks(run, model)):
                    self.wfile.write(chunk)
                self.wfile.flush()
                return
            self._json(oai.response(run, model) if responses else oai.completion(run, model))

        # ------------------------------------------------------------- streaming
        def _stream(self, run_id: str, start: int):
            live = app.live.get(run_id)
            if not live:
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")  # nginx: pass events through as they come
            self.end_headers()
            i = max(0, start)
            try:
                while True:
                    with live.cond:  # only wait under the lock; writing to the client happens outside it
                        if i >= len(live.events) and not live.done:
                            live.cond.wait(timeout=15)
                        batch = live.events[i:]
                        i += len(batch)
                        finished = live.done and i >= len(live.events)
                        result = live.result
                    if not batch and not finished:
                        self.wfile.write(b": ping\n\n")
                    for ev in batch:
                        self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                    if finished:
                        self.wfile.write(f"event: result\ndata: {json.dumps(result, ensure_ascii=False)}\n\n"
                                         .encode("utf-8"))
                        self.wfile.flush()
                        return
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, *, runs_dir: str = "runs", dry_run: bool = False,
          models: str | None = None, open_browser: bool = True, api_key: str | None = None,
          force_dry: bool = False) -> None:
    catalog = Catalog.load(models)
    app = App(catalog, Path(runs_dir), dry_run, api_key=api_key, force_dry=force_dry, bind_host=host)
    Path(runs_dir).mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    url = f"http://{host}:{port}/"
    mode = ("dry run (no key found)" if not app.has_key else "dry run for every request" if force_dry
            else "dry run by default" if dry_run else "live")
    print(f"Taskpenny is running at {url}  ·  {mode}  ·  Ctrl+C to stop")
    print(f"OpenAI-compatible API: base URL {url}v1  ·  model \"taskpenny\""
          + ("  ·  the page and the API need the key you set" if api_key else ""))
    print(f"Budget per run: at most ${app.max_cost_cap:g} (TASKPENNY_MAX_COST)")
    if host not in ("127.0.0.1", "localhost", "::1") and not api_key:
        print("Warning: this server is reachable from other machines and has no API key. "
              "Anyone who can reach it can see your runs and spend your credit. Set --api-key or TASKPENNY_API_KEY.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
