"""Streaming sentence buffer for chunked TTS playback.

Accumulates LLM token deltas, emits complete sentences at boundaries.
Used by reply_engine's fast path to stream chunks into the TTS worker
queue as they become available, instead of waiting for the full reply.

Pattern follows Pipecat's SentenceAggregator (see docs/research/voice_naturalness/findings.md).

Example:
    buf = SentenceBuffer()
    for chunk in llm.stream(messages):
        token = getattr(chunk, "content", "") or ""
        for sentence in buf.feed(token):
            tts.speak(sentence, emotion=..., blocking=False)
    for sentence in buf.flush():
        tts.speak(sentence, emotion=..., blocking=False)
"""
from __future__ import annotations

import re
from typing import Iterator


# Sentence boundary: punct followed by whitespace and uppercase letter or quote.
# (?<=...) lookbehind anchors the punct to the chunk; (?=...) anchors the next.
_BOUNDARY = re.compile(r'(?<=[.!?])\s+(?=["\'(]?[A-Z])')

# Words that end with a period but don't end a sentence.
# Lowercase compare. Strip the trailing period before checking.
_ABBREVIATIONS = frozenset({
    "mr", "mrs", "dr", "ms", "st", "sr", "jr", "mt",
    "vs", "etc", "inc", "ltd", "co", "corp",
    "u.s", "u.k", "e.g", "i.e", "ph.d", "a.m", "p.m",
})

# Minimum chunk length. Prevents one-word "Yeah." from getting split off and
# producing an awkward TTS gap. Below this, we accumulate until the next
# boundary even if it means a slightly longer chunk.
_MIN_CHARS = 8


def _ends_with_abbreviation(text: str) -> bool:
    """Return True if `text` ends with a known abbreviation + period.

    The boundary regex would otherwise treat `Dr. Smith` as two sentences.
    """
    m = re.search(r'([A-Za-z][A-Za-z\.]*)\.\s*$', text)
    if not m:
        return False
    word = m.group(1).lower().rstrip(".")
    return word in _ABBREVIATIONS


class SentenceBuffer:
    """Accumulates streaming tokens; yields complete sentences.

    Thread-safety: not thread-safe. One buffer per stream.
    """

    def __init__(self, min_chars: int = _MIN_CHARS):
        self._buf: str = ""
        self._min_chars = int(min_chars)

    def feed(self, token: str) -> Iterator[str]:
        """Feed a token fragment. Yield zero or more complete sentences."""
        if not token:
            return
        self._buf += token
        yield from self._drain()

    def flush(self) -> Iterator[str]:
        """Yield any remaining buffer as a final chunk. Resets state."""
        tail = self._buf.strip()
        self._buf = ""
        if tail:
            yield tail

    def _drain(self) -> Iterator[str]:
        """Yield all sentences currently completable from the buffer."""
        scan_from = 0
        while scan_from < len(self._buf):
            m = _BOUNDARY.search(self._buf, scan_from)
            if not m:
                return
            # Sentence is buf[0 : m.start()+1] including the punct.
            # Wait — m.start() is the start of the matched whitespace; the
            # punct itself is at m.start()-1. So the sentence (with punct)
            # is buf[0 : m.start()].
            sentence_end = m.start()
            sentence = self._buf[:sentence_end]

            stripped = sentence.strip()
            if len(stripped) < self._min_chars:
                # Too short — keep scanning past this boundary, accumulate more.
                scan_from = m.end()
                continue
            if _ends_with_abbreviation(stripped):
                # Not a real boundary (e.g. "Dr. Smith" or "U.S. Army").
                scan_from = m.end()
                continue

            # Real boundary. Yield and advance buffer.
            yield stripped
            self._buf = self._buf[m.end():]
            scan_from = 0  # restart from beginning of new buffer

    @property
    def buffered_chars(self) -> int:
        """Length of the currently-buffered tail (for diagnostics)."""
        return len(self._buf)
