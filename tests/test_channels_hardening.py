"""IM hardening, end to end against a fake channel: the discretion matrix, the
approval card's origin check, group read-only, redelivery idempotency, the
approval timeout and the worker that survives a crash.

The manager tests reuse the fake-channel harness of test_channels_manager.py:
one fake vendor, one real server, one scripted model.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nanomuse.channels.base import Channel, ChannelStatus, Field, InboundMessage
from nanomuse.channels.discretion import (
    ChatRule,
    DiscretionMatrix,
    DiscretionQuery,
    allows,
)
from nanomuse.channels.manager import ChannelManager
from nanomuse.channels.telegram import TelegramChannel, TelegramError
from nanomuse.channels.worker import ChannelWorkerSupervisor
from nanomuse.config import Settings
from nanomuse.llm import MockLLM
from nanomuse.schema import Function, LLMResponse, ToolCall
from nanomuse.server import create_app
from nanomuse.server.service import MuseService


def tc(name: str, **args: Any) -> ToolCall:
    return ToolCall(function=Function(name=name, arguments=json.dumps(args)))


def wait_for(pred, timeout: float = 8.0, interval: float = 0.03):  # noqa: ANN001
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met in time")


def q(**kw: Any) -> DiscretionQuery:
    base = {
        "channel": "telegram",
        "chat_id": "chat1",
        "sender_id": "u1",
        "is_group": False,
        "operation": "message",
        "sender_allowed": True,
    }
    base.update(kw)
    return DiscretionQuery(**base)


# ------------------------------------------------------------------ the matrix
def test_matrix_allows_a_direct_chat() -> None:
    assert allows(q())
    assert allows(q(operation="approve"))
    assert allows(q(operation="deliver"))


def test_matrix_denies_an_unlisted_sender() -> None:
    assert not allows(q(sender_allowed=False))


def test_matrix_denies_group_approvals_but_not_group_messages() -> None:
    assert not allows(q(is_group=True, operation="approve"))
    assert allows(q(is_group=True, operation="message", mentioned=True))
    assert not allows(q(is_group=True, operation="message", mentioned=False))
    assert allows(q(is_group=True, operation="message", mentioned=False, mention_required=False))


def test_matrix_denies_unknown_operations() -> None:
    assert not allows(q(operation="launch"))


def test_matrix_override_can_tighten_but_never_loosen() -> None:
    matrix = DiscretionMatrix(
        [ChatRule(chat_id="chat1", operations=frozenset({"deliver"}), effect="deny")]
    )
    assert not matrix.allows(q(operation="deliver"))
    assert matrix.allows(q(operation="message"))
    loosening = DiscretionMatrix([ChatRule(operations=frozenset({"approve"}), effect="allow")])
    assert not loosening.allows(q(is_group=True, operation="approve"))


# ------------------------------------------------------------------ the harness
class FakeChannel(Channel):
    name = "fake"
    label = "Fake"
    supports_edit = True
    console_url = "https://example.invalid/console"
    fields = (Field("token", "Token", "secret", required=True),)
    instances: list[FakeChannel] = []

    def __init__(self, settings: dict[str, Any]):
        super().__init__(settings)
        self.sent: list[tuple[str, str, list[str]]] = []
        self.streams: list[tuple[str, str, str, bool]] = []
        self.asks: list[tuple[str, str, str]] = []
        self.typing_calls: list[tuple[str, bool]] = []
        self.stopped = False
        FakeChannel.instances.append(self)

    async def start(self, bus) -> None:  # noqa: ANN001
        await super().start(bus)
        bus.set_status(self.name, "connected", "fake bot")

    async def stop(self) -> None:
        self.stopped = True

    async def send(self, chat_id: str, text: str, *, files: list[str] | None = None) -> str:
        self.sent.append((chat_id, text, list(files or [])))
        return "m1"

    async def stream(self, chat_id: str, stream_id: str, text: str, *, done: bool) -> None:
        self.streams.append((chat_id, stream_id, text, done))

    async def ask(self, chat_id: str, approval_id: str, text: str) -> None:
        self.asks.append((chat_id, approval_id, text))

    async def typing(self, chat_id: str, on: bool) -> None:
        self.typing_calls.append((chat_id, on))

    async def verify(self) -> str:
        return "Bot: fake"

    def texts(self, chat_id: str = "chat1") -> list[str]:
        out = [t for c, t, _ in self.sent if c == chat_id]
        out += [t for c, _, t, done in self.streams if c == chat_id and done]
        return out


def dm(text: str, sender: str = "u1", chat: str = "chat1", **kw: Any) -> InboundMessage:
    return InboundMessage(
        channel="fake", chat_id=chat, sender_id=sender, sender_name="Ann", text=text, **kw
    )


@pytest.fixture()
def chan(
    settings: Settings,
) -> Iterator[tuple[TestClient, MuseService, MockLLM, ChannelManager, FakeChannel]]:
    settings.server.token = "secret-token"
    settings.sentinel.always_allow_tools = ["python_execute"]
    settings.agent.language = "en"
    llm = MockLLM([])
    service = MuseService(settings, llm=llm)
    app = create_app(settings, service)
    manager: ChannelManager = service.channels  # type: ignore[attr-defined]
    manager.types["fake"] = FakeChannel
    manager.status["fake"] = ChannelStatus()
    manager.store.update(FakeChannel, enabled=True, values={"token": "t0k"})
    FakeChannel.instances.clear()
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {settings.server.token}"
        wait_for(lambda: manager.status["fake"].state == "connected")
        yield client, service, llm, manager, FakeChannel.instances[0]


def pair(client: TestClient, fake: FakeChannel, sender: str = "u1", chat: str = "chat1") -> str:
    fake.bus.inbound(dm("hello", sender=sender, chat=chat))
    pending = wait_for(lambda: client.get("/api/channels").json()["pending"])
    code = next(p["code"] for p in pending if p["sender_id"] == sender)
    n = len(fake.sent)
    assert client.post(f"/api/channels/pairing/{code}/approve").status_code == 200
    wait_for(lambda: len(fake.sent) > n)
    return code


def _ask_for_card(chan, llm) -> str:  # noqa: ANN001
    _client, _service, _llm, _manager, fake = chan
    llm.script.extend(
        [
            LLMResponse(content="Running it.", tool_calls=[tc("shell", command="echo from-chat")]),
            LLMResponse(content="Done, it printed from-chat."),
        ]
    )
    fake.bus.inbound(dm("run echo"))
    return wait_for(lambda: fake.asks)[0][1]


# ------------------------------------------------------------------ idempotency
def test_the_same_message_three_times_runs_once(chan) -> None:
    client, service, llm, manager, fake = chan
    pair(client, fake)
    calls: list[int] = []
    orig_send = service.send

    def counting_send(*args: Any, **kwargs: Any):  # noqa: ANN001, ANN002
        calls.append(1)
        return orig_send(*args, **kwargs)

    service.send = counting_send  # type: ignore[method-assign]
    llm.script.append(LLMResponse(content="Reply one."))
    for _ in range(3):
        fake.bus.inbound(dm("hello again", message_id="m-42"))
    wait_for(lambda: any("Reply one." in t for t in fake.texts()))
    time.sleep(0.5)
    assert len(calls) == 1
    assert sum("Reply one." in t for t in fake.texts()) == 1


def test_a_different_message_id_is_a_different_message(chan) -> None:
    client, service, llm, manager, fake = chan
    pair(client, fake)
    llm.script.append(LLMResponse(content="First."))
    llm.script.append(LLMResponse(content="Second."))
    fake.bus.inbound(dm("one", message_id="m-1"))
    wait_for(lambda: any("First." in t for t in fake.texts()))
    fake.bus.inbound(dm("two", message_id="m-2"))
    wait_for(lambda: any("Second." in t for t in fake.texts()))


# ------------------------------------------------------------------ approvals
def test_approval_answers_only_from_the_chat_it_was_asked_in(chan) -> None:
    client, service, llm, manager, fake = chan
    pair(client, fake)
    calls: list[tuple[Any, ...]] = []
    orig_decide = service.decide

    def counting_decide(*args: Any, **kwargs: Any):  # noqa: ANN001, ANN002
        calls.append(args)
        return orig_decide(*args, **kwargs)

    service.decide = counting_decide  # type: ignore[method-assign]
    approval_id = _ask_for_card(chan, llm)
    # another chat answering the card settles nothing
    fake.bus.action("fake", "chat2", "u1", {"approval": approval_id, "decision": "deny"})
    time.sleep(0.3)
    assert calls == []
    assert not any("Denied." in t for t in fake.texts())
    # the chat it was asked in settles it
    fake.bus.action("fake", "chat1", "u1", {"approval": approval_id, "decision": "deny"})
    wait_for(lambda: any("Denied." in t for t in fake.texts()))
    assert len(calls) == 1


def test_group_button_presses_never_settle_an_approval(chan) -> None:
    client, service, llm, manager, fake = chan
    pair(client, fake)
    calls: list[tuple[Any, ...]] = []
    orig_decide = service.decide

    def counting_decide(*args: Any, **kwargs: Any):  # noqa: ANN001, ANN002
        calls.append(args)
        return orig_decide(*args, **kwargs)

    service.decide = counting_decide  # type: ignore[method-assign]
    approval_id = _ask_for_card(chan, llm)
    fake.bus.action(
        "fake",
        "chat1",
        "u1",
        {"approval": approval_id, "decision": "allow", "is_group": True},
    )
    time.sleep(0.3)
    assert calls == []
    assert not any("Allowed." in t for t in fake.texts())
    assert service.decide(approval_id, False, "once", "test cleanup")


def test_group_word_approvals_are_refused_and_the_run_stays_waiting(chan) -> None:
    client, service, llm, manager, fake = chan
    pair(client, fake)
    llm.script.extend(
        [
            LLMResponse(content="Running it.", tool_calls=[tc("shell", command="echo group")]),
            LLMResponse(content="Done."),
        ]
    )
    fake.bus.inbound(dm("run echo", chat="group1", is_group=True, mentioned=True))
    _chat, approval_id, _text = wait_for(lambda: fake.asks)[0]
    assert _chat == "group1"
    fake.bus.inbound(dm("allow", chat="group1", is_group=True, mentioned=True))
    wait_for(lambda: any("direct chat" in t for t in fake.texts("group1")))
    time.sleep(0.3)
    assert service.decide(approval_id, False, "once", "test cleanup")
    wait_for(lambda: any("Done." in t for t in fake.texts("group1")))


def test_an_unanswered_approval_expires_and_cannot_be_decided_late(chan) -> None:
    client, service, llm, manager, fake = chan
    pair(client, fake)
    # a short fuse, for this test only: the card expires unanswered
    service.ui.approval_timeout = 0.3
    approval_id = _ask_for_card(chan, llm)
    wait_for(lambda: any("Done, it printed from-chat." in t for t in fake.texts()))
    # the timeout settled it: a late decision is refused (revocation by expiry)
    assert service.decide(approval_id, True, "once", "too late") is False


# ------------------------------------------------------------------ webhooks
class _Bus:
    def __init__(self) -> None:
        self.inbound_msgs: list[InboundMessage] = []
        self.statuses: list[tuple[str, str, str]] = []
        self.actions: list[tuple[str, str, str, dict[str, Any]]] = []

    def inbound(self, msg: InboundMessage) -> None:
        self.inbound_msgs.append(msg)

    def set_status(self, channel: str, state: str, detail: str = "") -> None:
        self.statuses.append((channel, state, detail))

    def action(self, channel: str, chat_id: str, sender_id: str, value: dict[str, Any]) -> None:
        self.actions.append((channel, chat_id, sender_id, value))


def _update(text: str = "hello") -> dict[str, Any]:
    return {
        "update_id": 7,
        "message": {
            "message_id": 9,
            "from": {"id": 11, "first_name": "Ann"},
            "chat": {"id": 22, "type": "private"},
            "text": text,
        },
    }


async def test_webhook_with_the_wrong_secret_reaches_nobody() -> None:
    channel = TelegramChannel({"bot_token": "t", "webhook_secret": "s3cr3t"})
    bus = _Bus()
    channel.bus = bus
    with pytest.raises(TelegramError, match="secret"):
        await channel.handle_webhook(_update(), "wrong")
    with pytest.raises(TelegramError, match="secret"):
        await channel.handle_webhook(_update(), None)
    assert bus.inbound_msgs == []


async def test_webhook_without_a_configured_secret_is_refused() -> None:
    channel = TelegramChannel({"bot_token": "t"})
    bus = _Bus()
    channel.bus = bus
    with pytest.raises(TelegramError, match="secret"):
        await channel.handle_webhook(_update(), "anything")
    assert bus.inbound_msgs == []


async def test_webhook_with_the_right_secret_is_handled_like_a_poll() -> None:
    channel = TelegramChannel({"bot_token": "t", "webhook_secret": "s3cr3t"})
    bus = _Bus()
    channel.bus = bus
    await channel.handle_webhook(_update("hello via webhook"), "s3cr3t")
    assert [m.text for m in bus.inbound_msgs] == ["hello via webhook"]
    assert channel.verify_webhook_secret("s3cr3t")
    assert not channel.verify_webhook_secret("s3cr3t ")


# ------------------------------------------------------------------ the worker
class _WorkerBus:
    def __init__(self) -> None:
        self.inbound_msgs: list[InboundMessage] = []
        self.statuses: list[tuple[str, str, str]] = []
        self.actions: list[tuple[str, str, str, dict[str, Any]]] = []

    def inbound(self, msg: InboundMessage) -> None:
        self.inbound_msgs.append(msg)

    def set_status(self, channel: str, state: str, detail: str = "") -> None:
        self.statuses.append((channel, state, detail))

    def action(self, channel: str, chat_id: str, sender_id: str, value: dict[str, Any]) -> None:
        self.actions.append((channel, chat_id, sender_id, value))


async def _wait_for(pred, timeout: float = 10.0):  # noqa: ANN001
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


async def test_worker_crash_does_not_take_the_parent_down() -> None:
    bus = _WorkerBus()
    supervisor = ChannelWorkerSupervisor("mock-crash", {}, bus, mock=True)
    try:
        await supervisor.start()
        assert await _wait_for(lambda: len(bus.inbound_msgs) == 1)
        assert bus.inbound_msgs[0].text == "hello from the worker"
        assert await _wait_for(lambda: supervisor.crashed)
        assert any(state == "error" for _, state, _ in bus.statuses)
        # the parent is alive: it can still do work after the crash
        assert supervisor.restarts == 0
        await supervisor.restart()
        assert supervisor.restarts == 1
        assert await _wait_for(lambda: len(bus.inbound_msgs) == 2)
        assert await _wait_for(lambda: supervisor.crashed)
    finally:
        await supervisor.stop()
    assert not supervisor.started
