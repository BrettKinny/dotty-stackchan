"""HouseholdRegistry — YAML parsing + hot reload + lookup APIs.

Lift-and-shift from bridge/household.py was done verbatim, but these
smoke tests confirm the module imports cleanly from the new
dotty-behaviour package path and exercises the public API surface.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from household import HouseholdRegistry, Person


def _write_yaml(td: Path, text: str) -> Path:
    p = td / "household.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_missing_yaml_is_empty_registry() -> None:
    with tempfile.TemporaryDirectory() as td:
        r = HouseholdRegistry(path=Path(td) / "nope.yaml")
        assert list(r.iter()) == []
        assert r.default_person == "_household"


def test_loads_people_with_full_schema() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = _write_yaml(
            Path(td),
            """
default_person: brett
people:
  brett:
    display_name: Brett
    relation: dad
    pronouns: he/him
    age: 40
    appearance: tall, glasses, blue shirt
    personality: dry, technical
    interests: [coding, coffee]
    self_id_phrases: ["it's brett", "this is brett"]
    calendar_prefix: "[Brett]"
  hudson:
    display_name: Hudson
""",
        )
        r = HouseholdRegistry(path=path)
        ppl = list(r.iter())
        assert {p.id for p in ppl} == {"brett", "hudson"}
        assert r.default_person == "brett"
        brett = r.get("brett")
        assert brett is not None
        assert brett.display_name == "Brett"
        assert brett.appearance == "tall, glasses, blue shirt"
        assert brett.interests == ("coding", "coffee")


def test_lookup_by_calendar_prefix() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = _write_yaml(
            Path(td),
            """
people:
  brett:
    display_name: Brett
    calendar_prefix: "[Brett]"
""",
        )
        r = HouseholdRegistry(path=path)
        # Brackets optional, case-insensitive
        assert r.get_by_calendar_prefix("Brett").id == "brett"  # type: ignore[union-attr]
        assert r.get_by_calendar_prefix("[brett]").id == "brett"  # type: ignore[union-attr]
        assert r.get_by_calendar_prefix("nobody") is None


def test_lookup_by_calendar_prefix_bracketless_yaml() -> None:
    # Audit 2026-06-06: `calendar_prefix: Brett` (no brackets) was stored
    # bare but looked up bracketed, so it never matched. Both YAML forms
    # must behave identically now.
    with tempfile.TemporaryDirectory() as td:
        path = _write_yaml(
            Path(td),
            """
people:
  brett:
    display_name: Brett
    calendar_prefix: Brett
""",
        )
        r = HouseholdRegistry(path=path)
        assert r.get_by_calendar_prefix("Brett").id == "brett"  # type: ignore[union-attr]
        assert r.get_by_calendar_prefix("[Brett]").id == "brett"  # type: ignore[union-attr]


def test_match_self_id_strips_leading_punct() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = _write_yaml(
            Path(td),
            """
people:
  brett:
    display_name: Brett
    self_id_phrases:
      - it's brett
""",
        )
        r = HouseholdRegistry(path=path)
        assert r.match_self_id("It's Brett, hi!").id == "brett"  # type: ignore[union-attr]
        assert r.match_self_id("  ... it's brett").id == "brett"  # type: ignore[union-attr]
        assert r.match_self_id("brett is here") is None


def test_render_roster_for_vlm_excludes_appearance_empty() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = _write_yaml(
            Path(td),
            """
people:
  brett:
    display_name: Brett
    appearance: tall, blue shirt
  hudson:
    display_name: Hudson
""",
        )
        r = HouseholdRegistry(path=path)
        roster = r.render_roster_for_vlm()
        assert "Brett" in roster
        assert "Hudson" not in roster


def test_roster_ids_with_appearance() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = _write_yaml(
            Path(td),
            """
people:
  brett:
    display_name: Brett
    appearance: tall
  hudson:
    display_name: Hudson
""",
        )
        r = HouseholdRegistry(path=path)
        assert r.roster_ids_with_appearance() == {"brett"}


def test_hot_reload_on_mtime_change() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = _write_yaml(
            Path(td),
            """
people:
  brett:
    display_name: Brett
""",
        )
        r = HouseholdRegistry(path=path)
        assert r.get("brett").display_name == "Brett"  # type: ignore[union-attr]
        # bump mtime
        time.sleep(0.05)
        path.write_text(
            """
people:
  brett:
    display_name: Brettington
""",
            encoding="utf-8",
        )
        assert (
            r.get("brett").display_name == "Brettington"  # type: ignore[union-attr]
        )


def test_person_compact_description() -> None:
    p = Person(
        id="brett", display_name="Brett", age=40, personality="dry",
        interests=("coffee", "coding", "music"),
    )
    out = p.compact_description()
    assert out.startswith("Brett —")
    assert "40yo" in out
    assert "dry" in out
    assert "loves coffee" in out
