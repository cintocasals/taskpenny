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
