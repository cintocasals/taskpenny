"""Every run is saved as one JSON file: request, task tree, every event and the cost receipt."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .engine import RunResult


def save(result: RunResult, directory: str | Path = "runs") -> Path:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{result.id}.json"
    path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _money(v) -> str:
    try:
        return f"${float(v):.5f}"
    except (TypeError, ValueError):
        return "-"


def _cell(v) -> str:
    return str(v if v not in (None, "") else "-").replace("|", "/").replace("\n", " ")


def to_markdown(run: dict) -> str:
    """A readable report of a run: request, answer, warnings, who did each task and the cost receipt.
    A damaged run file (missing fields, wrong types, a task tree with loops) still gives a report."""
    run = run if isinstance(run, dict) else {}
    r = run.get("receipt") if isinstance(run.get("receipt"), dict) else {}
    lines = [f"# Taskpenny run {_cell(run.get('id'))}", "", f"**Status:** {_cell(run.get('status'))} · "
             f"**Time:** {_cell(run.get('duration_s'))} s · **Cost:** {_money(r.get('total_cost'))}", "",
             "## Request", "", str(run.get("request") or ""), "", "## Answer", "", str(run.get("answer") or ""), ""]
    warnings = run.get("warnings") if isinstance(run.get("warnings"), list) else []
    if warnings:
        lines += ["## Warnings", ""] + [f"- {w}" for w in warnings] + [""]
    lines += ["## Task tree", "", "| Task | Title | Done by | Tier | Status | Cost |", "|---|---|---|---|---|---|"]
    nodes = run.get("nodes") if isinstance(run.get("nodes"), dict) else {}
    seen: set[str] = set()

    def walk(nid: str, depth: int) -> None:
        n = nodes.get(nid)
        if not isinstance(n, dict) or nid in seen or depth > 20:
            return
        seen.add(nid)
        who = "Jev" if n.get("kind") == "jev" else _cell(n.get("model"))
        title = ("  " * depth) + _cell(n.get("title") or nid)
        lines.append(f"| {_cell(nid)} | {title} | {who} | {_cell(n.get('tier'))} | {_cell(n.get('status'))} | "
                     f"{_money(n.get('cost') or 0)} |")
        for c in n.get("children") if isinstance(n.get("children"), list) else []:
            walk(str(c), depth + 1)
    walk("root", 0)
    b = r.get("baseline") if isinstance(r.get("baseline"), dict) else {}
    by_role = r.get("by_role") if isinstance(r.get("by_role"), dict) else {}
    lines += ["", "## Cost receipt", "", "| Part | Cost |", "|---|---|"]
    lines += [f"| {_cell(k)} | {_money(v)} |" for k, v in by_role.items()]
    saving = r.get("saving_pct")
    lines += [f"| **Total** | **{_money(r.get('total_cost'))}** |", "",
              f"Same request with {_cell(b.get('model'))} alone: about {_money(b.get('estimated_cost'))}"
              + (f" (saving {saving:.0f}%)" if isinstance(saving, (int, float)) and saving >= 0 else ""),
              "", f"_Estimate method: {b.get('method', '')}._", ""]
    return "\n".join(lines)
