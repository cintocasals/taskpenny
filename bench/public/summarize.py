#!/usr/bin/env python3
"""Tables for the benchmark report from one or two results files.

  python bench/public/summarize.py results/public-live-A.jsonl                 (one run)
  python bench/public/summarize.py results/public-live-B.jsonl --before results/public-live-A.jsonl
  python bench/public/summarize.py results/public-live-A.jsonl --ci            (with 95% bootstrap intervals)

Prints Markdown: every set, the 60 task core against the other tasks, and (with --before) the same tasks
before and after, so a change tuned on some tasks can be checked on tasks it never saw.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run import core_set, jev_list_cost, read_results  # noqa: E402  (same folder)

HERE = Path(__file__).parent
ORDER = ["mtbench", "hard", "multi", "classify", "ca", "es"]
NAMES = {"mtbench": "MT-Bench (8 categories)", "hard": "Arena-Hard", "multi": "Four asks in one message",
         "classify": "Label 10 messages", "ca": "Catalan", "es": "Spanish"}


def load(path: str) -> list[dict]:
    return read_results(path)


def stats(rows: list[dict]) -> dict:
    ok = [r for r in rows if r["baseline"]["status"] == "done"]
    w = [r["quality"]["winner"] for r in ok if r["quality"]["winner"] in ("taskpenny", "tie", "baseline")]
    sc, bc = sum(r["taskpenny"]["cost"] for r in ok), sum(r["baseline"]["cost"] for r in ok)
    n = len(ok) or 1  # a verdict that could not be read counts as not as good
    return {"n": len(ok), "taskpenny": sc, "base": bc, "saving": (1 - sc / bc) * 100 if bc else 0.0,
            "win": w.count("taskpenny"), "tie": w.count("tie"), "loss": w.count("baseline"),
            "good": 100 * sum(1 for x in w if x in ("taskpenny", "tie")) / n,
            "t_taskpenny": sum(r["taskpenny"]["seconds"] for r in ok) / n, "t_base": sum(r["baseline"]["seconds"] for r in ok) / n}


def cost_cell(s: dict) -> str:
    if not s["base"]:
        return "-"
    if s["saving"] >= 0:
        return f"**{s['saving']:.0f}% less**"
    return f"**{s['taskpenny'] / s['base']:.1f}x as much**" if s["base"] else "-"


def row(name: str, s: dict) -> str:
    if not s["n"]:
        return f"| {name} | 0 | - | - | - | - | - | - |"
    return (f"| {name} | {s['n']} | ${s['taskpenny']:.3f} | ${s['base']:.3f} | {cost_cell(s)} | "
            f"**{s['good']:.0f}%** | {s['win']} / {s['tie']} / {s['loss']} | {s['t_taskpenny']:.1f} s / {s['t_base']:.1f} s |")


HEAD = ("| Tasks | n | Taskpenny | One model | Taskpenny cost | As good or better | Wins / ties / losses | Time (Taskpenny / one model) |\n"
        "|---|---|---|---|---|---|---|---|")


ROUTES = [("Basic and standard (tier 1-2), one model",
           lambda r: r["taskpenny"]["kind"] != "split" and (r["taskpenny"]["tier"] or 0) <= 2),
          ("Advanced (tier 3), one model", lambda r: r["taskpenny"]["kind"] != "split" and r["taskpenny"]["tier"] == 3),
          ("Critical (tier 4), one model", lambda r: r["taskpenny"]["kind"] != "split" and r["taskpenny"]["tier"] == 4),
          ("Split into parts", lambda r: r["taskpenny"]["kind"] == "split")]


def bootstrap(rows: list[dict], price: float, n: int = 20_000, seed: int = 2026) -> dict:
    """95% intervals for the saving (with Jev at its list price) and for "as good or better", by resampling the
    tasks with replacement. Tasks where the baseline failed are left out, as in the tables."""
    ok = [r for r in rows if r["baseline"]["status"] == "done"]
    tp = [r["taskpenny"]["cost"] + jev_list_cost(r, price) for r in ok]
    base = [r["baseline"]["cost"] for r in ok]
    good = [r["quality"]["winner"] in ("taskpenny", "tie") for r in ok]
    rng = random.Random(seed)
    savings, goods = [], []
    for _ in range(n):
        idx = [rng.randrange(len(ok)) for _ in ok]
        b = sum(base[i] for i in idx)
        savings.append(100 * (1 - sum(tp[i] for i in idx) / b) if b else 0.0)
        goods.append(100 * sum(good[i] for i in idx) / len(idx))
    savings.sort(), goods.sort()
    lo, hi = int(0.025 * n), int(0.975 * n) - 1
    return {"saving": (100 * (1 - sum(tp) / sum(base)), savings[lo], savings[hi]),
            "good": (100 * sum(good) / len(good), goods[lo], goods[hi]), "n": len(ok)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--before")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--ci", action="store_true", help="95%% bootstrap intervals for the whole file")
    a = ap.parse_args()
    rows = load(a.results)
    tasks = [json.loads(x) for x in (HERE / "tasks.jsonl").read_text(encoding="utf-8").splitlines() if x]
    core = {t["id"] for t in core_set(tasks, a.seed)}
    out = ["## By set", "", HEAD, row("**All**", stats(rows))]
    for s in ORDER:
        rs = [r for r in rows if r["set"] == s]
        if rs:
            out.append(row(NAMES[s], stats(rs)))
    from taskpenny.catalog import Catalog
    price = Catalog.load().decider.price_in
    billed = [r for r in rows if sum(((r["taskpenny"].get("run") or {}).get("receipt") or {}).get("by_role", {})
                                     .get(k, 0) for k in ("decisions", "checks")) > 0]
    if billed:
        out += ["", f"Jev was billed by the gateway in {len(billed)} of these {len(rows)} runs, at its list price "
                    f"(${price:g} per million input tokens): its cost is already in Taskpenny's column."]
    out += ["", f"## With Jev at its list price (${price:g} per million input tokens)", "",
            "Where Jev was free on Vercel AI Gateway (until 26 September 2026), at most its decisions would have "
            "added:", "",
            "| Tasks | n | Taskpenny | Jev at list price, at most | One model | Taskpenny cost: free Jev → Jev at list price |",
            "|---|---|---|---|---|---|"]
    groups = ([("**All**", rows)] + [(NAMES[s], [r for r in rows if r["set"] == s]) for s in ORDER]
              + [(n, [r for r in rows if f(r)]) for n, f in ROUTES])
    for name, rs in groups:
        ok_rs = [r for r in rs if r["baseline"]["status"] == "done"]
        if not ok_rs:
            continue
        s = stats(ok_rs)
        j = sum(jev_list_cost(r, price) for r in ok_rs)
        before, after = s["taskpenny"] / s["base"], (s["taskpenny"] + j) / s["base"]  # one decimal: the change is small
        cell = (f"{(1 - before) * 100:.1f}% less → **{(1 - after) * 100:.1f}% less**" if after <= 1 else
                f"{before:.2f}x → **{after:.2f}x as much**")
        out.append(f"| {name} | {s['n']} | ${s['taskpenny']:.3f} | ${j:.4f} | ${s['base']:.3f} | {cell} |")
    gold = [r for r in rows if r["quality"]["mode"] == "gold" and r["baseline"]["status"] == "done"]
    if gold:
        t = sum(r["quality"]["taskpenny"]["total"] for r in gold)
        out += ["", f"Labelling against the true labels: Taskpenny {sum(r['quality']['taskpenny']['correct'] for r in gold)}"
                    f"/{t}, one model {sum(r['quality']['baseline']['correct'] for r in gold)}/{t}."]
    out += ["", "## By the route Taskpenny chose", "", HEAD] + [row(n, stats([r for r in rows if f(r)])) for n, f in ROUTES]
    in_core = [r for r in rows if r["id"] in core]
    if in_core:  # only the first task file has a core; our own ca/es cases helped calibrate the gate
        out += ["", "## Tasks used to tune Taskpenny and the others", "", HEAD,
                row("Core 60 (used while tuning)", stats(in_core)),
                row("Others, from outside sources", stats([r for r in rows if r["id"] not in core
                                                          and r["set"] not in ("ca", "es")])),
                row("Others, our own development cases", stats([r for r in rows if r["id"] not in core
                                                               and r["set"] in ("ca", "es")]))]
    if a.before:
        before = {r["id"]: r for r in load(a.before)}
        same = [r for r in rows if r["id"] in before]
        out += ["", "## Before and after the tuning, same tasks", "", HEAD,
                row("Before", stats([before[r["id"]] for r in same])), row("After", stats(same))]
    ok = [r for r in rows if r["baseline"]["status"] == "done" and r["quality"]["mode"] == "pairwise"]
    if ok:
        out += ["", f"Average answer length in judged tasks: Taskpenny {sum(len(r['taskpenny']['answer']) for r in ok) / len(ok):,.0f}"
                    f" characters, one model {sum(len(r['baseline']['answer']) for r in ok) / len(ok):,.0f}."]
    failed = [r["id"] for r in rows if r["baseline"]["status"] != "done"]
    if failed:
        out += ["", f"Left out because the baseline failed or gave no answer: {', '.join(failed)}."]
    unread = [r["id"] for r in rows if r["baseline"]["status"] == "done"
              and r["quality"]["winner"] not in ("taskpenny", "tie", "baseline")]
    if unread:
        out += ["", f"Judge verdict that could not be read ({', '.join(unread)}): counted as not as good, so wins, "
                    "ties and losses add up to one less than n in its rows."]
    stopped = [r["id"] for r in rows if r["taskpenny"]["status"] not in ("done", "unverified")]
    if stopped:
        out += ["", f"Taskpenny runs that ended early (partial or failed), counted as they are: {', '.join(stopped)}."]
    unverified = [r["id"] for r in rows if r["taskpenny"]["status"] == "unverified"]
    if unverified:
        out += ["", f"Taskpenny answers marked unverified (a check did not pass or could not run): {len(unverified)}."]
    if a.ci:
        b = bootstrap(rows, price)
        out += ["", f"95% bootstrap intervals over the {b['n']} compared tasks (20,000 resamples): saving with Jev at "
                    f"list price {b['saving'][0]:.1f}% ({b['saving'][1]:.1f} to {b['saving'][2]:.1f}); as good or better "
                    f"{b['good'][0]:.0f}% ({b['good'][1]:.0f} to {b['good'][2]:.0f})."]
    print("\n".join(out))


if __name__ == "__main__":
    main()
