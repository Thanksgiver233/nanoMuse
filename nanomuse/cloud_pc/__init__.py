"""Cloud computers the agent can rent by the hour (Phase 9, line A).

A cloud computer is a machine on someone else's cloud that the agent opens,
works in and destroys. Two providers sit behind one protocol
(:mod:`nanomuse.cloud_pc.provider`): Daytona sandboxes (A1) and InstaBox
machines (A2). The package holds the provider clients, the local record of
what machines exist and what they have cost, and the budget gate the
create tool checks before spending anything.

The tools themselves live in :mod:`nanomuse.tools.cloud_pc`; this package
is the machinery under them.
"""

from nanomuse.cloud_pc.daytona import CloudError, DaytonaClient
from nanomuse.cloud_pc.gate import CloudBudgetGate, CloudGateBlocked
from nanomuse.cloud_pc.instabox import InstaBoxClient
from nanomuse.cloud_pc.provider import CloudProvider, MachineInfo
from nanomuse.cloud_pc.registry import CloudRegistry, MachineRecord

__all__ = [
    "CloudBudgetGate",
    "CloudError",
    "CloudGateBlocked",
    "CloudProvider",
    "CloudRegistry",
    "DaytonaClient",
    "InstaBoxClient",
    "MachineInfo",
    "MachineRecord",
    "build_provider",
]


def build_provider(name: str, api_key: str, *, template: str = "agent-browser") -> CloudProvider:
    """The client for a configured provider name. Raises ValueError on a
    name this build does not know, so a typo in the config is loud."""
    if name == "daytona":
        return DaytonaClient(api_key)
    if name == "instabox":
        return InstaBoxClient(api_key, template=template)
    raise ValueError(f"unknown cloud_pc provider: {name!r} (expected daytona or instabox)")
