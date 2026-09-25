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


def to_markdown(run: dict) -> str:
    """A readable report of a run: request, answer, who did each task and the cost receipt."""
    r = run.get("receipt", {})
    lines = [f"# Taskpenny run {run.get('id', '')}", "", f"**Status:** {run.get('status')} · "
             f"**Time:** {run.get('duration_s', 0)} s · **Cost:** ${r.get('total_cost', 0):.5f}", "",
             "## Request", "", run.get("request", ""), "", "## Answer", "", run.get("answer", ""), "",
             "## Task tree", "", "| Task | Title | Done by | Tier | Cost |", "|---|---|---|---|---|"]
    nodes = run.get("nodes", {})

    def walk(nid: str, depth: int) -> None:
        n = nodes.get(nid)
        if not n:
            return
        who = "Jev" if n.get("kind") == "jev" else (n.get("model") or "-")
        title = ("  " * depth) + (n.get("title") or nid).replace("|", "/")
        lines.append(f"| {nid} | {title} | {who} | {n.get('tier') or '-'} | ${n.get('cost', 0):.5f} |")
        for c in n.get("children", []):
            walk(c, depth + 1)
    walk("root", 0)
    b = r.get("baseline", {})
    lines += ["", "## Cost receipt", "", "| Part | Cost |", "|---|---|"]
    lines += [f"| {k} | ${v:.5f} |" for k, v in r.get("by_role", {}).items()]
    lines += [f"| **Total** | **${r.get('total_cost', 0):.5f}** |", "",
              f"Same request with {b.get('model')} alone: about ${b.get('estimated_cost', 0):.5f}"
              + (f" (saving {r['saving_pct']:.0f}%)" if r.get("saving_pct") is not None and r["saving_pct"] >= 0 else ""),
              "", f"_Estimate method: {b.get('method', '')}._", ""]
    return "\n".join(lines)
