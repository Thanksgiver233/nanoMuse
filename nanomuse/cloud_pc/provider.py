"""The provider seam: what the cloud tools need from any cloud computer vendor.

A1 shipped with Daytona wired straight into the tools. A2 puts this
protocol between them, so a second provider is a new class in this
package and nothing else changes: the tools, the registry and the gate
never learn which vendor they are talking to.

One method set, six calls, mirroring the six tools. Every provider
returns the same normalised :class:`MachineInfo`, so the record the
registry keeps is comparable across vendors: that comparability is what
the A2 same-instruction check and the spend record rest on.

Errors are :class:`~nanomuse.cloud_pc.daytona.CloudError` from every
provider, with messages safe to show the model (never a key, never a raw
response body beyond a short excerpt the provider itself composed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class MachineInfo:
    id: str
    name: str
    cpu: int
    memory: int  # GiB
    disk: int  # GiB
    state: str
    detail: str = ""  # how the size was picked: a snapshot class, a template


class CloudProvider(Protocol):
    name: str

    async def create_machine(
        self, *, name: str, cpu: int, auto_stop_minutes: int
    ) -> MachineInfo: ...

    async def get_machine(self, machine_id: str) -> MachineInfo: ...

    async def delete_machine(self, machine_id: str) -> None: ...

    async def execute(self, machine_id: str, command: str, timeout: float) -> tuple[int, str]: ...

    async def upload(self, machine_id: str, remote_path: str, data: bytes) -> None: ...

    async def download(self, machine_id: str, remote_path: str) -> bytes: ...

    async def screenshot(self, machine_id: str) -> bytes: ...
