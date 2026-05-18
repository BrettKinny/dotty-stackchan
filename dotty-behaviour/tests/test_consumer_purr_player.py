"""PurrPlayer — play_asset on head_pet_started events."""

from __future__ import annotations

import asyncio
import time

from consumers import PurrPlayer
from perception import PerceptionEvent, PerceptionState

from ._fakes import FakeXiaozhi, let_consumer_settle


async def _spin(state, xiaozhi, body, *, cooldown=5.0, duration=2.0):
    consumer = PurrPlayer(
        state,
        xiaozhi,
        asset_path="/assets/purr.opus",
        cooldown_sec=cooldown,
        duration_sec=duration,
    )
    task = asyncio.create_task(consumer.run())
    try:
        await let_consumer_settle()
        await body()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_head_pet_started_dispatches_purr_asset() -> None:
    async def go() -> None:
        state = PerceptionState()
        xiaozhi = FakeXiaozhi()

        async def body() -> None:
            state.broadcast(
                PerceptionEvent(
                    device_id="dev-1",
                    name="head_pet_started",
                    data={},
                    ts=time.time(),
                )
            )
            await let_consumer_settle()
            assert xiaozhi.play_asset_calls == [
                {"device_id": "dev-1", "asset": "/assets/purr.opus"}
            ]

        await _spin(state, xiaozhi, body)

    asyncio.run(go())


def test_within_cooldown_skips_second_purr() -> None:
    async def go() -> None:
        state = PerceptionState()
        now = time.time()
        # Mark a recent purr
        state.state["dev-1"] = {"last_purr_t": now - 1.0}
        xiaozhi = FakeXiaozhi()

        async def body() -> None:
            state.broadcast(
                PerceptionEvent(
                    device_id="dev-1",
                    name="head_pet_started",
                    data={},
                    ts=now,
                )
            )
            await let_consumer_settle()
            assert xiaozhi.play_asset_calls == []

        await _spin(state, xiaozhi, body, cooldown=5.0)

    asyncio.run(go())


def test_purr_extends_last_chat_t_to_suppress_localiser() -> None:
    async def go() -> None:
        state = PerceptionState()
        xiaozhi = FakeXiaozhi()
        now = time.time()

        async def body() -> None:
            state.broadcast(
                PerceptionEvent(
                    device_id="dev-1",
                    name="head_pet_started",
                    data={},
                    ts=now,
                )
            )
            await let_consumer_settle()
            assert state.state["dev-1"]["last_chat_t"] == now + 2.0

        await _spin(state, xiaozhi, body, cooldown=5.0, duration=2.0)

    asyncio.run(go())
