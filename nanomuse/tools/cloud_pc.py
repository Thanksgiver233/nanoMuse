"""Cloud computer tools: open a machine on a cloud provider, work in it, destroy it.

Six tools over one runtime (:class:`CloudRuntime`): ``cloud_create``,
``cloud_status``, ``cloud_exec``, ``cloud_file``, ``cloud_screenshot`` and
``cloud_destroy``. The runtime talks to one provider behind the
:class:`~nanomuse.cloud_pc.provider.CloudProvider` protocol (Daytona or
InstaBox, chosen in the config); the tools never learn which.

How the Sentinel sees them, set here rather than left to defaults:

- ``cloud_exec`` is ``shell`` on a remote machine: SENSITIVE, egress.
- ``cloud_create`` starts something billable: MODERATE, with a warning that
  says so, and the budget gate runs before the provider is called.
- ``cloud_destroy`` is irreversible: SENSITIVE, with a warning naming what
  is lost.
- ``cloud_file`` up is MODERATE with egress; down is MODERATE and reads
  private data (the file's content comes back into the conversation).
- ``cloud_screenshot`` is MODERATE and reads private data: a screen can
  hold anything the machine was showing.

The API key reaches the runtime already resolved from the vault at wiring
time. No tool prints it, and provider errors are scrubbed of it.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from nanomuse.cloud_pc.daytona import CloudError
from nanomuse.cloud_pc.gate import CloudBudgetGate, CloudGateBlocked
from nanomuse.cloud_pc.provider import CloudProvider
from nanomuse.cloud_pc.registry import CloudRegistry, record_from_provider
from nanomuse.schema import RiskLevel, ToolResult
from nanomuse.tools.base import BaseTool, CallAssessment, number_arg


class CloudRuntime:
    """Everything the six tools share: client, registry, gate, workspace."""

    def __init__(
        self,
        *,
        provider: str,
        client: CloudProvider | None,
        registry: CloudRegistry,
        gate: CloudBudgetGate,
        workspace: Path,
        max_machines: int,
        idle_timeout_minutes: int,
        rate_per_vcpu_hour: float,
    ) -> None:
        self.provider = provider
        self.client = client
        self.registry = registry
        self.gate = gate
        self.workspace = workspace
        self.max_machines = max_machines
        self.idle_timeout_minutes = idle_timeout_minutes
        self.rate_per_vcpu_hour = rate_per_vcpu_hour

    def require_client(self) -> CloudProvider:
        if self.client is None:
            raise CloudError(
                "cloud_pc is enabled but no provider API key is configured; "
                "set [cloud_pc] api_key (a {{vault:NAME}} placeholder works) and restart"
            )
        return self.client


class _CloudTool(BaseTool):
    runtime: CloudRuntime

    def _machine(self, machine_id: str) -> Any:
        machine = self.runtime.registry.get(machine_id)
        if machine is None:
            raise CloudError(f"no cloud machine with id {machine_id!r} in the local record")
        if not machine.live:
            raise CloudError(f"cloud machine {machine_id} was already destroyed")
        return machine


class CloudCreate(_CloudTool):
    name: str = "cloud_create"
    description: str = (
        "Open a cloud computer on the configured provider (Daytona or InstaBox): an isolated "
        "Linux machine with its own filesystem that bills by the hour until destroyed. Sizes "
        "are the provider's own classes; asking for a vCPU count takes the smallest class "
        "that fits where the provider offers a choice. Returns the machine id. Use "
        "cloud_exec and cloud_file to work in it, and cloud_destroy when done."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "A label for the machine."},
            "cpu": {"type": "number", "description": "vCPUs wanted (default 1)."},
        },
    }
    risk: RiskLevel = RiskLevel.MODERATE

    def assess(self, args: dict[str, Any]) -> CallAssessment:
        return CallAssessment(
            risk=RiskLevel.MODERATE,
            egress=True,
            summary=f"cloud_create: {args.get('name') or 'machine'} on {self.runtime.provider}",
            warnings=["starts a billable cloud machine; it bills until cloud_destroy"],
        )

    async def execute(self, name: str = "", cpu: float = 1, **_: Any) -> ToolResult:
        rt = self.runtime
        try:
            warning = rt.gate.check_before_create()
        except CloudGateBlocked as exc:
            return ToolResult.fail(str(exc))
        live = rt.registry.live_machines()
        if len(live) >= rt.max_machines:
            return ToolResult.fail(
                f"{len(live)} cloud machine(s) already open and [cloud_pc] max_machines is "
                f"{rt.max_machines}; destroy one first"
            )
        try:
            client = rt.require_client()
            info = await client.create_machine(
                name=name or "nanomuse",
                cpu=max(1, int(cpu)),
                auto_stop_minutes=rt.idle_timeout_minutes,
            )
        except CloudError as exc:
            return ToolResult.fail(str(exc))
        machine = record_from_provider(info, rt.provider, time.time())
        if not machine.id:
            return ToolResult.fail("the provider answered without a machine id")
        rt.registry.add(machine)
        lines = [
            f"machine {machine.id} open on {rt.provider}: {machine.cpu} vCPU, "
            f"{machine.memory} GiB RAM, {machine.disk} GiB disk, from "
            f"{info.detail or 'the provider default'}, state {info.state}",
            f"it auto-stops after {rt.idle_timeout_minutes} idle minutes and bills until "
            "cloud_destroy; destroy it when the work is done",
        ]
        if warning:
            lines.append(warning)
        return ToolResult(output="\n".join(lines))


class CloudStatus(_CloudTool):
    name: str = "cloud_status"
    description: str = (
        "List the cloud machines this device has opened, with the provider's current state "
        "for the live ones and the recorded spend for the month."
    )
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    risk: RiskLevel = RiskLevel.SAFE

    async def execute(self, **_: Any) -> ToolResult:
        rt = self.runtime
        now = time.time()
        lines: list[str] = []
        for machine in rt.registry.machines:
            if machine.live:
                state = "unknown"
                try:
                    info = await rt.require_client().get_machine(machine.id)
                    state = info.state
                except CloudError as exc:
                    state = f"unreachable ({exc})"
                lines.append(
                    f"{machine.id} ({machine.name}) live, provider state {state}, "
                    f"open for {machine.billed_seconds(now) / 60:.0f} min"
                )
            else:
                lines.append(
                    f"{machine.id} ({machine.name}) destroyed, ran "
                    f"{machine.billed_seconds() / 60:.0f} min"
                )
        if not lines:
            lines.append("no cloud machines on record")
        month = rt.gate.month(now)
        lines.append(f"recorded cloud spend in {month}: {rt.registry.month_spend(month):.4f} USD")
        return ToolResult(output="\n".join(lines))


class CloudExec(_CloudTool):
    name: str = "cloud_exec"
    description: str = (
        "Run a shell command inside a cloud machine opened with cloud_create and return its "
        "combined output and exit code. This is shell on a remote, billable machine."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "machine_id": {"type": "string"},
            "command": {"type": "string"},
            "timeout": {"type": "number", "description": "Seconds (default 120, max 600)."},
        },
        "required": ["machine_id", "command"],
    }
    risk: RiskLevel = RiskLevel.SENSITIVE
    egress: bool = True

    def assess(self, args: dict[str, Any]) -> CallAssessment:
        command = str(args.get("command", ""))
        return CallAssessment(
            risk=RiskLevel.SENSITIVE,
            egress=True,
            summary=f"cloud_exec on {args.get('machine_id', '?')}: {command[:160]}",
        )

    async def execute(
        self, machine_id: str = "", command: str = "", timeout: float = 120, **_: Any
    ) -> ToolResult:
        if not command.strip():
            return ToolResult.fail("empty command")
        try:
            machine = self._machine(machine_id)
            code, result = await self.runtime.require_client().execute(
                machine.id, command, number_arg(timeout, 120, 1.0, 600.0)
            )
        except CloudError as exc:
            return ToolResult.fail(str(exc))
        if code != 0:
            return ToolResult.fail(f"command exited {code}:\n{result}")
        return ToolResult(output=result or "(no output)")


class CloudFile(_CloudTool):
    name: str = "cloud_file"
    description: str = (
        "Move one file between the workspace and a cloud machine. direction=upload sends a "
        "workspace file to remote_path on the machine; direction=download brings remote_path "
        "back to a workspace path, and its content then sits in the workspace."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "machine_id": {"type": "string"},
            "direction": {"type": "string", "enum": ["upload", "download"]},
            "remote_path": {"type": "string"},
            "local_path": {"type": "string", "description": "Path inside the workspace."},
        },
        "required": ["machine_id", "direction", "remote_path", "local_path"],
    }
    risk: RiskLevel = RiskLevel.MODERATE

    def assess(self, args: dict[str, Any]) -> CallAssessment:
        upload = str(args.get("direction", "")) == "upload"
        return CallAssessment(
            risk=RiskLevel.MODERATE,
            egress=upload,
            reads_private_data=not upload,
            summary=(
                f"cloud_file {args.get('direction', '?')}: {args.get('local_path', '?')} "
                f"{'->' if upload else '<-'} {args.get('machine_id', '?')}:{args.get('remote_path', '?')}"
            ),
        )

    def _local(self, local_path: str) -> Path:
        root = self.runtime.workspace.resolve()
        path = (root / local_path).resolve()
        if path != root and root not in path.parents:
            raise CloudError(f"local_path {local_path!r} is outside the workspace")
        return path

    async def execute(
        self,
        machine_id: str = "",
        direction: str = "",
        remote_path: str = "",
        local_path: str = "",
        **_: Any,
    ) -> ToolResult:
        try:
            machine = self._machine(machine_id)
            local = self._local(local_path)
            client = self.runtime.require_client()
            if direction == "upload":
                if not local.is_file():
                    return ToolResult.fail(f"no workspace file at {local_path}")
                await client.upload(machine.id, remote_path, local.read_bytes())
                return ToolResult(
                    output=f"uploaded {local_path} ({local.stat().st_size} bytes) to "
                    f"{machine.id}:{remote_path}"
                )
            if direction == "download":
                data = await client.download(machine.id, remote_path)
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(data)
                return ToolResult(
                    output=f"downloaded {machine.id}:{remote_path} to {local_path} ({len(data)} bytes)"
                )
            return ToolResult.fail("direction must be upload or download")
        except CloudError as exc:
            return ToolResult.fail(str(exc))


class CloudScreenshot(_CloudTool):
    name: str = "cloud_screenshot"
    description: str = (
        "Take a screenshot of a cloud machine's desktop and save it into the workspace. "
        "Machines without a desktop environment answer with an error."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"machine_id": {"type": "string"}},
        "required": ["machine_id"],
    }
    risk: RiskLevel = RiskLevel.MODERATE
    reads_private_data: bool = True

    def assess(self, args: dict[str, Any]) -> CallAssessment:
        return CallAssessment(
            risk=RiskLevel.MODERATE,
            reads_private_data=True,
            summary=f"cloud_screenshot: {args.get('machine_id', '?')}",
        )

    async def execute(self, machine_id: str = "", **_: Any) -> ToolResult:
        try:
            machine = self._machine(machine_id)
            data = await self.runtime.require_client().screenshot(machine.id)
        except CloudError as exc:
            return ToolResult.fail(str(exc))
        folder = self.runtime.workspace / "cloud_screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{machine.id}-{int(time.time())}.png"
        path.write_bytes(data)
        return ToolResult(
            output=f"screenshot of {machine.id} saved ({len(data)} bytes)",
            system=f"saved screenshot to {path}",
            images=[str(path)],
        )


class CloudDestroy(_CloudTool):
    name: str = "cloud_destroy"
    description: str = (
        "Destroy a cloud machine opened with cloud_create. Its disk and everything on it are "
        "gone for good, billing stops, and the machine's cost is written to the record."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"machine_id": {"type": "string"}},
        "required": ["machine_id"],
    }
    risk: RiskLevel = RiskLevel.SENSITIVE

    def assess(self, args: dict[str, Any]) -> CallAssessment:
        return CallAssessment(
            risk=RiskLevel.SENSITIVE,
            summary=f"cloud_destroy: {args.get('machine_id', '?')}",
            warnings=["destroys the machine and everything on its disk; this cannot be undone"],
        )

    async def execute(self, machine_id: str = "", **_: Any) -> ToolResult:
        rt = self.runtime
        try:
            machine = self._machine(machine_id)
            await rt.require_client().delete_machine(machine.id)
        except CloudError as exc:
            return ToolResult.fail(str(exc))
        entry = rt.registry.finalise_destroy(
            machine, rate_per_vcpu_hour=rt.rate_per_vcpu_hour, now=time.time()
        )
        return ToolResult(
            output=(
                f"machine {machine.id} destroyed after {entry.seconds / 60:.1f} min; "
                f"recorded cost {entry.cost_usd:.4f} USD; recorded cloud spend in "
                f"{entry.month} is now {rt.registry.month_spend(entry.month):.4f} USD"
            )
        )


def build_cloud_tools(
    *,
    provider: str,
    client: CloudProvider | None,
    registry_path: Path,
    workspace: Path,
    monthly_budget_usd: float | None,
    gate_mode: str,
    max_machines: int,
    idle_timeout_minutes: int,
    rate_per_vcpu_hour: float,
) -> list[BaseTool]:
    """Assemble the six tools over one runtime. A missing client (no key
    configured) still builds the tools; every call then fails with the
    configuration error, so the person hears what is missing instead of
    the tools silently not existing."""
    registry = CloudRegistry.load(registry_path)
    gate = CloudBudgetGate(registry, monthly_budget_usd, gate_mode)  # type: ignore[arg-type]
    runtime = CloudRuntime(
        provider=provider,
        client=client,
        registry=registry,
        gate=gate,
        workspace=workspace,
        max_machines=max_machines,
        idle_timeout_minutes=idle_timeout_minutes,
        rate_per_vcpu_hour=rate_per_vcpu_hour,
    )
    return [
        CloudCreate(runtime=runtime),
        CloudStatus(runtime=runtime),
        CloudExec(runtime=runtime),
        CloudFile(runtime=runtime),
        CloudScreenshot(runtime=runtime),
        CloudDestroy(runtime=runtime),
    ]
