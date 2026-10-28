"""Read the Feed aloud, and transcribe a voice note. Nothing else, yet.

The spoken text is built from the Feed entries themselves: each entry's title
and its first line, at most five entries, so a long morning never becomes a
long monologue. A placeholder (``{{vault:…}}`` or ``{{oauth:…}}``) in the text
refuses the whole read: a secret is never spoken, and a placeholder read aloud
would teach nobody anything. The transcription is the adapter's answer,
returned as text for the caller to do with as it pleases; it is never acted
on here.
"""

from __future__ import annotations

from dataclasses import dataclass

from nanomuse.voice.adapters import STTAdapter, TTSAdapter, VoiceError

MAX_ENTRIES = 5
MAX_ENTRY_CHARS = 160
_PLACEHOLDERS = ("{{vault:", "{{oauth:")


@dataclass(frozen=True)
class SpeechResult:
    """What was said, and the audio for it."""

    text: str
    audio: bytes


def _spoken_text(items: list[dict[str, object]], max_entries: int = MAX_ENTRIES) -> str:
    parts: list[str] = []
    for item in items[:max_entries]:
        title = str(item.get("title") or "").strip()
        text = str(item.get("text") or "").strip()
        first = text.split("\n", 1)[0].strip()
        if len(first) > MAX_ENTRY_CHARS:
            first = first[:MAX_ENTRY_CHARS].rstrip() + "…"
        line = f"{title}. {first}" if title and first else (title or first)
        if line:
            parts.append(line)
    return " ".join(parts)


async def read_feed_aloud(
    items: list[dict[str, object]],
    tts: TTSAdapter,
    *,
    language: str = "",
    max_entries: int = MAX_ENTRIES,
) -> SpeechResult:
    """Synthesize the spoken summary of ``items`` (Feed entries as
    ``GET /api/feed`` returns them)."""
    text = _spoken_text(items, max_entries)
    for marker in _PLACEHOLDERS:
        if marker in text:
            raise VoiceError(f"refusing to speak text containing a {marker} placeholder")
    if not text:
        return SpeechResult(text="", audio=b"")
    audio = await tts.synthesize(text, language=language)
    return SpeechResult(text=text, audio=audio)


async def transcribe_note(audio: bytes, stt: STTAdapter, *, language: str = "") -> str:
    """The words in a voice note, or ``""`` when there are none."""
    return await stt.transcribe(audio, language=language)


__all__ = ["MAX_ENTRIES", "SpeechResult", "read_feed_aloud", "transcribe_note"]
