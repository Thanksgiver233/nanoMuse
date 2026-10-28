"""The companion runtime package (Phase 4, mock scope)."""

from nanomuse.companion.runtime import (
    ChannelPort,
    CompanionDisabledError,
    CompanionRuntime,
    FakeTransport,
    ScheduledTask,
    WorkingState,
)

__all__ = [
    "ChannelPort",
    "CompanionDisabledError",
    "CompanionRuntime",
    "FakeTransport",
    "ScheduledTask",
    "WorkingState",
]
