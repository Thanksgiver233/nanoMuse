"""Reconcile tests: ledger files, vendor export shapes, error math.

Every bill fixture in this file is synthetic: the shapes copy the
vendors' documented export formats, but the numbers are invented, no
real account produced them, and no credential appears anywhere.
Nothing here touches the network.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nanomuse.cost.ledger import PriceEntry
from nanomuse.cost.reconcile import (
    ReconcileError,
    compare,
    compare_amounts,
    import_bill,
    load_ledger,
    merge_bills,
    price_bill,
)

PRICES = {"qwen": [2.0, 8.0], "gpt-5": [5.0, 15.0], "claude-sonnet-4-5": [3.0, 15.0]}

NOV_2026 = int(datetime(2026, 11, 2, tzinfo=UTC).timestamp())


def write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def write_text(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def priced(tmp_path: Path) -> Path:
    return write_json(tmp_path / "prices.json", PRICES)


@pytest.fixture()
def price_table() -> dict[str, PriceEntry]:
    return {model: PriceEntry(model, rates[0], rates[1]) for model, rates in PRICES.items()}


# ---------------------------------------------------------------------------
# The original amount-only comparison, kept as regression coverage


def test_compare_amounts_exact_match_is_within_tolerance() -> None:
    result = compare_amounts({"qwen": 6.0}, {"qwen": 6.0}, 0.05)
    assert result["within_tolerance"] is True
    assert result["total_relative_error"] == 0.0
    assert result["models"][0]["within_tolerance"] is True


def test_compare_amounts_flags_an_error_beyond_tolerance() -> None:
    result = compare_amounts({"qwen": 6.0}, {"qwen": 7.0}, 0.05)
    assert result["within_tolerance"] is False
    assert result["models"][0]["relative_error"] == pytest.approx(1 / 7)


def test_compare_amounts_flags_one_bad_model_even_when_the_total_matches() -> None:
    result = compare_amounts({"qwen": 5.0, "gpt": 7.0}, {"qwen": 6.0, "gpt": 6.0}, 0.05)
    assert result["within_tolerance"] is False
    assert result["total_relative_error"] == 0.0


def test_price_bill_uses_the_table_not_the_bills_cost_column() -> None:
    table = {"qwen": PriceEntry("qwen", 2.0, 8.0)}
    totals = price_bill(
        [{"model": "qwen", "input_tokens": 1_000_000, "output_tokens": 0, "cost_usd": 999.0}],
        table,
    )
    assert totals == {"qwen": 2.0}


# ---------------------------------------------------------------------------
# Ledger loading: the real JSONL file and the older JSON array


def ledger_row(model: str, in_tokens: int, out_tokens: int, cost: float, month: str) -> dict:
    return {
        "task_id": "t1",
        "device_id": "d1",
        "model": model,
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "cost_usd": cost,
        "month": month,
    }


def test_load_ledger_reads_the_persisted_jsonl_shape(tmp_path: Path) -> None:
    rows = [
        ledger_row("qwen", 100, 40, 0.5, "2026-11"),
        ledger_row("qwen", 50, 10, 0.25, "2026-11"),
        ledger_row("gpt-5", 7, 3, 1.5, "2026-11"),
    ]
    path = write_text(
        tmp_path / "cost_ledger.jsonl", "".join(json.dumps(row) + "\n" for row in rows)
    )
    totals = load_ledger(path)
    assert totals["qwen"].input_tokens == 150
    assert totals["qwen"].output_tokens == 50
    assert totals["qwen"].cost_usd == pytest.approx(0.75)
    assert totals["gpt-5"].cost_usd == pytest.approx(1.5)


def test_load_ledger_reads_a_json_array(tmp_path: Path) -> None:
    path = write_json(tmp_path / "ledger.json", [ledger_row("qwen", 10, 5, 0.1, "2026-11")])
    totals = load_ledger(path)
    assert totals["qwen"].input_tokens == 10


def test_load_ledger_filters_by_month(tmp_path: Path) -> None:
    rows = [
        ledger_row("qwen", 100, 0, 1.0, "2026-10"),
        ledger_row("qwen", 30, 0, 0.3, "2026-11"),
    ]
    path = write_text(
        tmp_path / "cost_ledger.jsonl", "".join(json.dumps(row) + "\n" for row in rows)
    )
    totals = load_ledger(path, month="2026-11")
    assert totals["qwen"].input_tokens == 30
    assert totals["qwen"].cost_usd == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# Bill importers, one synthetic fixture per supported shape


def test_import_fixture_bill_keeps_its_month_and_amounts(tmp_path: Path) -> None:
    bill = {
        "month": "2026-11",
        "entries": [
            {"model": "qwen", "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.1},
            {"model": "qwen", "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.02},
        ],
    }
    imported = import_bill(write_json(tmp_path / "bill.json", bill))
    assert imported.source == "fixture"
    assert imported.period == "2026-11"
    assert imported.totals["qwen"].input_tokens == 11
    assert imported.totals["qwen"].cost_usd == pytest.approx(0.12)


OPENAI_USAGE = {
    "object": "page",
    "data": [
        {
            "object": "bucket",
            "start_time": NOV_2026,
            "end_time": NOV_2026 + 86400,
            "result": [
                {
                    "object": "organization.usage.completions.result",
                    "input_tokens": 1200,
                    "output_tokens": 300,
                    "num_model_requests": 4,
                    "model": "gpt-5",
                    "project_id": None,
                    "user_id": None,
                    "api_key_id": None,
                    "batch": None,
                }
            ],
        }
    ],
    "has_more": False,
    "next_page": None,
}

OPENAI_COSTS = {
    "object": "page",
    "data": [
        {
            "object": "bucket",
            "start_time": NOV_2026,
            "end_time": NOV_2026 + 86400,
            "result": [
                {
                    "object": "organization.costs.result",
                    "amount": {"value": 4.5, "currency": "usd"},
                    "line_item": None,
                    "project_id": None,
                    "model": "gpt-5",
                }
            ],
        }
    ],
    "has_more": False,
    "next_page": None,
}


def test_import_openai_usage_export(tmp_path: Path) -> None:
    imported = import_bill(write_json(tmp_path / "usage.json", OPENAI_USAGE))
    assert imported.source == "openai-usage"
    assert imported.period == "2026-11"
    totals = imported.totals["gpt-5"]
    assert totals.input_tokens == 1200
    assert totals.output_tokens == 300
    assert totals.cost_usd is None


def test_import_openai_costs_export_and_merge_with_usage(tmp_path: Path) -> None:
    usage = import_bill(write_json(tmp_path / "usage.json", OPENAI_USAGE))
    costs = import_bill(write_json(tmp_path / "costs.json", OPENAI_COSTS))
    assert costs.source == "openai-costs"
    assert costs.totals["gpt-5"].cost_usd == pytest.approx(4.5)
    merged = merge_bills([usage, costs])
    totals = merged.totals["gpt-5"]
    assert totals.input_tokens == 1200
    assert totals.cost_usd == pytest.approx(4.5)
    assert merged.period == "2026-11"


ANTHROPIC_USAGE = {
    "data": [
        {
            "starting_at": "2026-11-01T00:00:00Z",
            "ending_at": "2026-12-01T00:00:00Z",
            "results": [
                {
                    "model": "claude-sonnet-4-5",
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "cache_creation_input_tokens": 200,
                    "cache_read_input_tokens": 300,
                    "service_tier": "standard",
                }
            ],
        }
    ],
    "has_more": False,
    "next_page": None,
}


def test_import_anthropic_usage_report_counts_cache_tokens_as_input(tmp_path: Path) -> None:
    imported = import_bill(write_json(tmp_path / "anthropic.json", ANTHROPIC_USAGE))
    assert imported.source == "anthropic-usage"
    assert imported.period == "2026-11"
    totals = imported.totals["claude-sonnet-4-5"]
    assert totals.input_tokens == 1500  # 1000 + 200 cache creation + 300 cache read
    assert totals.output_tokens == 500
    assert totals.cost_usd is None


def test_import_csv_bill_with_and_without_a_cost_column(tmp_path: Path) -> None:
    with_cost = write_text(
        tmp_path / "bill.csv",
        "model,input_tokens,output_tokens,cost_usd\ngpt-5,1200,300,4.50\nqwen,10,5,0.10\n",
    )
    imported = import_bill(with_cost)
    assert imported.source == "csv"
    assert imported.totals["gpt-5"].cost_usd == pytest.approx(4.5)
    without_cost = write_text(
        tmp_path / "tokens.csv", "model,input_tokens,output_tokens\ngpt-5,1200,300\n"
    )
    assert import_bill(without_cost).totals["gpt-5"].cost_usd is None


def test_import_bill_rejects_an_unrecognised_shape(tmp_path: Path) -> None:
    path = write_json(tmp_path / "mystery.json", {"hello": "world"})
    with pytest.raises(ReconcileError):
        import_bill(path)


# ---------------------------------------------------------------------------
# The comparison: tokens and amounts, bill amounts win over the table


def test_compare_uses_the_bills_own_amount_not_the_price_table(
    tmp_path: Path, price_table: dict[str, PriceEntry]
) -> None:
    # The table would price 1M gpt-5 input tokens at 5.00 USD; the
    # vendor billed 4.50, and the ledger recorded 4.50.  Pricing the
    # bill from the table would report a 10 percent error and fail;
    # using the bill's own amount reconciles exactly.
    ledger = load_ledger(
        write_json(
            tmp_path / "ledger.json",
            [ledger_row("gpt-5", 1_000_000, 0, 4.5, "2026-11")],
        )
    )
    costs = import_bill(write_json(tmp_path / "costs.json", OPENAI_COSTS))
    usage = import_bill(
        write_json(
            tmp_path / "usage.json",
            {
                "object": "page",
                "data": [
                    {
                        "object": "bucket",
                        "start_time": NOV_2026,
                        "end_time": NOV_2026 + 86400,
                        "result": [
                            {
                                "object": "organization.usage.completions.result",
                                "input_tokens": 1_000_000,
                                "output_tokens": 0,
                                "model": "gpt-5",
                            }
                        ],
                    }
                ],
            },
        )
    )
    bill = merge_bills([usage, costs])
    result = compare(ledger, bill, price_table, 0.05)
    row = result["models"][0]
    assert row["bill_usd"] == pytest.approx(4.5)
    assert row["priced_from_table"] is False
    assert result["within_tolerance"] is True


def test_compare_marks_table_pricing_and_compares_tokens(
    tmp_path: Path, price_table: dict[str, PriceEntry]
) -> None:
    ledger = load_ledger(
        write_json(
            tmp_path / "ledger.json",
            [ledger_row("claude-sonnet-4-5", 1500, 500, 0.012, "2026-11")],
        )
    )
    bill = import_bill(write_json(tmp_path / "anthropic.json", ANTHROPIC_USAGE))
    result = compare(ledger, bill, price_table, 0.05)
    row = result["models"][0]
    assert row["priced_from_table"] is True
    assert row["token_relative_error"] == 0.0
    # 1500 in and 500 out at [3.0, 15.0] per million is 0.012 USD.
    assert row["bill_usd"] == pytest.approx(0.012)
    assert result["within_tolerance"] is True
    assert result["bill_period"] == "2026-11"


def test_compare_fails_on_tokens_even_when_amounts_match(
    tmp_path: Path, price_table: dict[str, PriceEntry]
) -> None:
    # Amounts agree exactly; the vendor metered half the tokens the
    # ledger recorded.  The old amount-only check passed this pair.
    ledger = load_ledger(
        write_json(tmp_path / "ledger.json", [ledger_row("qwen", 1_000_000, 0, 2.0, "2026-10")])
    )
    bill = import_bill(
        write_json(
            tmp_path / "bill.json",
            {
                "month": "2026-10",
                "entries": [
                    {"model": "qwen", "input_tokens": 500_000, "output_tokens": 0, "cost_usd": 2.0}
                ],
            },
        )
    )
    result = compare(ledger, bill, price_table, 0.05)
    assert result["models"][0]["cost_relative_error"] == 0.0
    assert result["models"][0]["token_relative_error"] == pytest.approx(1.0)
    assert result["within_tolerance"] is False


# ---------------------------------------------------------------------------
# The command line


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    script = Path(__file__).resolve().parent.parent / "scripts" / "reconcile_costs.py"
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_exits_zero_on_a_matching_fixture(tmp_path: Path, priced: Path) -> None:
    rows = [ledger_row("qwen", 1_000_000, 0, 2.0, "2026-10")]
    ledger = write_json(tmp_path / "ledger.json", rows)
    bill = write_json(tmp_path / "bill.json", {"month": "2026-10", "entries": rows})
    result = run_cli("--ledger", str(ledger), "--bill", str(bill), "--prices", str(priced))
    assert result.returncode == 0
    assert "error 0.00%" in result.stdout


def test_cli_exits_one_on_a_mismatch_beyond_tolerance(tmp_path: Path, priced: Path) -> None:
    rows = [ledger_row("qwen", 1_000_000, 0, 2.0, "2026-10")]
    ledger = write_json(tmp_path / "ledger.json", rows)
    bill = write_json(
        tmp_path / "bill.json",
        {
            "month": "2026-10",
            "entries": [
                {"model": "qwen", "input_tokens": 2_000_000, "output_tokens": 0, "cost_usd": 4.0}
            ],
        },
    )
    result = run_cli("--ledger", str(ledger), "--bill", str(bill), "--prices", str(priced))
    assert result.returncode == 1
    assert "error 50.00%" in result.stdout


def test_cli_reads_the_jsonl_ledger_and_two_bill_files(tmp_path: Path, priced: Path) -> None:
    rows = [ledger_row("gpt-5", 1200, 300, 4.5, "2026-11")]
    ledger = write_text(
        tmp_path / "cost_ledger.jsonl", "".join(json.dumps(row) + "\n" for row in rows)
    )
    usage = write_json(tmp_path / "usage.json", OPENAI_USAGE)
    costs = write_json(tmp_path / "costs.json", OPENAI_COSTS)
    result = run_cli(
        "--ledger",
        str(ledger),
        "--bill",
        str(usage),
        "--bill",
        str(costs),
        "--prices",
        str(priced),
        "--month",
        "2026-11",
        "--json",
    )
    assert result.returncode == 0
    parsed = json.loads(result.stdout)
    assert parsed["within_tolerance"] is True
    assert parsed["bill_source"] == "openai-usage+openai-costs"
    assert parsed["bill_period"] == "2026-11"
    assert parsed["models"][0]["priced_from_table"] is False


def test_cli_month_filter_ignores_other_months(tmp_path: Path, priced: Path) -> None:
    rows = [
        ledger_row("qwen", 999_000_000, 0, 1998.0, "2026-10"),
        ledger_row("qwen", 1_000_000, 0, 2.0, "2026-11"),
    ]
    ledger = write_text(
        tmp_path / "cost_ledger.jsonl", "".join(json.dumps(row) + "\n" for row in rows)
    )
    bill = write_json(
        tmp_path / "bill.json",
        {
            "month": "2026-11",
            "entries": [
                {"model": "qwen", "input_tokens": 1_000_000, "output_tokens": 0, "cost_usd": 2.0}
            ],
        },
    )
    unfiltered = run_cli("--ledger", str(ledger), "--bill", str(bill), "--prices", str(priced))
    assert unfiltered.returncode == 1
    filtered = run_cli(
        "--ledger",
        str(ledger),
        "--bill",
        str(bill),
        "--prices",
        str(priced),
        "--month",
        "2026-11",
    )
    assert filtered.returncode == 0


def test_cli_exits_two_on_a_missing_file(tmp_path: Path, priced: Path) -> None:
    result = run_cli(
        "--ledger",
        str(tmp_path / "nope.json"),
        "--bill",
        str(tmp_path / "nope2.json"),
        "--prices",
        str(priced),
    )
    assert result.returncode == 2


def test_cli_exits_two_on_an_unrecognised_bill(tmp_path: Path, priced: Path) -> None:
    ledger = write_json(tmp_path / "ledger.json", [ledger_row("qwen", 1, 1, 0.1, "2026-11")])
    bill = write_json(tmp_path / "mystery.json", {"hello": "world"})
    result = run_cli("--ledger", str(ledger), "--bill", str(bill), "--prices", str(priced))
    assert result.returncode == 2
    assert "unrecognised" in result.stderr
