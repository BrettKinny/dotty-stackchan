"""Text post-processing for voice + chat responses.

Five small functions that sit between the LLM raw output and the TTS /
channel surface:

- ``ensure_emoji_prefix`` — guarantees the response starts with an allowed
  emoji so the firmware emotion dispatch always has a face animation.
- ``clean_for_tts`` — strips characters TTS reads literally or can't render.
- ``strip_extra_emojis`` — keeps only the leading allowed emoji.
- ``truncate_sentences`` — caps response length to ``MAX_SENTENCES``.
- ``content_filter`` — kid-safe replacement when blocked content is
  detected; three severity tiers for metrics/logging differentiation.
"""
import logging
import os
import re
import sys
import time
from collections import deque
from pathlib import Path

# Defensive sibling-import shim so this module is standalone-importable
# (e.g. from a test) without bridge.py running first.
_TEXTUTILS_DIR = str(Path(__file__).parent.parent / "custom-providers")
if _TEXTUTILS_DIR not in sys.path:
    sys.path.insert(0, _TEXTUTILS_DIR)

from textUtils import (  # noqa: E402
    ALLOWED_EMOJIS,
    CONTENT_FILTER_REPLACEMENT,
    FALLBACK_EMOJI,
    content_filter_match,
)

try:
    from bridge.metrics import dotty_content_filter_hits_total
except Exception:
    dotty_content_filter_hits_total = None  # type: ignore[assignment]

log = logging.getLogger("bridge.text")

MAX_SENTENCES = int(os.environ.get("MAX_SENTENCES", "6"))


def ensure_emoji_prefix(text: str) -> str:
    if not text:
        return f"{FALLBACK_EMOJI} (no response)"
    stripped = text.lstrip()
    if any(stripped.startswith(e) for e in ALLOWED_EMOJIS):
        return text
    return f"{FALLBACK_EMOJI} {text}"


_TTS_STRIP_RE = re.compile("[‍️*#>]")
_EXTRA_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F000-\U0001F0FF"
    "\U0001F100-\U0001F1FF]"
)


def clean_for_tts(text: str) -> str:
    """Strip characters that TTS engines read literally or can't render."""
    return _TTS_STRIP_RE.sub("", text)


def strip_extra_emojis(text: str) -> str:
    """Keep only the leading allowed emoji; remove all other emoji characters.

    The model is instructed to use exactly one emoji from ALLOWED_EMOJIS as the
    first character. In practice it sprinkles decorative emojis through the
    response. Those are wasted tokens, clutter the logs, and risk Piper reading
    them aloud. This is the safety net.
    """
    if not text:
        return text
    ws_len = len(text) - len(text.lstrip())
    stripped = text[ws_len:]
    for e in ALLOWED_EMOJIS:
        if stripped.startswith(e):
            head = text[: ws_len + len(e)]
            body = text[ws_len + len(e):]
            return head + _EXTRA_EMOJI_RE.sub("", body)
    return _EXTRA_EMOJI_RE.sub("", text)


def truncate_sentences(text: str, max_sentences: int = MAX_SENTENCES) -> str:
    count = 0
    for i, ch in enumerate(text):
        if ch in '.!?':
            count += 1
            if count >= max_sentences:
                return text[:i + 1]
    return text


# Content-filter severity tiers — the regexes and the kid-safe replacement
# live in the shared textUtils module (#157: single source of truth, also
# consumed by the live voice providers). This wrapper layers on the
# bridge-only side effects: logging level, the Prometheus counter label,
# and the /ui/safety/recent ring. All tiers return the same replacement so
# no information is leaked about WHY the filter fired:
#
#   redirect — common profanity / slurs             → log.warning
#   log      — explicit sexual / graphic violence   → log.warning
#   alert    — hard drugs                           → log.error  (alert on this label)
_CF_TIER_LEVELS = {
    "alert": logging.ERROR,
    "log": logging.WARNING,
    "redirect": logging.WARNING,
}

# #72 — in-memory ring of recent content-filter hits, surfaced at
# /ui/safety/recent. In-memory ONLY: the ring is lost on restart and is
# never written to disk. The matched term recorded here is no more
# exposed than the `content-filter-hit` log line content_filter() already
# emits.
_CF_RECENT_MAX = 20
_cf_recent: "deque[dict]" = deque(maxlen=_CF_RECENT_MAX)


def recent_content_filter_hits() -> list[dict]:
    """Recent content-filter hits, newest first — the /ui/safety/recent
    dashboard source (#72). Each entry has: ts, tier, rule (the matched
    term), prefix (first 8 chars of the filtered text)."""
    return list(reversed(_cf_recent))


def content_filter(text: str) -> str | None:
    """Return a safe replacement if blocked content is found, else None.

    Checks three severity tiers. The kid-facing replacement is identical
    for all tiers; only log level and the Prometheus tier label differ,
    letting operators alert on ``tier="alert"`` without noising up
    lower-tier counts.
    """
    hit = content_filter_match(text)
    if hit is None:
        return None
    tier, match = hit
    log.log(
        _CF_TIER_LEVELS.get(tier, logging.WARNING),
        "content-filter-hit tier=%s pattern=%r pos=%d len=%d",
        tier, match.group(), match.start(), len(text),
    )
    _cf_recent.append({
        "ts": time.time(),
        "tier": tier,
        "rule": match.group(),
        "prefix": text[:8],
    })
    if dotty_content_filter_hits_total is not None:
        try:
            dotty_content_filter_hits_total.labels(tier=tier).inc()
        except Exception:
            pass
    return CONTENT_FILTER_REPLACEMENT
