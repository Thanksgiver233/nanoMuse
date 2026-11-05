"""Reconcile the cost ledger against a vendor bill or usage export.

The ledger side is the real file now: ``load_ledger`` reads the JSONL
that ``nanomuse.cost.store`` appends (one ``field_row`` per line) and
the older JSON array shape ``CostLedger.to_rows`` writes.  The bill
side accepts the hand-written fixture shape plus saved exports from
vendor admin APIs, recognised by the shape of their contents:

* the fixture JSON: ``{"month": ..., "entries": [{model, input_tokens,
  output_tokens, cost_usd}]}``;
* an OpenAI usage export (the Admin API completions usage endpoint,
  saved to a file): token counts per model;
* an OpenAI costs export (the Admin API costs endpoint): amounts per
  model.  Pass it together with the usage export (``--bill`` repeats)
  and the two merge by model;
* an Anthropic usage report (the Admin API usage report endpoint):
  token counts per model.  Cache creation and cache read tokens are
  counted as input tokens, because the vendor bills them as input;
* a DeepSeek console export, which comes as a pair of CSVs: the
  ``amount`` file carries one row per day, model, and metering type
  (cache-hit and cache-miss input tokens count as input, as the
  vendor bills them as input), and the ``cost`` file carries the
  daily amounts per model.  Pass both (``--bill`` repeats) and they
  merge by model.  The amounts are yuan (CNY), so the amount check
  only lines up when the price table is kept in yuan as well; the
  token check does not care about currency;
* a plain CSV with ``model,input_tokens,output_tokens,cost_usd``
  columns (the cost column may be absent).

Nothing here talks to a vendor: the exports are files the person saved
from a console or an API call of their own.  When a bill carries its
own amounts, those amounts are what the ledger is checked against:
that is the real reconciliation.  When it does not, the bill's tokens
are priced with the person's own price table and the result is marked
``priced_from_table``, which checks the vendor's metering of tokens
but not the vendor's prices.

The tooling reads real files now, and a real DeepSeek export has been
parsed and totalled by the importer.  A full reconciliation has still
not run: it takes a ledger written by real nanoMuse runs to set
against a bill, and there is none yet, so the reconciliation Gate
item stays unmet.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nanomuse.cost.ledger import PriceEntry


class ReconcileError(ValueError):
    """A ledger or bill file could not be read or recognised."""


@dataclass(frozen=True)
class LedgerTotals:
    """Token counts and recorded cost for one model, summed."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class BillTotals:
    """Token counts and billed cost for one model, summed.

    ``cost_usd`` is None when the bill carried no amount for the model
    at all; a partial amount (some rows priced, some not) is summed
    from the rows that carried one and still counts as a bill amount.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


@dataclass(frozen=True)
class BillImport:
    """One or more bill files, normalised and summed by model."""

    totals: dict[str, BillTotals]
    period: str | None  # "YYYY-MM" when every file agrees on one month
    periods: tuple[str, ...]  # every distinct month the files reported
    source: str  # the importer that recognised the shape


@dataclass
class _BillAccum:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    has_cost: bool = False

    def add(
        self,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float | None,
    ) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        if cost_usd is not None:
            self.cost_usd += cost_usd
            self.has_cost = True

    def totals(self) -> BillTotals:
        return BillTotals(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=self.cost_usd if self.has_cost else None,
        )


def _accumulate(
    accums: dict[str, _BillAccum],
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float | None,
) -> None:
    accums.setdefault(model, _BillAccum()).add(input_tokens, output_tokens, cost_usd)


def _finish(accums: dict[str, _BillAccum]) -> dict[str, BillTotals]:
    return {model: accum.totals() for model, accum in sorted(accums.items())}


def _int(value: Any) -> int:
    return int(value or 0)


def _month_of(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m")


# ---------------------------------------------------------------------------
# Prices and the ledger side


def load_prices(path: Path) -> dict[str, PriceEntry]:
    """Read a price table: ``{model: [input_per_million, output_per_million]}``."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        str(model): PriceEntry(str(model), float(rates[0]), float(rates[1]))
        for model, rates in raw.items()
    }


def load_ledger(path: Path, month: str | None = None) -> dict[str, LedgerTotals]:
    """Sum ledger rows by model, from a JSONL file or a JSON array.

    The JSONL shape is the one ``PersistentCostLedger`` appends, one
    ``field_row`` object per line; the JSON shape is a bare array of
    the same rows (an object with an ``entries`` list is accepted
    too).  ``month`` keeps only rows whose ``month`` field equals it.
    """
    text = Path(path).read_text(encoding="utf-8")
    rows = _ledger_rows(text, path)
    totals: dict[str, LedgerTotals] = {}
    for row in rows:
        if month is not None and str(row.get("month", "")) != month:
            continue
        model = row.get("model")
        if model is None:
            raise ReconcileError(f"{path}: a ledger row has no model field")
        model = str(model)
        current = totals.get(model, LedgerTotals())
        totals[model] = LedgerTotals(
            input_tokens=current.input_tokens + _int(row.get("input_tokens")),
            output_tokens=current.output_tokens + _int(row.get("output_tokens")),
            cost_usd=current.cost_usd + float(row.get("cost_usd") or 0.0),
        )
    return totals


def _ledger_rows(text: str, path: Path) -> list[dict[str, Any]]:
    stripped = text.strip()
    if not stripped:
        raise ReconcileError(f"{path}: the ledger file is empty")
    parsed: Any = None
    try:
        parsed = json.loads(stripped)
    except ValueError:
        parsed = None
    if isinstance(parsed, list):
        return _require_rows(parsed, path)
    if isinstance(parsed, dict) and isinstance(parsed.get("entries"), list):
        return _require_rows(parsed["entries"], path)
    rows: list[Any] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as exc:
            raise ReconcileError(f"{path}: not JSON, a JSON array, or JSONL: {exc}") from exc
    return _require_rows(rows, path)


def _require_rows(rows: list[Any], path: Path) -> list[dict[str, Any]]:
    for row in rows:
        if not isinstance(row, dict):
            raise ReconcileError(f"{path}: a ledger row is not a JSON object")
    return rows


# ---------------------------------------------------------------------------
# Bill importers: each returns normalised totals summed by model


def import_bill(path: Path) -> BillImport:
    """Read one bill file, recognising its format from its contents."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return _import_csv(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ReconcileError(f"{path}: not valid JSON and not a .csv file: {exc}") from exc
    if not isinstance(data, dict):
        raise ReconcileError(f"{path}: unrecognised bill format (top level is not an object)")
    if isinstance(data.get("entries"), list):
        return _import_fixture(data)
    buckets = data.get("data")
    if isinstance(buckets, list) and buckets and isinstance(buckets[0], dict):
        if "result" in buckets[0]:
            return _import_openai(data, path)
        if "results" in buckets[0]:
            return _import_anthropic(data, path)
    raise ReconcileError(
        f"{path}: unrecognised bill format (no entries, result, or results shape found)"
    )


def merge_bills(imports: list[BillImport]) -> BillImport:
    """Merge several imported bills (for example usage plus costs)."""
    accums: dict[str, _BillAccum] = {}
    periods: list[str] = []
    sources: list[str] = []
    for bill in imports:
        sources.append(bill.source)
        for period in bill.periods:
            if period not in periods:
                periods.append(period)
        for model, totals in bill.totals.items():
            _accumulate(accums, model, totals.input_tokens, totals.output_tokens, totals.cost_usd)
    return BillImport(
        totals=_finish(accums),
        period=periods[0] if len(periods) == 1 else None,
        periods=tuple(sorted(periods)),
        source="+".join(sources),
    )


def _import_fixture(data: dict[str, Any]) -> BillImport:
    accums: dict[str, _BillAccum] = {}
    for entry in data["entries"]:
        if not isinstance(entry, dict) or entry.get("model") is None:
            raise ReconcileError("fixture bill: an entry has no model field")
        cost = entry.get("cost_usd")
        _accumulate(
            accums,
            str(entry["model"]),
            _int(entry.get("input_tokens")),
            _int(entry.get("output_tokens")),
            float(cost) if cost is not None else None,
        )
    month = data.get("month")
    periods = (str(month),) if isinstance(month, str) and month else ()
    return BillImport(
        totals=_finish(accums),
        period=periods[0] if periods else None,
        periods=periods,
        source="fixture",
    )


def _import_openai(data: dict[str, Any], path: Path) -> BillImport:
    accums: dict[str, _BillAccum] = {}
    months: set[str] = set()
    saw_usage = False
    saw_costs = False
    for bucket in data["data"]:
        if not isinstance(bucket, dict):
            continue
        start_time = bucket.get("start_time")
        if isinstance(start_time, int):
            months.add(_month_of(start_time))
        for result in bucket.get("result") or []:
            if not isinstance(result, dict):
                continue
            model = str(result.get("model") or "unknown")
            kind = str(result.get("object") or "")
            if kind == "organization.usage.completions.result" or (
                "input_tokens" in result and "amount" not in result
            ):
                saw_usage = True
                _accumulate(
                    accums,
                    model,
                    _int(result.get("input_tokens")),
                    _int(result.get("output_tokens")),
                    None,
                )
            elif kind == "organization.costs.result" or "amount" in result:
                saw_costs = True
                amount = result.get("amount")
                if not isinstance(amount, dict) or amount.get("value") is None:
                    raise ReconcileError(f"{path}: a costs result has no amount value")
                currency = str(amount.get("currency") or "usd").lower()
                if currency != "usd":
                    raise ReconcileError(
                        f"{path}: costs are in {currency}, and only usd is reconciled"
                    )
                _accumulate(accums, model, 0, 0, float(amount["value"]))
    if not (saw_usage or saw_costs):
        raise ReconcileError(f"{path}: no OpenAI usage or costs results found")
    source = (
        "openai-usage+costs"
        if saw_usage and saw_costs
        else ("openai-usage" if saw_usage else "openai-costs")
    )
    periods = tuple(sorted(months))
    return BillImport(
        totals=_finish(accums),
        period=periods[0] if len(periods) == 1 else None,
        periods=periods,
        source=source,
    )


def _import_anthropic(data: dict[str, Any], path: Path) -> BillImport:
    accums: dict[str, _BillAccum] = {}
    months: set[str] = set()
    for bucket in data["data"]:
        if not isinstance(bucket, dict):
            continue
        starting_at = bucket.get("starting_at")
        if isinstance(starting_at, str) and len(starting_at) >= 7:
            months.add(starting_at[:7])
        for result in bucket.get("results") or []:
            if not isinstance(result, dict) or result.get("model") is None:
                continue
            # Cache tokens are billed as input by the vendor, so they
            # count toward input_tokens here; the ledger meters a call
            # the same way, one input figure per call.
            input_tokens = (
                _int(result.get("input_tokens"))
                + _int(result.get("cache_creation_input_tokens"))
                + _int(result.get("cache_read_input_tokens"))
            )
            _accumulate(
                accums,
                str(result["model"]),
                input_tokens,
                _int(result.get("output_tokens")),
                None,
            )
    if not accums:
        raise ReconcileError(f"{path}: no Anthropic usage results found")
    periods = tuple(sorted(months))
    return BillImport(
        totals=_finish(accums),
        period=periods[0] if len(periods) == 1 else None,
        periods=periods,
        source="anthropic-usage",
    )


def _import_csv(path: Path) -> BillImport:
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ReconcileError(f"{path}: the CSV has no header row")
    columns = {name.strip().lstrip("\ufeff") for name in reader.fieldnames if name}
    if {"model", "type", "amount"} <= columns:
        return _import_deepseek_amount(path, reader)
    if {"model", "wallet_type", "cost"} <= columns:
        return _import_deepseek_cost(path, reader)
    missing = {"model", "input_tokens", "output_tokens"} - columns
    if missing:
        raise ReconcileError(f"{path}: the CSV is missing columns: {sorted(missing)}")
    accums: dict[str, _BillAccum] = {}
    for raw_row in reader:
        row = {(key.strip() if key else key): value for key, value in raw_row.items()}
        model = (row.get("model") or "").strip()
        if not model:
            continue
        cost_cell = (row.get("cost_usd") or "").strip()
        _accumulate(
            accums,
            model,
            _int(row.get("input_tokens")),
            _int(row.get("output_tokens")),
            float(cost_cell) if cost_cell else None,
        )
    return BillImport(totals=_finish(accums), period=None, periods=(), source="csv")


def _clean_row(raw_row: dict[str | None, str | None]) -> dict[str, str]:
    return {
        (key.strip().lstrip("\ufeff") if key else ""): (value or "")
        for key, value in raw_row.items()
    }


def _periods_of(rows: list[dict[str, str]]) -> tuple[str, ...]:
    months = {row["start_time_iso"][:7] for row in rows if len(row.get("start_time_iso", "")) >= 7}
    return tuple(sorted(months))


def _import_deepseek_amount(path: Path, reader: csv.DictReader[str]) -> BillImport:
    """The DeepSeek amount file: one row per day, model and metering type.

    Cache-hit and cache-miss input tokens both count as input, because
    the vendor bills them as input.  Request counts are not tokens and
    are skipped.  The file carries no summed amount per model that the
    reconciliation could check, so amounts come from the matching
    cost file when it is given as well.
    """
    accums: dict[str, _BillAccum] = {}
    rows: list[dict[str, str]] = []
    for raw_row in reader:
        row = _clean_row(raw_row)
        rows.append(row)
        model = row.get("model", "").strip()
        if not model:
            continue
        kind = row.get("type", "").strip()
        amount = _int(row.get("amount"))
        if kind == "output_tokens":
            _accumulate(accums, model, 0, amount, None)
        elif kind in ("input_cache_miss_tokens", "input_cache_hit_tokens", "input_tokens"):
            _accumulate(accums, model, amount, 0, None)
    periods = _periods_of(rows)
    return BillImport(
        totals=_finish(accums),
        period=periods[0] if len(periods) == 1 else None,
        periods=periods,
        source="deepseek-amount",
    )


def _import_deepseek_cost(path: Path, reader: csv.DictReader[str]) -> BillImport:
    """The DeepSeek cost file: one row per day and model, amounts in yuan."""
    accums: dict[str, _BillAccum] = {}
    rows: list[dict[str, str]] = []
    for raw_row in reader:
        row = _clean_row(raw_row)
        rows.append(row)
        model = row.get("model", "").strip()
        if not model:
            continue
        cost_cell = row.get("cost", "").strip()
        _accumulate(accums, model, 0, 0, float(cost_cell) if cost_cell else None)
    periods = _periods_of(rows)
    return BillImport(
        totals=_finish(accums),
        period=periods[0] if len(periods) == 1 else None,
        periods=periods,
        source="deepseek-cost-cny",
    )


# ---------------------------------------------------------------------------
# Comparison


def _relative_error(ledger: float, bill: float) -> float:
    if bill == 0.0:
        return 0.0 if ledger == 0.0 else 1.0
    return abs(ledger - bill) / bill


def compare(
    ledger_totals: dict[str, LedgerTotals],
    bill: BillImport,
    prices: dict[str, PriceEntry],
    tolerance: float,
) -> dict[str, Any]:
    """Compare ledger totals against a bill, per model and in total.

    Token counts are always compared directly.  Amounts come from the
    bill itself when the bill carried them; when it did not, the
    bill's tokens are priced with ``prices`` and the row is marked
    ``priced_from_table``.
    """
    models = sorted(set(ledger_totals) | set(bill.totals))
    rows: list[dict[str, Any]] = []
    for model in models:
        ledger = ledger_totals.get(model, LedgerTotals())
        billed = bill.totals.get(model, BillTotals())
        token_error = max(
            _relative_error(ledger.input_tokens, billed.input_tokens),
            _relative_error(ledger.output_tokens, billed.output_tokens),
        )
        if billed.cost_usd is not None:
            bill_usd = billed.cost_usd
            priced_from_table = False
        else:
            price = prices.get(model)
            bill_usd = price.cost(billed.input_tokens, billed.output_tokens) if price else 0.0
            priced_from_table = True
        cost_error = _relative_error(ledger.cost_usd, bill_usd)
        rows.append(
            {
                "model": model,
                "ledger_input_tokens": ledger.input_tokens,
                "ledger_output_tokens": ledger.output_tokens,
                "bill_input_tokens": billed.input_tokens,
                "bill_output_tokens": billed.output_tokens,
                "token_relative_error": round(token_error, 6),
                "ledger_usd": round(ledger.cost_usd, 6),
                "bill_usd": round(bill_usd, 6),
                "cost_relative_error": round(cost_error, 6),
                "priced_from_table": priced_from_table,
                "within_tolerance": token_error <= tolerance and cost_error <= tolerance,
            }
        )
    total_ledger_tokens_in = sum(t.input_tokens for t in ledger_totals.values())
    total_bill_tokens_in = sum(t.input_tokens for t in bill.totals.values())
    total_ledger_tokens_out = sum(t.output_tokens for t in ledger_totals.values())
    total_bill_tokens_out = sum(t.output_tokens for t in bill.totals.values())
    total_token_error = max(
        _relative_error(total_ledger_tokens_in, total_bill_tokens_in),
        _relative_error(total_ledger_tokens_out, total_bill_tokens_out),
    )
    total_ledger_usd = sum(t.cost_usd for t in ledger_totals.values())
    total_bill_usd = sum(float(row["bill_usd"]) for row in rows)
    total_cost_error = _relative_error(total_ledger_usd, total_bill_usd)
    return {
        "models": rows,
        "bill_source": bill.source,
        "bill_period": bill.period,
        "bill_periods": list(bill.periods),
        "total_ledger_input_tokens": total_ledger_tokens_in,
        "total_ledger_output_tokens": total_ledger_tokens_out,
        "total_bill_input_tokens": total_bill_tokens_in,
        "total_bill_output_tokens": total_bill_tokens_out,
        "total_token_relative_error": round(total_token_error, 6),
        "total_ledger_usd": round(total_ledger_usd, 6),
        "total_bill_usd": round(total_bill_usd, 6),
        "total_cost_relative_error": round(total_cost_error, 6),
        "total_relative_error": round(max(total_token_error, total_cost_error), 6),
        "within_tolerance": total_token_error <= tolerance
        and total_cost_error <= tolerance
        and all(row["within_tolerance"] for row in rows),
    }


# ---------------------------------------------------------------------------
# The original fixture arithmetic, kept for the callers that used it


def price_bill(entries: list[dict[str, Any]], prices: dict[str, PriceEntry]) -> dict[str, float]:
    """Price rows with the table, falling back to each row's own cost.

    This is the comparison the first version of the reconcile script
    made: both sides under one price table, so it measures arithmetic
    only.  ``compare`` above is the real check; this stays because
    existing callers import it.
    """
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


def compare_amounts(
    ledger_totals: dict[str, float],
    bill_totals: dict[str, float],
    tolerance: float,
) -> dict[str, Any]:
    """The original amount-only comparison, kept under a clearer name."""
    models = sorted(set(ledger_totals) | set(bill_totals))
    rows = []
    for model in models:
        ledger = ledger_totals.get(model, 0.0)
        billed = bill_totals.get(model, 0.0)
        error = 0.0 if billed == 0.0 else abs(ledger - billed) / billed
        rows.append(
            {
                "model": model,
                "ledger_usd": round(ledger, 6),
                "bill_usd": round(billed, 6),
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
        "within_tolerance": total_error <= tolerance
        and all(row["within_tolerance"] for row in rows),
    }


# Re-exported so the field name reads naturally at call sites.
__all__ = [
    "BillImport",
    "BillTotals",
    "LedgerTotals",
    "ReconcileError",
    "compare",
    "compare_amounts",
    "import_bill",
    "load_ledger",
    "load_prices",
    "merge_bills",
    "price_bill",
]
