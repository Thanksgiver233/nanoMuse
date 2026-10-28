"""The speech adapters: what a speech service looks like from the inside.

An adapter is two methods and no opinions about keys, endpoints or audio
formats: the caller hands it bytes and gets text, or hands it text and gets
bytes. ``VoiceConfig`` names the providers to use; ``"mock"`` is the only one
that ships, and naming anything else is an error that lists what exists, so a
typo never silently becomes a different service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class VoiceError(RuntimeError):
    pass


class STTAdapter(Protocol):
    """Speech to text: audio bytes in, the words out."""

    async def transcribe(self, audio: bytes, *, language: str = "") -> str:
        """The words in ``audio``. Empty audio is empty text, not an error."""
        ...


class TTSAdapter(Protocol):
    """Text to speech: the words in, audio bytes out."""

    async def synthesize(self, text: str, *, language: str = "") -> bytes:
        """Audio for ``text``. Empty text is empty audio, not an error."""
        ...


@dataclass
class VoiceConfig:
    """Which speech providers to use. ``"mock"`` stands in for a real service."""

    stt: str = "mock"
    tts: str = "mock"
    language: str = ""
    # the mock STT's script: audio marker bytes -> the words they stand for
    mock_transcripts: dict[bytes, str] = field(default_factory=dict)

    def available(self) -> tuple[str, ...]:
        return ("mock",)


def create_stt(config: VoiceConfig) -> STTAdapter:
    if config.stt == "mock":
        return MockSTTAdapter(config.mock_transcripts)
    raise VoiceError(f"unknown speech-to-text provider {config.stt!r}; available: mock")


def create_tts(config: VoiceConfig) -> TTSAdapter:
    if config.tts == "mock":
        return MockTTSAdapter()
    raise VoiceError(f"unknown text-to-speech provider {config.tts!r}; available: mock")


class MockSTTAdapter:
    """A scripted stand-in: audio it recognises answers its lines, anything
    else is silence. The mapping is the test's, not a model's."""

    def __init__(self, transcripts: dict[bytes, str] | None = None):
        self.transcripts = dict(transcripts or {})
        self.calls: list[bytes] = []

    async def transcribe(self, audio: bytes, *, language: str = "") -> str:
        self.calls.append(audio)
        return self.transcripts.get(audio, "")


class MockTTSAdapter:
    """A stand-in that returns the text itself, tagged, as the 'audio'. The
    bytes are only ever compared in tests; nothing plays them."""

    PREFIX = b"mock-audio:"

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def synthesize(self, text: str, *, language: str = "") -> bytes:
        self.texts.append(text)
        if not text:
            return b""
        return self.PREFIX + text.encode("utf-8")


__all__ = [
    "MockSTTAdapter",
    "MockTTSAdapter",
    "STTAdapter",
    "TTSAdapter",
    "VoiceConfig",
    "VoiceError",
    "create_stt",
    "create_tts",
]
