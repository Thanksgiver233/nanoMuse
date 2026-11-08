"""The idle sweeper: the companion-side patrol that destroys forgotten machines.

A machine bills from create to destroy, and the tools rely on the agent
remembering to destroy it. This is the backstop (Phase 9, step A4): while
the always-on companion runs, each of its heartbeats offers the sweeper
one pass over the record. A live machine whose last activity is older
than ``idle_destroy_minutes`` is destroyed through the provider, and the
destroy writes the usual spend entry, so a swept machine costs exactly
what a hand-destroyed one costs and the budget gate reads the same
record either way.

The two idle numbers are different on purpose. ``idle_timeout_minutes``
(30 by default) is the provider's own auto-stop: a pause, after which
the machine can be woken and worked in again. ``idle_destroy_minutes``
(240 by default) is this sweep's line: four idle hours, long past the
pause, is a machine nobody is coming back to.

A destroy the provider refuses is reported and left live, so the next
pass tries again; a pass never invents a destroy the provider did not
confirm. When a pass leaves the month's recorded spend at or over the
budget, the report says so, once per month: the gate in hard mode
already refuses new machines from that point, and the report is how a
person hears the line was crossed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from nanomuse.cloud_pc.daytona import CloudError
from nanomuse.cloud_pc.gate import CloudBudgetGate
from nanomuse.cloud_pc.provider import CloudProvider
from nanomuse.cloud_pc.registry import CloudRegistry


@dataclass
class SweptMachine:
    machine_id: str
    name: str
    idle_minutes: float
    seconds: float
    cost_usd: float


@dataclass
class SweepReport:
    destroyed: list[SweptMachine] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    month: str = ""
    month_spend_usd: float = 0.0
    budget_line: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.destroyed or self.budget_line)

    def lines(self) -> list[str]:
        out = [
            f"cloud sweep: destroyed idle machine {s.machine_id} ({s.name}) after "
            f"{s.idle_minutes:.0f} idle minutes; it ran {s.seconds / 60:.1f} minutes and "
            f"its recorded cost is {s.cost_usd:.4f} USD"
            for s in self.destroyed
        ]
        if self.budget_line:
            out.append(self.budget_line)
        return out


class CloudSweeper:
    """One patrol over the cloud record, built over the tools' own parts."""

    def __init__(
        self,
        *,
        client: CloudProvider,
        registry: CloudRegistry,
        gate: CloudBudgetGate,
        rate_per_vcpu_hour: float,
        idle_destroy_minutes: int,
    ) -> None:
        self.client = client
        self.registry = registry
        self.gate = gate
        self.rate_per_vcpu_hour = rate_per_vcpu_hour
        self.idle_destroy_seconds = max(1, idle_destroy_minutes) * 60.0
        self._budget_reported_month: str | None = None

    async def sweep(self, now: float | None = None) -> SweepReport:
        moment = time.time() if now is None else now
        report = SweepReport(month=self.gate.month(moment))
        for machine in list(self.registry.live_machines()):
            idle = machine.idle_seconds(moment)
            if idle < self.idle_destroy_seconds:
                continue
            try:
                await self.client.delete_machine(machine.id)
            except CloudError as exc:
                report.errors.append(f"{machine.id}: {exc}")
                continue
            entry = self.registry.finalise_destroy(
                machine, rate_per_vcpu_hour=self.rate_per_vcpu_hour, now=moment
            )
            report.destroyed.append(
                SweptMachine(
                    machine_id=machine.id,
                    name=machine.name,
                    idle_minutes=idle / 60.0,
                    seconds=entry.seconds,
                    cost_usd=entry.cost_usd,
                )
            )
        report.month_spend_usd = self.registry.month_spend(report.month)
        budget = self.gate.budget_usd
        if (
            budget is not None
            and self.gate.over_budget(report.month)
            and self._budget_reported_month != report.month
        ):
            self._budget_reported_month = report.month
            report.budget_line = (
                f"cloud sweep: recorded cloud spend in {report.month} is "
                f"{report.month_spend_usd:.4f} USD, at or over the "
                f"{budget:.2f} USD monthly budget; in hard mode the "
                "gate now refuses new machines"
            )
        return report
