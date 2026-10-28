"""The always-on companion runtime (Phase 4, mock scope).

A companion is a minimal agent runtime that keeps working while the
person's own devices are off.  It does exactly three things: scheduled
tasks, IM receive/send, and approval forwarding, and nothing else.  It
is OFF by default: a disabled runtime never starts, never schedules, and
never forwards, as if it did not exist.

This module is a simulation harness, not a deployment: the transport is
a fake in-memory link, the channel is a caller-supplied port, and no
real VPS, IM vendor, or model is ever touched.  The handover protocol
(``working`` frames + resumable task state) is the part being pinned
down here so a real runtime can implement the same shapes later.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol


class CompanionDisabledError(RuntimeError):
    """Raised when a caller tries to use a companion that is switched off."""


class ChannelPort(Protocol):
    """The one thing a companion needs from an IM channel: send and receive.

    Real channel adapters (Phase 3) speak richer protocols; the
    companion only ever needs these two calls, so the port stays small
    and a fake can stand in for every vendor in tests.
    """

    def send_message(self, chat_id: str, text: str) -> str: ...

    def poll_inbound(self) -> list[dict[str, Any]]: ...


@dataclass
class WorkingState:
    """The resumable state of one task, carried across a handover.

    Serialised onto a ``working`` frame when a device goes offline and
    restored when another runtime (or the same device, back online)
    picks the task up again.  ``step`` is the next step to run, so a
    resumed task never re-runs a step that already finished.
    """

    task_id: str
    conversation_id: str
    step: int = 0
    payload: dict[str, Any] = field(default_factory=dict)
    finished_steps: list[str] = field(default_factory=list)
    version: int = 1

    def to_frame(self) -> dict[str, Any]:
        return {
            "type": "working",
            "task_id": self.task_id,
            "cid": self.conversation_id,
            "step": self.step,
            "payload": self.payload,
            "finished_steps": self.finished_steps,
            "version": self.version,
        }

    @classmethod
    def from_frame(cls, frame: dict[str, Any]) -> WorkingState:
        if frame.get("type") != "working":
            raise ValueError(f"not a working frame: {frame.get('type')!r}")
        return cls(
            task_id=str(frame["task_id"]),
            conversation_id=str(frame["cid"]),
            step=int(frame.get("step", 0)),
            payload=dict(frame.get("payload") or {}),
            finished_steps=list(frame.get("finished_steps") or []),
            version=int(frame.get("version", 1)),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_frame(), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> WorkingState:
        return cls.from_frame(json.loads(raw))


@dataclass
class ScheduledTask:
    """One scheduled job the companion owns while devices are off."""

    task_id: str
    text: str
    run_at: float
    conversation_id: str = ""
    ran: bool = False
    result: str | None = None


class FakeTransport:
    """An in-memory stand-in for the Hub link between runtimes.

    Frames sent while the link is down are queued, never dropped, and
    delivered in order when the link comes back up -- the property the
    handover protocol depends on.  ``drop`` is test-only sabotage: it
    proves the queue, not the network, is what keeps state.
    """

    def __init__(self) -> None:
        self.online = True
        self.queued: list[dict[str, Any]] = []
        self.delivered: list[dict[str, Any]] = []

    def go_offline(self) -> None:
        self.online = False

    def go_online(self) -> None:
        self.online = True
        self.delivered.extend(self.queued)
        self.queued.clear()

    def send(self, frame: dict[str, Any]) -> None:
        if self.online:
            self.delivered.append(frame)
        else:
            self.queued.append(frame)


class CompanionRuntime:
    """The minimal always-on runtime.  Default: off."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        transport: FakeTransport | None = None,
        channel: ChannelPort | None = None,
        clock: Any = None,
    ) -> None:
        self.enabled = enabled
        self.transport = transport or FakeTransport()
        self.channel = channel
        self._clock = clock or time.monotonic
        self._tasks: dict[str, ScheduledTask] = {}
        self._handover_log: list[dict[str, Any]] = []
        self._running = False

    # -- lifecycle ----------------------------------------------------
    def start(self) -> bool:
        """Start the runtime.  Returns False (and does nothing) when off."""
        if not self.enabled:
            return False
        self._running = True
        return True

    def stop(self) -> None:
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def _require_running(self) -> None:
        if not self.enabled or not self._running:
            raise CompanionDisabledError(
                "the companion runtime is off; it does not exist until enabled"
            )

    # -- scheduled tasks (job 1 of 3) ----------------------------------
    def schedule(self, task: ScheduledTask) -> None:
        self._require_running()
        self._tasks[task.task_id] = task

    def run_due(self, now: float | None = None) -> list[ScheduledTask]:
        """Run every due task once.  A task that already ran never re-runs."""
        self._require_running()
        moment = time.time() if now is None else now
        ran: list[ScheduledTask] = []
        for task in self._tasks.values():
            if not task.ran and task.run_at <= moment:
                task.ran = True
                task.result = f"ran: {task.text}"
                ran.append(task)
        return ran

    # -- IM receive/send (job 2 of 3) ----------------------------------
    def receive_im(self) -> list[dict[str, Any]]:
        """Drain inbound IM messages.  The companion never acts on them
        here -- it only hands them to the caller, which owns the agent
        loop.  A companion with no channel configured receives nothing.
        """
        self._require_running()
        if self.channel is None:
            return []
        return self.channel.poll_inbound()

    def send_im(self, chat_id: str, text: str) -> str:
        self._require_running()
        if self.channel is None:
            raise CompanionDisabledError("no IM channel configured")
        return self.channel.send_message(chat_id, text)

    # -- approval forwarding (job 3 of 3) ------------------------------
    def forward_approval(self, approval_id: str, summary: str, device_id: str) -> dict[str, Any]:
        """Forward an approval card to the owner's device.

        The companion never decides: it frames the card, sends it over
        the transport, and returns the frame.  The decision comes back
        as a separate frame and is applied by the caller.
        """
        self._require_running()
        frame = {
            "type": "event",
            "to": device_id,
            "body": {
                "stage": "approval",
                "approval_id": approval_id,
                "summary": summary,
            },
        }
        self.transport.send(frame)
        return frame

    # -- handover -------------------------------------------------------
    def handover(self, state: WorkingState) -> WorkingState:
        """Hand a task's state to the next runtime over the transport.

        The frame is queued while offline and delivered on reconnect;
        the returned state is the exact state the other side restores,
        so no finished step is ever lost or repeated.
        """
        self._require_running()
        frame = state.to_frame()
        self.transport.send(frame)
        self._handover_log.append(frame)
        return WorkingState.from_frame(frame)

    @property
    def handover_log(self) -> list[dict[str, Any]]:
        return list(self._handover_log)
