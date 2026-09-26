#!/usr/bin/env python3
"""Build docs/demo/index.html: the live page in replay mode, with the real run from `taskpenny demo` inside it.

No server, no key, no cost: the page answers its own API calls from the embedded run. GitHub Pages can serve the
docs/ folder, so the demo is at <user>.github.io/taskpenny/demo/. Rebuild after changing the page or the run:

    python docs/demo/build.py
"""

from __future__ import annotations

import json
import sys
from importlib import resources
from pathlib import Path

OUT = Path(__file__).with_name("index.html")
DESCRIPTION = ("A real Taskpenny run, replayed in your browser: one request split into five tasks, each done by the "
               "cheapest model that can do it well, every result checked, and the receipt. No key, no cost.")

SHIM = """<script>
/* Static demo: the page's API calls are answered from the run below; nothing leaves the browser. */
(() => {
  const RUN = __RUN__;
  const entry = {id: RUN.id, request: RUN.request.slice(0, 160), status: RUN.status, cost: RUN.receipt.total_cost,
                 saving_pct: RUN.receipt.saving_pct, started_at: RUN.started_at, example: true};
  const reply = (body, status = 200) => Promise.resolve(new Response(JSON.stringify(body),
    {status, headers: {"Content-Type": "application/json"}}));
  const real = window.fetch.bind(window);
  window.fetch = (url, opts) => {
    const path = new URL(String(url), location.href).pathname;
    if (path.endsWith("/api/info")) return reply({static: true, demo_id: RUN.id, has_key: false, dry_run_default: true,
                                                  profiles: ["all"], login: false});
    if (path.endsWith("/api/runs")) return reply([entry]);
    if (path.endsWith("/api/live")) return reply([]);
    if (path.includes("/api/runs/")) return reply(RUN);
    if (path.includes("/api/")) return reply({error: "This page only replays a saved run."}, 400);
    return real(url, opts);
  };
})();
</script>
"""


def build() -> str:
    pkg = resources.files("taskpenny")
    page = pkg.joinpath("web/index.html").read_text(encoding="utf-8")
    run = json.loads(pkg.joinpath("demo-run.json").read_text(encoding="utf-8"))
    data = json.dumps(run, ensure_ascii=False).replace("<", "\\u003c")  # no tag can open or close inside the script
    assert page.count("<script>") == 1, "the page is expected to have one inline script"
    page = page.replace("<script>", SHIM.replace("__RUN__", data) + "<script>", 1)
    page = page.replace("<title>Taskpenny</title>",
                        '<title>Taskpenny · a real run, replayed</title>\n'
                        f'<meta name="description" content="{DESCRIPTION}">', 1)
    return page


if __name__ == "__main__":
    html = build()
    OUT.write_text(html, encoding="utf-8")
    print(f"{OUT} ({len(html) // 1024} KB)", file=sys.stderr)
