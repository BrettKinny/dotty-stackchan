"""FaceLostAborter — two-stage filter on face_lost events."""

from __future__ import annotations

import asyncio
import time

import pytest

from consumers import FaceLostAborter
from perception import PerceptionEvent, PerceptionState

from ._fakes import FakeXiaozhi, let_consumer_settle


async def _run_with_consumer(state, xiaozhi, window_sec, grace_sec, body):
    consumer = FaceLostAborter(
        state, xiaozhi, window_sec=window_sec, grace_sec=grace_sec
    )
    task = asyncio.create_task(consumer.run())
    try:
        await let_consumer_settle()
        await body()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_face_lost_outside_window_no_abort() -> None:
    async def go() -> None:
        state = PerceptionState()
        # No prior greet recorded → last_face_greet_t is 0 → way outside window
        xiaozhi = FakeXiaozhi()

        async def body() -> None:
            state.broadcast(
                PerceptionEvent(
                    device_id="dev-1", name="face_lost", data={}, ts=time.time()
                )
            )
            await asyncio.sleep(0.2)
            assert xiaozhi.abort_calls == []

        await _run_with_consumer(state, xiaozhi, 10.0, 0.05, body)

    asyncio.run(go())


def test_face_lost_inside_window_aborts_after_grace() -> None:
    async def go() -> None:
        state = PerceptionState()
        now = time.time()
        # Mark a fresh greet
        state.state["dev-1"] = {"last_face_greet_t": now - 1.0}
        xiaozhi = FakeXiaozhi()

        async def body() -> None:
            state.broadcast(
                PerceptionEvent(
                    device_id="dev-1", name="face_lost", data={}, ts=now
                )
            )
            await asyncio.sleep(0.15)
            assert xiaozhi.abort_calls == [{"device_id": "dev-1"}]

        await _run_with_consumer(state, xiaozhi, 10.0, 0.05, body)

    asyncio.run(go())


def test_face_returns_within_grace_cancels_abort() -> None:
    async def go() -> None:
        state = PerceptionState()
        now = time.time()
        state.state["dev-1"] = {"last_face_greet_t": now - 1.0}
        xiaozhi = FakeXiaozhi()

        async def body() -> None:
            state.broadcast(
                PerceptionEvent(
                    device_id="dev-1", name="face_lost", data={}, ts=now
                )
            )
            await asyncio.sleep(0.05)
            state.broadcast(
                PerceptionEvent(
                    device_id="dev-1", name="face_detected", data={}, ts=now + 0.1
                )
            )
            await asyncio.sleep(0.2)
            assert xiaozhi.abort_calls == []

        await _run_with_consumer(state, xiaozhi, 10.0, 0.15, body)

    asyncio.run(go())


_STATE_ABORTS = [
    (None, None, True),
    ("state_changed", {"state": "idle"}, True),
    ("state_changed", {"state": "talk"}, True),
    ("state_changed", {"state": "story_time"}, False),
    ("state_changed", {"state": "security"}, False),
    ("state_changed", {"state": "sleep"}, False),
    ("state_changed", {"state": "dance"}, False),
    ("dance_started", {}, False),
]


@pytest.mark.parametrize("name,data,aborts", _STATE_ABORTS)
def test_face_lost_aborts_only_in_idle_and_talk(name, data, aborts) -> None:
    """docs/modes.md: sticky states ignore face_lost, so a recent greet
    must not let a lost face cut a story, dance or scan short.

    AI-assisted (Claude Opus 5.5).
    """
    async def go() -> None:
        state = PerceptionState()
        now = time.time()
        if name is not None:
            state.update_state("dev-1", name, data, now - 2.0)
        state.state.setdefault("dev-1", {})["last_face_greet_t"] = now - 1.0
        xiaozhi = FakeXiaozhi()

        async def body() -> None:
            state.broadcast(
                PerceptionEvent(
                    device_id="dev-1", name="face_lost", data={}, ts=now
                )
            )
            await asyncio.sleep(0.15)
            assert bool(xiaozhi.abort_calls) is aborts

        await _run_with_consumer(state, xiaozhi, 10.0, 0.05, body)

    asyncio.run(go())


@pytest.mark.parametrize("name,data,aborts", _STATE_ABORTS)
def test_state_change_during_grace_rechecked(name, data, aborts) -> None:
    """A sticky state entered during the grace period (e.g. a dance
    asked for right after the greet) stops the pending abort from firing.

    AI-assisted (Claude Opus 5.5).
    """
    async def go() -> None:
        state = PerceptionState()
        now = time.time()
        state.state["dev-1"] = {"last_face_greet_t": now - 1.0}
        xiaozhi = FakeXiaozhi()

        async def body() -> None:
            state.broadcast(
                PerceptionEvent(
                    device_id="dev-1", name="face_lost", data={}, ts=now
                )
            )
            await asyncio.sleep(0.02)
            if name is not None:
                state.update_state("dev-1", name, data, now + 0.02)
            await asyncio.sleep(0.2)
            assert bool(xiaozhi.abort_calls) is aborts

        await _run_with_consumer(state, xiaozhi, 10.0, 0.1, body)

    asyncio.run(go())
