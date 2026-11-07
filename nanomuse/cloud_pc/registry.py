"""The local record of cloud machines and what they cost.

One JSON file (``cloud_pc.json`` in the data directory) holds two lists:

- ``machines``: every machine the tools opened, with its size, when it was
  created and, once destroyed, when. A machine with no ``destroyed_at`` is
  live as far as this device knows; the provider stays the source of truth
  and :class:`~nanomuse.tools.cloud_pc.CloudStatus` refreshes from it.
- ``spend``: one entry per destroyed machine, written when the destroy tool
  finalises it: billed seconds and the cost at the configured rate. Month
  totals over these entries are what the budget gate reads.

Cost model, stated plainly: the billed figure is wall-clock time from
create to destroy at ``hourly_rate_per_vcpu_usd`` times the vCPU count.
That rate is a configuration value, not a provider quote; the default is
the effective rate measured on the account this was built against. A
machine the provider auto-stops may bill less than this record says;
reconciling against the provider bill is the A2 step, and this record is
the number to reconcile, not the bill itself.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MachineRecord:
    id: str
    provider: str
    name: str
    cpu: int
    memory: int
    disk: int
    created_at: float
    destroyed_at: float | None = None

    @property
    def live(self) -> bool:
        return self.destroyed_at is None

    def billed_seconds(self, now: float | None = None) -> float:
        end = self.destroyed_at if self.destroyed_at is not None else (now or time.time())
        return max(0.0, end - self.created_at)


@dataclass
class SpendEntry:
    machine_id: str
    month: str
    seconds: float
    cost_usd: float


@dataclass
class CloudRegistry:
    path: Path
    machines: list[MachineRecord] = field(default_factory=list)
    spend: list[SpendEntry] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> CloudRegistry:
        registry = cls(path=path)
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return registry
            for item in raw.get("machines", []):
                registry.machines.append(MachineRecord(**item))
            for item in raw.get("spend", []):
                registry.spend.append(SpendEntry(**item))
        return registry

    def save(self) -> None:
        payload = {
            "machines": [vars(m) for m in self.machines],
            "spend": [vars(s) for s in self.spend],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, machine: MachineRecord) -> None:
        self.machines.append(machine)
        self.save()

    def get(self, machine_id: str) -> MachineRecord | None:
        for machine in self.machines:
            if machine.id == machine_id:
                return machine
        return None

    def live_machines(self) -> list[MachineRecord]:
        return [m for m in self.machines if m.live]

    def finalise_destroy(
        self, machine: MachineRecord, *, rate_per_vcpu_hour: float, now: float
    ) -> SpendEntry:
        """Mark a machine destroyed and write its spend entry. Returns the entry."""
        machine.destroyed_at = now
        seconds = machine.billed_seconds(now)
        cost = seconds / 3600.0 * rate_per_vcpu_hour * machine.cpu
        entry = SpendEntry(
            machine_id=machine.id,
            month=time.strftime("%Y-%m", time.localtime(now)),
            seconds=seconds,
            cost_usd=cost,
        )
        self.spend.append(entry)
        self.save()
        return entry

    def month_spend(self, month: str) -> float:
        return sum(s.cost_usd for s in self.spend if s.month == month)


def record_from_provider(data: dict[str, Any], provider: str, now: float) -> MachineRecord:
    return MachineRecord(
        id=str(data.get("id", "")),
        provider=provider,
        name=str(data.get("name", "")),
        cpu=int(data.get("cpu", 1)),
        memory=int(data.get("memory", 1)),
        disk=int(data.get("disk", 3)),
        created_at=now,
    )
