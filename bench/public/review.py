#!/usr/bin/env python3
"""Blind human review: pick 20 judged pairs, hide which answer is SIAC's, and later compare with the judge.

  python bench/public/review.py pick results/public-live-X.jsonl     -> review-X.json (pairs) + review-X.key.json
  python bench/public/review.py score results/public-live-X.jsonl votes.json
      votes.json: {"<task id>": "A" | "B" | "tie"}  -> agreement between the person and the judge
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path


def pick(results: Path, n: int = 20, seed: int = 2026) -> None:
    rows = [json.loads(line) for line in results.read_text(encoding="utf-8").splitlines() if line]
    rows = [r for r in rows if r["quality"]["mode"] == "pairwise" and r["quality"]["winner"] in ("siac", "baseline",
                                                                                                "tie")]
    rng = random.Random(seed)
    by_set: dict[str, list] = {}
    for r in rows:
        by_set.setdefault(r["set"], []).append(r)
    # every set gets its share, at least one each
    chosen = []
    for s in sorted(by_set):
        rng.shuffle(by_set[s])
        chosen.append(by_set[s].pop())
    rest = [r for s in sorted(by_set) for r in by_set[s]]
    rng.shuffle(rest)
    chosen += rest[: max(0, n - len(chosen))]
    tasks = {t["id"]: t for t in (json.loads(x) for x in (Path(__file__).parent / "tasks.jsonl")
                                  .read_text(encoding="utf-8").splitlines() if x)}
    pairs, key = [], {}
    for r in chosen:
        siac_first = rng.random() < 0.5
        a, b = (r["siac"]["answer"], r["baseline"]["answer"]) if siac_first else \
            (r["baseline"]["answer"], r["siac"]["answer"])
        pairs.append({"id": r["id"], "set": r["set"], "prompt": tasks[r["id"]]["prompt"], "a": a, "b": b})
        key[r["id"]] = {"A": "siac" if siac_first else "baseline", "B": "baseline" if siac_first else "siac",
                        "judge": r["quality"]["winner"]}
    stem = results.stem.replace("public-", "review-")
    (results.parent / f"{stem}.json").write_text(json.dumps(pairs, ensure_ascii=False, indent=1), encoding="utf-8")
    (results.parent / f"{stem}.key.json").write_text(json.dumps(key, indent=1), encoding="utf-8")
    print(f"{len(pairs)} pairs -> {stem}.json (key in {stem}.key.json)")


def score(results: Path, votes_path: Path) -> None:
    key = json.loads((results.parent / (results.stem.replace("public-", "review-") + ".key.json")).read_text())
    votes = json.loads(votes_path.read_text())
    agree, person = 0, {"siac": 0, "baseline": 0, "tie": 0}
    for tid, v in votes.items():
        k = key[tid]
        who = "tie" if v == "tie" else k[v]
        person[who] += 1
        agree += who == k["judge"]
    n = len(votes) or 1
    print(f"Person: SIAC better {person['siac']}, tie {person['tie']}, baseline better {person['baseline']} "
          f"· agrees with the judge on {agree}/{len(votes)} ({100 * agree / n:.0f}%)")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "pick":
        pick(Path(sys.argv[2]))
    elif len(sys.argv) >= 4 and sys.argv[1] == "score":
        score(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        sys.exit(__doc__)
