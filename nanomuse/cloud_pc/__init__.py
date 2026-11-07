"""Cloud computers the agent can rent by the hour (Phase 9, line A).

A cloud computer is a machine on someone else's cloud that the agent opens,
works in and destroys: the Daytona sandbox first. The package holds the
provider client, the local record of what machines exist and what they have
cost, and the budget gate the create tool checks before spending anything.

The tools themselves live in :mod:`nanomuse.tools.cloud_pc`; this package is
the machinery under them, kept separate so a second provider (A2) slots in
behind the same record and gate.
"""

from nanomuse.cloud_pc.daytona import CloudError, DaytonaClient
from nanomuse.cloud_pc.gate import CloudBudgetGate, CloudGateBlocked
from nanomuse.cloud_pc.registry import CloudRegistry, MachineRecord

__all__ = [
    "CloudBudgetGate",
    "CloudError",
    "CloudGateBlocked",
    "CloudRegistry",
    "DaytonaClient",
    "MachineRecord",
]
