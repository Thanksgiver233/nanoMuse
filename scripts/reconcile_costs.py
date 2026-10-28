#!/usr/bin/env python3
"""Reconcile a cost ledger against a vendor bill fixture.

The bill is a JSON file of the shape::

    {"entries": [{"model": ..., "input_tokens": ..., "output_tokens": ...,
                  "cost_usd": ...}], "month": "YYYY-MM"}

The ledger is a JSON file of rows in the shape ``CostLedger.to_rows``
writes.  Both sides are priced with the same price table (``--prices``
JSON: ``{model: [input_per_million, output_per_million]}``), so the
comparison measures the ledger's arithmetic, not the vendor's
metering.  Exit 0 when the relative error is within ``--tolerance``
(default 5%), 1 when it is not, 2 on a usage error.  ``--json`` prints
the comparison as JSON; the default prints one line per model plus a
total line.  Amounts are printed, never any credential: neither file
may contain one.

This script reconciles fixtures only.  A real vendor bill has never
been reconciled against this ledger, and Gate 4 item 4 stays unmet
until one is.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nanomuse.cost.ledger import PriceEntry  # noqa: E402


def load_prices(path: Path) -> dict[str, PriceEntry]:
    raw = json.loads(path.read_text())
    return {
        model: PriceEntry(model, float(rates[0]), float(rates[1])) for model, rates in raw.items()
    }


def price_bill(entries: list[dict], prices: dict[str, PriceEntry]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for entry in entries:
        price = prices.get(str(entry["model"]))
        cost = (
            price.cost(int(entry["input_tokens"]), int(entry["output_tokens"]))
            if price
            else float(entry.get("cost_usd", 0.0))
        )
        totals[str(entry["model"])] = totals.get(str(entry["model"]), 0.0) + cost
    return totals


def compare(
    ledger_totals: dict[str, float],
    bill_totals: dict[str, float],
    tolerance: float,
) -> dict:
    models = sorted(set(ledger_totals) | set(bill_totals))
    rows = []
    for model in models:
        ledger = ledger_totals.get(model, 0.0)
        bill = bill_totals.get(model, 0.0)
        error = 0.0 if bill == 0.0 else abs(ledger - bill) / bill
        rows.append(
            {
                "model": model,
                "ledger_usd": round(ledger, 6),
                "bill_usd": round(bill, 6),
                "relative_error": round(error, 6),
                "within_tolerance": error <= tolerance,
            }
        )
    total_ledger = sum(ledger_totals.values())
    total_bill = sum(bill_totals.values())
    total_error = 0.0 if total_bill == 0.0 else abs(total_ledger - total_bill) / total_bill
    return {
        "models": rows,
        "total_ledger_usd": round(total_ledger, 6),
        "total_bill_usd": round(total_bill, 6),
        "total_relative_error": round(total_error, 6),
        "within_tolerance": total_error <= tolerance and all(r["within_tolerance"] for r in rows),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile a cost ledger with a bill fixture.")
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--bill", required=True, type=Path)
    parser.add_argument("--prices", required=True, type=Path)
    parser.add_argument("--tolerance", type=float, default=0.05)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        ledger_rows = json.loads(args.ledger.read_text())
        bill = json.loads(args.bill.read_text())
        prices = load_prices(args.prices)
    except (OSError, ValueError) as exc:
        print(f"reconcile_costs: {exc}", file=sys.stderr)
        return 2
    ledger_totals = price_bill(ledger_rows, prices)
    bill_totals = price_bill(bill["entries"], prices)
    result = compare(ledger_totals, bill_totals, args.tolerance)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for row in result["models"]:
            print(
                f"{row['model']}: ledger {row['ledger_usd']:.6f} "
                f"bill {row['bill_usd']:.6f} "
                f"error {row['relative_error']:.2%}"
            )
        print(
            f"total: ledger {result['total_ledger_usd']:.6f} "
            f"bill {result['total_bill_usd']:.6f} "
            f"error {result['total_relative_error']:.2%}"
        )
    return 0 if result["within_tolerance"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
