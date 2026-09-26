"""`taskpenny demo`: replay a real saved run in the terminal. No key, no cost, nothing simulated.

The run ships with the package (demo-run.json): a real request, answered on 25 September 2026 through Vercel AI
Gateway, with every event, model, cost and answer as they happened. The replay shows the same steps at 4x.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

DEMO_RUN = Path(__file__).with_name("demo-run.json")
MAX_WAIT = 1.5  # seconds: long model calls are shortened so the replay keeps moving
RESULTS_URL = "https://github.com/cintocasals/taskpenny/blob/main/bench/public/RESULTS.md"


def load_demo() -> dict:
    return json.loads(DEMO_RUN.read_text(encoding="utf-8"))


def _first_line(text: str, width: int = 88) -> str:
    for line in (text or "").splitlines():
        line = line.replace("**", "").strip().strip("#*>_ ").strip()
        if line:
            return line if len(line) <= width else line[: width - 1] + "…"
    return ""


def replay(run: dict, printer=None, speed: float = 4.0, instant: bool = False, sleep=time.sleep) -> None:
    """Feed the saved events to `printer` with their original rhythm, `speed` times faster."""
    events = run.get("events") or []
    for i, ev in enumerate(events):
        if printer:
            printer(ev)
        if instant or i + 1 >= len(events):
            continue
        wait = (events[i + 1]["t"] - ev["t"]) / max(speed, 0.1)
        if wait > 0:
            sleep(min(wait, MAX_WAIT))


def summary(run: dict, full: bool = False, out=None) -> None:
    out = out or sys.stdout
    nodes = run.get("nodes") or {}
    root = nodes.get("root") or {}
    print("\nWho did what", file=out)
    for nid in root.get("children") or []:
        n = nodes[nid]
        who = "JEV" if n.get("kind") == "jev" else (n.get("model") or "").split("/")[-1]
        print(f"  {nid:<4} tier {n.get('tier')}  {who:<24} ${n.get('cost', 0):.5f}  {n.get('title', '')}", file=out)
        first = _first_line(n.get("result", ""))
        if first:
            print(f"       {first}", file=out)
    if root.get("model"):
        print(f"  assembly by {root['model'].split('/')[-1]}", file=out)

    answer = run.get("answer") or ""
    lines = answer.splitlines()
    print("\nAnswer" + ("" if full else " (first lines)"), file=out)
    shown = lines if full else lines[:12]
    print("\n".join(shown), file=out)
    if not full and len(lines) > len(shown):
        print(f"… {len(lines) - len(shown)} more lines. `taskpenny demo --full` shows the whole answer.", file=out)

    r = run.get("receipt") or {}
    print("\n" + "-" * 60, file=out)
    print(f"Cost receipt · real, as billed by the gateway · {run.get('duration_s', 0):.1f}s", file=out)
    for role, v in (r.get("by_role") or {}).items():
        print(f"  {role:<12} ${v:.5f}", file=out)
    print(f"  {'TOTAL':<12} ${r.get('total_cost', 0):.5f}  ({r.get('calls', 0)} calls, {r.get('tokens_in', 0)} tokens in, "
          f"{r.get('tokens_out', 0)} out)", file=out)
    b = r.get("baseline") or {}
    if b.get("estimated_cost"):
        print(f"  One strong model ({b['model']}) for the same request: about ${b['estimated_cost']:.5f}. "
              "This one is an estimate,", file=out)
        print(f"  not a measurement. Measured results against one model: {RESULTS_URL}", file=out)
    if (r.get("by_role") or {}).get("decisions") == 0:
        # every input token not spent by a language model call went to Jev or the planner: an upper bound
        llm_in = sum(e.get("tokens_in", 0) for e in run.get("events") or [] if e.get("type") == "llm_call")
        from .catalog import Catalog
        price = Catalog.load().decider.price_in
        most = max(0, r.get("tokens_in", 0) - llm_in) * price / 1e6
        print(f"  Jev's decisions were free on Vercel AI Gateway that day. At its list price (${price:g} per million",
              file=out)
        print(f"  input tokens; output is free) they would have added ${most:.4f} at most.", file=out)


def main(speed: float = 4.0, instant: bool = False, full: bool = False, quiet: bool = False) -> int:
    from .cli import Printer

    run = load_demo()
    pace = "" if instant or quiet else f" at {speed:g}x"
    print(f"Replaying a real Taskpenny run from {run.get('started_at', '')[:10]}{pace}: no key, no cost.\n"
          f"Request: {run.get('request', '')}\n", file=sys.stderr)
    replay(run, None if quiet else Printer(), speed=speed, instant=instant or quiet)
    summary(run, full=full)
    print("\nYour own requests: set a key (see README), then `taskpenny run \"...\"` or `taskpenny ui`.")
    return 0
