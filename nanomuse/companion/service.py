"""The companion as a service: a heartbeat loop around the runtime.

``CompanionRuntime`` is synchronous and knows nothing about the
server.  This wrapper is the seam between the two: while the server
runs, an asyncio task ticks the runtime (run due tasks, drain the
channel), publishes every inbound message on the event bus for the
web app to show, and keeps the messages in an inbox the caller can
read.  The channel port is synchronous (httpx), so polling runs in a
thread; a failed poll is logged and the loop carries on, because a
channel being down must never take the companion, or the server,
with it.

Starting and stopping follow the runtime's own rule: a disabled
companion's service starts nothing and reports not running.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from nanomuse.companion.runtime import CompanionRuntime
from nanomuse.logger import logger


class CompanionService:
    """Owns the companion runtime's lifecycle inside the server."""

    def __init__(
        self,
        runtime: CompanionRuntime,
        *,
        publish: Callable[[dict[str, Any]], None] | None = None,
        poll_seconds: float = 15.0,
    ) -> None:
        self.runtime = runtime
        self._publish = publish or (lambda event: None)
        self.poll_seconds = max(0.05, poll_seconds)
        self.inbox: list[dict[str, Any]] = []
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self.runtime.running and self._task is not None and not self._task.done()

    async def start(self) -> bool:
        """Start the runtime and its loop.  False when the companion is off."""
        if not self.runtime.start():
            return False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="companion")
        return True

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.runtime.stop()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001: the loop must outlive a bad tick
                logger.warning("companion loop error: {}", exc)
            await asyncio.sleep(self.poll_seconds)

    async def tick(self) -> None:
        """One heartbeat: due tasks first, then whatever the channel brought."""
        self.runtime.run_due()
        inbound = await asyncio.to_thread(self.runtime.receive_im)
        for message in inbound:
            self.inbox.append(message)
            self._publish({"kind": "companion", "message": message})

    def view(self) -> dict[str, Any]:
        channel = self.runtime.channel
        return {
            "enabled": self.runtime.enabled,
            "running": self.running,
            "channel": type(channel).__name__ if channel is not None else "",
            "inbox": len(self.inbox),
        }
