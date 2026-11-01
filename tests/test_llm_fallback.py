"""The fallback chain never silently downgrades a vision task to plain text."""

from __future__ import annotations

import pytest

from nanomuse.llm import MockLLM
from nanomuse.llm.fallback import FallbackLLM, VisionModelUnavailable
from nanomuse.schema import LLMResponse, Message


def _answer(text: str) -> LLMResponse:
    return LLMResponse(content=text, finish_reason="stop")


def _vision_llm(text: str = "seen") -> MockLLM:
    llm = MockLLM([_answer(text)])
    llm.vision_available = True
    return llm


def _text_only_llm(text: str = "blind") -> MockLLM:
    llm = MockLLM([_answer(text)])
    llm.vision_available = False
    return llm


async def test_vision_task_skips_a_text_only_candidate_without_calling_it() -> None:
    blind = _text_only_llm()
    seeing = _vision_llm()
    chain = FallbackLLM([lambda: blind, lambda: seeing], requires_vision=True)
    response = await chain.ask([Message.user("look")])
    assert response.content == "seen"
    assert blind.calls == []
    assert len(seeing.calls) == 1
    assert chain.skipped == ["mock"]


async def test_vision_task_with_no_vision_candidate_refuses() -> None:
    blind = _text_only_llm()
    chain = FallbackLLM([lambda: blind], requires_vision=True)
    with pytest.raises(VisionModelUnavailable):
        await chain.ask([Message.user("look")])
    assert blind.calls == []


async def test_a_failing_candidate_falls_back_to_the_next() -> None:
    def failing(_messages: list[Message]) -> LLMResponse:
        raise RuntimeError("endpoint down")

    primary = MockLLM([failing])
    primary.vision_available = True
    backup = _vision_llm("backup saw it")
    chain = FallbackLLM([lambda: primary, lambda: backup], requires_vision=True)
    response = await chain.ask([Message.user("look")])
    assert response.content == "backup saw it"
    assert len(primary.calls) == 1
    assert len(backup.calls) == 1


async def test_all_candidates_failing_raises_the_last_error() -> None:
    def failing(_messages: list[Message]) -> LLMResponse:
        raise RuntimeError("still down")

    first = MockLLM([failing])
    second = MockLLM([failing])
    chain = FallbackLLM([lambda: first, lambda: second])
    with pytest.raises(RuntimeError, match="still down"):
        await chain.ask([Message.user("hi")])


async def test_a_text_only_candidate_serves_a_plain_task() -> None:
    blind = _text_only_llm("plain answer")
    chain = FallbackLLM([lambda: blind])
    response = await chain.ask([Message.user("hi")])
    assert response.content == "plain answer"


async def test_vision_loss_mid_call_is_visible_on_the_chain() -> None:
    def go_blind(_messages: list[Message]) -> LLMResponse:
        llm.vision_available = False
        return _answer("answered without the picture")

    llm = MockLLM([go_blind])
    llm.vision_available = True
    chain = FallbackLLM([lambda: llm], requires_vision=True)
    await chain.ask([Message.user("look")])
    assert chain.vision_available is False


def test_an_empty_chain_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        FallbackLLM([])
