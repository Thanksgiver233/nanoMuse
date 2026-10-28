#!/usr/bin/env python3
"""Sample the Feed for the interruption audit (Gate 3 item 4).

The audit itself is done by a person: this script only draws the sample and
scores the filled-in sheet, so the numbers stay honest about what was and was
not checked by hand.

Draw a sample::

    python scripts/audit_interruptions.py sample feed.json --n 50 --seed 42 --out sample.csv

``feed.json`` is the list ``GET /api/feed`` returns. The sample is a CSV with
one row per Feed entry and two empty columns for the reviewer: ``verdict``
(``ok`` when the interruption was wanted, ``unwanted`` when it was a
misfire, ``skip`` when the row is not an interruption at all) and ``note``.
The same seed always draws the same sample, so a second reviewer can reproduce
the draw.

Score a filled-in sheet::

    python scripts/audit_interruptions.py score sample.csv

The misfire rate is ``unwanted / (ok + unwanted)`` over the reviewed rows;
``skip`` and empty rows do not count. Gate 3 allows at most 10%: the script
prints the rate and whether the sheet passes, and exits non-zero when it does
not. Nothing here judges an entry on its own: a script cannot tell whether an
interruption was wanted, and this one does not pretend to.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path
from typing import Any

MAX_TEXT_CHARS = 200
THRESHOLD = 0.10
VERDICTS = ("ok", "unwanted", "skip")
COLUMNS = ["id", "ts", "kind", "reason", "title", "text", "verdict", "note"]


def load_feed(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list of feed entries")
    return [row for row in data if isinstance(row, dict)]


def draw_sample(items: list[dict[str, Any]], n: int, seed: int) -> list[dict[str, Any]]:
    """Up to ``n`` entries, drawn deterministically: the same seed, the same sample."""
    if n < 1:
        raise ValueError("the sample size must be at least 1")
    if len(items) <= n:
        return list(items)
    return random.Random(seed).sample(items, n)


def sample_rows(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for item in items:
        text = str(item.get("text") or "")
        rows.append(
            {
                "id": str(item.get("id") or ""),
                "ts": str(item.get("ts") or ""),
                "kind": str(item.get("kind") or ""),
                "reason": str(item.get("reason") or ""),
                "title": str(item.get("title") or ""),
                "text": text[:MAX_TEXT_CHARS],
                "verdict": "",
                "note": "",
            }
        )
    return rows


def write_template(rows: list[dict[str, str]], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def score_sheet(path: Path) -> dict[str, Any]:
    """Count a filled-in sheet: reviewed rows, misfires, and the rate."""
    counts = {"ok": 0, "unwanted": 0, "skip": 0, "unreviewed": 0, "invalid": 0}
    with path.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            verdict = (row.get("verdict") or "").strip().lower()
            if verdict in VERDICTS:
                counts[verdict] += 1
            elif verdict == "":
                counts["unreviewed"] += 1
            else:
                counts["invalid"] += 1
    reviewed = counts["ok"] + counts["unwanted"]
    rate = counts["unwanted"] / reviewed if reviewed else None
    return {
        "reviewed": reviewed,
        "ok": counts["ok"],
        "unwanted": counts["unwanted"],
        "skip": counts["skip"],
        "unreviewed": counts["unreviewed"],
        "invalid": counts["invalid"],
        "misfire_rate": rate,
        "passes": rate is not None and rate <= THRESHOLD,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sample the Feed for the interruption audit.")
    sub = parser.add_subparsers(dest="command", required=True)
    sample = sub.add_parser("sample", help="draw a sample from a feed export")
    sample.add_argument("feed", type=Path)
    sample.add_argument("--n", type=int, default=50)
    sample.add_argument("--seed", type=int, default=42)
    sample.add_argument("--out", type=Path, required=True)
    score = sub.add_parser("score", help="score a filled-in sample sheet")
    score.add_argument("sheet", type=Path)
    args = parser.parse_args(argv)
    if args.command == "sample":
        try:
            items = load_feed(args.feed)
            rows = sample_rows(draw_sample(items, args.n, args.seed))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        write_template(rows, args.out)
        print(f"wrote {len(rows)} rows to {args.out} (seed {args.seed})")
        return 0
    result = score_sheet(args.sheet)
    rate = result["misfire_rate"]
    rate_text = f"{rate:.1%}" if rate is not None else "no reviewed rows"
    print(
        f"reviewed {result['reviewed']} rows: {result['ok']} ok, "
        f"{result['unwanted']} unwanted, {result['skip']} skipped, "
        f"{result['unreviewed']} unreviewed, {result['invalid']} invalid; "
        f"misfire rate {rate_text} (at most 10% passes)"
    )
    return 0 if result["passes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
