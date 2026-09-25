#!/usr/bin/env python3
"""Run the development cases through Taskpenny and compare Jev's gate with the expected answers.

Usage: python bench/run_dev.py [--dry-run] [--only en01,ca02] [--parallel 2] [--max-cost 0.10] [--total-budget 0.50]
Each case is appended to bench/results/dev-<tag>-<time>.jsonl as soon as it ends; a Markdown summary follows.
The whole run stops starting new cases once --total-budget is spent.
"""
import argparse
import asyncio
import json
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from taskpenny.catalog import Catalog
from taskpenny.engine import Engine, Limits
from taskpenny.simulate import SimulatedGateway

HERE = Path(__file__).parent


async def one(case, catalog, gw, args):
    eng = Engine(gw, catalog, limits=Limits(max_cost=args.max_cost))
    t0 = time.perf_counter()
    res = await eng.run(case["prompt"])
    root = res.nodes["root"]
    g = root.get("gate") or {}
    exp = case["expect"]
    tier_used = g.get("tier")
    return {
        "id": case["id"], "lang": case["lang"], "expect": exp,
        "gate": g, "kind": root["kind"], "status": res.status,
        "split_ok": (root["kind"] == "split") == exp["split"],
        "answer_ok": g.get("answer_type") == exp["answer"],
        "tier_raw": g.get("tier_raw"), "tier_used": tier_used,
        "below": (tier_used or 0) < exp["tier"] and root["kind"] != "split",
        "subtasks": len(root["children"]),
        "jev_solved": sum(1 for n in res.nodes.values() if n["kind"] == "jev"),
        "models": sorted({n["model"] for n in res.nodes.values() if n.get("model")}),
        "cost": res.receipt["total_cost"], "baseline": res.receipt["baseline"]["estimated_cost"],
        "saving_pct": res.receipt["saving_pct"], "seconds": round(time.perf_counter() - t0, 1),
        "answer_chars": len(res.answer), "error": res.error,
        "run": asdict(res),
    }


def summary(rows):
    n = len(rows)
    lines = [f"# Dev cases · {datetime.now():%Y-%m-%d %H:%M}", "",
             f"Cases: {n} · total cost ${sum(r['cost'] for r in rows):.4f} · "
             f"baseline estimate ${sum(r['baseline'] for r in rows):.4f}", ""]
    def pct(k, rs):
        return f"{sum(1 for r in rs if r[k])}/{len(rs)}"
    lines += ["| Group | Split decision right | Answer type right | Below expected tier | Cost | Baseline |",
              "|---|---|---|---|---|---|"]
    for lang in ("all", "en", "ca", "es"):
        rs = rows if lang == "all" else [r for r in rows if r["lang"] == lang]
        if not rs:
            continue
        lines.append(f"| {lang} | {pct('split_ok', rs)} | {pct('answer_ok', rs)} | {pct('below', rs)} | "
                     f"${sum(r['cost'] for r in rs):.4f} | ${sum(r['baseline'] for r in rs):.4f} |")
    lines += ["", "| Case | Split exp/got | Answer exp/got | Tier exp/raw/used | Sub-tasks | Jev solved | Cost | s | Status |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        e, g = r["expect"], r["gate"]
        lines.append(f"| {r['id']} | {'Y' if e['split'] else 'N'}/{'Y' if r['kind'] == 'split' else 'N'} | "
                     f"{e['answer']}/{g.get('answer_type')} | {e['tier']}/{r['tier_raw']}/{r['tier_used']} | "
                     f"{r['subtasks']} | {r['jev_solved']} | ${r['cost']:.4f} | {r['seconds']} | {r['status']} |")
    return "\n".join(lines) + "\n"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--parallel", type=int, default=2)
    ap.add_argument("--max-cost", type=float, default=0.10, help="budget per case, USD")
    ap.add_argument("--total-budget", type=float, default=0.50, help="stop starting cases after this, USD")
    args = ap.parse_args()
    cases = [json.loads(l) for l in (HERE / "dev_cases.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.only:
        keep = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in keep]
    catalog = Catalog.load()
    if args.dry_run:
        gw = SimulatedGateway(catalog, latency=(0, 0.01))
    else:
        from taskpenny.providers import connect
        gw, catalog = connect(catalog)
    sem = asyncio.Semaphore(args.parallel)
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    tag = "dry" if args.dry_run else "live"
    jl = out / f"dev-{tag}-{stamp}.jsonl"
    spent = {"usd": 0.0, "reserved": 0.0}

    async def guarded(c):
        async with sem:
            # wait while cases in flight may still spend; skip only when nothing is in flight and it does not fit
            while spent["usd"] + spent["reserved"] + args.max_cost > args.total_budget:
                if spent["reserved"] <= 0:
                    print(f"{c['id']}: skipped (total budget ${args.total_budget:.2f} reached)", flush=True)
                    return None
                await asyncio.sleep(1)
            spent["reserved"] += args.max_cost
            try:
                r = await one(c, catalog, gw, args)
            finally:
                spent["reserved"] -= args.max_cost
            spent["usd"] += r["cost"]
            with jl.open("a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            print(f"{r['id']}: {r['kind']:<5} tier {r['tier_used']} cost ${r['cost']:.4f} {r['seconds']}s {r['status']}"
                  f"  (total ${spent['usd']:.3f})", flush=True)
            return r
    rows = [r for r in await asyncio.gather(*(guarded(c) for c in cases)) if r]
    await gw.aclose()
    md = summary(rows)
    (out / f"dev-{tag}-{stamp}.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    asyncio.run(main())
