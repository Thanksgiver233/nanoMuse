"""The Daytona provider client: platform API for the machine, toolbox API for work in it.

Two hosts, one key (a bearer header on both):

- ``https://app.daytona.io/api`` creates, lists, reads and deletes sandboxes.
- ``https://proxy.app.daytona.io/toolbox/<sandbox id>`` runs commands, moves
  files and takes screenshots inside one running sandbox.

The client is a thin async wrapper over exactly the calls the cloud tools
make. It knows nothing about the registry, the budget or the Sentinel; the
tools layer owns those. The key is handed in by the caller (resolved from
the vault at wiring time) and never logged or echoed into an error.
"""

from __future__ import annotations

import base64
import posixpath
from typing import Any

import httpx

PLATFORM_BASE = "https://app.daytona.io/api"
TOOLBOX_BASE = "https://proxy.app.daytona.io/toolbox"


class CloudError(RuntimeError):
    """A provider call failed; the message is safe to show the model."""

    def __init__(self, message: str, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)


class DaytonaClient:
    def __init__(
        self,
        api_key: str,
        *,
        platform_base: str = PLATFORM_BASE,
        toolbox_base: str = TOOLBOX_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
            timeout=timeout,
        )
        self._platform = platform_base.rstrip("/")
        self._toolbox = toolbox_base.rstrip("/")

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- platform: the machine itself ------------------------------------

    async def list_snapshots(self) -> list[dict[str, Any]]:
        data = await self._json("GET", f"{self._platform}/snapshots")
        items = data.get("items")
        return items if isinstance(items, list) else []

    async def create_sandbox(
        self,
        *,
        name: str,
        cpu: int,
        auto_stop_minutes: int,
        labels: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        snapshot = await self._snapshot_for(cpu)
        body: dict[str, Any] = {
            "name": name,
            "snapshot": snapshot,
            "autoStopInterval": auto_stop_minutes,
            "labels": labels or {},
        }
        return await self._json("POST", f"{self._platform}/sandbox", json=body)

    async def _snapshot_for(self, cpu: int) -> str:
        """The general Linux snapshot class for a vCPU count.

        Daytona sizes are snapshot classes, not free numbers: the API
        refuses resource fields next to a snapshot, and a create with no
        snapshot silently takes the default class. The classes are read
        from the account, never hardcoded; the smallest class that fits
        the request wins, and an account with no fitting class is an
        error that names what it does have.
        """
        classes = []
        for snap in await self.list_snapshots():
            name = str(snap.get("name", ""))
            if not snap.get("general") or snap.get("state") != "active":
                continue
            if not name.startswith("daytona-") or "vm" in name or "gpu" in name:
                continue
            classes.append((int(snap.get("cpu", 0)), name))
        classes.sort()
        for size, name in classes:
            if size >= cpu:
                return name
        have = ", ".join(f"{size} vCPU ({name})" for size, name in classes) or "none"
        raise CloudError(f"no daytona snapshot class fits {cpu} vCPU; the account offers: {have}")

    async def get_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        return await self._json("GET", f"{self._platform}/sandbox/{sandbox_id}")

    async def list_sandboxes(self) -> list[dict[str, Any]]:
        data = await self._json("GET", f"{self._platform}/sandbox")
        items = data.get("items")
        return items if isinstance(items, list) else []

    async def delete_sandbox(self, sandbox_id: str) -> None:
        await self._json("DELETE", f"{self._platform}/sandbox/{sandbox_id}", allow_empty=True)

    # -- toolbox: work inside one machine --------------------------------

    async def execute(self, sandbox_id: str, command: str, timeout: float) -> tuple[int, str]:
        data = await self._json(
            "POST",
            f"{self._toolbox}/{sandbox_id}/process/execute",
            json={"command": command, "timeout": int(timeout)},
        )
        return int(data.get("exitCode", 0)), str(data.get("result", ""))

    async def upload(self, sandbox_id: str, remote_path: str, data: bytes) -> None:
        files = {"file": (posixpath.basename(remote_path) or "file", data)}
        response = await self._request(
            "POST",
            f"{self._toolbox}/{sandbox_id}/files/upload-v2",
            params={"path": remote_path},
            files=files,
        )
        self._raise_for_status(response, "upload")

    async def download(self, sandbox_id: str, remote_path: str) -> bytes:
        response = await self._request(
            "GET",
            f"{self._toolbox}/{sandbox_id}/files/download",
            params={"path": remote_path},
        )
        self._raise_for_status(response, "download")
        return response.content

    async def screenshot(self, sandbox_id: str) -> bytes:
        # A sandbox created from a plain image has no desktop running; starting
        # the computer-use service first is harmless when one already runs.
        try:
            await self._request("POST", f"{self._toolbox}/{sandbox_id}/computeruse/start")
        except CloudError:
            pass
        response = await self._request(
            "GET", f"{self._toolbox}/{sandbox_id}/computeruse/screenshot"
        )
        self._raise_for_status(response, "screenshot")
        # The endpoint answers with a JSON envelope ({"screenshot": base64}),
        # not the raw image; a host that does answer raw bytes passes through.
        if response.content.lstrip().startswith(b"{"):
            try:
                payload = response.json()
                return base64.b64decode(str(payload["screenshot"]))
            except (ValueError, KeyError, TypeError) as exc:
                raise CloudError("daytona screenshot answered without an image") from exc
        return response.content

    # -- plumbing ---------------------------------------------------------

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise CloudError(f"daytona request failed: {exc.__class__.__name__}") from exc

    def _raise_for_status(self, response: httpx.Response, what: str) -> None:
        if response.status_code >= 400:
            detail = response.text[:200].replace("\n", " ")
            raise CloudError(
                f"daytona {what} failed ({response.status_code}): {detail}", response.status_code
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
