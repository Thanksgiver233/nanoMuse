"""The InstaBox provider: one REST API for the machine and the work in it.

``https://api.instabox.sh`` with the key as a bearer header. InstaBox
machines are Firecracker microVMs with a persistent disk; on the free
plan there is exactly one size (4 vCPU, 8 GiB) and one machine at a time,
and an idle machine suspends itself after 30 minutes with its memory
kept. Changes (create, delete) answer ``202`` with an operation to
follow, and a delete also needs the machine's own id echoed in an
``X-Confirm-Delete`` header, which this client always sends.

Two honest limits of this step, both named rather than smoothed over:
sizes are the plan's, so the create call sends no size at all and records
what the machine reports; and the desktop is reached through a watch
stream rather than a screenshot call, so :meth:`screenshot` refuses with
that reason until the graphical step (A3) wires the stream up.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx

from nanomuse.cloud_pc.daytona import CloudError
from nanomuse.cloud_pc.provider import MachineInfo

API_BASE = "https://api.instabox.sh"


class InstaBoxClient:
    name = "instabox"

    def __init__(
        self,
        api_key: str,
        *,
        template: str = "agent-browser",
        base: str = API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
            timeout=timeout,
        )
        self._base = base.rstrip("/")
        self._template = template

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- the machine itself ------------------------------------------------

    async def create_machine(self, *, name: str, cpu: int, auto_stop_minutes: int) -> MachineInfo:
        body: dict[str, Any] = {
            "name": name,
            "templateId": self._template,
            "autoSuspendAfterSeconds": auto_stop_minutes * 60,
            "metadata": {"created_by": "nanomuse"},
        }
        data = await self._json(
            "POST",
            f"{self._base}/v1/machines",
            json=body,
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        machine_id = str(data.get("machineId") or data.get("id") or "")
        operation_id = (
            str(data.get("id") or "") if data.get("state") not in (None, "running") else ""
        )
        if operation_id and machine_id and operation_id != machine_id:
            await self._await_operation(operation_id)
        if not machine_id:
            raise CloudError("instabox create answered without a machine id")
        info = await self.get_machine(machine_id)
        if info.state != "running":
            info = await self._await_state(machine_id, {"running"})
        return info

    async def get_machine(self, machine_id: str) -> MachineInfo:
        return self._info(await self._json("GET", f"{self._base}/v1/machines/{machine_id}"))

    async def delete_machine(self, machine_id: str) -> None:
        await self._json(
            "DELETE",
            f"{self._base}/v1/machines/{machine_id}",
            headers={
                "Idempotency-Key": str(uuid.uuid4()),
                "X-Confirm-Delete": machine_id,
            },
            allow_empty=True,
        )
        for _ in range(60):
            try:
                await self.get_machine(machine_id)
            except CloudError as exc:
                if exc.status == 404:
                    return
                raise
            await asyncio.sleep(2)
        raise CloudError(f"instabox machine {machine_id} still exists after the delete")

    async def machine_usage(self, machine_id: str) -> dict[str, Any]:
        """The provider's own metered usage for one machine (not in the
        protocol: the reconciliation reads it, the tools do not)."""
        return await self._json("GET", f"{self._base}/v1/machines/{machine_id}/usage")

    # -- work inside one machine ------------------------------------------

    async def execute(self, machine_id: str, command: str, timeout: float) -> tuple[int, str]:
        await self._ensure_running(machine_id)
        data = await self._json(
            "POST",
            f"{self._base}/v1/machines/{machine_id}/exec",
            json={"cmd": command, "timeoutSeconds": int(timeout)},
        )
        out = str(data.get("stdout", ""))
        err = str(data.get("stderr", ""))
        if err:
            out = f"{out}\n{err}" if out else err
        return int(data.get("exitCode", 0)), out

    async def upload(self, machine_id: str, remote_path: str, data: bytes) -> None:
        await self._ensure_running(machine_id)
        response = await self._request(
            "PUT",
            f"{self._base}/v1/machines/{machine_id}/files",
            params={"path": remote_path, "parents": "true"},
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        self._raise_for_status(response, "upload")

    async def download(self, machine_id: str, remote_path: str) -> bytes:
        await self._ensure_running(machine_id)
        response = await self._request(
            "GET",
            f"{self._base}/v1/machines/{machine_id}/files/content",
            params={"path": remote_path},
        )
        self._raise_for_status(response, "download")
        return response.content

    async def screenshot(self, machine_id: str) -> bytes:
        raise CloudError(
            "instabox exposes its desktop as a watch stream, not a screenshot call; "
            "wiring that stream up is the graphical step (A3), not this one"
        )

    # -- state helpers ------------------------------------------------------

    async def _ensure_running(self, machine_id: str) -> None:
        info = await self.get_machine(machine_id)
        if info.state == "running":
            return
        action = "start" if info.state == "stopped" else "resume"
        await self._json(
            "POST",
            f"{self._base}/v1/machines/{machine_id}/{action}",
            json={},
            headers={"Idempotency-Key": str(uuid.uuid4())},
            allow_empty=True,
        )
        await self._await_state(machine_id, {"running"})

    async def _await_state(self, machine_id: str, wanted: set[str]) -> MachineInfo:
        for _ in range(90):
            info = await self.get_machine(machine_id)
            if info.state in wanted:
                return info
            await asyncio.sleep(2)
        raise CloudError(f"instabox machine {machine_id} did not reach {sorted(wanted)} in time")

    async def _await_operation(self, operation_id: str) -> None:
        for _ in range(150):
            data = await self._json("GET", f"{self._base}/v1/operations/{operation_id}")
            state = str(data.get("state", ""))
            if state == "succeeded":
                return
            if state in ("failed", "cancelled"):
                error = data.get("error") or {}
                reason = error.get("message") if isinstance(error, dict) else str(error)
                raise CloudError(f"instabox operation {operation_id} failed: {reason}")
            await asyncio.sleep(2)
        raise CloudError(f"instabox operation {operation_id} did not finish in time")

    @staticmethod
    def _info(data: dict[str, Any]) -> MachineInfo:
        spec = data.get("spec") or {}
        return MachineInfo(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            cpu=int(spec.get("vcpus", data.get("vcpus", 4))),
            memory=int(int(spec.get("memMiB", 8192)) / 1024),
            disk=int(spec.get("persistentDiskGiB", 20)),
            state=str(data.get("state", "unknown")),
            detail=str(data.get("templateId") or data.get("image") or ""),
        )

    # -- plumbing ---------------------------------------------------------

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise CloudError(f"instabox request failed: {exc.__class__.__name__}") from exc

    def _raise_for_status(self, response: httpx.Response, what: str) -> None:
        if response.status_code >= 400:
            code, message = "", response.text[:200].replace("\n", " ")
            try:
                error = response.json().get("error", {})
                code = str(error.get("code", ""))
                message = str(error.get("message", message))
            except ValueError:
                pass
            label = f"{code}: {message}" if code else message
            raise CloudError(
                f"instabox {what} failed ({response.status_code}): {label}", response.status_code
            )

    async def _json(
        self, method: str, url: str, *, allow_empty: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        response = await self._request(method, url, **kwargs)
        self._raise_for_status(response, method.lower())
        if allow_empty and not response.content:
            return {}
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
