#!/usr/bin/env python3
"""Tables for the benchmark report from one or two results files.

  python bench/public/summarize.py results/public-live-A.jsonl                 (one run)
  python bench/public/summarize.py results/public-live-B.jsonl --before results/public-live-A.jsonl

Prints Markdown: every set, the 60 task core against the other tasks, and (with --before) the same tasks
before and after, so a change tuned on some tasks can be checked on tasks it never saw.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run import core_set, read_results  # noqa: E402  (same folder)

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
    if s["saving"] >= 0:
        return f"**{s['saving']:.0f}% less**"
    return f"**{s['taskpenny'] / s['base']:.1f}x as much**" if s["base"] else "-"


def row(name: str, s: dict) -> str:
    return (f"| {name} | {s['n']} | ${s['taskpenny']:.3f} | ${s['base']:.3f} | {cost_cell(s)} | "
            f"**{s['good']:.0f}%** | {s['win']} / {s['tie']} / {s['loss']} | {s['t_taskpenny']:.1f} s / {s['t_base']:.1f} s |")


HEAD = ("| Tasks | n | Taskpenny | One model | Taskpenny cost | As good or better | Wins / ties / losses | Time (Taskpenny / one model) |\n"
        "|---|---|---|---|---|---|---|---|")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--before")
    ap.add_argument("--seed", type=int, default=2026)
    a = ap.parse_args()
    rows = load(a.results)
    tasks = [json.loads(x) for x in (HERE / "tasks.jsonl").read_text(encoding="utf-8").splitlines() if x]
    core = {t["id"] for t in core_set(tasks, a.seed)}
    out = ["## By set", "", HEAD, row("**All**", stats(rows))]
    for s in ORDER:
        rs = [r for r in rows if r["set"] == s]
        if rs:
            out.append(row(NAMES[s], stats(rs)))
    gold = [r for r in rows if r["quality"]["mode"] == "gold" and r["baseline"]["status"] == "done"]
    if gold:
        t = sum(r["quality"]["taskpenny"]["total"] for r in gold)
        out += ["", f"Labelling against the true labels: Taskpenny {sum(r['quality']['taskpenny']['correct'] for r in gold)}"
                    f"/{t}, one model {sum(r['quality']['baseline']['correct'] for r in gold)}/{t}."]
    routes = [("Basic and standard (tier 1-2), one model", lambda r: r["taskpenny"]["kind"] != "split" and (r["taskpenny"]["tier"] or 0) <= 2),
              ("Advanced (tier 3), one model", lambda r: r["taskpenny"]["kind"] != "split" and r["taskpenny"]["tier"] == 3),
              ("Critical (tier 4), one model", lambda r: r["taskpenny"]["kind"] != "split" and r["taskpenny"]["tier"] == 4),
              ("Split into parts", lambda r: r["taskpenny"]["kind"] == "split")]
    out += ["", "## By the route Taskpenny chose", "", HEAD] + [row(n, stats([r for r in rows if f(r)])) for n, f in routes]
    out += ["", "## Tasks used to tune Taskpenny and tasks it never saw", "", HEAD,
            row("Core 60 (seen while tuning)", stats([r for r in rows if r["id"] in core])),
            row("Other tasks (never seen)", stats([r for r in rows if r["id"] not in core]))]
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
        out += ["", f"Left out because the baseline failed: {', '.join(failed)}."]
    print("\n".join(out))


if __name__ == "__main__":
    main()
