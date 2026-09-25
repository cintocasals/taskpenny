"""Command line: `siac run "..."`, `siac demo`, `siac models`."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from . import __version__
from .catalog import Catalog
from .engine import Engine, Limits
from .gateway import Gateway, GatewayError
from .runlog import save
from .simulate import SimulatedGateway

DEMO_PROMPT = ("Prepare the launch of a small online course on AI for bakeries: 1) define two buyer personas, "
               "2) write the landing page headline and three benefits, 3) write a two-email welcome sequence, "
               "4) list five social post ideas, and 5) for each post idea, decide whether it is educational, "
               "promotional or social proof.")


class Printer:
    """Turns engine events into one readable line each."""

    def __init__(self, stream=sys.stderr, color: bool | None = None):
        self.out = stream
        self.color = stream.isatty() if color is None else color
        self.depth: dict[str, int] = {"root": 0}

    def c(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def __call__(self, ev: dict) -> None:
        node = ev.get("node", "")
        if ev["type"] == "node_started":
            self.depth[node] = ev.get("depth", 0)
        pad = "  " * self.depth.get(node, 0)
        t = self.c(f"{ev['t']:6.1f}s", "2")
        tag = self.c(f"{node:<10}", "1")
        cost = f"  {self.c('$%.5f' % ev['cost'], '2')}" if ev.get("cost") else ""
        typ = ev["type"]
        if typ == "run_started":
            line = f"SIAC · profile {ev['profile']} · budget ${ev['limits']['max_cost']:.2f}"
        elif typ == "gate":
            tier = f"tier {ev['tier']}" + (" (raised: Jev unsure)" if ev.get("raised") else "")
            line = f"gate  {tier} · {ev['task_type']} · answer {ev['answer_type']} · split {ev['split']:.2f}"
        elif typ == "plan":
            n = len(ev["subtasks"])
            nj = sum(1 for s in ev["subtasks"] if s["jev"])
            line = f"plan  {n} sub-tasks by {ev['model']}" + (f" ({nj} solved by Jev)" if nj else "")
        elif typ == "route":
            line = f"route tier {ev['tier']} -> {ev['model']}" + (f" (attempt {ev['attempt']})" if ev["attempt"] > 1 else "")
        elif typ == "verify":
            line = f"check {'ok' if ev['ok'] else 'NOT OK'} ({ev['probability']:.2f})"
        elif typ == "repair":
            line = "repair" + (" one tier up" if ev.get("tier_up") else " with feedback")
        elif typ == "jev_solve":
            line = f"jev   solved {ev['items']} item(s)" + (f", {len(ev['unsure'])} unsure" if ev["unsure"] else "")
        elif typ == "fallback":
            line = f"fallback {ev['items']} unsure item(s) -> {ev['model']}"
        elif typ == "model_fallback":
            line = self.c(f"model {ev['model']} refused ({ev['status']}) -> {ev['next']}", "33")
        elif typ == "aggregate":
            line = f"assemble with {ev['model']}"
        elif typ == "final_check":
            line = f"final check {'ok' if ev['ok'] else 'NOT OK'} ({ev['probability']:.2f})"
        elif typ == "node_failed":
            line = self.c(f"failed: {ev['message']}", "31")
        elif typ in ("budget_exceeded", "error"):
            line = self.c(ev["message"], "31")
        else:
            return  # llm_call, node_started, node_done, run_done: shown elsewhere
        print(f"{t} {pad}{tag} {line}{cost}", file=self.out, flush=True)


def print_receipt(result, stream=sys.stdout) -> None:
    r = result.receipt
    print("\n" + "-" * 60, file=stream)
    print(f"Cost receipt · run {result.id} · {result.status} · {result.duration_s:.1f}s", file=stream)
    for role, v in r["by_role"].items():
        print(f"  {role:<12} ${v:.5f}", file=stream)
    print(f"  {'TOTAL':<12} ${r['total_cost']:.5f}  ({r['calls']} calls, {r['tokens_in']} tokens in, "
          f"{r['tokens_out']} out)", file=stream)
    b = r["baseline"]
    print(f"  Same request with {b['model']} alone: about ${b['estimated_cost']:.5f}", file=stream)
    s = r.get("saving_pct")
    if s is not None and s >= 0:
        print(f"  Saving: {s:.0f}%  (estimate; see the run file for the method)", file=stream)
    elif s is not None:
        print(f"  This run cost {1 - s / 100:.1f} times the estimate for the baseline alone "
              "(planning did not pay off for a request this size)", file=stream)


def _read_prompt(args) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.prompt == "-" or (not args.prompt and not sys.stdin.isatty()):
        return sys.stdin.read()
    if not args.prompt:
        sys.exit("siac: give a prompt, a --file, or pipe text in")
    return args.prompt


async def _run(args, prompt: str) -> int:
    catalog = Catalog.load(args.models)
    gw = SimulatedGateway(catalog) if args.dry_run else Gateway(catalog=catalog)
    printer = None if args.quiet or args.json else Printer()
    engine = Engine(gw, catalog, profile=args.profile, on_event=printer,
                    limits=Limits(max_cost=args.max_cost, max_depth=args.max_depth),
                    allow_split=not args.no_split)
    try:
        result = await engine.run(prompt)
    finally:
        await gw.aclose()
    path = save(result, args.save_dir) if args.save_dir else None
    if args.json:
        from dataclasses import asdict
        print(json.dumps(asdict(result), ensure_ascii=False, indent=1))
    else:
        if args.dry_run:
            print("\n(dry run: simulated answers and decisions, real flow and catalog prices)", file=sys.stderr)
        print("\n" + result.answer)
        print_receipt(result)
        if path:
            print(f"  Run saved to {path}")
        if result.error:
            print(f"  Note: {result.error}")
    return 0 if result.status == "done" else 2


def _models(args) -> int:
    catalog = Catalog.load(args.models)
    print(f"Decider: {catalog.decider.id} (${catalog.decider.price_in}/M in, output free)")
    print(f"Planner: {catalog.planner}   Baseline: {catalog.baseline}\n")
    print(f"{'model':<34} {'tiers':<8} {'$/M in':>8} {'$/M out':>8}  per typical call")
    for m in catalog.models:
        print(f"{m.id:<34} {','.join(map(str, m.tiers)):<8} {m.price_in:>8.2f} {m.price_out:>8.2f}  ${m.typical_cost:.5f}")
    print("\nCheapest per tier (profile all): " + ", ".join(
        f"{t}: {catalog.pick(t).id}" for t in sorted(catalog.tiers)))
    if args.check:
        import httpx
        live = {m["id"]: m for m in httpx.get("https://ai-gateway.vercel.sh/v1/models", timeout=20).json()["data"]}
        print("\nLive check against Vercel AI Gateway:")
        bad = 0
        for m in [catalog.decider] + catalog.models:
            lm = live.get(m.id)
            if not lm:
                print(f"  MISSING  {m.id}")
                bad += 1
                continue
            p = lm.get("pricing") or {}
            li, lo = float(p.get("input") or 0) * 1e6, float(p.get("output") or 0) * 1e6
            if abs(li - m.price_in) > 1e-6 or abs(lo - m.price_out) > 1e-6:
                print(f"  CHANGED  {m.id}: now ${li:g} in / ${lo:g} out")
                bad += 1
        print("  All prices match." if not bad else f"  {bad} difference(s): update models.yaml.")
        return 1 if bad else 0
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="siac", description="Split a big prompt into small tasks and send each one "
                                "to the cheapest model that can do it well.")
    p.add_argument("--version", action="version", version=f"siac {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def run_opts(sp):
        sp.add_argument("--profile", default="all", help="which providers may be used (see models.yaml)")
        sp.add_argument("--max-cost", type=float, default=0.50, help="budget per run in USD (default 0.50)")
        sp.add_argument("--max-depth", type=int, default=3)
        sp.add_argument("--no-split", action="store_true", help="route the whole request to one model")
        sp.add_argument("--models", help="path to a models.yaml of your own")
        sp.add_argument("--save-dir", default=os.environ.get("SIAC_RUNS_DIR", "runs"), help="where runs are saved")
        sp.add_argument("--json", action="store_true", help="print the whole run as JSON")
        sp.add_argument("--quiet", action="store_true", help="only the answer and the receipt")

    r = sub.add_parser("run", help="run a request")
    r.add_argument("prompt", nargs="?", help="the request, or - to read it from stdin")
    r.add_argument("--file", help="read the request from a file")
    r.add_argument("--dry-run", action="store_true", help="simulated models: no key, no cost")
    run_opts(r)
    d = sub.add_parser("demo", help="see SIAC work on a sample request, without a key")
    run_opts(d)
    u = sub.add_parser("ui", help="open the live task tree in your browser")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--host", default="127.0.0.1")
    u.add_argument("--dry-run", action="store_true", help="simulated models by default")
    u.add_argument("--no-browser", action="store_true")
    u.add_argument("--models", help="path to a models.yaml of your own")
    u.add_argument("--save-dir", default=os.environ.get("SIAC_RUNS_DIR", "runs"))
    m = sub.add_parser("models", help="show the model catalog")
    m.add_argument("--models", help="path to a models.yaml of your own")
    m.add_argument("--check", action="store_true", help="compare prices with the live Vercel catalog")

    args = p.parse_args(argv)
    if args.cmd == "models":
        return _models(args)
    if args.cmd == "ui":
        from .server import serve
        serve(args.host, args.port, runs_dir=args.save_dir, dry_run=args.dry_run, models=args.models,
              open_browser=not args.no_browser)
        return 0
    if args.cmd == "demo":
        args.dry_run, args.file, prompt = True, None, DEMO_PROMPT
    else:
        prompt = _read_prompt(args)
    try:
        return asyncio.run(_run(args, prompt))
    except GatewayError as e:
        print(f"siac: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
