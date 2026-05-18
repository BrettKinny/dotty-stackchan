"""WakeWordTurner — deliberate head turn on wake_word_detected."""

from __future__ import annotations

import asyncio
import time

from consumers import WakeWordTurner
from perception import PerceptionEvent, PerceptionState

from ._fakes import FakeXiaozhi, let_consumer_settle


async def _spin(state, xiaozhi, body):
    consumer = WakeWordTurner(state, xiaozhi, yaw_deg=45, speed=200)
    task = asyncio.create_task(consumer.run())
    try:
        await let_consumer_settle()
        await body()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_left_direction_turns_negative_yaw() -> None:
    async def go() -> None:
        state = PerceptionState()
        xiaozhi = FakeXiaozhi()

        async def body() -> None:
            state.broadcast(
                PerceptionEvent(
                    device_id="dev-1",
                    name="wake_word_detected",
                    data={"direction": "left", "phrase": "hey dotty"},
                    ts=time.time(),
                )
            )
            await let_consumer_settle()
            assert xiaozhi.set_head_angles_calls[0]["yaw"] == -45
            assert xiaozhi.set_head_angles_calls[0]["speed"] == 200

        await _spin(state, xiaozhi, body)

    asyncio.run(go())


def test_face_present_suppresses_turn() -> None:
    async def go() -> None:
        state = PerceptionState()
        state.state["dev-1"] = {"face_present": True}
        xiaozhi = FakeXiaozhi()

        async def body() -> None:
            state.broadcast(
                PerceptionEvent(
                    device_id="dev-1",
                    name="wake_word_detected",
                    data={"direction": "left"},
                    ts=time.time(),
                )
            )
            await let_consumer_settle()
            assert xiaozhi.set_head_angles_calls == []

        await _spin(state, xiaozhi, body)

    asyncio.run(go())


def test_centre_direction_skipped() -> None:
    async def go() -> None:
        state = PerceptionState()
        xiaozhi = FakeXiaozhi()

        async def body() -> None:
            state.broadcast(
                PerceptionEvent(
                    device_id="dev-1",
                    name="wake_word_detected",
                    data={"direction": "centre"},
                    ts=time.time(),
                )
            )
            await let_consumer_settle()
            assert xiaozhi.set_head_angles_calls == []

        await _spin(state, xiaozhi, body)

    asyncio.run(go())


def test_wake_word_writes_last_sound_turn_t_to_suppress_ambient() -> None:
    """The wake-word turn should poke last_sound_turn_t so the
    sound-localiser doesn't immediately re-fire on the user's
    continued voice after the wake word."""
    async def go() -> None:
        state = PerceptionState()
        xiaozhi = FakeXiaozhi()
        now = time.time()

        async def body() -> None:
            state.broadcast(
                PerceptionEvent(
                    device_id="dev-1",
                    name="wake_word_detected",
                    data={"direction": "right"},
                    ts=now,
                )
            )
            await let_consumer_settle()
            assert state.state["dev-1"]["last_sound_turn_t"] == now

        await _spin(state, xiaozhi, body)

    asyncio.run(go())
