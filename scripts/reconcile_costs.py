#!/usr/bin/env python3
"""Reconcile the cost ledger against a vendor bill or usage export.

A thin command line wrapper over ``nanomuse.cost.reconcile``; the
importers and the comparison live there.  The ledger (``--ledger``)
is the persisted JSONL file ``nanomuse.cost.store`` appends, or a
JSON array of the same rows.  The bill (``--bill``, repeatable) is a
saved vendor export recognised by its shape: the hand-written fixture
JSON, an OpenAI usage or costs export, an Anthropic usage report,
a CSV with model, token, and cost columns, or a balance snapshot
pair (one model, the account balances before and after a run window)
for a vendor that reports a balance but no usage history.  Usage and
costs exports from the same vendor merge by model when both are
passed.

When the bill carries its own amounts, the ledger is checked against
those amounts: the real reconciliation.  When it does not, the bill's
tokens are priced with ``--prices`` (``{model: [input_per_million,
output_per_million]}``) and the output marks those models as priced
from the table.  ``--month YYYY-MM`` keeps only that month's ledger
rows.  Exit 0 when token and amount errors are both within
``--tolerance`` (default 5 percent), 1 beyond it, 2 on a usage
error.  ``--json`` prints the comparison as JSON; the default prints
one line per model plus a total line.  Amounts and token counts are
printed, never any credential: neither file may contain one.

The tooling reads real ledger files and real vendor exports now.
A real vendor bill has still never been reconciled against this
ledger, and the reconciliation Gate item stays unmet until one is.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nanomuse.cost.reconcile import (  # noqa: E402
    ReconcileError,
    compare,
    compare_amounts,
    import_bill,
    load_ledger,
    load_prices,
    merge_bills,
    price_bill,
)

__all__ = ["compare_amounts", "compare", "load_prices", "price_bill"]


def _format_row(row: dict) -> str:
    if row["tokens_compared"]:
        token_part = (
            f"tokens ledger {row['ledger_input_tokens']}/{row['ledger_output_tokens']} "
            f"bill {row['bill_input_tokens']}/{row['bill_output_tokens']} "
            f"error {row['token_relative_error']:.2%}"
        )
    else:
        token_part = "tokens not compared (balance snapshot: the amount is the check)"
    line = (
        f"{row['model']}: {token_part} | "
        f"cost ledger {row['ledger_usd']:.6f} bill {row['bill_usd']:.6f} "
        f"error {row['cost_relative_error']:.2%}"
    )
    if row["priced_from_table"]:
        line += " (bill priced from the price table)"
    return line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile the cost ledger against a vendor bill or usage export."
    )
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--bill", required=True, type=Path, action="append")
    parser.add_argument("--prices", type=Path)
    parser.add_argument("--tolerance", type=float, default=0.05)
    parser.add_argument("--month", help="Only reconcile ledger rows from this YYYY-MM month.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        ledger_totals = load_ledger(args.ledger, month=args.month)
        bill = merge_bills([import_bill(path) for path in args.bill])
        prices = load_prices(args.prices) if args.prices else {}
    except (OSError, ReconcileError, ValueError, KeyError) as exc:
        print(f"reconcile_costs: {exc}", file=sys.stderr)
        return 2
    result = compare(ledger_totals, bill, prices, args.tolerance)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        if result["bill_period"]:
            print(f"bill period: {result['bill_period']} (source: {result['bill_source']})")
        for row in result["models"]:
            print(_format_row(row))
        if result["total_token_relative_error"] is not None:
            token_error_text = f"{result['total_token_relative_error']:.2%}"
        else:
            token_error_text = "n/a"
        print(
            f"total: tokens ledger {result['total_ledger_input_tokens']}/"
            f"{result['total_ledger_output_tokens']} "
            f"bill {result['total_bill_input_tokens']}/{result['total_bill_output_tokens']} "
            f"error {token_error_text} | "
            f"cost ledger {result['total_ledger_usd']:.6f} "
            f"bill {result['total_bill_usd']:.6f} "
            f"error {result['total_cost_relative_error']:.2%}"
        )
    return 0 if result["within_tolerance"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
