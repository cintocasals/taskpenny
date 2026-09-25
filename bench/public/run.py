#!/usr/bin/env python3
"""Public benchmark: SIAC against one strong model, on the same tasks, with real costs from the gateway.

For every task in tasks.jsonl:
  1. SIAC answers it (Jev decides, cheap models work, Jev checks).
  2. The baseline model answers it in one call, with its default settings.
  3. Quality: classification tasks are scored against their true labels; every other task is judged pairwise
     by a third model from another provider, twice with the answers swapped (a verdict counts only if both
     orders agree, otherwise it is a tie). With MT-Bench's reference answer when there is one.

Usage:
  python bench/public/run.py --dry-run --limit 5                 (no key, no cost: checks the pipeline)
  python bench/public/run.py --sets mtbench,classify --total-budget 3
  python bench/public/run.py --resume results/public-live-20260925-1500.jsonl
Each task is appended to bench/results/public-<tag>-<time>.jsonl as soon as it ends; a Markdown report follows.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from siac.catalog import Catalog
from siac.engine import Engine, Limits
from siac.gateway import GatewayError
from siac.simulate import SimulatedGateway

HERE = Path(__file__).parent
RESULTS = HERE.parent / "results"

JUDGE_SYSTEM = (
    "Please act as an impartial judge and evaluate the quality of the responses provided by two AI assistants "
    "to the user request displayed below. Choose the assistant that follows the user's instructions and answers "
    "the request better. Consider helpfulness, correctness, relevance, depth and whether every part of the "
    "request was done, in the format asked for. Avoid any position bias: the order in which the responses are "
    "shown must not influence you. Do not let the length of the responses influence you, and do not favour "
    "any names or styles. Be as objective as possible.\n"
    "Explain your reasoning in at most three sentences, then give your final verdict on the last line, "
    "strictly as [[A]] if assistant A is better, [[B]] if assistant B is better, or [[C]] for a tie."
)
JUDGE_REF = (
    "\nA reference answer is provided. Compare both responses with it, identify any mistakes, and prefer the "
    "response that is correct."
)
# Adapted from the pairwise judge prompt of MT-Bench (lm-sys/FastChat, Apache 2.0).


def judge_user(task: dict, a: str, b: str) -> str:
    parts = [f"[User request]\n{task['prompt']}\n"]
    if task.get("reference"):
        parts.append(f"[Reference answer]\n{task['reference']}\n")
    parts.append(f"[The start of assistant A's answer]\n{a}\n[The end of assistant A's answer]\n")
    parts.append(f"[The start of assistant B's answer]\n{b}\n[The end of assistant B's answer]")
    return "\n".join(parts)


VERDICT = re.compile(r"\[\[([ABC])\]\]")


def parse_verdict(text: str) -> str | None:
    found = VERDICT.findall(text or "")
    return found[-1] if found else None


LINE = re.compile(r"^\W*(\d{1,2})[*_]*\s*[.:)\-]\s*(.*)$")


def score_labels(answer: str, gold: dict[str, str], options: list[str]) -> dict:
    """Accuracy against the true labels. `strict` says whether the answer used exactly the format asked for."""
    opts = sorted(options, key=len, reverse=True)
    got: dict[str, str] = {}
    strict_lines = 0
    for raw in (answer or "").splitlines():
        m = LINE.match(raw.strip())
        if not m or m.group(1) not in gold or m.group(1) in got:
            continue
        rest = m.group(2)
        tail = rest.split("->")[-1] if "->" in rest else rest.split(":")[-1] if ":" in rest else rest
        tail = re.sub(r"[*_`\"'.]", " ", tail).lower()
        tail = " ".join(tail.split())
        label = next((o for o in opts if o == tail), None) or next((o for o in opts if o in tail), None)
        if label:
            got[m.group(1)] = label
            if re.fullmatch(r"\d{1,2}\s*:\s*" + re.escape(label) + r"\.?", raw.strip(), re.I):
                strict_lines += 1
    right = sum(1 for k, v in gold.items() if got.get(k) == v)
    return {"correct": right, "total": len(gold), "accuracy": right / len(gold), "answered": len(got),
            "strict": strict_lines == len(gold)}


CORE = {"hard": 5, "multi": 5, "classify": 5, "ca": 3, "es": 2}


def core_set(tasks: list[dict], seed: int) -> list[dict]:
    """A smaller set with the same shape: 5 MT-Bench tasks per category and a few of every other set."""
    rng = random.Random(seed)
    groups: dict[str, list[dict]] = {}
    for t in tasks:
        key = "mtbench/" + t["category"] if t["set"] == "mtbench" else t["set"]
        groups.setdefault(key, []).append(t)
    keep = set()
    for key in sorted(groups):
        n = 5 if key.startswith("mtbench/") else CORE.get(key, 0)
        keep.update(t["id"] for t in rng.sample(groups[key], min(n, len(groups[key]))))
    return [t for t in tasks if t["id"] in keep]


class Budget:
    """Stops starting tasks before the total is reached. With a Vercel key it also watches the real balance,
    so money the run did not record (a call that timed out but was billed) still counts."""

    def __init__(self, total: float, balance=None):
        self.total, self.spent, self.reserved = total, 0.0, 0.0
        self.balance, self.start, self.billed, self._checked = balance, None, 0.0, 0.0

    async def refresh(self, force: bool = False) -> None:
        if not self.balance or (not force and time.monotonic() - self._checked < 30):
            return
        self._checked = time.monotonic()
        now = await self.balance()
        if now is None:
            return
        if self.start is None:
            self.start = now
        self.billed = max(0.0, self.start - now)

    def used(self) -> float:
        return max(self.spent, self.billed)

    async def reserve(self, amount: float) -> bool:
        await self.refresh()
        while self.used() + self.reserved + amount > self.total:
            if self.reserved <= 0:
                return False
            await asyncio.sleep(1)
        self.reserved += amount
        return True

    def release(self, amount: float, used: float) -> None:
        self.reserved -= amount
        self.spent += used


async def vercel_balance() -> float | None:
    """The Vercel AI Gateway credit balance in USD, or None without a Vercel key or on any error."""
    import os

    import httpx
    key = os.environ.get("AI_GATEWAY_API_KEY")
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://ai-gateway.vercel.sh/v1/credits", headers={"Authorization": f"Bearer {key}"})
            return float(r.json()["balance"])
    except Exception:  # noqa: BLE001 - the guard must never stop the run by itself
        return None


async def run_siac(task, catalog, gw, args) -> dict:
    eng = Engine(gw, catalog, limits=Limits(max_cost=args.max_cost))
    t0 = time.perf_counter()
    res = await eng.run(task["prompt"])
    root = res.nodes["root"]
    return {"answer": res.answer, "status": res.status, "error": res.error, "cost": res.receipt["total_cost"],
            "seconds": round(time.perf_counter() - t0, 1), "kind": root["kind"],
            "tier": (root.get("gate") or {}).get("tier"), "subtasks": len(root["children"]),
            "jev_solved": sum(1 for n in res.nodes.values() if n["kind"] == "jev"),
            "models": sorted({n["model"] for n in res.nodes.values() if n.get("model")}),
            "estimate": res.receipt["baseline"]["estimated_cost"], "run": asdict(res)}


async def run_baseline(task, gw, args) -> dict:
    t0 = time.perf_counter()
    for attempt in range(args.patience + 1):
        try:
            r = await gw.chat(args.baseline, [{"role": "user", "content": task["prompt"]}],
                              max_tokens=args.baseline_tokens)
            break
        except GatewayError as e:
            if e.status == 429 and attempt < args.patience:  # "no access at this time": wait and ask again
                await asyncio.sleep(30)
                continue
            return {"answer": "", "status": "failed", "error": str(e)[:300], "cost": 0.0,
                    "seconds": round(time.perf_counter() - t0, 1), "model": args.baseline}
    return {"answer": r.text, "status": "done" if r.text.strip() else "empty", "error": "", "cost": r.usage.cost,
            "cost_source": r.usage.cost_source, "tokens_out": r.usage.tokens_out,
            "seconds": round(time.perf_counter() - t0, 1), "model": r.model}


async def judge(task, siac: str, base: str, gw, args) -> dict:
    """Two calls with the order swapped. Returns 'siac', 'baseline' or 'tie'."""
    rng = random.Random(f"{args.seed}-{task['id']}")
    system = JUDGE_SYSTEM + (JUDGE_REF if task.get("reference") else "")
    first_siac = rng.random() < 0.5
    orders = [(siac, base, "siac", "baseline"), (base, siac, "baseline", "siac")]
    if not first_siac:
        orders.reverse()
    votes, cost, notes = [], 0.0, []
    for a, b, name_a, name_b in orders:
        try:
            r = await gw.chat(args.judge, [{"role": "system", "content": system},
                                           {"role": "user", "content": judge_user(task, a, b)}],
                              max_tokens=3000, temperature=0, reasoning=args.judge_reasoning)
        except GatewayError as e:
            votes.append(None)
            notes.append(f"error: {str(e)[:120]}")
            continue
        cost += r.usage.cost
        v = parse_verdict(r.text)
        votes.append({"A": name_a, "B": name_b, "C": "tie"}.get(v or ""))
        notes.append(r.text.strip()[-600:])
    if None in votes:
        winner = "error"
    elif votes[0] == votes[1]:
        winner = votes[0]
    else:
        winner = "tie"
    return {"winner": winner, "votes": votes, "cost": cost, "notes": notes, "judge": args.judge}


async def one(task, catalog, gw, args) -> dict:
    reused = args.reuse.get(task["id"])
    reused_base = args.reuse_base.get(task["id"])

    async def keep(x):
        return x
    # a side taken from an earlier run is not run (nor paid) again
    siac, base = await asyncio.gather(keep(reused) if reused else run_siac(task, catalog, gw, args),
                                      keep(reused_base) if reused_base else run_baseline(task, gw, args))
    row = {"id": task["id"], "set": task["set"], "lang": task["lang"], "category": task.get("category"),
           "siac": siac, "baseline": base}
    if task["judge"] == "gold":
        s, b = score_labels(siac["answer"], task["gold"], task["options"]), \
            score_labels(base["answer"], task["gold"], task["options"])
        row["quality"] = {"mode": "gold", "siac": s, "baseline": b,
                          "winner": "siac" if s["correct"] > b["correct"] else
                          "baseline" if b["correct"] > s["correct"] else "tie", "cost": 0.0}
    elif not base["answer"].strip() or not siac["answer"].strip():
        row["quality"] = {"mode": "pairwise", "winner": "siac" if base["answer"].strip() == "" and siac["answer"].strip()
                          else "baseline" if siac["answer"].strip() == "" and base["answer"].strip() else "none",
                          "votes": [], "cost": 0.0, "notes": ["one side gave no answer"]}
    else:
        row["quality"] = {"mode": "pairwise", **await judge(task, siac["answer"], base["answer"], gw, args)}
    row["cost_total"] = ((0.0 if reused else siac["cost"]) + (0.0 if reused_base else base["cost"])
                         + row["quality"].get("cost", 0.0))
    return row


def report(rows: list[dict], args) -> str:
    def money(x):
        return f"${x:.4f}"
    lines = [f"# SIAC public benchmark · {datetime.now():%Y-%m-%d %H:%M}", "",
             f"Baseline: `{args.baseline}` · judge: `{args.judge}` · tasks: {len(rows)}", "",
             "| Set | Tasks | SIAC cost | Baseline cost | Saving | SIAC wins | Ties | Baseline wins | "
             "SIAC as good or better | SIAC time | Baseline time |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    groups = ["all"] + sorted({r["set"] for r in rows}, key=lambda s: ["mtbench", "hard", "multi", "classify",
                                                                         "ca", "es"].index(s) if s in
                                   ["mtbench", "hard", "multi", "classify", "ca", "es"] else 9)
    for g in groups:
        rs = rows if g == "all" else [r for r in rows if r["set"] == g]
        rs_ok = [r for r in rs if r["baseline"]["status"] == "done"]
        if not rs:
            continue
        sc, bc = sum(r["siac"]["cost"] for r in rs_ok), sum(r["baseline"]["cost"] for r in rs_ok)
        w = [r["quality"]["winner"] for r in rs_ok]
        n = len(w) or 1
        good = sum(1 for x in w if x in ("siac", "tie"))
        lines.append(f"| {g} | {len(rs)} | {money(sc)} | {money(bc)} | {100 * (1 - sc / bc):.0f}% | "
                     f"{w.count('siac')} | {w.count('tie')} | {w.count('baseline')} | {100 * good / n:.0f}% | "
                     f"{sum(r['siac']['seconds'] for r in rs_ok) / n:.1f} s | "
                     f"{sum(r['baseline']['seconds'] for r in rs_ok) / n:.1f} s |" if bc else
                     f"| {g} | {len(rs)} | {money(sc)} | - | - | - | - | - | - | - | - |")
    gold = [r for r in rows if r["quality"]["mode"] == "gold" and r["baseline"]["status"] == "done"]
    if gold:
        s = sum(r["quality"]["siac"]["correct"] for r in gold)
        b = sum(r["quality"]["baseline"]["correct"] for r in gold)
        t = sum(r["quality"]["siac"]["total"] for r in gold)
        sf = sum(1 for r in gold if r["quality"]["siac"]["strict"])
        bf = sum(1 for r in gold if r["quality"]["baseline"]["strict"])
        lines += ["", f"Classification against true labels: SIAC {s}/{t}, baseline {b}/{t}. "
                      f"Exact format asked for: SIAC {sf}/{len(gold)}, baseline {bf}/{len(gold)}."]
    ok = [r for r in rows if r["baseline"]["status"] == "done" and r["quality"]["mode"] == "pairwise"]
    if ok:
        sl = sum(len(r["siac"]["answer"]) for r in ok) / len(ok)
        bl = sum(len(r["baseline"]["answer"]) for r in ok) / len(ok)
        lines += ["", f"Average answer length in judged tasks: SIAC {sl:,.0f} characters, baseline {bl:,.0f}. "
                      "Judges tend to prefer longer answers, so read the win rate with this in mind."]
    failed = [r for r in rows if r["baseline"]["status"] != "done"]
    if failed:
        lines += ["", f"Baseline failed or empty on {len(failed)} tasks (left out of the table): "
                      + ", ".join(r["id"] for r in failed)]
    spent = sum(r["cost_total"] for r in rows)
    lines += ["", f"Everything together (SIAC, baseline and judge): {money(spent)}", "",
              "| Task | Set | SIAC | Baseline | Winner | SIAC path |", "|---|---|---|---|---|---|"]
    for r in rows:
        s = r["siac"]
        path = f"{s['kind']} t{s['tier']}" + (f", {s['subtasks']} parts" if s["subtasks"] else "") + \
               (f", {s['jev_solved']} by Jev" if s["jev_solved"] else "")
        lines.append(f"| {r['id']} | {r['set']} | {money(s['cost'])} | {money(r['baseline']['cost'])} | "
                     f"{r['quality']['winner']} | {path} |")
    return "\n".join(lines) + "\n"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tasks", default=str(HERE / "tasks.jsonl"), help="task file (tasks-holdout.jsonl for new tasks)")
    ap.add_argument("--sets", help="comma list: mtbench,hard,multi,classify,ca,es")
    ap.add_argument("--only", help="comma list of task ids")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--sample", type=int, help="random sample of this many tasks (seeded)")
    ap.add_argument("--core", action="store_true", help="the 60 task core set: 5 per MT-Bench category, "
                                                         "5 hard, 5 multi, 5 classify, 3 ca, 2 es (seeded)")
    ap.add_argument("--parallel", type=int, default=2)
    ap.add_argument("--max-cost", type=float, default=0.30, help="SIAC budget per task, USD")
    ap.add_argument("--reserve", type=float, default=0.25, help="expected worst case per task for the guard, USD")
    ap.add_argument("--total-budget", type=float, default=1.0, help="stop starting tasks after this, USD")
    ap.add_argument("--baseline", default=None, help="default: the catalog baseline")
    ap.add_argument("--baseline-tokens", type=int, default=16000)
    ap.add_argument("--patience", type=int, default=2, help="when the baseline is refused (429), wait 30 s and "
                                                             "try again this many times")
    ap.add_argument("--judge", default="google/gemini-3.1-pro-preview")
    ap.add_argument("--ceiling", help="cap SIAC at this model (and use it as the baseline unless --baseline says)")
    ap.add_argument("--judge-reasoning", default="low")
    ap.add_argument("--resume", help="a results .jsonl: skip the tasks it already has and append to it")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--reuse-siac", help="a results .jsonl: take SIAC's answers from it instead of running SIAC again")
    ap.add_argument("--reuse-baseline", help="a results .jsonl: take the baseline's answers from it (same model)")
    args = ap.parse_args()

    args.reuse, args.reuse_base = {}, {}
    if args.reuse_siac:
        for line in Path(args.reuse_siac).read_text(encoding="utf-8").splitlines():
            if line:
                r = json.loads(line)
                if r["siac"]["status"] == "done":
                    args.reuse[r["id"]] = r["siac"] | {"reused_from": Path(args.reuse_siac).name}
    tasks = [json.loads(line) for line in Path(args.tasks).read_text(encoding="utf-8").splitlines() if line]
    if args.sets:
        keep = set(args.sets.split(","))
        tasks = [t for t in tasks if t["set"] in keep]
    if args.only:
        keep = set(args.only.split(","))
        tasks = [t for t in tasks if t["id"] in keep]
    if args.core:
        tasks = core_set(tasks, args.seed)
    if args.sample:
        tasks = random.Random(args.seed).sample(tasks, min(args.sample, len(tasks)))
    if args.limit:
        tasks = tasks[: args.limit]
    if args.ceiling:
        import os
        os.environ["SIAC_CEILING"] = args.ceiling
    catalog = Catalog.load()
    args.baseline = args.baseline or catalog.baseline
    if args.reuse_baseline:
        for line in Path(args.reuse_baseline).read_text(encoding="utf-8").splitlines():
            if line:
                r = json.loads(line)
                same = r["baseline"].get("model", "").split("/")[-1] == args.baseline.split("/")[-1]
                if r["baseline"]["status"] == "done" and same:
                    args.reuse_base[r["id"]] = r["baseline"] | {"reused_from": Path(args.reuse_baseline).name}
    if args.dry_run:
        gw = SimulatedGateway(catalog, latency=(0, 0.01))
    else:
        from siac.providers import connect
        gw, catalog = connect(catalog)
    RESULTS.mkdir(exist_ok=True)
    if args.resume:
        jl = Path(args.resume)
        done = {json.loads(line)["id"] for line in jl.read_text(encoding="utf-8").splitlines() if line}
        tasks = [t for t in tasks if t["id"] not in done]
    else:
        jl = RESULTS / f"public-{'dry' if args.dry_run else 'live'}-{datetime.now():%Y%m%d-%H%M}.jsonl"
    budget, sem = Budget(args.total_budget, None if args.dry_run else vercel_balance), asyncio.Semaphore(args.parallel)
    await budget.refresh(force=True)

    async def guarded(t):
        async with sem:
            if not await budget.reserve(args.reserve):
                print(f"{t['id']}: skipped (total budget ${args.total_budget:.2f} reached)", flush=True)
                return None
            used = 0.0
            try:
                r = await one(t, catalog, gw, args)
                used = r["cost_total"]
            finally:
                budget.release(args.reserve, used)
            with jl.open("a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            print(f"{r['id']}: SIAC ${r['siac']['cost']:.4f} ({r['siac']['kind']} t{r['siac']['tier']}) · baseline "
                  f"${r['baseline']['cost']:.4f} {r['baseline']['status']} · {r['quality']['winner']}"
                  f"  (total ${budget.spent:.3f})", flush=True)
            return r
    await asyncio.gather(*(guarded(t) for t in tasks))
    await gw.aclose()
    rows = [json.loads(line) for line in jl.read_text(encoding="utf-8").splitlines() if line] if jl.exists() else []
    await budget.refresh(force=True)
    md = report(rows, args)
    if budget.start is not None:
        md += (f"\nBilled by Vercel during this run: ${budget.billed:.4f}; recorded by the runner: "
               f"${budget.spent:.4f}.\n")
    jl.with_suffix(".md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    asyncio.run(main())
