"""Deterministic backstop for text spoken outside the voice LLM path.

The live voice providers use ``custom-providers/textUtils.py``.  Ambient
speech in this service goes directly through the xiaozhi ``say`` endpoint,
so it needs the same small blocked-word backstop locally.  Prompt steering
remains the primary defence; this intentionally mirrors the provider's
matcher rather than pretending to be a complete content-safety classifier.
"""

from __future__ import annotations

import re


BLOCKED_PATTERN_SOURCES = (
    r"\b(cocaine|heroin|methamphetamine|fentanyl|ecstasy)\b",
    r"\b(penis|vagina|orgasm|porn\w*|hentai|decapitat\w*|dismember\w*|mutilat\w*)\b",
    r"\b(fuck\w*|shit\w*|bitch\w*|bastard|cunt|nigger|nigga|faggot|retard(?:ed)?)\b",
)
BLOCKED_PATTERNS = tuple(
    re.compile(source, re.IGNORECASE) for source in BLOCKED_PATTERN_SOURCES
)

KID_MODE_SAFE_REPLACEMENT = (
    "😐 Let's talk about something fun instead! "
    "What's your favorite animal?"
)


def filter_spoken_text(text: str, kid_mode: bool) -> str:
    """Replace a known blocked term before direct ambient speech.

    Kid Mode off is transparent. Empty text and clean text retain their
    original value, preserving the existing direct-TTS contract.
    """
    if not kid_mode or not text:
        return text
    if any(pattern.search(text) for pattern in BLOCKED_PATTERNS):
        return KID_MODE_SAFE_REPLACEMENT
    return text
