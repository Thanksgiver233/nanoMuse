"""One model slot, tried in order: the fallback chain (Phase 2, Gate 1 ③).

A slot that needs pictures (the Hands operator) must never silently fall
back to a model that cannot take them: a text-only model handed screenshots
would answer about a screen it never saw, and the operator would act on it.
So when ``requires_vision`` is set, a candidate whose ``vision_available``
is False is skipped without ever being called, and when no candidate can
take images the chain raises :class:`VisionModelUnavailable` instead of
answering blind. A candidate that fails with an exception is tried next;
when every candidate fails, the last exception is raised.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nanomuse.llm.base import BaseLLM, DeltaCallback
from nanomuse.logger import logger
from nanomuse.schema import LLMResponse, Message


class VisionModelUnavailable(Exception):
    """No candidate for this slot can take images, so the task cannot start."""


class FallbackLLM(BaseLLM):
    """Try candidate clients in order; never downgrade vision to plain text."""

    name = "fallback"

    def __init__(
        self,
        candidates: list[Callable[[], BaseLLM]],
        requires_vision: bool = False,
    ) -> None:
        if not candidates:
            raise ValueError("a fallback chain needs at least one candidate")
        self._factories = list(candidates)
        self._clients: list[BaseLLM | None] = [None] * len(candidates)
        self.requires_vision = requires_vision
        self.vision_available: bool | None = None
        self.skipped: list[str] = []
        self.attempted: list[str] = []

    def _client(self, index: int) -> BaseLLM:
        client = self._clients[index]
        if client is None:
            client = self._factories[index]()
            self._clients[index] = client
        return client

    async def ask(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        on_delta: DeltaCallback | None = None,
        on_reasoning_delta: DeltaCallback | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        last_error: Exception | None = None
        tried_vision: list[str] = []
        for index in range(len(self._factories)):
            client = self._client(index)
            label = getattr(client, "name", f"candidate {index}")
            if self.requires_vision and client.vision_available is False:
                # A model known not to take images is never asked a vision
                # task: skipping it is the guard, not a retry.
                if label not in self.skipped:
                    self.skipped.append(label)
                    logger.warning(
                        "fallback: skipping {} for a vision task: it does not take images",
                        label,
                    )
                continue
            tried_vision.append(label)
            if label not in self.attempted:
                self.attempted.append(label)
            try:
                response = await client.ask(
                    messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    on_delta=on_delta,
                    on_reasoning_delta=on_reasoning_delta,
                    max_tokens=max_tokens,
                )
            except Exception as exc:  # noqa: BLE001 - the next candidate is the recovery
                last_error = exc
                logger.warning("fallback: {} failed ({}); trying the next candidate", label, exc)
                continue
            # Mirror what the answering client learned about pictures, so a
            # mid-task loss of vision is visible to the caller (the operator
            # stops instead of acting on a blind answer).
            self.vision_available = client.vision_available
            return response
        if self.requires_vision and not tried_vision:
            raise VisionModelUnavailable(
                "no fallback candidate takes images; refusing a vision task "
                "instead of answering without seeing the screen"
            )
        if last_error is not None:
            raise last_error
        raise VisionModelUnavailable("no fallback candidate could answer")


__all__ = ["FallbackLLM", "VisionModelUnavailable"]
