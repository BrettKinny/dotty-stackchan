"""Perception HTTP surface — ingest, state introspection, SSE feed.

Wire-compatible with bridge.py's `/api/perception/{event,state,feed}`
so xiaozhi-server-side patches can be retargeted at this daemon by
just swapping the host:port in their POST URLs.
"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import PERCEPTION_RECENT_MAX
from perception import PerceptionEvent, PerceptionState

router = APIRouter(prefix="/api/perception")


def get_perception_state(request: Request) -> PerceptionState:
    """FastAPI dependency — pulls the PerceptionState singleton off app.state.

    Lives at module scope so tests can override it via
    ``app.dependency_overrides[get_perception_state] = lambda: …``.
    """
    state = getattr(request.app.state, "perception", None)
    if state is None:
        raise RuntimeError(
            "PerceptionState not attached to app.state — main.lifespan "
            "must run before any request is served."
        )
    return state


class PerceptionEventIn(BaseModel):
    device_id: str = "unknown"
    ts: float | None = None
    name: str
    data: dict = {}


@router.post("/event", status_code=204)
async def perception_event(
    payload: PerceptionEventIn,
    state: PerceptionState = Depends(get_perception_state),
) -> None:
    """Ingest one ambient-perception event.

    Producers: xiaozhi-server's textMessageHandlerRegistry relays
    firmware `event` frames (face_detected, face_lost, sound_event,
    state_changed, chat_status, dance_started/ended,
    head_pet_started, head_pet_ended). Server-side classifiers may add more.

    Updates per-device state, then fans the event to all bus
    subscribers (the 9 consumers + dashboard SSE).
    """
    ts = payload.ts if payload.ts is not None else time.time()
    event = PerceptionEvent(
        device_id=payload.device_id,
        name=payload.name,
        data=payload.data or {},
        ts=ts,
    )
    state.update_state(payload.device_id, payload.name, event.data, ts)
    state.broadcast(event)
    return None


@router.get("/state")
async def perception_state_route(
    device_id: str = "",
    state: PerceptionState = Depends(get_perception_state),
) -> dict:
    """Debug introspection — annotated per-device state.

    Each entry is decorated with `sensor_age_s` + `sensor_stale`
    (seconds since the last event vs PERCEPTION_STALE_THRESHOLD_SEC).
    """
    if device_id:
        return state.annotate_for_introspection(devices=[device_id])
    return state.annotate_for_introspection()


@router.get("/recent/{device_id}")
async def perception_recent(
    device_id: str,
    limit: int = 50,
    state: PerceptionState = Depends(get_perception_state),
) -> list[dict]:
    """Most-recent perception events for a device (newest first).

    Bounded by PERCEPTION_RECENT_MAX. Used by the bridge dashboard
    perception card.
    """
    return state.get_recent(device_id, limit=limit)


@router.get("/sound-balance/{device_id}")
async def sound_balance_series(
    device_id: str,
    limit: int = 30,
    state: PerceptionState = Depends(get_perception_state),
) -> list[float]:
    """Recent sound_event balance values for a device (oldest-first).

    Used by the bridge dashboard state-card sparkline. Returns at most
    ``limit`` recent values; missing/non-numeric balances are skipped.
    Iterates the recent-events ring (newest-first) in reverse so the
    output is oldest-first — matches the sparkline's left-to-right
    time axis.
    """
    recent = state.get_recent(device_id, limit=PERCEPTION_RECENT_MAX)
    balances: list[float] = []
    for event in reversed(recent):
        if event.get("name") != "sound_event":
            continue
        balance = event.get("data", {}).get("balance")
        if isinstance(balance, (int, float)):
            balances.append(float(balance))
    return balances[-limit:]


@router.get("/feed")
async def perception_feed(
    request: Request,
    state: PerceptionState = Depends(get_perception_state),
) -> StreamingResponse:
    """Server-Sent Events stream of live perception events.

    Each event arrives as:
        data: {"name": "...", "data": {...}, "device_id": "...", "ts": …}\\n\\n

    Sends `: keepalive` every 15 s of idle. Same wire shape as
    bridge.py so the dashboard reconnects unchanged.
    """
    queue = state.subscribe()

    async def _generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(event.to_payload())}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            state.unsubscribe(queue)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
