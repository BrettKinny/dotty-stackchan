"""Room-view roster recognition — pure prompt-build + reply parser.

Ported from bridge.py (the retired ZeroClaw bridge) so the dotty-
behaviour `/api/vision/explain` sentinel branch can light up the
named-greet path PR #93 defers to. See issue #101.

The route-level orchestration (cache, perception bus broadcast,
talk/dance gates) lives in routes/vision.py; this module only owns
the closed-set prompt + the deterministic parser, both of which are
fully testable without FastAPI or asyncio.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Optional, Protocol

log = logging.getLogger("dotty-behaviour.vision.room_view")


# Sentinel placed in the multipart `question` field by the xiaozhi side
# to opt in to the roster-aware path. The dotty-behaviour route owns
# the actual prompt + roster lookup — the xiaozhi side just signals
# intent. Versioning is in the sentinel itself for future format revs.
ROOM_VIEW_SENTINEL = "__ROOM_VIEW_V1__"

# System prompt for the roster-aware VLM call. Constrains the model to
# a closed-set name vocabulary ("only from this list, else 'unknown'")
# — doubles as the kid-mode safety guard: the VLM can only emit one of
# the configured roster names or "unknown", so a stranger or
# hallucinated name is structurally impossible to leak downstream.
ROOM_VIEW_SYSTEM_PROMPT = (
    "You are looking at a photo from a small family robot's camera. "
    "Reply in the EXACT format the user message requests. "
    "Identify the person ONLY by names from the list the user provides. "
    "Never invent names; never name anyone outside the list. "
    "If you are not confident or no match is clear, use the name 'unknown'. "
    "Keep the description to one short sentence. "
    "Read the person's apparent mood from face + posture only, choosing "
    "from a fixed vocabulary."
)

# Exact reply format the VLM is asked to produce. Pinned to one line so
# a streaming or partial completion still parses; `DESC: ` and `NAME: `
# are explicit markers the parser anchors on.
_ROOM_VIEW_PROMPT_TEMPLATE = (
    "Look at this photo and do THREE things in one reply.\n"
    "\n"
    "1. Describe the person in ONE short sentence — approximate age "
    "range, hair, clothing, distinguishing features.\n"
    "2. If the person clearly matches one of these family members, "
    "give that exact name. Otherwise reply with the name 'unknown'.\n"
    "3. Read the person's apparent mood — pick exactly one of: "
    "engaged, tired, excited, distressed, neutral.\n"
    "\n"
    "Family:\n"
    "{roster}\n"
    "\n"
    "Reply on a SINGLE line in this exact format:\n"
    "DESC: <one sentence> | NAME: <{name_choices}|unknown> | MOOD: <engaged|tired|excited|distressed|neutral>\n"
    "\n"
    "If you cannot see a person at all, reply with exactly: no one in view\n"
    "Do not invent names. Do not add commentary."
)

# Sentinel reply for empty frames — same string the v1 prompt used in
# bridge.py, so existing log-grep regexes keep working post-port.
ROOM_VIEW_NO_PERSON = "no one in view"

# Parser regex. Anchored at start, allows whitespace flexibility, and
# tolerates trailing punctuation around the name (e.g. `NAME: Hudson.`).
# The NAME group permits internal spaces/apostrophes — the prompt offers
# display names, and "Mary Anne" must parse (audit 2026-06-06: the old
# single-token pattern made multi-word members a 100% silent miss).
_ROOM_VIEW_RESP_RE = re.compile(
    r"^\s*DESC:\s*(?P<desc>.+?)\s*"
    r"\|\s*NAME:\s*(?P<name>[A-Za-z_][A-Za-z0-9_' -]*?)\s*"
    # Accept ANY single-word MOOD value here so an out-of-vocab reply
    # ("chaotic") still parses the desc + name cleanly — the parser
    # validates the vocab and drops invalid moods to None.
    r"(?:\|\s*MOOD:\s*(?P<mood>[A-Za-z]+)\s*)?"
    r"[.!?]?\s*$",
    re.IGNORECASE | re.DOTALL,
)

ROOM_VIEW_MOODS = frozenset(
    {"engaged", "tired", "excited", "distressed", "neutral"}
)


class _RegistryLike(Protocol):
    """Structural shape we need from HouseholdRegistry — keeps this
    module decoupled from the registry's full interface so tests can
    pass a small fake."""

    def render_roster_for_vlm(self, *, max_line_chars: int = ...) -> str: ...

    def iter(self) -> Iterable: ...  # noqa: A003 — matches registry method


class _NameResolverLike(Protocol):
    """Structural shape we need from household.PersonResolver — the one
    method that maps a VLM NAME token back to a Person (or None)."""

    def resolve_vlm_name(self, name: str) -> Optional[Any]: ...


def build_room_view_question(
    registry: Optional[_RegistryLike],
) -> Optional[str]:
    """Build the roster-aware room_view prompt from the household
    registry. Returns None when the registry is unavailable or has no
    members with `appearance:` set — caller should fall back to the
    v1 description-only prompt."""
    if registry is None:
        return None
    try:
        roster = registry.render_roster_for_vlm()
    except Exception:
        log.exception("room_view: render_roster_for_vlm raised")
        return None
    if not roster.strip():
        return None
    try:
        name_choices = "|".join(sorted(
            p.display_name for p in registry.iter()
            if (getattr(p, "appearance", None) or "").strip()
        ))
    except Exception:
        log.exception("room_view: roster name iteration raised")
        return None
    return _ROOM_VIEW_PROMPT_TEMPLATE.format(
        roster=roster, name_choices=name_choices,
    )


def parse_room_view_response(
    raw: str,
    resolver: _NameResolverLike,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Parse the VLM's room_view reply into `(description, person_id, mood)`.

    The returned person_id is CANONICAL (`Person.id`) — the NAME token
    the VLM echoes back is a *display name* from the prompt vocabulary,
    and `resolver.resolve_vlm_name()` owns the mapping back (case fold,
    multi-word names, id-vs-display-name). The old set-membership check
    compared display names against ids, so any member whose id differed
    from their lowercased display name was a silent miss (audit
    2026-06-06, confirmed 3/3).

    Behaviour:
      * Empty input  → (None, None, None)
      * "no one in view" sentinel → (None, None, None)
      * Format match + name resolves → (desc, person_id, mood_or_None)
      * Format match + name == "unknown" or unresolvable → (desc, None, mood_or_None)
      * Format mismatch → (raw_stripped, None, None) — graceful degrade
        to v1 behaviour so we never lose the description signal even
        when the model deviates from the requested format.

    `mood` is None when the model omits the MOOD: field (older replies)
    or returns a value outside the fixed vocabulary. The MOOD field is
    optional in the regex precisely so older / non-conforming replies
    still parse cleanly.
    """
    if not raw:
        return None, None, None
    cleaned = raw.strip()
    if not cleaned:
        return None, None, None
    if ROOM_VIEW_NO_PERSON in cleaned.lower():
        return None, None, None
    m = _ROOM_VIEW_RESP_RE.match(cleaned)
    if not m:
        # Fall back: treat the whole reply as a description. Mirrors the
        # v1 path so a botched format never costs us the description.
        return cleaned, None, None
    desc = m.group("desc").strip()
    name = m.group("name").strip()
    raw_mood = (m.group("mood") or "").strip().lower()
    mood = raw_mood if raw_mood in ROOM_VIEW_MOODS else None
    if not desc:
        desc = None
    if name.lower() == "unknown":
        return desc, None, mood
    try:
        person = resolver.resolve_vlm_name(name)
    except Exception:
        log.exception("room_view: name resolution raised for %r", name)
        person = None
    if person is None:
        return desc, None, mood
    return desc, person.id, mood
