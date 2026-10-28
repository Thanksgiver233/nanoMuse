"""Telegram over a fake Bot API (httpx MockTransport): polling, mentions, files, replies
that edit in place, approval buttons."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from nanomuse.channels.base import InboundMessage
from nanomuse.channels.telegram import TelegramChannel, TelegramError, _chunks


class Bus:
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


class FakeTelegram:
    """Just enough of api.telegram.org."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.next_message_id = 100
        self.fail_token = False

    def push(self, update: dict[str, Any]) -> None:
        self.updates.append(update)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/file/bot"):
            return httpx.Response(200, content=b"file-bytes")
        method = path.rsplit("/", 1)[-1]
        if self.fail_token:
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        body: dict[str, Any] = {}
        if request.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(request.content or b"{}")
        else:
            body = {"_multipart": True, "size": len(request.content)}
        self.calls.append((method, body))
        if method == "getMe":
            return httpx.Response(
                200, json={"ok": True, "result": {"id": 42, "username": "muse_bot"}}
            )
        if method == "getUpdates":
            offset = int(body.get("offset") or 0)
            due = [u for u in self.updates if u["update_id"] >= offset]
            if not due:
                await asyncio.sleep(0.02)
            return httpx.Response(200, json={"ok": True, "result": due})
        if method in ("sendMessage", "editMessageText"):
            if method == "editMessageText" and body.get("text") == "same":
                return httpx.Response(
                    400, json={"ok": False, "description": "Bad Request: message is not modified"}
                )
            self.next_message_id += 1
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": self.next_message_id}}
            )
        if method == "getFile":
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/p1.jpg"}})
        return httpx.Response(200, json={"ok": True, "result": True})

    def of(self, method: str) -> list[dict[str, Any]]:
        return [b for m, b in self.calls if m == method]


@pytest.fixture()
def tg() -> FakeTelegram:
    return FakeTelegram()


def channel(tg: FakeTelegram, **settings: Any) -> TelegramChannel:
    return TelegramChannel(
        {"bot_token": "123:abc", **settings}, transport=httpx.MockTransport(tg.handler)
    )


async def until(pred, timeout: float = 3.0):  # noqa: ANN001
    for _ in range(int(timeout / 0.02)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def test_chunks_cut_at_lines():
    assert _chunks("") == []
    text = "\n".join(f"line {i}" for i in range(2000))
    parts = _chunks(text)
    assert all(len(p) <= 4000 for p in parts) and "".join(parts).replace("\n", "") == text.replace(
        "\n", ""
    )
    assert _chunks("x" * 9000) == ["x" * 4000, "x" * 4000, "x" * 1000]


async def test_polling_brings_messages_in(tg: FakeTelegram):
    bus = Bus()
    ch = channel(tg)
    await ch.start(bus)
    assert ("telegram", "connected", "@muse_bot") in bus.statuses
    tg.push(
        {
            "update_id": 1,
            "message": {
                "message_id": 7,
                "from": {"id": 1001, "first_name": "Ann", "last_name": "Lee"},
                "chat": {"id": 1001, "type": "private"},
                "text": "hello bot",
            },
        }
    )
    await until(lambda: bus.inbound_msgs)
    msg = bus.inbound_msgs[0]
    assert msg.chat_id == "1001" and msg.sender_id == "1001" and msg.sender_name == "Ann Lee"
    assert msg.text == "hello bot" and not msg.is_group and msg.mentioned and msg.message_id == "7"
    # the offset moves past what was seen
    await until(lambda: tg.of("getUpdates") and tg.of("getUpdates")[-1]["offset"] == 2)
    # a group message: mentioned only with an @ or a reply to the bot
    tg.push(
        {
            "update_id": 2,
            "message": {
                "message_id": 8,
                "from": {"id": 1002, "username": "bob"},
                "chat": {"id": -500, "type": "supergroup", "title": "Team"},
                "text": "just chatting",
            },
        }
    )
    tg.push(
        {
            "update_id": 3,
            "message": {
                "message_id": 9,
                "from": {"id": 1002, "username": "bob"},
                "chat": {"id": -500, "type": "supergroup", "title": "Team"},
                "text": "@muse_bot summarize this",
                "photo": [{"file_id": "small"}, {"file_id": "big"}],
            },
        }
    )
    tg.push(
        {
            "update_id": 4,
            "message": {
                "message_id": 10,
                "from": {"id": 1002, "username": "bob"},
                "chat": {"id": -500, "type": "supergroup"},
                "text": "and this",
                "reply_to_message": {"from": {"id": 42, "is_bot": True}},
            },
        }
    )
    tg.push(
        {
            "update_id": 5,
            "message": {"from": {"id": 42, "is_bot": True}, "chat": {"id": 1}, "text": "me"},
        }
    )
    await until(lambda: len(bus.inbound_msgs) >= 4)
    plain, mention, reply = bus.inbound_msgs[1:4]
    assert plain.is_group and not plain.mentioned and plain.chat_name == "Team"
    assert mention.mentioned and mention.text == "summarize this" and mention.sender_name == "bob"
    assert [f.name for f in mention.files] == ["p1.jpg"] and mention.files[0].data == b"file-bytes"
    assert mention.files[0].kind == "image"
    assert reply.mentioned and reply.chat_id == "-500"
    # the bot's own messages are not fed back
    await asyncio.sleep(0.1)
    assert len(bus.inbound_msgs) == 4
    await ch.stop()


async def test_replies_stream_by_editing_and_buttons_decide(tg: FakeTelegram):
    bus = Bus()
    ch = channel(tg)
    await ch.start(bus)
    assert await ch.send("1001", "hi") == "101"
    assert tg.of("sendMessage")[-1] == {"chat_id": "1001", "text": "hi"}
    await ch.stream("1001", "s1", "Working", done=False)
    await ch.stream("1001", "s1", "Working on it", done=False)
    await ch.stream("1001", "s1", "Working on it. Done.", done=True)
    edits = tg.of("editMessageText")
    assert [e["text"] for e in edits] == ["Working on it", "Working on it. Done."]
    assert edits[0]["message_id"] == 102
    # "not modified" is not an error; other refusals are
    await ch.stream("1001", "s2", "a", done=False)
    await ch.stream("1001", "s2", "same", done=False)
    tg.fail_token = True
    with pytest.raises(TelegramError, match="Unauthorized"):
        await ch.send("1001", "x")
    tg.fail_token = False
    # approvals: two inline buttons; a press comes back as an action
    await ch.ask("1001", "ap-1", "May I?")
    asked = tg.of("sendMessage")[-1]
    buttons = asked["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["a:y:ap-1", "a:n:ap-1"]
    tg.push(
        {
            "update_id": 9,
            "callback_query": {
                "id": "cq1",
                "from": {"id": 1001},
                "data": "a:n:ap-1",
                "message": {"message_id": 103, "chat": {"id": 1001}},
            },
        }
    )
    await until(lambda: bus.actions)
    assert bus.actions[0] == (
        "telegram",
        "1001",
        "1001",
        {"approval": "ap-1", "decision": "deny", "is_group": False},
    )
    await until(lambda: tg.of("answerCallbackQuery") and tg.of("editMessageReplyMarkup"))
    # typing on/off
    await ch.typing("1001", True)
    await until(lambda: tg.of("sendChatAction"))
    await ch.typing("1001", False)
    # files go out as photo or document (multipart)
    await ch.stop()


async def test_files_out_and_verify(tg: FakeTelegram, tmp_path):  # noqa: ANN001
    ch = channel(tg)
    bus = Bus()
    await ch.start(bus)
    photo = tmp_path / "a.png"
    photo.write_bytes(b"png")
    doc = tmp_path / "notes.txt"
    doc.write_text("n", "utf-8")
    await ch.send("1001", "", files=[str(photo), str(doc), str(tmp_path / "missing.bin")])
    assert tg.of("sendPhoto") and tg.of("sendDocument")
    await ch.stop()
    # verify without a running channel opens and closes its own client
    assert await channel(tg).verify() == "@muse_bot"
    tg.fail_token = True
    with pytest.raises(TelegramError):
        await channel(tg).verify()


async def test_bad_token_fails_to_start(tg: FakeTelegram):
    tg.fail_token = True
    with pytest.raises(TelegramError, match="Unauthorized"):
        await channel(tg).start(Bus())
