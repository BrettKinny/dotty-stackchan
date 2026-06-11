"""PersonResolver tests — the single seam mapping identity-bearing
strings (VLM names, bus identities, calendar tags) onto Person.id.

Built on the real HouseholdRegistry + a temp household.yaml so the
resolver is exercised against the registry's actual case-folding and
prefix-normalisation behaviour, not a re-implementation (the audit
found the old room_view test fake masked the id/display_name bug by
re-implementing the helper).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from household import HouseholdRegistry, PersonResolver

_YAML = """
people:
  brett:
    display_name: Brett
    appearance: tall, dark hair
    calendar_prefix: "[Brett]"
  dad:
    display_name: Greg
    appearance: grey beard
  maryanne:
    display_name: Mary Anne
    appearance: curly hair
    calendar_prefix: Mum
"""


def _resolver(yaml_text: str = _YAML) -> PersonResolver:
    td = Path(tempfile.mkdtemp(prefix="dotty-resolver-"))
    path = td / "household.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return PersonResolver(HouseholdRegistry(path=path))


# ---------------------------------------------------------------------------
# resolve — canonical-id lookup
# ---------------------------------------------------------------------------


def test_resolve_by_id_case_folded() -> None:
    r = _resolver()
    assert r.resolve("brett").id == "brett"  # type: ignore[union-attr]
    assert r.resolve("BRETT").id == "brett"  # type: ignore[union-attr]
    assert r.resolve("  brett  ").id == "brett"  # type: ignore[union-attr]


def test_resolve_unknown_and_empty_return_none() -> None:
    r = _resolver()
    assert r.resolve("unknown") is None
    assert r.resolve("UNKNOWN") is None
    assert r.resolve("") is None
    assert r.resolve(None) is None
    assert r.resolve("nobody") is None


def test_resolve_with_no_registry_returns_none() -> None:
    r = PersonResolver(None)
    assert r.resolve("brett") is None
    assert r.resolve_vlm_name("Brett") is None
    assert r.resolve_calendar_tag("[Brett]") is None
    assert r.calendar_tags("brett") == {"brett"}


# ---------------------------------------------------------------------------
# resolve_vlm_name — room_view NAME tokens
# ---------------------------------------------------------------------------


def test_vlm_name_matches_display_name_when_id_differs() -> None:
    # id "dad", display "Greg" — the audit's silent-miss case.
    r = _resolver()
    assert r.resolve_vlm_name("Greg").id == "dad"  # type: ignore[union-attr]


def test_vlm_name_matches_multi_word_display_name() -> None:
    r = _resolver()
    assert r.resolve_vlm_name("Mary Anne").id == "maryanne"  # type: ignore[union-attr]
    # Case + internal-whitespace folding
    assert r.resolve_vlm_name("mary  anne").id == "maryanne"  # type: ignore[union-attr]


def test_vlm_name_tolerates_trailing_punctuation() -> None:
    r = _resolver()
    assert r.resolve_vlm_name("Greg.").id == "dad"  # type: ignore[union-attr]
    assert r.resolve_vlm_name("Mary Anne! ").id == "maryanne"  # type: ignore[union-attr]


def test_vlm_name_id_takes_priority_then_unknown_is_none() -> None:
    r = _resolver()
    assert r.resolve_vlm_name("brett").id == "brett"  # type: ignore[union-attr]
    assert r.resolve_vlm_name("unknown") is None
    assert r.resolve_vlm_name("Alice") is None
    assert r.resolve_vlm_name("") is None


# ---------------------------------------------------------------------------
# resolve_calendar_tag / calendar_tags — [Name] prefixes
# ---------------------------------------------------------------------------


def test_calendar_tag_resolves_prefix_id_and_display() -> None:
    r = _resolver()
    # Explicit calendar_prefix (bracketed in YAML, any query form)
    assert r.resolve_calendar_tag("[Brett]").id == "brett"  # type: ignore[union-attr]
    assert r.resolve_calendar_tag("brett").id == "brett"  # type: ignore[union-attr]
    # Bare calendar_prefix in YAML ("Mum") — audit F832's broken case
    assert r.resolve_calendar_tag("[Mum]").id == "maryanne"  # type: ignore[union-attr]
    assert r.resolve_calendar_tag("mum").id == "maryanne"  # type: ignore[union-attr]
    # Fallbacks: id, then display name
    assert r.resolve_calendar_tag("[dad]").id == "dad"  # type: ignore[union-attr]
    assert r.resolve_calendar_tag("[Greg]").id == "dad"  # type: ignore[union-attr]
    assert r.resolve_calendar_tag("[Granny]") is None


def test_calendar_tags_expand_id_display_and_prefix() -> None:
    r = _resolver()
    assert r.calendar_tags("maryanne") == {"maryanne", "mary anne", "mum"}
    assert r.calendar_tags("dad") == {"dad", "greg"}
    # The case-mismatch bug: identity "brett" must match a "[Brett]" tag
    # after both sides fold.
    assert "brett" in r.calendar_tags("brett")


def test_calendar_tags_unknown_identity_falls_back_to_folded_self() -> None:
    r = _resolver()
    assert r.calendar_tags("Visitor") == {"visitor"}
    assert r.calendar_tags("unknown") == set()
    assert r.calendar_tags("") == set()
    assert r.calendar_tags(None) == set()
