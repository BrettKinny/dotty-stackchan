"""Unit tests for the room-view VLM response parser.

Covers the v2 (DESC|NAME|MOOD) format introduced in commit 4 plus
backward compat with v1 (DESC|NAME) replies and graceful degradation
when the model breaks format entirely.

Pure regex/string tests — does not import bridge.py (the parser is
stateless; we replicate it locally to avoid the FastAPI ctor cost).
Keep in sync with `_ROOM_VIEW_RESP_RE` and `_parse_room_view_response`
in bridge.py.
"""
from __future__ import annotations

import re
import unittest


_ROOM_VIEW_NO_PERSON = "no one in view"

_ROOM_VIEW_RESP_RE = re.compile(
    r"^\s*DESC:\s*(?P<desc>.+?)\s*"
    r"\|\s*NAME:\s*(?P<name>[A-Za-z_][A-Za-z0-9_-]*)\s*"
    r"(?:\|\s*MOOD:\s*(?P<mood>[A-Za-z]+)\s*)?"
    r"[.!?]?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_ROOM_VIEW_MOODS = frozenset(
    {"engaged", "tired", "excited", "distressed", "neutral"}
)


def parse(raw: str, roster_ids: set[str]):
    if not raw:
        return None, None, None
    cleaned = raw.strip()
    if not cleaned:
        return None, None, None
    if _ROOM_VIEW_NO_PERSON in cleaned.lower():
        return None, None, None
    m = _ROOM_VIEW_RESP_RE.match(cleaned)
    if not m:
        return cleaned, None, None
    desc = m.group("desc").strip()
    name = m.group("name").strip().lower()
    raw_mood = (m.group("mood") or "").strip().lower()
    mood = raw_mood if raw_mood in _ROOM_VIEW_MOODS else None
    if not desc:
        desc = None
    if name == "unknown" or name not in roster_ids:
        return desc, None, mood
    return desc, name, mood


ROSTER = {"hudson", "brett", "olivia"}


class V2FormatTests(unittest.TestCase):

    def test_v2_full_match(self):
        desc, name, mood = parse(
            "DESC: a child with curly hair in a striped shirt | "
            "NAME: hudson | MOOD: engaged",
            ROSTER,
        )
        self.assertEqual(desc, "a child with curly hair in a striped shirt")
        self.assertEqual(name, "hudson")
        self.assertEqual(mood, "engaged")

    def test_v2_unknown_name_passes_mood_through(self):
        desc, name, mood = parse(
            "DESC: an adult man in a dark sweater | "
            "NAME: unknown | MOOD: tired",
            ROSTER,
        )
        self.assertIsNotNone(desc)
        self.assertIsNone(name)
        self.assertEqual(mood, "tired")

    def test_v2_off_roster_name_drops_to_unknown(self):
        desc, name, mood = parse(
            "DESC: a teen | NAME: zelda | MOOD: excited",
            ROSTER,
        )
        self.assertIsNone(name)
        self.assertEqual(mood, "excited")

    def test_v2_invalid_mood_drops_to_none(self):
        desc, name, mood = parse(
            "DESC: ok | NAME: hudson | MOOD: chaotic",
            ROSTER,
        )
        # Invalid moods are silently dropped — but invalid mood means
        # the optional group doesn't match, so the whole regex still
        # succeeds (mood=None) without breaking desc/name.
        self.assertIsNone(mood)
        self.assertEqual(desc, "ok")
        self.assertEqual(name, "hudson")


class V1BackwardCompatTests(unittest.TestCase):

    def test_v1_format_still_parses(self):
        desc, name, mood = parse(
            "DESC: a child reading at the table | NAME: hudson",
            ROSTER,
        )
        self.assertEqual(desc, "a child reading at the table")
        self.assertEqual(name, "hudson")
        self.assertIsNone(mood)


class GracefulDegradeTests(unittest.TestCase):

    def test_no_one_in_view(self):
        d, n, m = parse("no one in view", ROSTER)
        self.assertEqual((d, n, m), (None, None, None))

    def test_format_break_falls_back_to_raw_desc(self):
        # Some VLM replies skip the markers entirely. We still want the
        # description signal — preferable to losing it for format strictness.
        d, n, m = parse("A child playing with lego.", ROSTER)
        self.assertEqual(d, "A child playing with lego.")
        self.assertIsNone(n)
        self.assertIsNone(m)

    def test_empty_string(self):
        self.assertEqual(parse("", ROSTER), (None, None, None))

    def test_whitespace_only(self):
        self.assertEqual(parse("   \n\n   ", ROSTER), (None, None, None))


if __name__ == "__main__":
    unittest.main()
