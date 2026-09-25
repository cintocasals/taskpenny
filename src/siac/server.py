"""`siac ui`: a local page that shows every run live as a task tree.

Standard library only. Each run executes in its own thread with its own event loop; the page follows it
through Server-Sent Events, and can replay any saved run from the runs folder.
"""

from __future__ import annotations

import asyncio
import json
import os
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
from .gateway import Gateway, GatewayError
from .runlog import save
from .simulate import SimulatedGateway

RUN_ID = re.compile(r"^[\w.-]{1,80}$")


class LiveRun:
    def __init__(self, run_id: str, request: str):
        self.id, self.request = run_id, request
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
    def __init__(self, catalog: Catalog, runs_dir: Path, dry_run_default: bool):
        self.catalog, self.runs_dir = catalog, runs_dir
        self.dry_run_default = dry_run_default
        self.live: dict[str, LiveRun] = {}
        self.has_key = bool(os.environ.get("AI_GATEWAY_API_KEY"))

    def start(self, request: str, *, dry_run: bool, profile: str, max_cost: float, allow_split: bool) -> str:
        run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{len(self.live):03d}"
        live = LiveRun(run_id, request)
        self.live[run_id] = live

        def work():
            async def go():
                gw = SimulatedGateway(self.catalog, latency=(0.2, 0.9)) if dry_run else Gateway(catalog=self.catalog)
                try:
                    eng = Engine(gw, self.catalog, profile=profile, limits=Limits(max_cost=max_cost),
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
                            "baseline": app.catalog.baseline, "decider": app.catalog.decider.id})
            elif u.path == "/api/runs":
                self._json(app.saved_runs())
            elif u.path.startswith("/api/runs/"):
                run = app.load_run(u.path.rsplit("/", 1)[-1])
                self._json(run if run else {"error": "not found"}, HTTPStatus.OK if run else HTTPStatus.NOT_FOUND)
            elif u.path.startswith("/api/events/"):
                self._stream(u.path.rsplit("/", 1)[-1], int((parse_qs(u.query).get("from") or ["0"])[0]))
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self):
            if urlparse(self.path).path != "/api/run":
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
          models: str | None = None, open_browser: bool = True) -> None:
    catalog = Catalog.load(models)
    app = App(catalog, Path(runs_dir), dry_run)
    Path(runs_dir).mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    url = f"http://{host}:{port}/"
    mode = "dry run (no key found)" if not app.has_key else ("dry run by default" if dry_run else "live")
    print(f"SIAC is running at {url}  ·  {mode}  ·  Ctrl+C to stop")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
