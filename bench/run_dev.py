#!/usr/bin/env python3
"""Run the development cases through SIAC and compare Jev's gate with the expected answers.

Usage: python bench/run_dev.py [--dry-run] [--only en01,ca02] [--parallel 3] [--max-cost 0.3]
Writes bench/results/dev-<time>.json and a Markdown summary next to it.
"""
import argparse
import asyncio
import json
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from siac.catalog import Catalog
from siac.engine import Engine, Limits
from siac.gateway import Gateway
from siac.simulate import SimulatedGateway

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
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--max-cost", type=float, default=0.30)
    args = ap.parse_args()
    cases = [json.loads(l) for l in (HERE / "dev_cases.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.only:
        keep = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in keep]
    catalog = Catalog.load()
    gw = SimulatedGateway(catalog, latency=(0, 0.01)) if args.dry_run else Gateway(catalog=catalog)
    sem = asyncio.Semaphore(args.parallel)

    async def guarded(c):
        async with sem:
            r = await one(c, catalog, gw, args)
            print(f"{r['id']}: {r['kind']:<5} tier {r['tier_used']} cost ${r['cost']:.4f} {r['seconds']}s {r['status']}",
                  flush=True)
            return r
    rows = await asyncio.gather(*(guarded(c) for c in cases))
    await gw.aclose()
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    tag = "dry" if args.dry_run else "live"
    (out / f"dev-{tag}-{stamp}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    md = summary(rows)
    (out / f"dev-{tag}-{stamp}.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    asyncio.run(main())
