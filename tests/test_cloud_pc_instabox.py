"""The InstaBox provider over a fake transport, and the tools over both providers.

Same discipline as the Daytona tests: no network, a synthetic key, and the
provider's documented shapes (an operation to follow after a 202, the
confirm-delete header, metered usage fields). The cross-provider test runs
one instruction through the Daytona fake and the InstaBox fake and checks
the tool layer cannot tell them apart.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from nanomuse.cloud_pc.gate import CloudBudgetGate
from nanomuse.cloud_pc.instabox import InstaBoxClient
from nanomuse.cloud_pc.registry import CloudRegistry
from nanomuse.tools.cloud_pc import (
    CloudCreate,
    CloudDestroy,
    CloudExec,
    CloudFile,
    CloudRuntime,
    CloudScreenshot,
)

KEY = "ibk_test_not_a_real_one"
PNG = b"\x89PNG\r\n\x1a\nfake"


def instabox_transport(calls: list[str], state: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(f"{request.method} {url}")
        assert request.headers.get("Authorization") == f"Bearer {KEY}"
        if url.endswith("/v1/machines") and request.method == "POST":
            assert request.headers.get("Idempotency-Key")
            body = json.loads(request.content)
            state["name"] = body.get("name")
            return httpx.Response(202, json={"id": "op-1", "machineId": "m-1", "state": "pending"})
        if url.endswith("/v1/operations/op-1"):
            return httpx.Response(
                200, json={"id": "op-1", "machineId": "m-1", "state": "succeeded"}
            )
        if url.endswith("/v1/machines/m-1") and request.method == "GET":
            if state.get("deleted"):
                return httpx.Response(
                    404, json={"error": {"code": "not_found", "message": "no such machine"}}
                )
            return httpx.Response(
                200,
                json={
                    "id": "m-1",
                    "name": state.get("name", "machine"),
                    "state": state.get("machine_state", "running"),
                    "spec": {"vcpus": 4, "memMiB": 8192, "persistentDiskGiB": 20},
                    "templateId": "agent-browser",
                },
            )
        if url.endswith("/v1/machines/m-1") and request.method == "DELETE":
            assert request.headers.get("X-Confirm-Delete") == "m-1"
            state["deleted"] = True
            return httpx.Response(202, json={"id": "op-2", "machineId": "m-1", "state": "pending"})
        if url.endswith("/v1/machines/m-1/resume"):
            state["machine_state"] = "running"
            return httpx.Response(202, json={"id": "op-3", "machineId": "m-1", "state": "pending"})
        if url.endswith("/v1/machines/m-1/exec"):
            return httpx.Response(
                200, json={"exitCode": 0, "stdout": "hello from inside", "stderr": ""}
            )
        if "/v1/machines/m-1/files" in url and request.method == "PUT":
            state["uploaded"] = request.content
            return httpx.Response(200, json={})
        if "/v1/machines/m-1/files/content" in url:
            return httpx.Response(200, content=b"downloaded body")
        if url.endswith("/v1/machines/m-1/usage"):
            return httpx.Response(
                200,
                json={"vcpuSeconds": 14400.0, "memGiBSeconds": 28800.0, "runningSeconds": 3600.0},
            )
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "no route"}})

    return httpx.MockTransport(handler)


def make_runtime(tmp_path: Path, state: dict) -> tuple[CloudRuntime, list[str]]:
    calls: list[str] = []
    client = InstaBoxClient(KEY, transport=instabox_transport(calls, state))
    registry = CloudRegistry.load(tmp_path / "cloud_pc.json")
    runtime = CloudRuntime(
        provider="instabox",
        client=client,
        registry=registry,
        gate=CloudBudgetGate(registry, None, "warn"),
        workspace=tmp_path / "ws",
        max_machines=1,
        idle_timeout_minutes=30,
        rate_per_vcpu_hour=0.0085,
    )
    return runtime, calls


async def test_instabox_full_loop(tmp_path: Path) -> None:
    state: dict = {}
    runtime, _ = make_runtime(tmp_path, state)
    created = await CloudCreate(runtime=runtime).execute(name="worker")
    assert created.ok, created.error
    assert "m-1" in created.output and "4 vCPU" in created.output

    result = await CloudExec(runtime=runtime).execute(machine_id="m-1", command="echo hi")
    assert result.ok and "hello from inside" in result.output

    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    (workspace / "in.txt").write_bytes(b"payload")
    up = await CloudFile(runtime=runtime).execute(
        machine_id="m-1", direction="upload", remote_path="/data/in.txt", local_path="in.txt"
    )
    assert up.ok, up.error
    assert state["uploaded"] == b"payload"
    down = await CloudFile(runtime=runtime).execute(
        machine_id="m-1", direction="download", remote_path="/data/out.txt", local_path="out.txt"
    )
    assert down.ok and (workspace / "out.txt").read_bytes() == b"downloaded body"

    shot = await CloudScreenshot(runtime=runtime).execute(machine_id="m-1")
    assert not shot.ok and "A3" in (shot.error or "")

    client = runtime.require_client()
    usage = await client.machine_usage("m-1")  # type: ignore[attr-defined]
    assert usage["vcpuSeconds"] == 14400.0

    machine = runtime.registry.get("m-1")
    assert machine is not None
    machine.created_at = time.time() - 3600
    done = await CloudDestroy(runtime=runtime).execute(machine_id="m-1")
    assert done.ok, done.error
    assert "0.0340 USD" in done.output  # 1 h x 4 vCPU x 0.0085 USD


async def test_instabox_exec_wakes_a_suspended_machine(tmp_path: Path) -> None:
    state: dict = {}
    runtime, calls = make_runtime(tmp_path, state)
    created = await CloudCreate(runtime=runtime).execute(name="w")
    assert created.ok, created.error  # create followed the operation, then the state
    state["machine_state"] = "suspended"
    result = await CloudExec(runtime=runtime).execute(machine_id="m-1", command="echo hi")
    assert result.ok, result.error
    assert any("/resume" in c for c in calls)


async def test_same_instruction_both_providers(tmp_path: Path) -> None:
    from test_cloud_pc import fake_transport as daytona_fake

    from nanomuse.cloud_pc.daytona import DaytonaClient

    state: dict = {}
    insta, _ = make_runtime(tmp_path / "insta", state)
    daytona_calls: list[str] = []
    daytona_client = DaytonaClient("test-key-not-a-real-one", transport=daytona_fake(daytona_calls))
    registry = CloudRegistry.load(tmp_path / "daytona" / "cloud_pc.json")
    daytona = CloudRuntime(
        provider="daytona",
        client=daytona_client,
        registry=registry,
        gate=CloudBudgetGate(registry, None, "warn"),
        workspace=tmp_path / "daytona" / "ws",
        max_machines=1,
        idle_timeout_minutes=30,
        rate_per_vcpu_hour=0.10,
    )
    for runtime, machine_id in ((daytona, "sbx-1"), (insta, "m-1")):
        created = await CloudCreate(runtime=runtime).execute(name="probe")
        assert created.ok, created.error
        result = await CloudExec(runtime=runtime).execute(machine_id=machine_id, command="echo hi")
        assert result.ok and result.output == "hello from inside"
        done = await CloudDestroy(runtime=runtime).execute(machine_id=machine_id)
        assert done.ok, done.error


def test_provider_factory_and_config_shape(tmp_path: Path) -> None:
    from nanomuse.cloud_pc import build_provider
    from nanomuse.config import Settings

    assert build_provider("daytona", "k").name == "daytona"
    assert build_provider("instabox", "k").name == "instabox"
    with pytest.raises(ValueError):
        build_provider("nope", "k")

    settings = Settings()
    assert settings.cloud_pc.provider == "daytona"
    key, rate, _ = settings.cloud_pc.provider_key_and_rate()
    assert key == "" and rate == pytest.approx(0.0666)
    settings.cloud_pc.provider = "instabox"
    _, rate, template = settings.cloud_pc.provider_key_and_rate()
    assert rate == pytest.approx(0.0085) and template == "agent-browser"
