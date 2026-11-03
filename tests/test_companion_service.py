"""Phase 6 (C): the companion inside the server, and a compressed long run.

The service wrapper ticks the runtime while the server runs.  The
long-run test compresses two virtual days into seconds: tasks fall
due hour by hour, the service is stopped and rebuilt ten times along
the way (the store is the only thing that survives), and at the end
every task must have run exactly once.  That is a simulation of the
8 hour soak, not the soak itself: the real one needs a deployment and
wall-clock time, and docs/always-on.md keeps saying so.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from nanomuse.companion.runtime import CompanionRuntime, ScheduledTask
from nanomuse.companion.service import CompanionService
from nanomuse.companion.store import CompanionStore
from nanomuse.config import Settings
from nanomuse.llm import MockLLM
from nanomuse.server.api import create_app
from nanomuse.server.service import MuseService


class FakeChannel:
    """A channel port with a script: each poll hands over the next batch."""

    def __init__(self, batches: list[list[dict[str, Any]]] | None = None) -> None:
        self.batches = list(batches or [])
        self.sent: list[tuple[str, str]] = []

    def send_message(self, chat_id: str, text: str) -> str:
        self.sent.append((chat_id, text))
        return "1"

    def poll_inbound(self) -> list[dict[str, Any]]:
        return self.batches.pop(0) if self.batches else []


def wait_for(pred, timeout: float = 5.0) -> None:  # noqa: ANN001
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


async def await_for(pred, timeout: float = 5.0) -> None:  # noqa: ANN001
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


# ------------------------------------------------------------------ the wrapper


async def test_the_service_ticks_publishes_and_stops(tmp_path: Path) -> None:
    channel = FakeChannel(batches=[[{"chat_id": "5", "text": "ping"}]])
    runtime = CompanionRuntime(
        enabled=True, channel=channel, store=CompanionStore(tmp_path / "c.json")
    )
    events: list[dict[str, Any]] = []
    service = CompanionService(runtime, publish=events.append, poll_seconds=0.05)
    assert await service.start()
    runtime.schedule(ScheduledTask(task_id="t", text="due now", run_at=0.0))
    await await_for(lambda: len(service.inbox) == 1)
    await await_for(lambda: runtime.store.tasks()[0].ran)  # type: ignore[union-attr]
    assert events and events[0]["kind"] == "companion"
    assert events[0]["message"]["text"] == "ping"
    await service.stop()
    assert not service.running
    assert service.view()["inbox"] == 1


async def test_a_disabled_companion_starts_nothing(tmp_path: Path) -> None:
    runtime = CompanionRuntime(enabled=False, store=CompanionStore(tmp_path / "c.json"))
    service = CompanionService(runtime, poll_seconds=0.05)
    assert await service.start() is False
    assert not service.running
    await service.stop()  # stopping a never-started service is fine


async def test_a_failing_channel_never_kills_the_loop(tmp_path: Path) -> None:
    class Down:
        def send_message(self, chat_id: str, text: str) -> str:
            raise RuntimeError("channel down")

        def poll_inbound(self) -> list[dict[str, Any]]:
            raise RuntimeError("channel down")

    runtime = CompanionRuntime(
        enabled=True, channel=Down(), store=CompanionStore(tmp_path / "c.json")
    )
    service = CompanionService(runtime, poll_seconds=0.05)
    assert await service.start()
    await asyncio.sleep(0.22)  # several ticks, all failing
    assert service.running  # still alive, still ticking
    await service.stop()


# ------------------------------------------------------------------ the long run


def test_two_virtual_days_with_ten_restarts_run_every_task_once(tmp_path: Path) -> None:
    path = tmp_path / "companion.json"
    t0 = 1_000_000.0
    total = 48  # one task an hour for two days
    for restart in range(10):
        runtime = CompanionRuntime(enabled=True, store=CompanionStore(path))
        assert runtime.start()
        if restart == 0:
            for hour in range(total):
                runtime.schedule(
                    ScheduledTask(task_id=f"h{hour}", text=f"hour {hour}", run_at=t0 + hour * 3600)
                )
        # This leg covers the next stretch of virtual time, then "crashes":
        # the runtime is dropped without stop(), as a killed process would be.
        runtime.run_due(now=t0 + (restart + 1) * (total * 3600 / 10))
    store = CompanionStore(path)
    tasks = store.tasks()
    assert len(tasks) == total
    assert all(task.ran for task in tasks)


# ------------------------------------------------------------------ in the server


def test_the_server_starts_and_stops_the_companion(settings: Settings) -> None:
    settings.companion.enabled = True
    settings.companion.poll_seconds = 1.0
    service = MuseService(settings, llm=MockLLM([]))
    service.companion.runtime.channel = FakeChannel(
        batches=[[{"chat_id": "9", "text": "hello companion"}]]
    )
    app = create_app(settings, service)
    with TestClient(app):
        wait_for(lambda: service.companion.running)
        wait_for(lambda: len(service.companion.inbox) == 1)
        assert service.companion_view()["channel"] == "FakeChannel"
    assert not service.companion.running


def test_the_server_leaves_a_disabled_companion_off(settings: Settings) -> None:
    service = MuseService(settings, llm=MockLLM([]))
    app = create_app(settings, service)
    with TestClient(app):
        assert service.companion_view() == {
            "enabled": False,
            "running": False,
            "channel": "",
            "inbox": 0,
        }
    assert not service.companion.running
