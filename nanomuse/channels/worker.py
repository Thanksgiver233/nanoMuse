"""One channel in its own process, so a crashing vendor loop cannot take the
agent down with it.

The child runs the channel and writes what arrives as one JSON object per line
on stdout (an inbound message, a status change, a button press); the parent
(:class:`ChannelWorkerSupervisor`) reads those lines and hands them to the
manager's bus. The child never shares memory with the agent: the only thing
that crosses the process boundary is the JSON. When the child dies — a vendor
error the loop did not survive, a killed process — the supervisor records the
crash, reports the channel as ``error``, and the agent keeps running; a
restart spawns a fresh child.

The child is started with ``python -m nanomuse.channels.worker`` and a JSON
blob of the channel's settings on stdin. It is not wired into the manager by
default: the in-process path stays as it is until the worker has soaked
against a real vendor. ``--mock`` runs a fake channel instead of a vendor one,
for tests: it delivers one message and then crashes, which is the case the
supervisor exists for.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from dataclasses import asdict
from typing import Any

from nanomuse.channels.base import Channel, ChannelBus, InboundMessage
from nanomuse.channels.telegram import TelegramChannel
from nanomuse.logger import logger

CHANNELS: dict[str, type[Channel]] = {"telegram": TelegramChannel}


class _StdoutBus(ChannelBus):
    """The child's bus: every callback becomes a JSON line on stdout."""

    def inbound(self, msg: InboundMessage) -> None:
        data = asdict(msg)
        data["files"] = [
            {
                "name": f["name"],
                "data": base64.b64encode(f["data"]).decode("ascii"),
                "kind": f["kind"],
            }
            for f in data["files"]
        ]
        _emit({"kind": "inbound", "message": data})

    def set_status(self, channel: str, state: str, detail: str = "") -> None:
        _emit({"kind": "status", "channel": channel, "state": state, "detail": detail})

    def action(self, channel: str, chat_id: str, sender_id: str, value: dict[str, Any]) -> None:
        _emit(
            {
                "kind": "action",
                "channel": channel,
                "chat_id": chat_id,
                "sender_id": sender_id,
                "value": value,
            }
        )


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class _MockCrashChannel(Channel):
    """A test double that delivers one message and then dies ungracefully."""

    name = "mock-crash"
    label = "Mock crash"

    async def start(self, bus: ChannelBus) -> None:
        await super().start(bus)
        bus.set_status(self.name, "connected")
        bus.inbound(
            InboundMessage(
                channel=self.name,
                chat_id="chat-1",
                sender_id="sender-1",
                text="hello from the worker",
                message_id="m-1",
            )
        )
        raise RuntimeError("mock channel crashed")


async def _run_child(channel_name: str, settings: dict[str, Any], mock: bool) -> int:
    if mock:
        channel: Channel = _MockCrashChannel(settings)
    else:
        cls = CHANNELS.get(channel_name)
        if cls is None:
            print(f"unknown channel: {channel_name}", file=sys.stderr)
            return 2
        channel = cls(settings)
    bus = _StdoutBus()
    try:
        await channel.start(bus)
    except Exception as exc:  # noqa: BLE001 - the exit code is the crash report
        print(f"channel {channel_name} failed: {exc}", file=sys.stderr)
        return 1
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        return 0
    finally:
        try:
            await channel.stop()
        except Exception:  # noqa: BLE001 - stopping a dead channel must not hang the exit
            pass


def main(argv: list[str]) -> int:
    mock = "--mock" in argv
    rest = [a for a in argv if a != "--mock"]
    if len(rest) != 2 or rest[0] != "--channel":
        print(
            "usage: python -m nanomuse.channels.worker --channel <name> [--mock]", file=sys.stderr
        )
        return 2
    try:
        settings = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        print("settings on stdin must be JSON", file=sys.stderr)
        return 2
    if not isinstance(settings, dict):
        print("settings on stdin must be a JSON object", file=sys.stderr)
        return 2
    return asyncio.run(_run_child(rest[1], settings, mock))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))


# ------------------------------------------------------------------ supervisor
class ChannelWorkerSupervisor:
    """The parent side: spawn the child, read its lines, survive its death."""

    def __init__(
        self,
        channel_name: str,
        settings: dict[str, Any],
        bus: ChannelBus,
        *,
        mock: bool = False,
        python: str = sys.executable,
    ):
        self.channel_name = channel_name
        self.settings = dict(settings)
        self.bus = bus
        self._mock = mock
        self._python = python
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        self.crashed = False
        self.restarts = 0
        self.started = False

    async def start(self) -> None:
        if self.started:
            return
        self.started = True
        self.crashed = False
        args = [self._python, "-m", "nanomuse.channels.worker", "--channel", self.channel_name]
        if self._mock:
            args.append("--mock")
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(self.settings).encode("utf-8"))
        await self._proc.stdin.drain()
        self._proc.stdin.close()
        self._reader = asyncio.create_task(self._read_lines())
        self._stderr = asyncio.create_task(self._drain_stderr())

    async def stop(self) -> None:
        self.started = False
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        for task in (self._reader, self._stderr):
            if task is not None:
                task.cancel()
        self._reader = self._stderr = None

    async def restart(self) -> None:
        """Spawn a fresh child after a crash. The bus and the manager never notice
        the gap except for the ``error`` status the crash reported."""
        await self.stop()
        self.restarts += 1
        await self.start()

    async def _read_lines(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            async for raw in self._proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    self._dispatch(line)
        except asyncio.CancelledError:
            return
        code = await self._proc.wait()
        if code not in (0, None) and self.started:
            # a stop() sets started False first, so a terminated child is not
            # reported as a crash; only an unexpected death is
            self.crashed = True
            logger.warning("channel worker {} crashed (exit {})", self.channel_name, code)
            self.bus.set_status(self.channel_name, "error", f"worker exited with code {code}")

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            async for raw in self._proc.stderr:
                logger.debug("channel worker {}: {}", self.channel_name, raw.decode().strip())
        except asyncio.CancelledError:
            return

    def _dispatch(self, line: str) -> None:
        try:
            payload = json.loads(line)
        except ValueError:
            logger.warning("channel worker {}: unparsable line: {}", self.channel_name, line[:200])
            return
        kind = payload.get("kind")
        if kind == "inbound":
            data = payload["message"]
            files = data.get("files") or []
            from nanomuse.channels.base import InboundFile

            self.bus.inbound(
                InboundMessage(
                    channel=data["channel"],
                    chat_id=data["chat_id"],
                    sender_id=data["sender_id"],
                    sender_name=data.get("sender_name", ""),
                    text=data.get("text", ""),
                    files=[
                        InboundFile(
                            name=f["name"],
                            data=base64.b64decode(f["data"]),
                            kind=f.get("kind", "file"),
                        )
                        for f in files
                    ],
                    is_group=bool(data.get("is_group")),
                    mentioned=bool(data.get("mentioned", True)),
                    message_id=data.get("message_id", ""),
                    chat_name=data.get("chat_name", ""),
                )
            )
        elif kind == "status":
            self.bus.set_status(
                payload["channel"], payload["state"], str(payload.get("detail", ""))
            )
        elif kind == "action":
            self.bus.action(
                payload["channel"], payload["chat_id"], payload["sender_id"], payload["value"]
            )


__all__ = ["CHANNELS", "ChannelWorkerSupervisor"]
