"""The idle sweeper (Phase 9, A4): forgotten machines are destroyed, on the record.

A stub provider stands in for both vendors: the sweeper talks to the
provider protocol only, so one stub covers the shape. All figures are
synthetic: a fixed clock, backdated records and a round rate, never a
real account's numbers. The companion tick that runs the sweep in the
server is covered at the end, through the real CompanionService.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from nanomuse.cloud_pc.daytona import CloudError
from nanomuse.cloud_pc.gate import CloudBudgetGate
from nanomuse.cloud_pc.provider import MachineInfo
from nanomuse.cloud_pc.registry import CloudRegistry, MachineRecord, SpendEntry
from nanomuse.cloud_pc.sweeper import CloudSweeper
from nanomuse.companion.runtime import CompanionRuntime
from nanomuse.companion.service import CompanionService
from nanomuse.tools.cloud_pc import build_cloud_tools

NOW = 1_700_000_000.0
RATE = 0.5


class StubProvider:
    name = "stub"

    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.fail_delete = False
        self.created = 0

    async def create_machine(self, *, name: str, cpu: int, auto_stop_minutes: int) -> MachineInfo:
        self.created += 1
        return MachineInfo(
            id=f"stub-{self.created}", name=name, cpu=cpu, memory=1, disk=3, state="started"
        )

    async def get_machine(self, machine_id: str) -> MachineInfo:
        return MachineInfo(id=machine_id, name="n", cpu=1, memory=1, disk=3, state="started")

    async def delete_machine(self, machine_id: str) -> None:
        if self.fail_delete:
            raise CloudError("provider refused the delete")
        self.deleted.append(machine_id)

    async def execute(self, machine_id: str, command: str, timeout: float) -> tuple[int, str]:
        return 0, "ok"

    async def upload(self, machine_id: str, remote_path: str, data: bytes) -> None:
        return None

    async def download(self, machine_id: str, remote_path: str) -> bytes:
        return b""

    async def screenshot(self, machine_id: str) -> bytes:
        return b""


def make_sweeper(
    tmp_path: Path, *, budget: float | None = None, mode: str = "warn", destroy_minutes: int = 240
) -> tuple[CloudSweeper, CloudRegistry, StubProvider]:
    registry = CloudRegistry.load(tmp_path / "cloud_pc.json")
    gate = CloudBudgetGate(registry, budget, mode)  # type: ignore[arg-type]
    provider = StubProvider()
    sweeper = CloudSweeper(
        client=provider,
        registry=registry,
        gate=gate,
        rate_per_vcpu_hour=RATE,
        idle_destroy_minutes=destroy_minutes,
    )
    return sweeper, registry, provider


def add_machine(
    registry: CloudRegistry,
    machine_id: str,
    *,
    created_at: float,
    last_activity_at: float | None,
    cpu: int = 2,
) -> MachineRecord:
    machine = MachineRecord(
        id=machine_id,
        provider="stub",
        name=machine_id,
        cpu=cpu,
        memory=1,
        disk=3,
        created_at=created_at,
        last_activity_at=last_activity_at,
    )
    registry.add(machine)
    return machine


async def test_sweep_destroys_only_the_idle_machine(tmp_path: Path) -> None:
    sweeper, registry, provider = make_sweeper(tmp_path)
    add_machine(registry, "idle", created_at=NOW - 300 * 60, last_activity_at=NOW - 300 * 60)
    add_machine(registry, "fresh", created_at=NOW - 300 * 60, last_activity_at=NOW - 10 * 60)

    report = await sweeper.sweep(now=NOW)

    assert [s.machine_id for s in report.destroyed] == ["idle"]
    assert provider.deleted == ["idle"]
    assert report.errors == []
    idle = registry.get("idle")
    fresh = registry.get("fresh")
    assert idle is not None and not idle.live
    assert fresh is not None and fresh.live
    # 300 minutes at 2 vCPU and 0.5 USD per vCPU-hour is exactly 5 USD
    month = time.strftime("%Y-%m", time.localtime(NOW))
    assert registry.month_spend(month) == pytest.approx(5.0)
    assert report.destroyed[0].cost_usd == pytest.approx(5.0)
    assert any("idle" in line and "5.0000 USD" in line for line in report.lines())
    # the destroy is on disk, not only in this object
    reloaded = CloudRegistry.load(tmp_path / "cloud_pc.json")
    machine = reloaded.get("idle")
    assert machine is not None and not machine.live


async def test_a_record_without_activity_counts_from_its_creation(tmp_path: Path) -> None:
    path = tmp_path / "cloud_pc.json"
    path.write_text(
        json.dumps(
            {
                "machines": [
                    {
                        "id": "old-style",
                        "provider": "stub",
                        "name": "old-style",
                        "cpu": 1,
                        "memory": 1,
                        "disk": 3,
                        "created_at": NOW - 300 * 60,
                        "destroyed_at": None,
                    }
                ],
                "spend": [],
            }
        ),
        encoding="utf-8",
    )
    sweeper, registry, provider = make_sweeper(tmp_path)
    assert registry.live_machines()[0].idle_seconds(NOW) == pytest.approx(300 * 60)

    report = await sweeper.sweep(now=NOW)

    assert [s.machine_id for s in report.destroyed] == ["old-style"]
    assert provider.deleted == ["old-style"]


async def test_a_refused_delete_stays_live_and_the_next_pass_retries(tmp_path: Path) -> None:
    sweeper, registry, provider = make_sweeper(tmp_path)
    add_machine(registry, "stuck", created_at=NOW - 300 * 60, last_activity_at=NOW - 300 * 60)
    provider.fail_delete = True

    report = await sweeper.sweep(now=NOW)

    assert report.destroyed == []
    assert report.errors and "stuck" in report.errors[0]
    machine = registry.get("stuck")
    assert machine is not None and machine.live
    assert registry.spend == []

    provider.fail_delete = False
    report = await sweeper.sweep(now=NOW)
    assert [s.machine_id for s in report.destroyed] == ["stuck"]


async def test_the_budget_crossing_is_reported_once(tmp_path: Path) -> None:
    sweeper, registry, _provider = make_sweeper(tmp_path, budget=1.0)
    month = time.strftime("%Y-%m", time.localtime(NOW))
    registry.spend.append(SpendEntry(machine_id="old", month=month, seconds=60, cost_usd=1.5))
    add_machine(registry, "idle", created_at=NOW - 300 * 60, last_activity_at=NOW - 300 * 60)

    first = await sweeper.sweep(now=NOW)
    assert first.budget_line is not None and month in first.budget_line
    assert first.changed

    second = await sweeper.sweep(now=NOW)
    assert second.budget_line is None
    assert not second.changed


async def test_working_in_a_machine_stamps_its_activity(tmp_path: Path) -> None:
    provider = StubProvider()
    tools = build_cloud_tools(
        provider="stub",
        client=provider,  # type: ignore[arg-type]
        registry_path=tmp_path / "cloud_pc.json",
        workspace=tmp_path / "ws",
        monthly_budget_usd=None,
        gate_mode="warn",
        max_machines=1,
        idle_timeout_minutes=30,
        rate_per_vcpu_hour=RATE,
    )
    by = {tool.name: tool for tool in tools}
    created = await by["cloud_create"].execute(name="worker", cpu=1)
    assert created.ok, created.error
    registry = CloudRegistry.load(tmp_path / "cloud_pc.json")
    machine = registry.live_machines()[0]
    assert machine.last_activity_at == pytest.approx(machine.created_at)

    machine.last_activity_at = machine.created_at
    registry.save()
    ran = await by["cloud_exec"].execute(machine_id=machine.id, command="echo hi")
    assert ran.ok, ran.error
    reloaded = CloudRegistry.load(tmp_path / "cloud_pc.json")
    stamped = reloaded.get(machine.id)
    assert stamped is not None and stamped.last_activity > machine.created_at


async def test_the_companion_tick_runs_the_sweep_and_publishes_it(tmp_path: Path) -> None:
    sweeper, registry, _provider = make_sweeper(tmp_path)
    add_machine(registry, "forgotten", created_at=NOW - 300 * 60, last_activity_at=NOW - 300 * 60)
    events: list[dict[str, Any]] = []
    runtime = CompanionRuntime(enabled=True)
    assert runtime.start()
    service = CompanionService(runtime, publish=events.append, sweeper=sweeper)

    await service.tick()

    # the sweep ran at wall-clock now, far past the fixed NOW the record
    # was backdated to, so the machine was idle by any reading
    machine = registry.get("forgotten")
    assert machine is not None and not machine.live
    assert service.sweep_log and "forgotten" in service.sweep_log[0]
    assert events and events[0]["kind"] == "cloud_sweep"
    assert service.view()["cloud_sweep"] is True
