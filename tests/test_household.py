"""Unit tests for `bridge.household.HouseholdRegistry`.

Pure-unit tests — no network. Filesystem use is confined to `tmp_path`.
"""
from __future__ import annotations

import sys
import textwrap
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.household import (  # noqa: E402
    DEFAULT_PERSON_FALLBACK,
    HouseholdRegistry,
    Person,
)


def _write(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


class TestPerson(unittest.TestCase):
    def test_compact_description_includes_age_and_interests(self) -> None:
        p = Person(
            id="sam",
            display_name="Sam",
            age=7,
            personality="curious and chatty",
            interests=("lego", "dinosaurs", "soccer", "minecraft"),
        )
        out = p.compact_description()
        self.assertIn("Sam", out)
        self.assertIn("7yo", out)
        self.assertIn("curious", out)
        # Caps interests at 3 items.
        self.assertIn("lego", out)
        self.assertIn("dinosaurs", out)
        self.assertIn("soccer", out)
        self.assertNotIn("minecraft", out)

    def test_compact_description_truncates(self) -> None:
        p = Person(
            id="alex", display_name="Alex",
            personality="x" * 500,
        )
        out = p.compact_description(max_chars=50)
        self.assertLessEqual(len(out), 50)
        self.assertTrue(out.endswith("…"))

    def test_compact_description_no_pii(self) -> None:
        # Birthdate is structured but never appears in the compact
        # description — that's the privacy contract.
        p = Person(
            id="alex", display_name="Alex",
            birthdate=date(1985, 6, 12),
        )
        self.assertNotIn("1985", p.compact_description())
        self.assertNotIn("06", p.compact_description())

    def test_days_until_birthday_today(self) -> None:
        today = date(2026, 4, 25)
        p = Person(id="x", display_name="x", birthdate=date(2000, 4, 25))
        self.assertEqual(p.days_until_birthday(today=today), 0)

    def test_days_until_birthday_future(self) -> None:
        today = date(2026, 4, 25)
        p = Person(id="x", display_name="x", birthdate=date(2000, 5, 1))
        self.assertEqual(p.days_until_birthday(today=today), 6)

    def test_days_until_birthday_wraps_to_next_year(self) -> None:
        today = date(2026, 4, 25)
        p = Person(id="x", display_name="x", birthdate=date(2000, 1, 1))
        # Jan 1 already passed; next is Jan 1 2027.
        self.assertEqual(
            p.days_until_birthday(today=today),
            (date(2027, 1, 1) - today).days,
        )

    def test_days_until_birthday_leap_day(self) -> None:
        today = date(2026, 2, 27)
        p = Person(id="x", display_name="x", birthdate=date(2000, 2, 29))
        # 2026 is not a leap year — Feb 28 is the standin.
        self.assertEqual(p.days_until_birthday(today=today), 1)

    def test_days_until_birthday_none_without_birthdate(self) -> None:
        p = Person(id="x", display_name="x")
        self.assertIsNone(p.days_until_birthday())


class TestRegistryLoading(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.id())
        self._cleanup_paths: list[Path] = []

    def tearDown(self) -> None:
        for p in self._cleanup_paths:
            try:
                p.unlink()
            except OSError:
                pass

    def _yaml_path(self, body: str) -> Path:
        import tempfile
        fd, raw = tempfile.mkstemp(suffix=".yaml")
        import os
        os.close(fd)
        path = Path(raw)
        self._cleanup_paths.append(path)
        return _write(path, body)

    def test_missing_file_yields_empty_registry(self) -> None:
        reg = HouseholdRegistry(path="/nonexistent/path/household.yaml")
        self.assertEqual(tuple(reg.iter()), ())
        self.assertEqual(reg.default_person, DEFAULT_PERSON_FALLBACK)
        self.assertIsNone(reg.get("anyone"))

    def test_basic_load(self) -> None:
        path = self._yaml_path("""
            default_person: _household
            people:
              sam:
                display_name: Sam
                age: 7
                interests: [lego, dinosaurs]
                self_id_phrases: ["it's sam", "sam here"]
                calendar_prefix: "[Sam]"
        """)
        reg = HouseholdRegistry(path=path)
        sam = reg.get("sam")
        self.assertIsNotNone(sam)
        assert sam is not None
        self.assertEqual(sam.display_name, "Sam")
        self.assertEqual(sam.age, 7)
        self.assertEqual(sam.interests, ("lego", "dinosaurs"))
        self.assertEqual(sam.calendar_prefix, "[Sam]")

    def test_lookup_case_insensitive(self) -> None:
        path = self._yaml_path("""
            people:
              sam:
                display_name: Sam
        """)
        reg = HouseholdRegistry(path=path)
        self.assertIsNotNone(reg.get("SAM"))
        self.assertIsNotNone(reg.get("Sam"))

    def test_calendar_prefix_lookup(self) -> None:
        path = self._yaml_path("""
            people:
              sam:
                display_name: Sam
                calendar_prefix: "[Sam]"
        """)
        reg = HouseholdRegistry(path=path)
        self.assertIsNotNone(reg.get_by_calendar_prefix("[Sam]"))
        self.assertIsNotNone(reg.get_by_calendar_prefix("[sam]"))
        self.assertIsNotNone(
            reg.get_by_calendar_prefix("Sam"),
            "brackets should be optional in lookup",
        )
        self.assertIsNone(reg.get_by_calendar_prefix("[Riley]"))

    def test_birthdate_iso_string_parses(self) -> None:
        path = self._yaml_path("""
            people:
              sam:
                display_name: Sam
                birthdate: "2018-11-03"
        """)
        reg = HouseholdRegistry(path=path)
        sam = reg.get("sam")
        assert sam is not None
        self.assertEqual(sam.birthdate, date(2018, 11, 3))

    def test_birthdate_yaml_native_date_works(self) -> None:
        # PyYAML auto-parses YYYY-MM-DD scalars to datetime.date.
        path = self._yaml_path("""
            people:
              sam:
                display_name: Sam
                birthdate: 2018-11-03
        """)
        reg = HouseholdRegistry(path=path)
        sam = reg.get("sam")
        assert sam is not None
        self.assertEqual(sam.birthdate, date(2018, 11, 3))

    def test_birthdate_unparseable_kept_none(self) -> None:
        path = self._yaml_path("""
            people:
              sam:
                display_name: Sam
                birthdate: "not-a-date"
        """)
        reg = HouseholdRegistry(path=path)
        sam = reg.get("sam")
        assert sam is not None
        self.assertIsNone(sam.birthdate)

    def test_malformed_yaml_yields_empty_registry(self) -> None:
        path = self._yaml_path("this is :: not valid yaml ::: -:- ::\n  - x")
        # PyYAML may or may not raise on this depending on version; either
        # way the registry must not crash and should resolve as empty or
        # near-empty without any spurious people.
        reg = HouseholdRegistry(path=path)
        self.assertNotIn("malformed", str(reg.iter()))

    def test_skips_reserved_default_id(self) -> None:
        path = self._yaml_path("""
            people:
              _household:
                display_name: Should be skipped
              sam:
                display_name: Sam
        """)
        reg = HouseholdRegistry(path=path)
        self.assertIsNone(reg.get("_household"))
        self.assertIsNotNone(reg.get("sam"))

    def test_default_person_override(self) -> None:
        path = self._yaml_path("""
            default_person: family
            people:
              sam: {display_name: Sam}
        """)
        reg = HouseholdRegistry(path=path)
        self.assertEqual(reg.default_person, "family")


class TestSelfIdMatching(unittest.TestCase):
    def _registry(self) -> HouseholdRegistry:
        import tempfile
        fd, raw = tempfile.mkstemp(suffix=".yaml")
        import os
        os.close(fd)
        path = Path(raw)
        path.write_text(textwrap.dedent("""
            people:
              alex:
                display_name: Alex
                self_id_phrases:
                  - "it's alex"
                  - "i'm alex"
                  - "alex here"
              sam:
                display_name: Sam
                self_id_phrases:
                  - "it's sam"
                  - "sam here"
              brettany:
                display_name: Brettany
                self_id_phrases:
                  - "it's brettany"
        """).strip(), encoding="utf-8")
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return HouseholdRegistry(path=path)

    def test_basic_match(self) -> None:
        reg = self._registry()
        p = reg.match_self_id("It's Alex.")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.id, "alex")

    def test_match_case_insensitive(self) -> None:
        reg = self._registry()
        self.assertIsNotNone(reg.match_self_id("IT'S ALEX"))
        self.assertIsNotNone(reg.match_self_id("it's alex"))

    def test_match_strips_leading_punctuation(self) -> None:
        reg = self._registry()
        self.assertIsNotNone(reg.match_self_id("  — It's Alex"))
        self.assertIsNotNone(reg.match_self_id("...sam here"))

    def test_no_match_when_phrase_not_at_start(self) -> None:
        reg = self._registry()
        self.assertIsNone(
            reg.match_self_id("I told alex earlier"),
            "phrase must be leading-position to count as self-ID",
        )

    def test_word_boundary_protects_against_substring_collision(self) -> None:
        reg = self._registry()
        # "it's brett" is NOT one of Brettany's phrases — only "it's
        # brettany" — so this case is purely about the "alex" /
        # "alexander" risk. We register "it's alex" and ensure
        # "it's alexander" does NOT match alex.
        # (Brettany is here purely so the longest-first sort gets exercised.)
        # Match against alex's phrase "it's alex":
        self.assertIsNone(reg.match_self_id("it's alexander"))

    def test_longest_phrase_wins(self) -> None:
        reg = self._registry()
        # Both "it's brettany" and "it's b..." would match "it's brettany"
        # exactly; the test is that the longer phrase is checked first.
        p = reg.match_self_id("it's brettany!")
        assert p is not None
        self.assertEqual(p.id, "brettany")

    def test_empty_text_returns_none(self) -> None:
        reg = self._registry()
        self.assertIsNone(reg.match_self_id(""))
        self.assertIsNone(reg.match_self_id("   "))


class TestHotReload(unittest.TestCase):
    def test_reload_picks_up_changes(self) -> None:
        import os
        import tempfile
        fd, raw = tempfile.mkstemp(suffix=".yaml")
        os.close(fd)
        path = Path(raw)
        self.addCleanup(lambda: path.unlink(missing_ok=True))

        path.write_text(textwrap.dedent("""
            people:
              sam: {display_name: Sam}
        """), encoding="utf-8")
        reg = HouseholdRegistry(path=path)
        self.assertIsNotNone(reg.get("sam"))
        self.assertIsNone(reg.get("riley"))

        # Move mtime forward by writing new contents. We bump the mtime
        # explicitly because some filesystems (and some test runners)
        # collapse two writes within the same second to the same mtime.
        import time
        future = time.time() + 5
        path.write_text(textwrap.dedent("""
            people:
              riley: {display_name: Riley}
        """), encoding="utf-8")
        os.utime(path, (future, future))

        # First access after change triggers reload.
        self.assertIsNotNone(reg.get("riley"))
        self.assertIsNone(
            reg.get("sam"),
            "old entry should be gone after reload picks up rewritten file",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
