"""Voice-tool helper routes — small read-only endpoints invoked by
dotty-pi-ext's TypeScript tool implementations.

Today only `take_photo` lives here. The other four pi-ext tools
(memory_lookup, remember, think_hard, play_song) talk directly to
brain.db or xiaozhi-server and don't need a dotty-behaviour endpoint.
"""

from __future__ import annotations

import logging
from time import perf_counter

from fastapi import APIRouter, Depends, Request

from perception import PerceptionState

log = logging.getLogger("dotty-behaviour.routes.voice")


def get_perception_state(request: Request) -> PerceptionState:
    state = getattr(request.app.state, "perception", None)
    if state is None:
        raise RuntimeError("PerceptionState not attached to app.state")
    return state


TAKE_PHOTO_FRESHNESS_SEC = 30.0
TAKE_PHOTO_FALLBACK = "(I can't see anything fresh right now)"
TAKE_PHOTO_MAX_CHARS = 300


router = APIRouter()


@router.get("/api/voice/take_photo")
async def voice_take_photo(
    state: PerceptionState = Depends(get_perception_state),
) -> dict:
    """Return the latest cached vision description if ≤30 s old.

    Wire-compatible with bridge.py's `_voice_tool_take_photo` v1
    behaviour: picks the freshest entry across all devices, returns the
    description capped at 300 chars, falls back to a fixed string when
    nothing is fresh. Future v2: actively fire take_photo MCP and await
    a new capture.
    """
    best_desc = ""
    best_age = float("inf")
    now = perf_counter()
    for entry in state.vision_cache.values():
        age = now - entry.get("timestamp", 0.0)
        if age < best_age and entry.get("description"):
            best_age = age
            best_desc = entry.get("description", "")
    if best_desc and best_age <= TAKE_PHOTO_FRESHNESS_SEC:
        return {"description": best_desc[:TAKE_PHOTO_MAX_CHARS]}
    return {"description": TAKE_PHOTO_FALLBACK}


# --- per-person memory: kid-safety review classifier (#53) -----------------
#
# dotty-pi-ext's remember_person tool writes per-person facts straight to
# brain.db, but a fact about a minor must be human-reviewed before it
# becomes readable context. The gate *decision* needs the household
# registry, so it lives here (single-source, Python) rather than being
# duplicated in the TS tool — remember_person calls this classifier, then
# writes to person:<id> or person_pending:<id> accordingly.


def get_household(request: Request):
    hh = getattr(request.app.state, "household", None)
    if hh is None:
        raise RuntimeError("HouseholdRegistry not attached to app.state")
    return hh


# #53 kid-safety gate — CANONICAL SOURCE.
#
# `_ADULT_RELATIONS` + `person_needs_review` below are the single source
# of truth for the per-person-memory gate. bridge.py carries a
# byte-identical transitional mirror (`_ADULT_RELATIONS` /
# `_person_memory_needs_review`) because its legacy write paths gate
# in-process — the two services are separate Docker images on separate
# hosts, so they cannot share an import. Edit *here* first, then mirror
# into bridge.py; the mirror disappears when the #36 rehoming retires
# bridge.py + bridge/*.
#
# Relations that affirmatively mark a household member as an adult — lets
# a registry entry with no `age:` still auto-commit. Everything *not* in
# this set (a known minor, an ambiguous relation, an unknown person)
# routes to review.
_ADULT_RELATIONS = frozenset({
    "self", "owner", "parent", "mother", "father", "mum", "mom", "dad",
    "partner", "spouse", "husband", "wife", "grandparent", "grandmother",
    "grandfather", "aunt", "uncle", "sibling", "brother", "sister",
})


def person_needs_review(household, person_id: str) -> bool:
    """#53 kid-safety gate. A declared per-person fact may be auto-
    committed only when the speaker is affirmatively an adult per the
    household registry (`age >= 18`, or an adult `relation`). A known
    minor, an unknown person, or a registry entry too sparse to classify
    all route to review. Safe failure mode: "a human looks first"."""
    if household is None:
        return True
    try:
        person = household.get(person_id)
    except Exception:
        log.debug("person_needs_review: registry.get raised", exc_info=True)
        return True
    if person is None:
        return True  # unknown person — cannot rule out a minor
    if person.age is not None:
        return person.age < 18
    return (person.relation or "").strip().lower() not in _ADULT_RELATIONS


@router.get("/api/voice/person_review_status")
async def voice_person_review_status(
    person_id: str,
    household=Depends(get_household),
) -> dict:
    """Kid-safety classifier for dotty-pi-ext's remember_person tool.

    Returns whether a declared fact about `person_id` must be routed to
    the `person_pending:<id>` review queue (minor / unknown /
    unclassifiable) rather than committed straight to readable
    `person:<id>` memory."""
    pid = (person_id or "").strip().lower()
    return {
        "person_id": pid,
        "needs_review": person_needs_review(household, pid),
    }
