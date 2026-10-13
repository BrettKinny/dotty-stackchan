"""Smoke tests for the PerceptionState class.

Equivalent in spirit to bridge's test_perception_bus / test_update_state
— we're asserting the lift preserved semantics, not exploring new
behaviour. Async tests use plain asyncio.run rather than pulling
pytest-asyncio in.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from perception import PerceptionEvent, PerceptionState


def test_current_device_state_survives_daemon_reconstruction(tmp_path: Path) -> None:
    state_file = tmp_path / "perception-state.json"
    before_restart = PerceptionState(state_path=state_file)
    before_restart.update_state(
        "dev-1", "state_changed", {"state": "security"}, 100.0
    )

    after_restart = PerceptionState(state_path=state_file)

    assert after_restart.current_device_state("dev-1") == "security"


def test_invalid_persisted_state_fails_safe_to_idle(tmp_path: Path) -> None:
    state_file = tmp_path / "perception-state.json"
    state_file.write_text('{"dev-1":"security"}', encoding="utf-8")

    restarted = PerceptionState(state_path=state_file)

    assert restarted.current_device_state("dev-1") == "idle"


def test_invalid_persisted_mutex_value_fails_safe_to_idle(tmp_path: Path) -> None:
    state_file = tmp_path / "perception-state.json"
    state_file.write_text('{"dev-1":{"current_state":7}}', encoding="utf-8")

    restarted = PerceptionState(state_path=state_file)

    assert restarted.current_device_state("dev-1") == "idle"


def test_restart_does_not_revive_transient_sensor_state(tmp_path: Path) -> None:
    state_file = tmp_path / "perception-state.json"
    before_restart = PerceptionState(state_path=state_file)
    before_restart.update_state("dev-1", "face_detected", {}, 90.0)
    before_restart.update_state(
        "dev-1", "state_changed", {"state": "security"}, 100.0
    )

    after_restart = PerceptionState(state_path=state_file)

    assert after_restart.current_device_state("dev-1") == "security"
    assert "face_present" not in after_restart.state["dev-1"]


def test_subscribe_broadcast_unsubscribe_roundtrip() -> None:
    async def go() -> None:
        ps = PerceptionState()
        q = ps.subscribe()
        assert ps.listener_count() == 1
        ev = PerceptionEvent(
            device_id="dev-1", name="face_detected", data={}, ts=time.time()
        )
        ps.broadcast(ev)
        got = await asyncio.wait_for(q.get(), timeout=1.0)
        assert got.name == "face_detected"
        assert got.device_id == "dev-1"
        ps.unsubscribe(q)
        assert ps.listener_count() == 0

    asyncio.run(go())


def test_broadcast_with_no_listeners_is_noop() -> None:
    ps = PerceptionState()
    ev = PerceptionEvent(
        device_id="dev-1", name="face_detected", data={}, ts=time.time()
    )
    ps.broadcast(ev)  # must not raise
    assert ps.get_recent("dev-1") == [
        {"ts": ev.ts, "name": "face_detected", "data": {}}
    ]


def test_recent_ring_skips_unknown_device() -> None:
    ps = PerceptionState()
    ev = PerceptionEvent(
        device_id="unknown", name="face_detected", data={}, ts=1.0
    )
    ps.broadcast(ev)
    assert ps.get_recent("unknown") == []


def test_update_state_face_detected_lost() -> None:
    ps = PerceptionState()
    ps.update_state("dev-1", "face_detected", {}, 100.0)
    assert ps.state["dev-1"]["face_present"] is True
    assert ps.state["dev-1"]["last_face_t"] == 100.0
    ps.update_state("dev-1", "face_lost", {}, 101.0)
    assert ps.state["dev-1"]["face_present"] is False
    assert ps.state["dev-1"]["last_face_lost_t"] == 101.0


def test_update_state_face_lost_does_not_clear_identity() -> None:
    """Mirror of bridge.py's face-flicker tolerance — face_lost must not
    pop last_face_id / face_mood. Freshness is enforced at read time
    by snapshot()."""
    ps = PerceptionState()
    ps.update_state(
        "dev-1", "face_recognized", {"identity": "brett"}, 100.0
    )
    assert ps.state["dev-1"]["last_face_id"] == "brett"
    ps.update_state("dev-1", "face_lost", {}, 101.0)
    assert ps.state["dev-1"].get("last_face_id") == "brett"


def test_update_state_state_changed_into_and_out_of_dance() -> None:
    ps = PerceptionState()
    ps.update_state("dev-1", "state_changed", {"state": "dance"}, 1.0)
    assert ps.is_dance_active("dev-1") is True
    assert ps.current_device_state("dev-1") == "dance"
    ps.update_state("dev-1", "state_changed", {"state": "idle"}, 2.0)
    assert ps.is_dance_active("dev-1") is False
    assert ps.current_device_state("dev-1") == "idle"


def test_update_state_chat_status_toggles_listening() -> None:
    ps = PerceptionState()
    ps.update_state("dev-1", "chat_status", {"listening": True}, 1.0)
    assert ps.state["dev-1"]["listening"] is True
    ps.update_state("dev-1", "chat_status", {"listening": False}, 2.0)
    assert ps.state["dev-1"]["listening"] is False


def test_chat_edges_refresh_quiet_timestamp_without_shortening_purr() -> None:
    """AI-assisted regression: OpenAI Codex (GPT-6)."""
    ps = PerceptionState()
    ps.update_state("dev-1", "chat_status", {"listening": True}, 100.0)
    assert ps.state["dev-1"]["last_chat_t"] == 100.0
    ps.update_state("dev-1", "chat_status", {"listening": False}, 110.0)
    assert ps.state["dev-1"]["last_chat_t"] == 110.0
    # PurrPlayer intentionally reserves a future quiet window for its audio.
    ps.state["dev-1"]["last_chat_t"] = 120.0
    ps.update_state("dev-1", "chat_status", {"listening": True}, 115.0)
    assert ps.state["dev-1"]["last_chat_t"] == 120.0
    ps.update_state("dev-1", "chat_status", {"listening": False}, 125.0)
    assert ps.state["dev-1"]["last_chat_t"] == 125.0


def test_full_queue_drops_event_without_raising() -> None:
    async def go() -> None:
        ps = PerceptionState()
        q = ps.subscribe()
        # Fill the queue to its bounded maxsize
        for i in range(q.maxsize):
            q.put_nowait(
                PerceptionEvent(device_id="x", name="x", data={}, ts=float(i))
            )
        # One more broadcast → must not raise; event is dropped
        ps.broadcast(
            PerceptionEvent(
                device_id="dev-1", name="face_detected", data={}, ts=1.0
            )
        )
        # Original queued events are still available
        first = await q.get()
        assert first.name == "x"

    asyncio.run(go())


def test_annotate_for_introspection_marks_stale() -> None:
    ps = PerceptionState()
    ps.update_state("dev-1", "face_detected", {}, 0.0)
    out = ps.annotate_for_introspection(now=10_000.0)
    assert out["dev-1"]["sensor_stale"] is True
    assert out["dev-1"]["sensor_age_s"] == 10_000.0


def test_get_recent_newest_first_with_limit() -> None:
    ps = PerceptionState()
    for i in range(5):
        ps.broadcast(
            PerceptionEvent(
                device_id="dev-1", name=f"e{i}", data={}, ts=float(i)
            )
        )
    recent = ps.get_recent("dev-1", limit=3)
    assert [r["name"] for r in recent] == ["e4", "e3", "e2"]
