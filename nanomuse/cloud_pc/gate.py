"""The cloud budget gate: off, warn, or hard, over the registry's spend record.

The same three modes as the monthly budget gate for model spend
(:mod:`nanomuse.cost.gate`), applied to cloud machines: opening a machine
is where the money commits, so the check happens in the create tool,
before the provider is called.

- ``off``: no gate. The registry still records.
- ``warn``: crossing the configured monthly budget adds a warning to the
  create result and work continues.
- ``hard``: once the month's recorded cloud spend reaches the budget, a
  new machine is refused with :class:`CloudGateBlocked`. The message names
  the month, the budget and the spend, as a fact to act on.

The gate reads finalised spend only: a machine still running has not
written its entry yet, so a hard gate can be crossed mid-flight by the
machine already open. ``max_machines`` in the create tool is the bound
that keeps that gap small.
"""

from __future__ import annotations

import time
from typing import Literal

from nanomuse.cloud_pc.registry import CloudRegistry

GateMode = Literal["off", "warn", "hard"]
GATE_MODES: tuple[str, ...] = ("off", "warn", "hard")


class CloudGateBlocked(RuntimeError):
    """Raised in hard mode when a new machine would open over the monthly budget."""

    def __init__(self, month: str, budget_usd: float, spent_usd: float) -> None:
        self.month = month
        self.budget_usd = budget_usd
        self.spent_usd = spent_usd
        super().__init__(
            f"monthly cloud budget reached: {spent_usd:.2f} USD recorded in {month} "
            f"against a {budget_usd:.2f} USD budget; the cloud gate is set to hard, "
            "so no new machine opens until the budget is raised or the mode is changed"
        )


class CloudBudgetGate:
    def __init__(
        self, registry: CloudRegistry, budget_usd: float | None, mode: GateMode = "warn"
    ) -> None:
        if mode not in GATE_MODES:
            raise ValueError(f"unknown cloud gate mode: {mode!r} (expected one of {GATE_MODES})")
        self.registry = registry
        self.budget_usd = budget_usd
        self.mode: GateMode = mode

    def month(self, now: float | None = None) -> str:
        return time.strftime("%Y-%m", time.localtime(time.time() if now is None else now))

    def over_budget(self, month: str) -> bool:
        return self.budget_usd is not None and self.registry.month_spend(month) >= self.budget_usd

    def check_before_create(self, now: float | None = None) -> str | None:
        """Refuse in hard mode; return a warning line in warn mode; else None."""
        if self.mode == "off" or self.budget_usd is None:
            return None
        month = self.month(now)
        spent = self.registry.month_spend(month)
        if spent < self.budget_usd:
            return None
        if self.mode == "hard":
            raise CloudGateBlocked(month, self.budget_usd, spent)
        return (
            f"warning: cloud spend in {month} is {spent:.2f} USD, at or over the "
            f"{self.budget_usd:.2f} USD monthly budget (gate mode: warn, continuing)"
        )
