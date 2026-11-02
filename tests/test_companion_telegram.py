"""Phase 6 (C): the companion's real Telegram port and on-disk persistence.

The port is exercised against an httpx MockTransport, so no network
and no real bot is involved: what is pinned here is the request shape,
the update parsing, the offset bookkeeping and the token redaction.
The store tests pin the restart behaviour: tasks, ran flags and
working states survive, and a task that ran before a restart does not
run again after it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from nanomuse.companion.factory import build_runtime
from nanomuse.companion.runtime import CompanionRuntime, ScheduledTask, WorkingState
from nanomuse.companion.store import CompanionStore
from nanomuse.companion.telegram_port import TelegramPort, TelegramPortError
from nanomuse.config import Settings

TOKEN = "123456:TEST-TOKEN"


def port_with(handler, tmp_path: Path | None = None, offset: bool = False) -> TelegramPort:
    return TelegramPort(
        TOKEN,
        transport=httpx.MockTransport(handler),
        offset_path=(tmp_path / "offset.json") if offset and tmp_path else None,
    )


def ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


# ------------------------------------------------------------------ the port


def test_send_message_posts_and_returns_the_id() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return ok({"message_id": 42})

    port = port_with(handler)
    assert port.send_message("777", "hello") == "42"
    assert seen["url"].endswith(f"/bot{TOKEN}/sendMessage")
    assert seen["body"] == {"chat_id": "777", "text": "hello"}


def test_poll_inbound_parses_text_messages_and_advances_the_offset(tmp_path: Path) -> None:
    updates = [
        {
            "update_id": 10,
            "message": {
                "message_id": 5,
                "chat": {"id": 777},
                "from": {"id": 31, "first_name": "Ana"},
                "text": "are you there",
            },
        },
        {"update_id": 11, "edited_message": {"text": "not an inbound message"}},
        {
            "update_id": 12,
            "message": {"message_id": 6, "chat": {"id": 777}, "from": {"id": 31}, "photo": []},
        },
    ]
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return ok(updates if len(calls) == 1 else [])

    port = port_with(handler, tmp_path, offset=True)
    inbound = port.poll_inbound()
    assert inbound == [
        {
            "chat_id": "777",
            "sender_id": "31",
            "sender_name": "Ana",
            "text": "are you there",
            "message_id": "5",
        }
    ]
    # The next poll starts after the last update seen, persisted to disk.
    assert port.poll_inbound() == []
    assert calls[1]["offset"] == 13
    reopened = port_with(handler, tmp_path, offset=True)
    assert reopened.poll_inbound() == []
    assert calls[2]["offset"] == 13


def test_a_failed_call_raises_without_leaking_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    port = port_with(handler)
    with pytest.raises(TelegramPortError) as caught:
        port.send_message("777", "hello")
    assert "Unauthorized" in str(caught.value)
    assert TOKEN not in str(caught.value)


def test_the_runtime_sends_and_receives_through_the_port() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("sendMessage"):
            return ok({"message_id": 9})
        return ok(
            [
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 2,
                        "chat": {"id": 5},
                        "from": {"id": 6},
                        "text": "ping",
                    },
                }
            ]
        )

    runtime = CompanionRuntime(enabled=True, channel=port_with(handler))
    assert runtime.start()
    assert runtime.send_im("5", "pong") == "9"
    assert runtime.receive_im()[0]["text"] == "ping"


# ------------------------------------------------------------------ the store


def test_tasks_and_working_states_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "companion.json"
    runtime = CompanionRuntime(enabled=True, store=CompanionStore(path))
    assert runtime.start()
    runtime.schedule(ScheduledTask(task_id="t1", text="water the plants", run_at=100.0))
    runtime.run_due(now=150.0)
    runtime.handover(WorkingState(task_id="t1", conversation_id="c1", step=3,
                                   finished_steps=["a", "b"]))

    restarted = CompanionRuntime(enabled=True, store=CompanionStore(path))
    assert restarted.start()
    # The task is back, already ran, and does not run twice.
    assert restarted.run_due(now=200.0) == []
    state = restarted.stored_working("t1")
    assert state is not None
    assert state.step == 3
    assert state.finished_steps == ["a", "b"]


def test_a_corrupt_store_loads_empty_and_says_so(tmp_path: Path) -> None:
    path = tmp_path / "companion.json"
    path.write_text("{broken", encoding="utf-8")
    store = CompanionStore(path)
    assert store.corrupt is True
    assert store.tasks() == []


def test_the_factory_builds_a_disabled_runtime_by_default(tmp_path: Path) -> None:
    runtime = build_runtime(Settings(data_dir=tmp_path))
    assert runtime.start() is False  # off means off


def test_the_factory_wires_the_store_and_channel(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    settings.companion.enabled = True
    settings.companion.telegram_bot_token = TOKEN
    runtime = build_runtime(settings)
    assert runtime.start()
    assert isinstance(runtime.channel, TelegramPort)
    runtime.schedule(ScheduledTask(task_id="x", text="later", run_at=1.0))
    assert (tmp_path / "companion.json").exists()
