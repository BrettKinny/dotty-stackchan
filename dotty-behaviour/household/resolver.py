"""PersonResolver — the single answer to "who is this?".

Identity-bearing strings reach dotty-behaviour from three directions,
each speaking its own name space:

  * the room_view VLM reply — a display name (possibly multi-word, any
    case, maybe trailing punctuation),
  * perception-bus ``identity`` fields — canonical person ids,
  * calendar ``[Name]`` title prefixes — free-typed by humans.

Before this module each consumer re-implemented its own mapping, and
each grew its own bug: roster recognition silently failing whenever
``id != display_name`` (twice), multi-word display names never
matching, and the greeter's calendar lookup dropping a person's own
events on a case mismatch (AUDIT-REPORT 2026-06-06). All resolution now
funnels through here. The canonical key space is ``Person.id``
(lowercase) — resolve once at the edge, pass ids everywhere else.

Stateless and cheap: every call delegates to the (hot-reloading)
HouseholdRegistry, so a household.yaml edit is picked up without a
restart, same as the registry itself.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable, Optional

from .registry import HouseholdRegistry, Person

log = logging.getLogger("dotty-behaviour.household.resolver")

_TRAILING_PUNCT_RE = re.compile(r"[\s.!?,:;]+$")


def _fold(value: str) -> str:
    """Case- and whitespace-fold a name for comparison."""
    return " ".join((value or "").lower().split())


def _strip_brackets(value: str) -> str:
    v = (value or "").strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1].strip()
    return v


class PersonResolver:
    """Maps identity-bearing strings onto household ``Person`` records.

    Construct with a HouseholdRegistry (or None — every resolve then
    returns None, the same graceful degrade as an empty registry).
    """

    def __init__(self, registry: Optional[HouseholdRegistry]) -> None:
        self._registry = registry

    # ------------------------------------------------------------------
    # Canonical-id lookup
    # ------------------------------------------------------------------
    def resolve(self, identity: Optional[str]) -> Optional[Person]:
        """Look up a canonical person id (case-folded). ``unknown`` and
        empty strings resolve to None."""
        if self._registry is None or not identity:
            return None
        ident = identity.strip()
        if not ident or ident.lower() == "unknown":
            return None
        try:
            return self._registry.get(ident)
        except Exception:
            log.debug("resolve(%r) raised", identity, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # VLM name-space (room_view replies)
    # ------------------------------------------------------------------
    def resolve_vlm_name(self, name: Optional[str]) -> Optional[Person]:
        """Map a room_view ``NAME:`` token back to a Person.

        The prompt offers the VLM *display names*, so the reply must be
        matched against display_name as well as id — case- and
        whitespace-folded, trailing punctuation tolerated, multi-word
        names supported.
        """
        if self._registry is None or not name:
            return None
        folded = _fold(_TRAILING_PUNCT_RE.sub("", name))
        if not folded or folded == "unknown":
            return None
        person = self.resolve(folded)
        if person is not None:
            return person
        for p in self._iter_people():
            if _fold(p.display_name) == folded:
                return p
        return None

    # ------------------------------------------------------------------
    # Calendar name-space ([Name] title prefixes)
    # ------------------------------------------------------------------
    def resolve_calendar_tag(self, tag: Optional[str]) -> Optional[Person]:
        """Map a calendar ``[Name]`` prefix (brackets optional) to a
        Person: explicit ``calendar_prefix`` first, then id, then
        display name."""
        if self._registry is None or not tag:
            return None
        bare = _strip_brackets(tag)
        if not bare:
            return None
        try:
            person = self._registry.get_by_calendar_prefix(bare)
        except Exception:
            log.debug("get_by_calendar_prefix(%r) raised", tag, exc_info=True)
            person = None
        if person is not None:
            return person
        person = self.resolve(bare)
        if person is not None:
            return person
        folded = _fold(bare)
        for p in self._iter_people():
            if _fold(p.display_name) == folded:
                return p
        return None

    def calendar_tags(self, identity: Optional[str]) -> set[str]:
        """Folded tag set that refers to ``identity`` in calendar events
        — feed to ``summarize_for_prompt(person=...)`` so a person's own
        events match whether the human typed ``[hudson]``, ``[Hudson]``,
        or the configured ``calendar_prefix``. Unknown identities fall
        back to ``{folded identity}`` (today's exact-string behaviour,
        minus the case sensitivity)."""
        person = self.resolve(identity)
        if person is None:
            folded = _fold(identity or "")
            return {folded} if folded and folded != "unknown" else set()
        tags = {person.id, _fold(person.display_name)}
        if person.calendar_prefix:
            tags.add(_fold(_strip_brackets(person.calendar_prefix)))
        return {t for t in tags if t}

    # ------------------------------------------------------------------
    def _iter_people(self) -> Iterable[Person]:
        if self._registry is None:
            return ()
        try:
            return self._registry.iter()
        except Exception:
            log.debug("registry.iter() raised", exc_info=True)
            return ()


__all__ = ["PersonResolver"]
