"""Speech in, speech out, behind two small adapters.

The runtime does not do speech recognition or synthesis itself: it talks to a
service through :class:`STTAdapter` and :class:`TTSAdapter`, and the only
adapters that ship are the mocks in this module, which stand in for a real
service in tests and in any run without a speech key. A real adapter is a
class that implements the same two methods and is handed in where a mock
would go; no other code changes, and no key is read anywhere here.

What the adapters are used for, in this first pass, is narrow on purpose:
read the Feed (or a summary) aloud, and transcribe a voice note into text.
No full-duplex conversation, no voice control of tools: the spoken words are
read, never acted on.
"""

from __future__ import annotations

from nanomuse.voice.adapters import (
    MockSTTAdapter,
    MockTTSAdapter,
    STTAdapter,
    TTSAdapter,
    VoiceConfig,
    VoiceError,
    create_stt,
    create_tts,
)
from nanomuse.voice.reader import SpeechResult, read_feed_aloud, transcribe_note

__all__ = [
    "MockSTTAdapter",
    "MockTTSAdapter",
    "STTAdapter",
    "SpeechResult",
    "TTSAdapter",
    "VoiceConfig",
    "VoiceError",
    "create_stt",
    "create_tts",
    "read_feed_aloud",
    "transcribe_note",
]
