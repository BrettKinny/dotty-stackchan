"""FaceLostAborter — two-stage filter on face_lost events."""

from __future__ import annotations

import asyncio
import time

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
