"""Cloud computer tools over a fake Daytona transport: no network, no key, no bill.

The fake answers the platform and toolbox calls the client makes, so the
whole loop (create, exec, file both ways, screenshot, destroy) runs against
the real tool code, the real registry and the real gate. Spend figures use
a synthetic rate and a backdated machine, never a real account's numbers.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from nanomuse.cloud_pc.daytona import DaytonaClient
from nanomuse.cloud_pc.gate import CloudBudgetGate
from nanomuse.cloud_pc.registry import CloudRegistry, MachineRecord, SpendEntry
from nanomuse.config import Settings
from nanomuse.schema import RiskLevel
from nanomuse.tools.cloud_pc import (
    CloudCreate,
    CloudDestroy,
    CloudExec,
    CloudFile,
    CloudRuntime,
    CloudScreenshot,
    CloudStatus,
)

KEY = "test-key-not-a-real-one"
PNG = b"\x89PNG\r\n\x1a\nfake"


def fake_transport(calls: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(f"{request.method} {url}")
        assert request.headers.get("Authorization") == f"Bearer {KEY}"
        if url.endswith("/api/snapshots") and request.method == "GET":
            classes = [
                {"name": "daytona-small", "cpu": 1, "disk": 3, "state": "active", "general": True},
                {"name": "daytona-medium", "cpu": 2, "disk": 8, "state": "active", "general": True},
                {"name": "daytona-large", "cpu": 4, "disk": 10, "state": "active", "general": True},
                {
                    "name": "daytona-vm-large",
                    "cpu": 4,
                    "disk": 10,
                    "state": "active",
                    "general": True,
                },
                {
                    "name": "windows-medium",
                    "cpu": 2,
                    "disk": 50,
                    "state": "active",
                    "general": True,
                },
                {
                    "name": "daytona-huge",
                    "cpu": 8,
                    "disk": 20,
                    "state": "inactive",
                    "general": True,
                },
            ]
            return httpx.Response(200, json={"items": classes})
        if url.endswith("/api/sandbox") and request.method == "POST":
            body = json.loads(request.content)
            sizes = {
                "daytona-small": (1, 1, 3),
                "daytona-medium": (2, 4, 8),
                "daytona-large": (4, 8, 10),
            }
            cpu, memory, disk = sizes[body.get("snapshot", "daytona-small")]
            return httpx.Response(
                200,
                json={
                    "id": "sbx-1",
                    "name": body.get("name"),
                    "snapshot": body.get("snapshot"),
                    "cpu": cpu,
                    "memory": memory,
                    "disk": disk,
                    "state": "started",
                },
            )
        if url.endswith("/api/sandbox/sbx-1") and request.method == "GET":
            return httpx.Response(200, json={"id": "sbx-1", "state": "started"})
        if url.endswith("/api/sandbox/sbx-1") and request.method == "DELETE":
            return httpx.Response(200, json={})
        if url.endswith("/process/execute"):
            body = json.loads(request.content)
            if "exit 3" in body.get("command", ""):
                return httpx.Response(200, json={"exitCode": 3, "result": "boom"})
            return httpx.Response(200, json={"exitCode": 0, "result": "hello from inside"})
        if "/files/upload-v2" in url:
            return httpx.Response(200, json={})
        if "/files/download" in url:
            return httpx.Response(200, content=b"downloaded body")
        if "/computeruse/start" in url:
            return httpx.Response(200, json={})
        if "/computeruse/screenshot" in url:
            import base64

            envelope = json.dumps({"screenshot": base64.b64encode(PNG).decode()})
            return httpx.Response(
                200, content=envelope, headers={"Content-Type": "application/json"}
            )
        return httpx.Response(404, json={"message": "not found"})

    return httpx.MockTransport(handler)


def make_runtime(
    tmp_path: Path, *, budget: float | None = None, mode: str = "warn", max_machines: int = 1
):
    calls: list[str] = []
    client = DaytonaClient(KEY, transport=fake_transport(calls))
    registry = CloudRegistry.load(tmp_path / "cloud_pc.json")
    gate = CloudBudgetGate(registry, budget, mode)  # type: ignore[arg-type]
    runtime = CloudRuntime(
        provider="daytona",
        client=client,
        registry=registry,
        gate=gate,
        workspace=tmp_path / "ws",
        max_machines=max_machines,
        idle_timeout_minutes=30,
        rate_per_vcpu_hour=0.10,
    )
    return runtime, calls


async def test_full_loop_and_spend_record(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path)
    create = CloudCreate(runtime=runtime)
    result = await create.execute(name="worker", cpu=2)
    assert result.ok, result.error
    assert "sbx-1" in result.output
    assert KEY not in result.output

    status = await CloudStatus(runtime=runtime).execute()
    assert "live" in status.output and "started" in status.output

    exec_result = await CloudExec(runtime=runtime).execute(machine_id="sbx-1", command="echo hi")
    assert exec_result.ok and "hello from inside" in exec_result.output

    failed = await CloudExec(runtime=runtime).execute(machine_id="sbx-1", command="exit 3")
    assert not failed.ok and "exited 3" in (failed.error or "")

    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    (workspace / "in.txt").write_text("payload", encoding="utf-8")
    up = await CloudFile(runtime=runtime).execute(
        machine_id="sbx-1", direction="upload", remote_path="/tmp/in.txt", local_path="in.txt"
    )
    assert up.ok, up.error
    down = await CloudFile(runtime=runtime).execute(
        machine_id="sbx-1", direction="download", remote_path="/tmp/out.txt", local_path="out.txt"
    )
    assert down.ok, down.error
    assert (workspace / "out.txt").read_bytes() == b"downloaded body"

    shot = await CloudScreenshot(runtime=runtime).execute(machine_id="sbx-1")
    assert shot.ok and shot.images and Path(shot.images[0]).read_bytes() == PNG

    # backdate the machine one hour so the spend figure is exact at the synthetic rate
    machine = runtime.registry.get("sbx-1")
    assert machine is not None
    machine.created_at = time.time() - 3600
    done = await CloudDestroy(runtime=runtime).execute(machine_id="sbx-1")
    assert done.ok, done.error
    assert "0.2000 USD" in done.output  # 1 h x 2 vCPU x 0.10 USD
    month = time.strftime("%Y-%m")
    assert runtime.registry.month_spend(month) == pytest.approx(0.20)

    again = await CloudDestroy(runtime=runtime).execute(machine_id="sbx-1")
    assert not again.ok


async def test_max_machines_and_unknown_machine(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path)
    assert (await CloudCreate(runtime=runtime).execute()).ok
    second = await CloudCreate(runtime=runtime).execute()
    assert not second.ok and "max_machines" in (second.error or "")
    missing = await CloudExec(runtime=runtime).execute(machine_id="nope", command="ls")
    assert not missing.ok and "no cloud machine" in (missing.error or "")


async def test_hard_gate_refuses_before_any_provider_call(tmp_path: Path) -> None:
    runtime, calls = make_runtime(tmp_path, budget=1.0, mode="hard")
    month = time.strftime("%Y-%m")
    runtime.registry.spend.append(
        SpendEntry(machine_id="old", month=month, seconds=60, cost_usd=1.5)
    )
    result = await CloudCreate(runtime=runtime).execute()
    assert not result.ok and "monthly cloud budget reached" in (result.error or "")
    assert not any("/api/sandbox" in c for c in calls)


async def test_warn_gate_notes_the_crossing_and_continues(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, budget=1.0, mode="warn")
    month = time.strftime("%Y-%m")
    runtime.registry.spend.append(
        SpendEntry(machine_id="old", month=month, seconds=60, cost_usd=2.0)
    )
    result = await CloudCreate(runtime=runtime).execute()
    assert result.ok and "warning: cloud spend" in result.output


async def test_file_tool_keeps_local_paths_inside_the_workspace(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path)
    assert (await CloudCreate(runtime=runtime).execute()).ok
    escape = await CloudFile(runtime=runtime).execute(
        machine_id="sbx-1",
        direction="download",
        remote_path="/etc/hostname",
        local_path="../outside.txt",
    )
    assert not escape.ok and "outside the workspace" in (escape.error or "")


def test_assessments_carry_the_risks_the_docs_promise() -> None:
    runtime, _ = make_runtime(Path("/tmp"))
    assert CloudExec(runtime=runtime).assess({"command": "ls"}).risk is RiskLevel.SENSITIVE
    destroy = CloudDestroy(runtime=runtime).assess({"machine_id": "x"})
    assert destroy.risk is RiskLevel.SENSITIVE and destroy.warnings
    shot = CloudScreenshot(runtime=runtime).assess({"machine_id": "x"})
    assert shot.reads_private_data
    create = CloudCreate(runtime=runtime).assess({})
    assert create.risk is RiskLevel.MODERATE and create.warnings
    down = CloudFile(runtime=runtime).assess({"direction": "download"})
    up = CloudFile(runtime=runtime).assess({"direction": "upload"})
    assert down.reads_private_data and not down.egress
    assert up.egress and not up.reads_private_data


def test_registry_round_trip_and_defaults(tmp_path: Path) -> None:
    path = tmp_path / "cloud_pc.json"
    registry = CloudRegistry.load(path)
    registry.add(
        MachineRecord(
            id="m1", provider="daytona", name="n", cpu=1, memory=1, disk=3, created_at=1.0
        )
    )
    loaded = CloudRegistry.load(path)
    assert loaded.get("m1") is not None and loaded.live_machines()[0].id == "m1"

    settings = Settings()
    assert settings.cloud_pc.enabled is False
    assert settings.cloud_pc.mode == "warn"
    assert settings.cloud_pc.max_machines == 1
