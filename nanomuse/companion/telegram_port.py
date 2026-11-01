"""A real Telegram channel port for the companion runtime.

The companion needs exactly two calls from an IM channel
(``ChannelPort`` in ``runtime.py``): send a message, and poll for
inbound ones.  The full Telegram channel in ``nanomuse.channels`` is
async and manager-driven, built for interactive chat; this port is the
small synchronous version for the always-on runtime, over the same Bot
API with nothing but httpx:

- ``send_message`` posts ``sendMessage`` and returns the message id.
- ``poll_inbound`` calls ``getUpdates`` once (no long poll; the
  runtime's own loop decides how often to ask) and returns the text
  messages it finds, in the companion's plain dict shape.  The update
  offset is kept in memory and, when ``offset_path`` is given, on disk,
  so a restart does not re-deliver messages the runtime already saw.

The bot token is a secret: it is part of every request URL, so errors
are reported with the token redacted, and nothing here logs a URL.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

API = "https://api.telegram.org"


class TelegramPortError(RuntimeError):
    """A Bot API call failed; the message never carries the bot token."""


class TelegramPort:
    """The companion's two calls, against a real Telegram bot."""

    def __init__(
        self,
        bot_token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        offset_path: Path | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not bot_token:
            raise ValueError("a Telegram bot token is required")
        self._token = bot_token
        self._client = httpx.Client(
            base_url=f"{API}/bot{bot_token}", transport=transport, timeout=timeout
        )
        self._offset_path = Path(offset_path) if offset_path else None
        self._offset = self._load_offset()

    # ------------------------------------------------------------------ port
    def send_message(self, chat_id: str, text: str) -> str:
        result = self._call("sendMessage", {"chat_id": chat_id, "text": text})
        message = result if isinstance(result, dict) else {}
        return str(message.get("message_id") or "")

    def poll_inbound(self) -> list[dict[str, Any]]:
        updates = self._call("getUpdates", {"offset": self._offset, "timeout": 0})
        inbound: list[dict[str, Any]] = []
        if not isinstance(updates, list):
            return inbound
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int) and update_id >= self._offset:
                self._offset = update_id + 1
                self._save_offset()
            message = update.get("message")
            if not isinstance(message, dict):
                continue  # edits, callbacks, joins: not inbound text for the companion
            chat = message.get("chat") or {}
            sender = message.get("from") or {}
            text = message.get("text")
            if not isinstance(text, str) or not text:
                continue
            inbound.append(
                {
                    "chat_id": str(chat.get("id") or ""),
                    "sender_id": str(sender.get("id") or ""),
                    "sender_name": str(sender.get("first_name") or ""),
                    "text": text,
                    "message_id": str(message.get("message_id") or ""),
                }
            )
        return inbound

    # ------------------------------------------------------------------ plumbing
    def _call(self, method: str, params: dict[str, Any]) -> Any:
        try:
            response = self._client.post(f"/{method}", json=params)
        except httpx.HTTPError as exc:
            raise TelegramPortError(f"Telegram {method} failed: {self._redact(str(exc))}") from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not isinstance(body, dict) or not body.get("ok"):
            detail = ""
            if isinstance(body, dict):
                detail = str(body.get("description") or "")
            raise TelegramPortError(
                f"Telegram {method} failed with HTTP {response.status_code}"
                + (f": {detail}" if detail else "")
            )
        return body.get("result")

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***")

    def _load_offset(self) -> int:
        if self._offset_path is None or not self._offset_path.exists():
            return 0
        try:
            return int(json.loads(self._offset_path.read_text(encoding="utf-8"))["offset"])
        except (ValueError, KeyError, TypeError):
            return 0

    def _save_offset(self) -> None:
        if self._offset_path is None:
            return
        self._offset_path.parent.mkdir(parents=True, exist_ok=True)
        self._offset_path.write_text(json.dumps({"offset": self._offset}), encoding="utf-8")

    def close(self) -> None:
        self._client.close()
