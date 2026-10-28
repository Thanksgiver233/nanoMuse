"""Companion runtime tests: off means off, handover loses nothing."""

from __future__ import annotations

from typing import Any

import pytest

from nanomuse.companion.runtime import (
    CompanionDisabledError,
    CompanionRuntime,
    FakeTransport,
    ScheduledTask,
    WorkingState,
)


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.inbound: list[dict[str, Any]] = []

    def send_message(self, chat_id: str, text: str) -> str:
        self.sent.append((chat_id, text))
        return f"msg-{len(self.sent)}"

    def poll_inbound(self) -> list[dict[str, Any]]:
        inbound, self.inbound = self.inbound, []
        return inbound


def running_runtime(**kwargs: Any) -> CompanionRuntime:
    runtime = CompanionRuntime(enabled=True, **kwargs)
    assert runtime.start() is True
    return runtime


def test_a_disabled_runtime_does_not_exist() -> None:
    runtime = CompanionRuntime()
    assert runtime.enabled is False
    assert runtime.start() is False
    assert runtime.running is False
    with pytest.raises(CompanionDisabledError):
        runtime.schedule(ScheduledTask("t1", "do it", 0.0))
    with pytest.raises(CompanionDisabledError):
        runtime.run_due()
    with pytest.raises(CompanionDisabledError):
        runtime.receive_im()
    with pytest.raises(CompanionDisabledError):
        runtime.forward_approval("a1", "approve?", "phone")
    with pytest.raises(CompanionDisabledError):
        runtime.handover(WorkingState("t1", "c1"))


def test_scheduled_tasks_run_once_when_due() -> None:
    runtime = running_runtime()
    runtime.schedule(ScheduledTask("t1", "morning brief", 10.0))
    runtime.schedule(ScheduledTask("t2", "later", 99.0))
    assert runtime.run_due(now=5.0) == []
    ran = runtime.run_due(now=10.0)
    assert [t.task_id for t in ran] == ["t1"]
    assert ran[0].result == "ran: morning brief"
    assert runtime.run_due(now=100.0)[0].task_id == "t2"
    assert runtime.run_due(now=200.0) == []


def test_im_receive_and_send_go_through_the_channel_port() -> None:
    channel = FakeChannel()
    channel.inbound.append({"chat_id": "c1", "text": "ping"})
    runtime = running_runtime(channel=channel)
    assert runtime.receive_im() == [{"chat_id": "c1", "text": "ping"}]
    assert runtime.receive_im() == []
    assert runtime.send_im("c1", "pong") == "msg-1"
    assert channel.sent == [("c1", "pong")]


def test_im_without_a_channel_receives_nothing() -> None:
    runtime = running_runtime()
    assert runtime.receive_im() == []
    with pytest.raises(CompanionDisabledError):
        runtime.send_im("c1", "hi")


def test_working_state_survives_a_json_round_trip() -> None:
    state = WorkingState("t1", "c1", step=3, payload={"q": "x"}, finished_steps=["s1", "s2"])
    restored = WorkingState.from_json(state.to_json())
    assert restored == state


def test_handover_queues_while_offline_and_delivers_in_order() -> None:
    transport = FakeTransport()
    runtime = running_runtime(transport=transport)
    transport.go_offline()
    first = runtime.handover(WorkingState("t1", "c1", step=1))
    second = runtime.handover(WorkingState("t2", "c1", step=2))
    assert transport.delivered == []
    assert transport.queued[0]["task_id"] == "t1"
    transport.go_online()
    assert [f["task_id"] for f in transport.delivered] == ["t1", "t2"]
    assert transport.queued == []
    assert first.step == 1 and second.step == 2
    assert runtime.handover_log[0]["task_id"] == "t1"


def test_handover_after_reconnect_keeps_finished_steps() -> None:
    transport = FakeTransport()
    runtime = running_runtime(transport=transport)
    transport.go_offline()
    state = WorkingState("t1", "c1", step=2, finished_steps=["draft", "check"])
    runtime.handover(state)
    transport.go_online()
    restored = WorkingState.from_frame(transport.delivered[0])
    assert restored.finished_steps == ["draft", "check"]
    assert restored.step == 2


def test_forward_approval_frames_but_never_decides() -> None:
    transport = FakeTransport()
    runtime = running_runtime(transport=transport)
    frame = runtime.forward_approval("a1", "delete file?", "phone-1")
    assert frame["body"]["stage"] == "approval"
    assert frame["body"]["approval_id"] == "a1"
    assert frame["to"] == "phone-1"
    assert transport.delivered == [frame]
    assert "decision" not in frame["body"]
