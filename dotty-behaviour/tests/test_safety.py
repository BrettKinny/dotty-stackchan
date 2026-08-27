import importlib.util
from pathlib import Path

from safety import (
    BLOCKED_PATTERNS,
    KID_MODE_SAFE_REPLACEMENT,
    filter_spoken_text,
)


def _canonical_text_utils():
    path = Path(__file__).resolve().parents[2] / "custom-providers" / "textUtils.py"
    spec = importlib.util.spec_from_file_location("canonical_text_utils", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_kid_mode_replaces_blocked_direct_speech() -> None:
    assert filter_spoken_text("A story about cocaine", True) == KID_MODE_SAFE_REPLACEMENT


def test_kid_mode_off_preserves_direct_speech() -> None:
    text = "A story about cocaine"
    assert filter_spoken_text(text, False) == text


def test_clean_direct_speech_is_preserved() -> None:
    text = "Good morning, Brett!"
    assert filter_spoken_text(text, True) == text


def test_ambient_filter_stays_in_parity_with_voice_filter() -> None:
    """The service mirrors the canonical matcher because its Docker image
    does not package custom-providers/textUtils.py. Keep drift reviewable and
    fail CI if either the blocked tiers or replacement changes independently.
    """
    canonical = _canonical_text_utils()
    assert KID_MODE_SAFE_REPLACEMENT == canonical.CONTENT_FILTER_REPLACEMENT
    assert tuple(
        (pattern.pattern, pattern.flags) for pattern in BLOCKED_PATTERNS
    ) == tuple(
        (pattern.pattern, pattern.flags) for pattern, _tier in canonical._CF_TIERS
    )
    samples = (
        "fuck",
        "a penis",
        "cocaine",
        "a perfectly safe greeting",
        "The word f u c k is spaced out",
    )
    for text in samples:
        canonical_hit = canonical.content_filter_match(text) is not None
        mirrored_hit = filter_spoken_text(text, True) == KID_MODE_SAFE_REPLACEMENT
        assert mirrored_hit is canonical_hit
