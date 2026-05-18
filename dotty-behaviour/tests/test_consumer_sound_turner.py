"""SoundTurner — head turn on ambient sound events, idle-only."""

from __future__ import annotations

import asyncio
import time

from consumers import SoundTurner
from perception import PerceptionEvent, PerceptionState

from ._fakes import FakeXiaozhi, let_consumer_settle


async def _spin(state, xiaozhi, body, *, cooldown=3.0, quiet=30.0):
    consumer = SoundTurner(
        state,
        xiaozhi,
        cooldown_sec=cooldown,
        yaw_deg=45,
        speed=250,
        quiet_after_chat_sec=quiet,
    )
    task = asyncio.create_task(consumer.run())
    try:
        await let_consumer_settle()
        await body()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_left_sound_turns_negative_yaw() -> None:
    async def go() -> None:
        state = PerceptionState()
        xiaozhi = FakeXiaozhi()

        async def body() -> None:
            state.broadcast(
                PerceptionEvent(
                    device_id="dev-1",
                    name="sound_event",
                    data={"direction": "left"},
                    ts=time.time(),
                )
            )
            await let_consumer_settle()
            assert len(xiaozhi.set_head_angles_calls) == 1
            assert xiaozhi.set_head_angles_calls[0]["yaw"] == -45
            assert xiaozhi.set_head_angles_calls[0]["speed"] == 250

        await _spin(state, xiaozhi, body)

    asyncio.run(go())


def test_right_sound_turns_positive_yaw() -> None:
    async def go() -> None:
        state = PerceptionState()
        xiaozhi = FakeXiaozhi()

        async def body() -> None:
            state.broadcast(
                PerceptionEvent(
                    device_id="dev-1",
                    name="sound_event",
                    data={"direction": "right"},
                    ts=time.time(),
                )
            )
            await let_consumer_settle()
            assert xiaozhi.set_head_angles_calls[0]["yaw"] == 45

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
                    name="sound_event",
                    data={"direction": "left"},
                    ts=time.time(),
                )
            )
            await let_consumer_settle()
            assert xiaozhi.set_head_angles_calls == []

        await _spin(state, xiaozhi, body)

    asyncio.run(go())


def test_within_cooldown_suppresses_turn() -> None:
    async def go() -> None:
        state = PerceptionState()
        now = time.time()
        state.state["dev-1"] = {"last_sound_turn_t": now - 1.0}
        xiaozhi = FakeXiaozhi()

        async def body() -> None:
            state.broadcast(
                PerceptionEvent(
                    device_id="dev-1",
                    name="sound_event",
                    data={"direction": "left"},
                    ts=now,
                )
            )
            await let_consumer_settle()
            assert xiaozhi.set_head_angles_calls == []

        # cooldown = 3s, last turn was 1s ago → still in cooldown
        await _spin(state, xiaozhi, body, cooldown=3.0)

    asyncio.run(go())


def test_within_quiet_after_chat_suppresses_turn() -> None:
    async def go() -> None:
        state = PerceptionState()
        now = time.time()
        state.state["dev-1"] = {"last_chat_t": now - 5.0}
        xiaozhi = FakeXiaozhi()

        async def body() -> None:
            state.broadcast(
                PerceptionEvent(
                    device_id="dev-1",
                    name="sound_event",
                    data={"direction": "left"},
                    ts=now,
                )
            )
            await let_consumer_settle()
            assert xiaozhi.set_head_angles_calls == []

        await _spin(state, xiaozhi, body, quiet=30.0)

    asyncio.run(go())


def test_emits_head_turn_event_to_bus() -> None:
    async def go() -> None:
        state = PerceptionState()
        xiaozhi = FakeXiaozhi()

        async def body() -> None:
            # Subscribe a second queue to observe the synthetic head_turn
            observer = state.subscribe()
            state.broadcast(
                PerceptionEvent(
                    device_id="dev-1",
                    name="sound_event",
                    data={"direction": "right"},
                    ts=time.time(),
                )
            )
            # Drain — original sound_event + synthetic head_turn must both arrive
            seen_names: list[str] = []
            for _ in range(2):
                ev = await asyncio.wait_for(observer.get(), timeout=0.5)
                seen_names.append(ev.name)
            state.unsubscribe(observer)
            assert "head_turn" in seen_names

        await _spin(state, xiaozhi, body)

    asyncio.run(go())
