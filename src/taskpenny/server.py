"""`taskpenny ui` and `taskpenny serve`: a local page that shows every run live as a task tree, and an
OpenAI-compatible endpoint (/v1/chat/completions, /v1/responses) so any tool can send its prompts through Taskpenny.

Standard library only. Each run executes in its own thread with its own event loop; the page follows it
through Server-Sent Events, and can replay any saved run from the runs folder. Runs that arrive through the
API show up on the page while they work.

Real runs need AI_GATEWAY_API_KEY (Jev decides everything, through Vercel); without it every run is simulated.

With an API key set (TASKPENNY_API_KEY), everything except the page, its fonts, /health, /api/login and
/api/logout needs it: API clients send it as a Bearer token, and the page signs in once and gets a session cookie
that lasts 30 days (kept, hashed, in the runs folder, so a restart does not sign anyone out). Wrong keys are
throttled for the whole server, not per connection. POST bodies must be JSON, which keeps other websites from
starting runs through a visitor's browser.

API clients: a stream gets its headers and keep-alive comments at once; a client that goes away cancels its run,
unless it sent an Idempotency-Key, in which case the run goes on and the same key gets its answer later. Without
AI_GATEWAY_API_KEY the API refuses real requests (503) instead of answering with simulated text, unless the server
was started with --dry-run or the request asks for a dry run; simulated answers say so (`taskpenny.simulated`).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import selectors
import socket
import threading
import time
import webbrowser
from collections import deque
from dataclasses import asdict
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import openai_api as oai
from .catalog import Catalog
from .demo import load_demo
from .engine import Engine, Limits, RunCancelled
from .gateway import GatewayError
from .providers import connect, has_jev_key
from .runlog import save
from .simulate import SimulatedGateway

RUN_ID = re.compile(r"^[\w-][\w.-]{0,79}$")  # never a hidden file such as .sessions.json
FONT = re.compile(r"^IBMPlex(Sans|Mono)-(Regular|Medium|SemiBold)\.woff2$")
COOKIE = "taskpenny_session"
SESSION_DAYS = 30
MAX_BODY = 4_000_000
KEEP_FINISHED_S = 600    # finished runs stay followable this long, then only their saved file remains
KEEP_IDEMPOTENCY_S = 86_400
API_WAIT_S = 900         # an API call waits this long for its run
KEEPALIVE_S = 10
MAX_WRONG_KEYS = 20      # wrong keys per minute for the whole server; past this, key checks wait
SOCKET_TIMEOUT_S = 120   # a client that stops sending in the middle of a request is dropped


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


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LiveRun:
    def __init__(self, run_id: str, request: str, source: str = "page"):
        self.id, self.request, self.source = run_id, request, source
        self.started = time.time()
        self.finished_at: float | None = None
        self.events: list[dict] = []
        self.done = False
        self.result: dict | None = None
        self.cond = threading.Condition()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task | None = None
        self.cancelled = False

    def push(self, ev: dict) -> None:
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()

    def finish(self, result: dict | None) -> None:
        with self.cond:
            self.result, self.done, self.finished_at = result, True, time.time()
            self.cond.notify_all()

    def cancel(self) -> None:
        """Stop the run (its client went away). What was already answered is kept in the run file."""
        self.cancelled = True
        loop, task = self.loop, self.task
        if loop and task and not self.done:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:  # the loop just closed: the run is over anyway
                pass


class App:
    def __init__(self, catalog: Catalog, runs_dir: Path, dry_run_default: bool, api_key: str | None = None,
                 max_cost_cap: float | None = None, force_dry: bool = False, bind_host: str = "127.0.0.1"):
        self.catalog, self.runs_dir = catalog, Path(runs_dir)
        self.dry_run_default = dry_run_default or force_dry
        self.force_dry = force_dry  # `serve --dry-run`: no request may spend
        self.bind_host = bind_host
        self.allowed_hosts = {h.strip().lower() for h in os.environ.get("TASKPENNY_ALLOWED_HOSTS", "").split(",")
                              if h.strip()}
        self.live: dict[str, LiveRun] = {}
        self.has_key = has_jev_key()  # real runs need Jev, and Jev is reached through Vercel
        self.api_key = api_key or None
        cap = max_cost_cap if max_cost_cap is not None else float(os.environ.get("TASKPENNY_MAX_COST", "2") or 2)
        self.max_cost_cap = cap if math.isfinite(cap) and cap > 0 else 2.0
        self.profiles = list(catalog.profiles) or ["all"]
        if os.environ.get("TASKPENNY_LOCAL", "").strip() and "local" not in self.profiles:
            self.profiles.append("local")
        self._lock = threading.Lock()
        self._idem: dict[str, tuple[str, float, str]] = {}  # key -> run id, time, hash of the request
        self._wrong: deque[float] = deque()
        self._sessions_file = self.runs_dir / ".sessions.json"
        self.sessions: dict[str, float] = self._load_sessions()  # sha256(token) -> expiry time

    # ------------------------------------------------------------ sessions
    def _load_sessions(self) -> dict[str, float]:
        try:
            data = json.loads(self._sessions_file.read_text(encoding="utf-8"))
            now = time.time()
            return {str(k): float(v) for k, v in data.items() if float(v) > now} if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _save_sessions(self) -> None:
        try:
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._sessions_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.sessions), encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self._sessions_file)
        except OSError:
            pass  # sessions still work until the server restarts

    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = time.time()
            self.sessions = {k: v for k, v in self.sessions.items() if v > now}
            self.sessions[_hash(token)] = now + SESSION_DAYS * 86_400
            self._save_sessions()
        return token

    def session_ok(self, token: str) -> bool:
        exp = self.sessions.get(_hash(token)) if token else None
        return bool(exp) and exp > time.time()

    def end_session(self, token: str) -> None:
        with self._lock:
            if self.sessions.pop(_hash(token), None) is not None:
                self._save_sessions()

    # ------------------------------------------------------ wrong-key throttle
    def throttled(self) -> bool:
        with self._lock:
            now = time.time()
            while self._wrong and now - self._wrong[0] > 60:
                self._wrong.popleft()
            return len(self._wrong) >= MAX_WRONG_KEYS

    def wrong_key(self) -> None:
        with self._lock:
            self._wrong.append(time.time())

    # ---------------------------------------------------------------- inputs
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
        if profile != "all" and profile not in self.profiles:
            raise BadInput(f"unknown profile {profile!r}; known: {', '.join(self.profiles)}")
        return profile

    # ------------------------------------------------------------------ runs
    def start(self, request: str, *, dry_run: bool, profile: str, max_cost: float, allow_split: bool,
              source: str = "page", idempotency_key: str = "") -> str:
        """Start a run in its own thread, or, for an idempotency key already seen with the same request, return that
        run's id. Keys are kept in memory for a day: after a restart, the same key starts a new run."""
        fingerprint = hashlib.sha256(json.dumps([request, dry_run, profile, max_cost, allow_split]).encode()).hexdigest()
        with self._lock:
            now = time.time()
            if idempotency_key:
                self._idem = {k: v for k, v in self._idem.items() if now - v[1] < KEEP_IDEMPOTENCY_S}
                known = self._idem.get(idempotency_key)
                if known:
                    if known[2] != fingerprint:
                        raise BadInput("this Idempotency-Key was already used for a different request")
                    return known[0]
            run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
            for k in [k for k, r in self.live.items() if r.done and now - (r.finished_at or now) > KEEP_FINISHED_S]:
                del self.live[k]
            live = LiveRun(run_id, request, source)
            self.live[run_id] = live
            if idempotency_key:
                self._idem[idempotency_key] = (run_id, now, fingerprint)

        def work():
            async def go():
                live.loop, live.task = asyncio.get_running_loop(), asyncio.current_task()
                gw, cat = ((SimulatedGateway(self.catalog, latency=(0.2, 0.9)), self.catalog) if dry_run
                           else connect(self.catalog))
                try:
                    eng = Engine(gw, cat, profile=profile, limits=Limits(max_cost=max_cost),
                                 on_event=live.push, allow_split=allow_split)
                    try:
                        res = await eng.run(request)
                    except RunCancelled as c:  # what was done before the client left is still saved
                        res = c.result
                    live.task = None  # from here on a late cancel must not lose the finished run
                finally:
                    try:
                        await gw.aclose()
                    except (Exception, asyncio.CancelledError):  # noqa: BLE001 - closing never loses a run
                        pass
                res.id = run_id
                try:
                    save(res, self.runs_dir)
                except OSError as e:  # the answer is paid for: it still reaches the client
                    msg = f"the run could not be saved ({type(e).__name__}: {e})"[:300]
                    res.warnings.append(msg)
                    live.push({"t": res.duration_s, "type": "warning", "node": "root", "message": msg})
                return asdict(res)
            result = None
            try:
                result = asyncio.run(go())
            except (Exception, asyncio.CancelledError) as e:  # noqa: BLE001 - the run must end for its watchers
                live.push({"t": 0, "type": "error", "node": "root", "message": f"{type(e).__name__}: {e}"[:500]})
            finally:
                live.finish(result)

        threading.Thread(target=work, name=f"run-{run_id}", daemon=True).start()
        return run_id

    def wait(self, run_id: str, timeout: float) -> LiveRun | None:
        live = self.live.get(run_id)
        if live is None:
            return None
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
            if not RUN_ID.match(p.stem):
                continue
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                continue
            if isinstance(d, dict):
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
        except (OSError, ValueError, RecursionError):
            return None
        return d if isinstance(d, dict) else None


def make_handler(app: App):
    web = resources.files("taskpenny").joinpath("web")
    page = web.joinpath("index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "taskpenny"
        timeout = SOCKET_TIMEOUT_S

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
        def _cookie(self) -> str:
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie") or "")
            except Exception:  # noqa: BLE001 - a malformed cookie header is just no cookie
                return ""
            return jar[COOKIE].value if COOKIE in jar else ""

        def _authorized(self) -> bool | None:
            """True, False, or None when wrong keys are being throttled (the key is not even compared)."""
            if not app.api_key:
                return True
            if app.session_ok(self._cookie()):
                return True
            bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            if not bearer:
                return False
            if app.throttled():
                return None
            if hmac.compare_digest(bearer.encode(), app.api_key.encode()):
                return True
            app.wrong_key()
            time.sleep(0.5)
            return False

        def _host_ok(self) -> bool:
            if app.api_key or host_allowed(self.headers.get("Host"), app.bind_host, app.allowed_hosts):
                return True
            self._json({"error": "this Taskpenny server has no key, so it only answers to localhost; set "
                                 "TASKPENNY_API_KEY, or list this name in TASKPENNY_ALLOWED_HOSTS"},
                       HTTPStatus.FORBIDDEN)
            return False

        def _require_auth(self, api_style: bool = False) -> bool:
            ok = self._authorized()
            if ok:
                return True
            if ok is None:
                msg = "too many wrong keys for this server in the last minute: try again in a minute"
                self._json(oai.error(msg, "rate_limit_error") if api_style else {"error": msg},
                           HTTPStatus.TOO_MANY_REQUESTS, headers={"Retry-After": "60"})
                return False
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
            except (ValueError, RecursionError):
                raise BadInput("the body is not valid JSON, or is nested too deeply") from None
            if not isinstance(body, dict):
                raise BadInput("the body must be a JSON object")
            return body

        def _secure(self) -> bool:
            return (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

        def _client_gone(self) -> bool:
            """True when the client closed its connection (it will never read the answer). A check that cannot be
            made is not a disconnection."""
            try:
                with selectors.DefaultSelector() as sel:  # poll/epoll: no limit on file descriptor numbers
                    sel.register(self.connection, selectors.EVENT_READ)
                    if not sel.select(timeout=0):
                        return False
                return self.connection.recv(1, socket.MSG_PEEK) == b""
            except BlockingIOError:
                return False
            except (ConnectionResetError, BrokenPipeError):
                return True
            except (OSError, ValueError):
                return False

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
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(page)
                return
            if u.path.startswith("/fonts/") and FONT.match(u.path[7:]):  # the page's own fonts: public, like the page
                data = web.joinpath("fonts", u.path[7:]).read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "font/woff2")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=604800, immutable")
                self.end_headers()
                self.wfile.write(data)
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
                            "decider": app.catalog.decider.id, "max_cost_cap": app.max_cost_cap,
                            "login": bool(app.api_key)})
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
            if app.throttled():
                return self._json({"error": "too many wrong keys in the last minute: try again in a minute"},
                                  HTTPStatus.TOO_MANY_REQUESTS, headers={"Retry-After": "60"})
            key = str(body.get("key") or "")
            if not hmac.compare_digest(key.encode(), app.api_key.encode()):
                app.wrong_key()
                time.sleep(1.0)  # slow down guessing (and every guess counts towards the server-wide limit)
                return self._json({"error": "wrong key"}, HTTPStatus.UNAUTHORIZED)
            token = app.new_session()
            flags = (f"; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_DAYS * 86_400}"
                     + ("; Secure" if self._secure() else ""))
            self._json({"ok": True}, headers={"Set-Cookie": f"{COOKIE}={token}{flags}"})

        def _logout(self):
            token = self._cookie()
            if token:
                app.end_session(token)  # the token stops working everywhere, not just here
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
                ignored = oai.ignored_params(body)
            except oai.BadRequest as e:
                raise BadInput(str(e)) from None
            opts = body.get("taskpenny") if isinstance(body.get("taskpenny"), dict) else {}
            max_cost = app.budget(opts["max_cost"] if "max_cost" in opts else self.headers.get("X-Taskpenny-Max-Cost"))
            dry = bool(opts.get("dry_run")) or app.dry_run_default
            if not dry and not app.has_key:  # never pass simulated text off as a real answer
                return self._json(oai.error("this Taskpenny server has no AI_GATEWAY_API_KEY, so it cannot run real "
                                            "requests: Taskpenny needs Jev, reached through Vercel AI Gateway. Set the "
                                            "key, or ask for a simulated run with \"taskpenny\": {\"dry_run\": true}",
                                            "service_unavailable"), HTTPStatus.SERVICE_UNAVAILABLE)
            idem = (self.headers.get("Idempotency-Key") or "").strip()[:200]
            run_id = app.start(request, dry_run=dry, profile=profile, max_cost=max_cost,
                               allow_split=not opts.get("no_split"), source="api", idempotency_key=idem)
            stream = oai.wants_stream(body)
            if stream:  # headers now, so the client sees the call is alive; the answer comes at the end
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("X-Taskpenny-Run-Id", run_id)
                self.end_headers()
                self.wfile.flush()
            run = self._wait_for(run_id, stream, cancel=not idem, responses=responses)
            if run is None:
                return  # the client went away, or the wait timed out and was answered
            if isinstance(run, str):  # the run ended without a usable answer
                return self._fail(run, "upstream_error", HTTPStatus.BAD_GATEWAY, stream, responses)
            headers = {"X-Taskpenny-Run-Id": run_id, "X-Taskpenny-Status": str(run.get("status"))}
            if (run.get("receipt") or {}).get("simulated"):
                headers["X-Taskpenny-Simulated"] = "true"
            if ignored:
                headers["X-Taskpenny-Ignored"] = ",".join(ignored)
            if stream:
                chunks = (oai.response_events(run, model, ignored) if responses
                          else oai.stream_chunks(run, model, ignored))
                try:
                    for chunk in chunks:
                        self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                return
            self._json(oai.response(run, model, ignored) if responses else oai.completion(run, model, ignored),
                       headers=headers)

        def _wait_for(self, run_id: str, stream: bool, cancel: bool, responses: bool = False):
            """The finished run as a dict; an error message (str) if it ended with no answer; None if the client
            left (the run is cancelled unless `cancel` is False) or the wait timed out (already answered)."""
            deadline, last_ping = time.time() + API_WAIT_S, time.time()
            while True:
                live = app.wait(run_id, timeout=2)
                if live is None:  # finished long ago (an idempotent retry): its file has the answer
                    run = app.load_run(run_id)
                    break
                if live.done:
                    run = live.result
                    if run is None:
                        return next((str(e.get("message")) for e in reversed(live.events) if e.get("type") == "error"),
                                    "the run failed")
                    break
                gone = False
                if stream and time.time() - last_ping >= KEEPALIVE_S:
                    try:
                        self.wfile.write(b": taskpenny is working\n\n")
                        self.wfile.flush()
                        last_ping = time.time()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        gone = True
                elif not stream:
                    gone = self._client_gone()
                if gone:
                    if cancel:
                        live.cancel()
                    return None
                if time.time() > deadline:
                    self._fail(f"Taskpenny is still working on this request after {API_WAIT_S // 60} minutes; "
                               f"its run id is {run_id}", "timeout", HTTPStatus.GATEWAY_TIMEOUT, stream, responses)
                    return None
            if not isinstance(run, dict):
                return "the run could not be found"
            if run.get("status") == "failed" or not str(run.get("answer") or "").strip():
                # nothing usable came back: say so, never send an empty answer as a success
                return str(run.get("error") or "the run produced no answer")
            return run

        def _fail(self, message: str, kind: str, status: HTTPStatus, stream: bool, responses: bool):
            if not stream:
                return self._json(oai.error(message, kind), status)
            try:
                for chunk in oai.stream_error(message, kind, responses):
                    self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

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
            except (BrokenPipeError, ConnectionResetError, OSError):
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
    mode = ("dry run: no AI_GATEWAY_API_KEY, and Taskpenny needs Jev, which is reached through Vercel"
            if not app.has_key else "dry run for every request" if force_dry
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
