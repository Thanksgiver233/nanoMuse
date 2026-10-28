"""Voice v1: mock adapters, the feed reader, and the placeholder refusal.

No real speech service is contacted anywhere here: the mocks stand in for one,
and the tests assert the shape of the layer (what is spoken, what is refused)
rather than the sound of any voice.
"""

from __future__ import annotations

import pytest

from nanomuse.voice import (
    MockSTTAdapter,
    MockTTSAdapter,
    VoiceConfig,
    VoiceError,
    create_stt,
    create_tts,
    read_feed_aloud,
    transcribe_note,
)


def _item(title: str, text: str) -> dict[str, object]:
    return {"title": title, "text": text, "kind": "background", "reason": "goal-check-in"}


async def test_mock_stt_returns_its_script_and_silence_otherwise() -> None:
    stt = MockSTTAdapter({b"note-1": "call mum at six"})
    assert await stt.transcribe(b"note-1") == "call mum at six"
    assert await stt.transcribe(b"unheard-bytes") == ""
    assert await stt.transcribe(b"") == ""
    assert stt.calls == [b"note-1", b"unheard-bytes", b""]


async def test_mock_tts_tags_the_text_it_is_given() -> None:
    tts = MockTTSAdapter()
    audio = await tts.synthesize("Good morning.")
    assert audio == b"mock-audio:Good morning."
    assert await tts.synthesize("") == b""
    assert tts.texts == ["Good morning.", ""]


async def test_factories_only_know_mock() -> None:
    config = VoiceConfig()
    assert isinstance(create_stt(config), MockSTTAdapter)
    assert isinstance(create_tts(config), MockTTSAdapter)
    with pytest.raises(VoiceError, match="available: mock"):
        create_stt(VoiceConfig(stt="whisper"))
    with pytest.raises(VoiceError, match="available: mock"):
        create_tts(VoiceConfig(tts="polly"))


async def test_read_feed_aloud_speaks_titles_and_first_lines() -> None:
    tts = MockTTSAdapter()
    result = await read_feed_aloud(
        [
            _item("Check-in: Walk", "Morning! Did the walk happen today?\nSecond line."),
            _item("Reminder: call mum", "At six."),
        ],
        tts,
    )
    assert result.text == (
        "Check-in: Walk. Morning! Did the walk happen today? Reminder: call mum. At six."
    )
    assert result.audio == b"mock-audio:" + result.text.encode("utf-8")
    assert tts.texts == [result.text]


async def test_read_feed_aloud_caps_the_number_of_entries() -> None:
    tts = MockTTSAdapter()
    items = [_item(f"Entry {i}", f"line {i}") for i in range(8)]
    result = await read_feed_aloud(items, tts)
    assert "Entry 4" in result.text
    assert "Entry 5" not in result.text
    result = await read_feed_aloud(items, tts, max_entries=2)
    assert "Entry 1" in result.text and "Entry 2" not in result.text


async def test_read_feed_aloud_truncates_a_long_entry() -> None:
    tts = MockTTSAdapter()
    result = await read_feed_aloud([_item("Long", "x" * 300)], tts)
    assert result.text == "Long. " + "x" * 160 + "…"


async def test_read_feed_aloud_refuses_placeholders() -> None:
    tts = MockTTSAdapter()
    with pytest.raises(VoiceError, match="placeholder"):
        await read_feed_aloud([_item("Leak", "the password is {{vault:EMAIL}}")], tts)
    with pytest.raises(VoiceError, match="placeholder"):
        await read_feed_aloud([_item("Leak", "token {{oauth:github}} here")], tts)
    assert tts.texts == []


async def test_read_feed_aloud_of_nothing_says_nothing() -> None:
    tts = MockTTSAdapter()
    result = await read_feed_aloud([], tts)
    assert result.text == "" and result.audio == b""
    assert tts.texts == []


async def test_transcribe_note_returns_the_adapters_words() -> None:
    stt = MockSTTAdapter({b"audio-9": "buy milk"})
    assert await transcribe_note(b"audio-9", stt) == "buy milk"
    assert await transcribe_note(b"nothing", stt) == ""
