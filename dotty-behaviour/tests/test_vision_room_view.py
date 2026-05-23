"""Unit tests for vision.room_view — pure prompt-build + parser."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from vision.room_view import (
    ROOM_VIEW_MOODS,
    ROOM_VIEW_SENTINEL,
    ROOM_VIEW_SYSTEM_PROMPT,
    build_room_view_question,
    parse_room_view_response,
)


@dataclass
class _FakePerson:
    display_name: str
    appearance: Optional[str] = None


@dataclass
class _FakeRegistry:
    roster: str
    people: Iterable[_FakePerson]

    def render_roster_for_vlm(self, *, max_line_chars: int = 80) -> str:
        return self.roster

    def iter(self):  # noqa: A003 — matches HouseholdRegistry's method
        return tuple(self.people)


# ---------------------------------------------------------------------------
# Sentinel / constants
# ---------------------------------------------------------------------------


def test_sentinel_is_versioned_v1() -> None:
    # Wire-shape contract with the xiaozhi side — bumping this string
    # means coordinating with custom-providers/xiaozhi-patches.
    assert ROOM_VIEW_SENTINEL == "__ROOM_VIEW_V1__"


def test_system_prompt_constrains_to_roster_names() -> None:
    # Doubles as the kid-mode safety guard — closed-set vocabulary.
    assert "ONLY by names from the list" in ROOM_VIEW_SYSTEM_PROMPT
    assert "Never invent names" in ROOM_VIEW_SYSTEM_PROMPT


def test_moods_vocab_is_exactly_five() -> None:
    assert ROOM_VIEW_MOODS == {
        "engaged", "tired", "excited", "distressed", "neutral",
    }


# ---------------------------------------------------------------------------
# build_room_view_question
# ---------------------------------------------------------------------------


def test_build_question_returns_none_when_registry_is_none() -> None:
    assert build_room_view_question(None) is None


def test_build_question_returns_none_when_roster_render_empty() -> None:
    # Registry exists but render_roster_for_vlm returns whitespace-only
    # (no members with `appearance:` set).
    reg = _FakeRegistry(roster="   \n   ", people=())
    assert build_room_view_question(reg) is None


def test_build_question_substitutes_roster_and_name_choices() -> None:
    reg = _FakeRegistry(
        roster="  Brett: tall, dark hair\n  Hudson: small child, blond",
        people=(
            _FakePerson("Brett", appearance="tall, dark hair"),
            _FakePerson("Hudson", appearance="small child, blond"),
            _FakePerson("Ghost", appearance=""),  # no appearance — excluded
        ),
    )
    q = build_room_view_question(reg)
    assert q is not None
    assert "Brett: tall, dark hair" in q
    assert "Hudson: small child, blond" in q
    # name_choices is the appearance-bearing members only, sorted
    assert "<Brett|Hudson|unknown>" in q
    # Ghost has empty appearance → must NOT appear in the choice set
    assert "Ghost" not in q


def test_build_question_handles_render_raising() -> None:
    class _Boom:
        def render_roster_for_vlm(self, *, max_line_chars: int = 80) -> str:
            raise RuntimeError("boom")

        def iter(self):
            return ()

    assert build_room_view_question(_Boom()) is None


# ---------------------------------------------------------------------------
# parse_room_view_response
# ---------------------------------------------------------------------------


_ROSTER = {"brett", "hudson"}


def test_parse_empty_string_returns_all_none() -> None:
    assert parse_room_view_response("", _ROSTER) == (None, None, None)


def test_parse_whitespace_only_returns_all_none() -> None:
    assert parse_room_view_response("   \n  ", _ROSTER) == (None, None, None)


def test_parse_no_one_in_view_returns_all_none() -> None:
    assert parse_room_view_response("no one in view", _ROSTER) == (
        None, None, None,
    )
    # Case-insensitive + tolerates surrounding noise
    assert parse_room_view_response("No one in view.", _ROSTER) == (
        None, None, None,
    )


def test_parse_valid_format_with_roster_match() -> None:
    raw = (
        "DESC: adult with goatee and dark sweater | "
        "NAME: Brett | MOOD: engaged"
    )
    desc, pid, mood = parse_room_view_response(raw, _ROSTER)
    assert desc == "adult with goatee and dark sweater"
    assert pid == "brett"
    assert mood == "engaged"


def test_parse_valid_format_with_unknown_name_strips_identity() -> None:
    raw = (
        "DESC: stranger in a red jacket | "
        "NAME: unknown | MOOD: neutral"
    )
    desc, pid, mood = parse_room_view_response(raw, _ROSTER)
    assert desc == "stranger in a red jacket"
    assert pid is None
    assert mood == "neutral"


def test_parse_valid_format_with_off_roster_name_strips_identity() -> None:
    raw = (
        "DESC: young woman with curly hair | "
        "NAME: Alice | MOOD: excited"
    )
    desc, pid, mood = parse_room_view_response(raw, _ROSTER)
    assert desc == "young woman with curly hair"
    assert pid is None  # Alice not in roster
    assert mood == "excited"


def test_parse_format_mismatch_falls_back_to_description_only() -> None:
    raw = "I see a person sitting at a desk reading a book."
    desc, pid, mood = parse_room_view_response(raw, _ROSTER)
    assert desc == "I see a person sitting at a desk reading a book."
    assert pid is None
    assert mood is None


def test_parse_invalid_mood_drops_mood_keeps_match() -> None:
    raw = "DESC: small child | NAME: Hudson | MOOD: chaotic"
    desc, pid, mood = parse_room_view_response(raw, _ROSTER)
    assert desc == "small child"
    assert pid == "hudson"
    assert mood is None


def test_parse_missing_mood_field_still_returns_match() -> None:
    # Older replies / non-conforming models may omit MOOD entirely.
    raw = "DESC: small child | NAME: Hudson"
    desc, pid, mood = parse_room_view_response(raw, _ROSTER)
    assert desc == "small child"
    assert pid == "hudson"
    assert mood is None


def test_parse_trailing_punctuation_tolerated() -> None:
    # Regex permits one trailing `.!?` at end-of-line — so `NAME: Hudson.`
    # parses when MOOD is absent. The mid-line case (period after NAME
    # before ` | MOOD: ...`) is NOT supported; the comment in bridge.py
    # was wishful — keep the test honest.
    raw = "DESC: tall adult | NAME: Brett."
    desc, pid, mood = parse_room_view_response(raw, _ROSTER)
    assert desc == "tall adult"
    assert pid == "brett"
    assert mood is None
