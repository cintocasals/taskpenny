#!/usr/bin/env python3
"""Build the public benchmark task set from pinned public sources. Same sources, same seed, same file.

Usage: python bench/public/build.py            (writes bench/public/tasks.jsonl)
       python bench/public/build.py --holdout  (writes tasks-holdout.jsonl: 50 new tasks for checking changes)

Sets (English unless noted):
  mtbench   80  MT-Bench first turns, 8 categories (lm-sys/FastChat, Apache 2.0). Math, reasoning and coding
                questions carry the MT-Bench reference answer for the judge.
  hard      15  Random sample of Arena-Hard-Auto v0.1 prompts (lmarena/arena-hard-auto, Apache 2.0).
  multi     10  Several asks in one message: four Dolly prompts each (databricks-dolly-15k, CC BY-SA 3.0).
  classify  10  Ten customer messages to label per request, with the true labels (Banking77, CC BY 4.0).
  ca, es    28  Our own development requests in Catalan and Spanish (MIT).

Every source file is downloaded from a fixed commit and checked against its SHA-256 before use.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import random
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
CACHE = HERE / ".cache"
SEED = 2026

SOURCES = {
    "mtbench": ("https://raw.githubusercontent.com/lm-sys/FastChat/587d5cfa1609a43d192cedb8441cac3c17db105d/"
                "fastchat/llm_judge/data/mt_bench/question.jsonl",
                "119565adbab82227089cefdb44c8d7e2cf04dc0a0ec233634c82e7d4e2a944f7"),
    "mtbench_ref": ("https://raw.githubusercontent.com/lm-sys/FastChat/587d5cfa1609a43d192cedb8441cac3c17db105d/"
                    "fastchat/llm_judge/data/mt_bench/reference_answer/gpt-4.jsonl",
                    "f957a5bc977badb66885ec970e6cd08527845780313f0995764260e5777b9b3f"),
    "arenahard": ("https://raw.githubusercontent.com/lmarena/arena-hard-auto/196f6b826783b3da7310e361a805fa36f0be83f3/"
                  "data/arena-hard-v0.1/question.jsonl",
                  "2650242d2883f22b2abd498f273b6a7e02253cce533f5c91a858e1d44aa3002f"),
    "dolly": ("https://huggingface.co/datasets/databricks/databricks-dolly-15k/resolve/"
              "bdd27f4d94b9c1f951818a7da7fd7aeea5dbff1a/databricks-dolly-15k.jsonl",
              "2df9083338b4abd6bceb5635764dab5d833b393b55759dffb0959b6fcbf794ec"),
    "banking77": ("https://raw.githubusercontent.com/PolyAI-LDN/task-specific-datasets/"
                  "57ec275d8078af65b7731c2a98be812d844a6d6b/banking_data/test.csv",
                  "d12d6e3bc4c3103966ae786dc435913c0c563dfa328f5a3646d0e62cfeeb474d"),
}
LICENSE = {"mtbench": "Apache-2.0", "arenahard": "Apache-2.0", "dolly": "CC-BY-SA-3.0",
           "banking77": "CC-BY-4.0", "taskpenny": "MIT"}


def fetch(name: str) -> bytes:
    url, sha = SOURCES[name]
    CACHE.mkdir(exist_ok=True)
    path = CACHE / name
    if path.exists():
        data = path.read_bytes()
    else:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read()
    got = hashlib.sha256(data).hexdigest()
    if got != sha:
        sys.exit(f"{name}: SHA-256 {got} does not match the pinned {sha}; source changed, stopping.")
    path.write_bytes(data)
    return data


def jsonl(data: bytes) -> list[dict]:
    return [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]


def mtbench() -> list[dict]:
    refs = {r["question_id"]: r["choices"][0]["turns"][0] for r in jsonl(fetch("mtbench_ref"))}
    out = []
    for q in jsonl(fetch("mtbench")):
        t = {"id": f"mt{q['question_id']}", "set": "mtbench", "lang": "en", "prompt": q["turns"][0],
             "category": q["category"], "judge": "pairwise",
             "source": {"dataset": "MT-Bench", "id": q["question_id"], "license": LICENSE["mtbench"]}}
        if q["category"] in ("math", "reasoning", "coding") and q["question_id"] in refs:
            t["reference"] = refs[q["question_id"]]
        out.append(t)
    return out


def hard(n: int = 15, seed: int = SEED, prefix: str = "ah", exclude: set | None = None) -> list[dict]:
    rows = [r for r in jsonl(fetch("arenahard")) if r["uid"] not in (exclude or set())]
    pick = random.Random(seed).sample(rows, n)
    return [{"id": f"{prefix}{i:02d}", "set": "hard", "lang": "en", "prompt": q["prompt"], "category": q["cluster"],
             "judge": "pairwise", "source": {"dataset": "Arena-Hard-Auto v0.1", "id": q["uid"],
                                              "license": LICENSE["arenahard"]}}
            for i, q in enumerate(pick, 1)]


def multi(n: int = 10, seed: int = SEED + 1, prefix: str = "mp", exclude: set | None = None) -> list[dict]:
    rows = [r | {"row": i} for i, r in enumerate(jsonl(fetch("dolly")))]
    ok = [r for r in rows if 30 <= len(r["instruction"]) <= 250 and len(r["context"]) <= 700
          and r["row"] not in (exclude or set())]
    pools = {
        "text": [r for r in ok if r["category"] in ("information_extraction", "closed_qa") and r["context"]],
        "ideas": [r for r in ok if r["category"] == "brainstorming" and not r["context"]],
        "write": [r for r in ok if r["category"] == "creative_writing" and not r["context"]
                  and r["instruction"].lower().startswith("write")],
        "sort": [r for r in ok if r["category"] == "classification" and not r["context"]],
    }
    rng = random.Random(seed)
    picks = {k: rng.sample(v, n) for k, v in pools.items()}
    out = []
    for i in range(n):
        parts = [picks["text"][i], picks["ideas"][i], picks["write"][i], picks["sort"][i]]
        rng.shuffle(parts)
        lines = ["I have a few things for you today, please do them all:", ""]
        for j, p in enumerate(parts, 1):
            ask = p["instruction"].strip()
            if p["context"]:
                ask += "\n   Text: \"" + " ".join(p["context"].split()) + "\""
            lines.append(f"{j}) {ask}")
        out.append({"id": f"{prefix}{i + 1:02d}", "set": "multi", "lang": "en", "prompt": "\n".join(lines),
                    "category": "multi-part", "judge": "pairwise",
                    "source": {"dataset": "databricks-dolly-15k", "id": [p["row"] for p in parts],
                               "license": LICENSE["dolly"]}})
    return out


STOP = {"my", "or", "to", "not", "by", "the", "a", "of", "for", "after", "via", "and", "on", "into"}


def classify(n: int = 10, labels: int = 6, items: int = 10) -> list[dict]:
    rows = list(csv.DictReader(io.StringIO(fetch("banking77").decode("utf-8"))))
    by = {}
    for i, r in enumerate(rows):
        by.setdefault(r["category"], []).append((i, r["text"].strip()))
    names = sorted(by)
    rng = random.Random(SEED + 2)
    out = []
    for k in range(n):
        # six intents that share no word, so each set has one right label per message
        chosen: list[str] = []
        for c in rng.sample(names, len(names)):
            words = set(c.lower().strip("?").split("_")) - STOP
            if all(not (words & (set(o.lower().strip("?").split("_")) - STOP)) for o in chosen):
                chosen.append(c)
            if len(chosen) == labels:
                break
        msgs = []
        for m in range(items):
            c = chosen[m % labels] if m < labels else rng.choice(chosen)
            msgs.append((c, rng.choice(by[c])))
        rng.shuffle(msgs)
        human = {c: c.strip("?").replace("_", " ").lower() for c in chosen}
        opts = sorted(human.values())
        lines = ["Our support inbox needs sorting. Label each customer message below with exactly one of these "
                 "categories: " + "; ".join(opts) + ".",
                 "Answer with one line per message: the message number, a colon and the category, nothing else.", ""]
        gold = {}
        for j, (c, (row, text)) in enumerate(msgs, 1):
            lines.append(f"{j}. {text}")
            gold[str(j)] = human[c]
        out.append({"id": f"cl{k + 1:02d}", "set": "classify", "lang": "en", "prompt": "\n".join(lines),
                    "category": "classification", "judge": "gold", "gold": gold, "options": opts,
                    "source": {"dataset": "Banking77 (test)", "id": [row for _, (row, _) in msgs],
                               "license": LICENSE["banking77"]}})
    return out


def ours() -> list[dict]:
    out = []
    for line in (HERE.parent / "dev_cases.jsonl").read_text(encoding="utf-8").splitlines():
        c = json.loads(line)
        if c["lang"] in ("ca", "es"):
            out.append({"id": c["id"], "set": c["lang"], "lang": c["lang"], "prompt": c["prompt"],
                        "category": c["expect"]["type"], "judge": "pairwise", "expect": c["expect"],
                        # the label from before the rename, so the published tasks.jsonl rebuilds byte for byte
                        "source": {"dataset": "SIAC dev cases", "id": c["id"], "license": LICENSE["taskpenny"]}})
    return out


def holdout() -> list[dict]:
    """New tasks for checking changes made after the first published run: none of them was used before."""
    used = [json.loads(x) for x in (HERE / "tasks.jsonl").read_text(encoding="utf-8").splitlines() if x]
    uids = {t["source"]["id"] for t in used if t["set"] == "hard"}
    rows = {r for t in used if t["set"] == "multi" for r in t["source"]["id"]}
    return hard(40, seed=SEED + 100, prefix="h2", exclude=uids) + multi(10, seed=SEED + 101, prefix="m2", exclude=rows)


def main() -> None:
    if "--holdout" in sys.argv:
        tasks = holdout()
        path = HERE / "tasks-holdout.jsonl"
        path.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in tasks), encoding="utf-8")
        print(f"{path.name}: {len(tasks)} tasks · sha256 {hashlib.sha256(path.read_bytes()).hexdigest()}")
        return
    tasks = mtbench() + hard() + multi() + classify() + ours()
    ids = [t["id"] for t in tasks]
    assert len(ids) == len(set(ids)), "duplicate ids"
    path = HERE / "tasks.jsonl"
    path.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in tasks), encoding="utf-8")
    counts = {}
    for t in tasks:
        counts[t["set"]] = counts.get(t["set"], 0) + 1
    print(f"{path.name}: {len(tasks)} tasks · " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    print("sha256", hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
