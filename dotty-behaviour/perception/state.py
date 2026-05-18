"""Central PerceptionState — event bus + 4 caches + per-device state.

Lifted from bridge.py's module-level `_perception_*` / `_vision_cache`
/ `_audio_cache` / `_scene_synthesis_cache` globals into a single
class so the daemon can construct an isolated instance per test and
so consumers can take it as an explicit dependency.

Behaviour is identical to bridge.py — same event names mutate the same
fields with the same precedence rules, same queue size, same recent-
events ring buffer cap. Diff is structural only.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable

from config import (
    PERCEPTION_QUEUE_MAX,
    PERCEPTION_RECENT_MAX,
    PERCEPTION_STALE_THRESHOLD_SEC,
)

log = logging.getLogger("dotty-behaviour.perception")


@dataclass(frozen=True)
class PerceptionEvent:
    """Wire-shape for a perception event flowing through the bus.

    Mirrors the JSON payload at /api/perception/event byte-for-byte
    so subscribers can be exercised in tests without going through
    FastAPI parsing.
    """

    device_id: str
    name: str
    data: dict[str, Any]
    ts: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "data": dict(self.data),
            "ts": self.ts,
        }


class PerceptionState:
    """Per-daemon perception subsystem singleton.

    Lifecycle: created once in main.lifespan, stored on app.state,
    pulled into routes/consumers via FastAPI's Depends. Tests
    construct instances directly.
    """

    def __init__(self) -> None:
        # Event bus
        self._listeners: list[asyncio.Queue[PerceptionEvent]] = []
        # Per-device state (face_present, listening, current_state,
        # last_*_t timestamps, dance_active, etc.)
        self.state: dict[str, dict[str, Any]] = {}
        # Bounded per-device ring buffer of recent events (dashboard).
        self._recent: dict[
            str, collections.deque[dict[str, Any]]
        ] = {}
        # Caches consumed by snapshot() + the dashboard's perception card.
        self.vision_cache: dict[str, dict[str, Any]] = {}
        self.audio_cache: dict[str, dict[str, Any]] = {}
        self.scene_synthesis_cache: dict[str, dict[str, Any]] = {}
        # Most-recent user voice line per device — populated from outside
        # (formerly /api/message ingress; in dotty-behaviour we have no
        # such ingress, so this stays empty unless a future consumer
        # writes it). Kept on the state object so the snapshot/dashboard
        # surface remains identical to bridge.py.
        self.last_user_line: dict[str, dict[str, Any]] = {}
        # Per-device list of waiters polling /api/vision/latest. The
        # /api/vision/explain handler signals every waiter for the
        # device after the cache updates so concurrent callers (room-
        # view + voice "what do you see") both wake.
        self._vision_waiters: dict[str, list[asyncio.Event]] = {}
        # Same shape for /api/audio/latest (when ported in a later
        # slice — included now so the audio explain route can share
        # the pattern).
        self._audio_waiters: dict[str, list[asyncio.Event]] = {}

    # ------------------------------------------------------------------
    # Bus
    # ------------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[PerceptionEvent]:
        """Register a new subscriber. Caller must `unsubscribe` when done."""
        q: asyncio.Queue[PerceptionEvent] = asyncio.Queue(
            maxsize=PERCEPTION_QUEUE_MAX
        )
        self._listeners.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[PerceptionEvent]) -> None:
        try:
            self._listeners.remove(q)
        except ValueError:
            pass

    def listener_count(self) -> int:
        return len(self._listeners)

    def broadcast(self, event: PerceptionEvent) -> None:
        """Fan an event out to all subscribers, append to the recent-events
        ring, and (in the bridge) bump Prometheus counters.

        Drops events for a subscriber whose queue is full (warns once
        per drop). Never raises — perception is best-effort.
        """
        self._recent_append(event)
        if not self._listeners:
            return
        for q in list(self._listeners):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning(
                    "perception queue full, dropping event: %s", event.name
                )

    def _recent_append(self, event: PerceptionEvent) -> None:
        device_id = event.device_id or ""
        if not device_id or device_id == "unknown":
            return
        ring = self._recent.get(device_id)
        if ring is None:
            ring = collections.deque(maxlen=PERCEPTION_RECENT_MAX)
            self._recent[device_id] = ring
        ring.append(
            {"ts": event.ts, "name": event.name, "data": dict(event.data)}
        )

    def get_recent(
        self, device_id: str, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Most-recent perception events for ``device_id`` (newest first)."""
        ring = self._recent.get(device_id)
        if not ring:
            return []
        items = list(ring)
        items.reverse()
        if limit is not None:
            items = items[:limit]
        return items

    # ------------------------------------------------------------------
    # State machine — mutate per-device state from incoming events
    # ------------------------------------------------------------------

    def update_state(
        self, device_id: str, name: str, data: dict[str, Any], ts: float
    ) -> None:
        """Mutate per-device state for one event.

        Mirrors bridge.py's `_update_perception_state` exactly. The
        bridge's "don't pop last_face_id on face_lost" invariant lives
        here too — the HuMan detector flickers and freshness is
        enforced at read time via FACE_IDENTITY_AGE_GATE_SEC in
        snapshot.py.
        """
        state = self.state.setdefault(device_id, {})
        state["last_event_t"] = ts
        state["last_event_name"] = name

        if name == "face_detected":
            state["face_present"] = True
            state["last_face_t"] = ts
        elif name == "face_lost":
            state["face_present"] = False
            state["last_face_lost_t"] = ts
        elif name == "sound_event":
            state["last_sound_dir"] = data.get("direction")
            state["last_sound_t"] = ts
            state["last_sound_energy"] = data.get("energy")
        elif name == "state_changed":
            new_state = (data.get("state") or "").strip().lower()
            if new_state:
                state["current_state"] = new_state
                state["last_state_change_t"] = ts
                if new_state == "dance":
                    state["dance_active"] = True
                elif state.get("dance_active"):
                    state["dance_active"] = False
        elif name == "dance_started":
            state["dance_active"] = True
            state["last_dance_started_t"] = ts
        elif name == "dance_ended":
            state["dance_active"] = False
            state["last_dance_ended_t"] = ts
        elif name == "chat_status":
            listening = bool(data.get("listening"))
            state["listening"] = listening
            state["last_chat_status_t"] = ts
        elif name == "face_recognized":
            identity = (data.get("identity") or "").strip()
            if identity:
                state["last_face_id"] = identity
                state["last_face_recognized_t"] = ts

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    def is_dance_active(self, device_id: str) -> bool:
        return bool(self.state.get(device_id, {}).get("dance_active"))

    def current_device_state(self, device_id: str) -> str:
        return self.state.get(device_id, {}).get("current_state", "idle")

    def get_fresh_face_id(
        self,
        device_id: str,
        *,
        ttl_sec: float,
        now: float | None = None,
    ) -> str | None:
        """Return the most-recent identified face for ``device_id`` if
        the identity is younger than ``ttl_sec``.

        Single source of truth for "is this person still identified?"
        across the dashboard chip, talk-turn perception snapshot, and
        the face-identified LED refresh loop. Replaces the "pop on
        face_lost" approach which collapsed identity within ~1 s of
        detector flicker.
        """
        state = self.state.get(device_id) or {}
        identity = (state.get("last_face_id") or "").strip()
        if not identity or identity == "unknown":
            return None
        last_t = state.get("last_face_recognized_t") or 0.0
        if not last_t:
            return None
        wall_now = now if now is not None else time.time()
        if (wall_now - last_t) > ttl_sec:
            return None
        return identity

    # ------------------------------------------------------------------
    # Vision/audio explain waiter map — fan-out signal from the explain
    # POST to every listener polling the latest endpoint for the same
    # device. Concurrent callers (room-view + voice "what do you see")
    # both wake; bridge.py's single-event pattern lost the first waiter
    # when the second overwrote the entry.
    # ------------------------------------------------------------------

    def register_vision_waiter(self, device_id: str) -> asyncio.Event:
        event = asyncio.Event()
        self._vision_waiters.setdefault(device_id, []).append(event)
        return event

    def unregister_vision_waiter(
        self, device_id: str, event: asyncio.Event
    ) -> None:
        waiters = self._vision_waiters.get(device_id)
        if not waiters:
            return
        try:
            waiters.remove(event)
        except ValueError:
            pass
        if not waiters:
            self._vision_waiters.pop(device_id, None)

    def signal_vision_waiters(self, device_id: str) -> None:
        for ev in self._vision_waiters.get(device_id, ()):
            ev.set()

    def register_audio_waiter(self, device_id: str) -> asyncio.Event:
        event = asyncio.Event()
        self._audio_waiters.setdefault(device_id, []).append(event)
        return event

    def unregister_audio_waiter(
        self, device_id: str, event: asyncio.Event
    ) -> None:
        waiters = self._audio_waiters.get(device_id)
        if not waiters:
            return
        try:
            waiters.remove(event)
        except ValueError:
            pass
        if not waiters:
            self._audio_waiters.pop(device_id, None)

    def signal_audio_waiters(self, device_id: str) -> None:
        for ev in self._audio_waiters.get(device_id, ()):
            ev.set()

    def annotate_for_introspection(
        self, devices: Iterable[str] | None = None, *, now: float | None = None
    ) -> dict[str, dict[str, Any]]:
        """Snapshot of self.state with `sensor_age_s` + `sensor_stale`
        annotations, shaped the way `/api/perception/state` returns.

        ``devices`` selects specific device_ids; None means all known.
        """
        wall = now if now is not None else time.time()

        def _annotate(raw: dict[str, Any]) -> dict[str, Any]:
            out = dict(raw)
            last_t = out.get("last_event_t")
            if last_t is None:
                age = float("inf")
            else:
                age = max(0.0, wall - last_t)
            out["sensor_age_s"] = age
            out["sensor_stale"] = age > PERCEPTION_STALE_THRESHOLD_SEC
            return out

        if devices is None:
            return {did: _annotate(s) for did, s in self.state.items()}
        return {did: _annotate(self.state.get(did, {})) for did in devices}
