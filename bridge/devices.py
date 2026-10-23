"""Pick the real robot out of dotty-behaviour's per-device perception state.

dotty-behaviour keys its state by device id and keeps every id it has ever
seen (and persists their mutex state across restarts, #173). That includes
ids no robot owns: acceptance probes such as ``automation-persistence-probe``,
the ``unknown`` fallback id, and devices restored from disk that have sent
nothing since. The dashboard must not let any of those decide the State
card, the robot status dot, or the "sensors stale" warning.

Pure functions over the ``/api/perception/state`` payload so they are easy
to test; no I/O here.
"""

from __future__ import annotations

import math
import os
import re
from typing import Any, Iterable

# Same default as dotty-behaviour's PERCEPTION_EPHEMERAL_DEVICE_RE. Mirrored
# here so the dashboard ignores probes even against an older dotty-behaviour
# that does not expire them yet.
_PROBE_RE_SRC = os.environ.get(
    "PERCEPTION_EPHEMERAL_DEVICE_RE",
    r"(automation|test|probe|smoke|ci|uat)[-_].*|unknown",
)
try:
    _PROBE_RE: re.Pattern[str] | None = re.compile(_PROBE_RE_SRC)
except re.error:
    _PROBE_RE = None


def is_probe_device(device_id: str) -> bool:
    """True for test / automation / fallback ids that are not a robot."""
    if not device_id:
        return True
    return bool(_PROBE_RE and _PROBE_RE.fullmatch(device_id))


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def activity_t(device_state: dict) -> float:
    """Newest wall-clock timestamp the device has produced (0 when none)."""
    stamps = (
        _num(device_state.get(key))
        for key in ("last_event_t", "last_state_change_t", "last_chat_t")
    )
    return max((t for t in stamps if t is not None), default=0.0)


def sensor_seen(device_state: dict) -> bool:
    """Has this device sent a sensor event since dotty-behaviour started?

    Uses dotty-behaviour's ``sensor_seen`` when present, else infers it from
    a finite ``sensor_age_s`` / a ``last_event_t``."""
    seen = device_state.get("sensor_seen")
    if isinstance(seen, bool):
        return seen
    if _num(device_state.get("sensor_age_s")) is not None:
        return True
    return _num(device_state.get("last_event_t")) is not None


def candidate_devices(pstate: dict) -> dict[str, dict]:
    """Per-device entries that could be the real robot."""
    if not isinstance(pstate, dict):
        return {}
    return {
        did: s for did, s in pstate.items()
        if isinstance(did, str) and isinstance(s, dict) and not is_probe_device(did)
    }


def pick_live_device(
    pstate: dict, connected: Iterable[str] | None = None,
) -> str | None:
    """The device the dashboard should describe.

    A device with a live xiaozhi WebSocket wins; otherwise the one with the
    newest activity. Probes and fallback ids are never picked."""
    devices = candidate_devices(pstate)
    if not devices:
        return None
    connected_set = set(connected or ())
    return max(
        devices,
        key=lambda did: (did in connected_set, activity_t(devices[did])),
    )


def sensor_health(
    pstate: dict, connected: Iterable[str] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Split devices into (judged, stale, fresh) for the stale-sensor check.

    A device is judged only when it is a real device, has sent a sensor
    event since dotty-behaviour started and, when the connected list is
    known (``connected`` is not None), currently holds a xiaozhi WebSocket.
    A robot that is not connected is offline or asleep, not "hung"; a
    device that never streamed cannot have a stuck stream."""
    connected_set = None if connected is None else set(connected)
    judged: list[str] = []
    stale: list[str] = []
    fresh: list[str] = []
    for did, s in candidate_devices(pstate).items():
        if not sensor_seen(s):
            continue
        if connected_set is not None and did not in connected_set:
            continue
        judged.append(did)
        (stale if s.get("sensor_stale") else fresh).append(did)
    return judged, stale, fresh
