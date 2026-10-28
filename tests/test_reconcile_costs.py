"""Reconcile script tests: fixture bills only, error math with tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from reconcile_costs import compare, price_bill  # noqa: E402

PRICES = {"qwen": [2.0, 8.0], "gpt": [5.0, 15.0]}


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture()
def priced(tmp_path: Path) -> Path:
    prices = tmp_path / "prices.json"
    write_json(prices, PRICES)
    return prices


def test_compare_exact_match_is_within_tolerance() -> None:
    result = compare({"qwen": 6.0}, {"qwen": 6.0}, 0.05)
    assert result["within_tolerance"] is True
    assert result["total_relative_error"] == 0.0
    assert result["models"][0]["within_tolerance"] is True


def test_compare_flags_an_error_beyond_tolerance() -> None:
    result = compare({"qwen": 6.0}, {"qwen": 7.0}, 0.05)
    assert result["within_tolerance"] is False
    assert result["models"][0]["relative_error"] == pytest.approx(1 / 7)


def test_compare_flags_one_bad_model_even_when_the_total_matches() -> None:
    result = compare({"qwen": 5.0, "gpt": 7.0}, {"qwen": 6.0, "gpt": 6.0}, 0.05)
    assert result["within_tolerance"] is False
    assert result["total_relative_error"] == 0.0


def test_price_bill_uses_the_table_not_the_bills_cost_column() -> None:
    from nanomuse.cost.ledger import PriceEntry

    table = {"qwen": PriceEntry("qwen", 2.0, 8.0)}
    totals = price_bill(
        [{"model": "qwen", "input_tokens": 1_000_000, "output_tokens": 0, "cost_usd": 999.0}],
        table,
    )
    assert totals == {"qwen": 2.0}


def run_cli(
    tmp_path: Path, ledger_rows: object, bill: object, priced: Path, extra: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
    ledger_path = tmp_path / "ledger.json"
    bill_path = tmp_path / "bill.json"
    write_json(ledger_path, ledger_rows)
    write_json(bill_path, bill)
    script = Path(__file__).resolve().parent.parent / "scripts" / "reconcile_costs.py"
    return subprocess.run(
        [
            sys.executable,
            str(script),
            "--ledger",
            str(ledger_path),
            "--bill",
            str(bill_path),
            "--prices",
            str(priced),
            *(extra or []),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_exits_zero_on_a_matching_fixture(tmp_path: Path, priced: Path) -> None:
    rows = [{"model": "qwen", "input_tokens": 1_000_000, "output_tokens": 0, "cost_usd": 2.0}]
    bill = {"month": "2026-10", "entries": list(rows)}
    result = run_cli(tmp_path, rows, bill, priced)
    assert result.returncode == 0
    assert "error 0.00%" in result.stdout


def test_cli_exits_one_on_a_mismatch_beyond_tolerance(tmp_path: Path, priced: Path) -> None:
    rows = [{"model": "qwen", "input_tokens": 1_000_000, "output_tokens": 0, "cost_usd": 2.0}]
    bill = {
        "month": "2026-10",
        "entries": [
            {"model": "qwen", "input_tokens": 2_000_000, "output_tokens": 0, "cost_usd": 4.0}
        ],
    }
    result = run_cli(tmp_path, rows, bill, priced)
    assert result.returncode == 1
    assert "error 50.00%" in result.stdout


def test_cli_json_output_parses(tmp_path: Path, priced: Path) -> None:
    rows = [{"model": "qwen", "input_tokens": 1_000_000, "output_tokens": 0, "cost_usd": 2.0}]
    bill = {"month": "2026-10", "entries": list(rows)}
    result = run_cli(tmp_path, rows, bill, priced, ["--json"])
    assert result.returncode == 0
    parsed = json.loads(result.stdout)
    assert parsed["within_tolerance"] is True


def test_cli_exits_two_on_a_missing_file(tmp_path: Path, priced: Path) -> None:
    script = Path(__file__).resolve().parent.parent / "scripts" / "reconcile_costs.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--ledger",
            str(tmp_path / "nope.json"),
            "--bill",
            str(tmp_path / "nope2.json"),
            "--prices",
            str(priced),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
