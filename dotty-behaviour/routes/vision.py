"""Vision endpoints — POST /api/vision/explain + GET /api/vision/latest.

The explain endpoint accepts a JPEG upload, base64-encodes it, asks
the VLM to describe it (using the kid-mode-aware system prompt), and
caches the description under perception_state.vision_cache[device_id].
The latest endpoint blocks until a fresh result lands.

Wire-compatible with bridge.py's /api/vision/{explain,latest/...} so
xiaozhi-patches can retarget by URL swap.

The room_view roster path (opt-in via the `__ROOM_VIEW_V1__` sentinel
in `question`, household roster substitution, and `face_recognized`
broadcast on roster match) ports the bridge.py named-greet path so
PR #93's bare-greet suppression isn't a silent regression for known
household members. Issue #101.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from time import perf_counter
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

import config
from dispatch import VLMClient
from household import HouseholdRegistry
from perception import PerceptionEvent, PerceptionState
from vision.room_view import (
    ROOM_VIEW_NO_PERSON,
    ROOM_VIEW_SENTINEL,
    ROOM_VIEW_SYSTEM_PROMPT,
    build_room_view_question,
    parse_room_view_response,
)

log = logging.getLogger("dotty-behaviour.routes.vision")

# Fallback v1 question used when the sentinel arrives but the registry
# is empty / unavailable. Verbatim from bridge.py:3614-3621 so a roster-
# less deployment still produces a usable description.
_ROOM_VIEW_V1_FALLBACK_QUESTION = (
    "Describe the person you can see in one short "
    "sentence — approximate age range, hair, clothing, "
    "distinguishing features. If you cannot see a "
    "person, reply with exactly: no one in view. "
    "Do not guess names."
)


def get_perception_state(request: Request) -> PerceptionState:
    state = getattr(request.app.state, "perception", None)
    if state is None:
        raise RuntimeError("PerceptionState not attached to app.state")
    return state


def get_vlm_client(request: Request) -> VLMClient:
    vlm = getattr(request.app.state, "vlm", None)
    if vlm is None:
        raise RuntimeError("VLMClient not attached to app.state")
    return vlm


def get_kid_mode(request: Request) -> bool:
    """Live kid-mode reader. Set on app.state by the kid-mode toggle
    handler (deferred slice). Defaults to False until the dashboard
    plumbing lands."""
    return bool(getattr(request.app.state, "kid_mode", False))


def get_household(request: Request) -> Optional[HouseholdRegistry]:
    """Optional — None when no registry was attached (e.g. PyYAML missing
    at boot, or the room_view path is being exercised by a test that
    skips lifespan)."""
    return getattr(request.app.state, "household", None)


def _room_view_gates_block(
    state: PerceptionState,
    device_id: str,
    now_wall: float,
) -> Optional[str]:
    """Return a short reason string when the room_view VLM call should
    be skipped, else None. Mirrors bridge.py:3518-3552.

    Skip conditions:
      * dance_active — face greeting on top of a dance is jarring
      * talk_active (state == "talk" OR listening, outside the kickoff
        grace window) — don't run a heavyweight VLM call mid-turn
      * cooldown — we already ran a room_view capture for this device
        in the last DOTTY_IDLE_VISION_COOLDOWN_SEC window
    """
    dev = state.state.setdefault(device_id, {})
    if state.is_dance_active(device_id):
        return "dance_active"

    current_state = (dev.get("current_state") or "idle").lower()
    listening = bool(dev.get("listening"))
    last_state_change_t = dev.get("last_state_change_t", 0.0)
    talk_kickoff = (
        current_state == "talk"
        and (now_wall - last_state_change_t)
            < config.ROOM_VIEW_TALK_KICKOFF_GRACE_SEC
    )
    talk_active = (
        (current_state == "talk" or listening) and not talk_kickoff
    )
    if talk_active:
        return (
            f"talk_active (state={current_state} listening={listening} "
            f"age={now_wall - last_state_change_t:.2f}s)"
        )

    last_capture = dev.get("last_room_view_capture_t", 0.0)
    cooldown_age = now_wall - last_capture
    if cooldown_age < config.DOTTY_IDLE_VISION_COOLDOWN_SEC:
        return (
            f"cooldown ({cooldown_age:.1f}s/"
            f"{config.DOTTY_IDLE_VISION_COOLDOWN_SEC:.0f}s)"
        )

    return None


router = APIRouter()


@router.post("/api/vision/explain")
async def vision_explain(
    request: Request,
    question: str = Form("What do you see?"),
    file: UploadFile = File(...),
    state: PerceptionState = Depends(get_perception_state),
    vlm: VLMClient = Depends(get_vlm_client),
    kid_mode: bool = Depends(get_kid_mode),
    household: Optional[HouseholdRegistry] = Depends(get_household),
) -> dict:
    device_id = request.headers.get("device-id", "unknown")
    jpeg_bytes = await file.read()
    log.info(
        "vision device=%s question=%s bytes=%d",
        device_id, question[:80], len(jpeg_bytes),
    )
    b64_image = base64.b64encode(jpeg_bytes).decode("ascii")

    # Room-view roster identification opt-in. The xiaozhi side sends
    # the sentinel in the `question` field when it wants the roster-
    # aware path. Falls back to the v1 description prompt if the
    # registry is empty / unavailable so a roster-less deployment
    # still produces a usable description.
    is_room_view_request = question == ROOM_VIEW_SENTINEL
    room_view_question = (
        build_room_view_question(household) if is_room_view_request
        else None
    )
    room_match_person_id: Optional[str] = None
    effective_question = question
    source = "v1"

    if room_view_question is not None:
        source = "room_view"
        now_wall = time.time()
        block_reason = _room_view_gates_block(state, device_id, now_wall)
        if block_reason is not None:
            log.info(
                "room_view skipped: device=%s reason=%s",
                device_id, block_reason,
            )
            description = ROOM_VIEW_NO_PERSON
            state.vision_cache[device_id] = {
                "description": description,
                "timestamp": perf_counter(),
                "wall_ts": now_wall,
                "jpeg_bytes": jpeg_bytes,
                "question": question,
                "room_match_person_id": None,
                "source": source,
            }
            state.signal_vision_waiters(device_id)
            return {"description": description}

        state.state.setdefault(device_id, {})["last_room_view_capture_t"] = (
            now_wall
        )
        roster_ids = (
            household.roster_ids_with_appearance()
            if household is not None else set()
        )
        raw = await vlm.describe_image(
            b64_image,
            room_view_question,
            system_prompt=ROOM_VIEW_SYSTEM_PROMPT,
            timeout_s=config.VISION_TIMEOUT_SEC,
        )
        parsed_desc, room_match_person_id, parsed_mood = (
            parse_room_view_response(raw, roster_ids)
        )
        description = parsed_desc or ROOM_VIEW_NO_PERSON
        effective_question = room_view_question
        if parsed_mood:
            # TTL-bound: dashboard/snapshot read sites check
            # `face_mood_t` against FACE_IDENTITY_TTL_SEC. Mood lives
            # or dies with the identification it was attached to.
            pstate = state.state.setdefault(device_id, {})
            pstate["face_mood"] = parsed_mood
            pstate["face_mood_t"] = now_wall
        log.info(
            "room_view device=%s match=%s mood=%s desc=%s",
            device_id, room_match_person_id or "-",
            parsed_mood or "-", description[:120],
        )
    else:
        # v1 path — either a normal "what do you see" call, OR a
        # sentinel call that fell back because the registry is empty.
        if is_room_view_request:
            effective_question = _ROOM_VIEW_V1_FALLBACK_QUESTION
            source = "room_view"  # still room_view source for cache attribution
        system_prompt = config.build_vision_system_prompt(kid_mode)
        description = await vlm.describe_image(
            b64_image,
            effective_question,
            system_prompt=system_prompt,
            timeout_s=config.VISION_TIMEOUT_SEC,
        )

    now_perf = perf_counter()
    now_wall = time.time()
    state.vision_cache[device_id] = {
        "description": description,
        "timestamp": now_perf,
        "wall_ts": now_wall,
        "jpeg_bytes": jpeg_bytes,
        "question": effective_question,
        "room_match_person_id": room_match_person_id,
        "source": source,
    }
    state.signal_vision_waiters(device_id)

    # Layer-6 hook: when room_view resolves to a roster member,
    # broadcast a synthetic `face_recognized` event so consumers
    # (FaceGreeter, ProactiveGreeter) see the resolved identity.
    # Without this the person_id is trapped in the cache and never
    # reaches the perception bus.
    if room_match_person_id:
        state.broadcast(PerceptionEvent(
            device_id=device_id,
            name="face_recognized",
            data={"identity": room_match_person_id, "source": "room_view"},
            ts=now_wall,
        ))

    # Evict stale cache entries (other devices) so the cache doesn't
    # grow unbounded on a long-running daemon.
    stale = [
        k
        for k, v in state.vision_cache.items()
        if now_perf - v.get("timestamp", 0) > config.VISION_CACHE_TTL_SEC
    ]
    for k in stale:
        state.vision_cache.pop(k, None)

    log.info(
        "vision result device=%s desc=%s",
        device_id, description[:120],
    )
    return {"description": description}


@router.get("/api/vision/latest/{device_id}")
async def vision_latest(
    device_id: str,
    state: PerceptionState = Depends(get_perception_state),
):
    # Drop any stale entry first so the waiter won't immediately
    # return last-turn's cache.
    state.vision_cache.pop(device_id, None)
    event = state.register_vision_waiter(device_id)
    try:
        await asyncio.wait_for(event.wait(), timeout=15.0)
        entry = state.vision_cache.get(device_id)
        if entry:
            return {
                "description": entry["description"],
                "room_match_person_id": entry.get("room_match_person_id"),
            }
        return JSONResponse(
            status_code=500,
            content={"error": "vision processing failed"},
        )
    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=404,
            content={"error": "no vision result in time"},
        )
    finally:
        state.unregister_vision_waiter(device_id, event)
