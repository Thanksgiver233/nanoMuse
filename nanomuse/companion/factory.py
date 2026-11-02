"""Building the companion runtime from settings.

One place turns ``[companion]`` settings into a runtime with its real
parts attached: the persistent store (tasks and working states survive
restarts) and, when a bot token is configured, the Telegram port as
its channel.  A disabled companion builds into a runtime that refuses
to start, exactly as the Phase 4 harness did.
"""

from __future__ import annotations

from nanomuse.companion.runtime import CompanionRuntime
from nanomuse.companion.store import CompanionStore
from nanomuse.companion.telegram_port import TelegramPort
from nanomuse.config import Settings


def build_runtime(settings: Settings) -> CompanionRuntime:
    companion = settings.companion
    channel = None
    if companion.telegram_bot_token:
        channel = TelegramPort(
            companion.telegram_bot_token,
            offset_path=settings.companion_telegram_offset_file,
        )
    return CompanionRuntime(
        enabled=companion.enabled,
        channel=channel,
        store=CompanionStore(settings.companion_file),
    )
