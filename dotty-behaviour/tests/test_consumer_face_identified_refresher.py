"""FaceIdentifiedRefresher — periodic re-fire of set_face_identified."""

from __future__ import annotations

import asyncio
import time

from consumers import FaceIdentifiedRefresher
from perception import PerceptionState

from ._fakes import FakeXiaozhi


async def _spin(state, xiaozhi, body, *, interval=0.05, ttl=30.0, quiet=2.0):
    consumer = FaceIdentifiedRefresher(
        state,
        xiaozhi,
        interval_sec=interval,
        ttl_sec=ttl,
        quiet_after_lost_sec=quiet,
    )
    task = asyncio.create_task(consumer.run())
    try:
        await body()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_refreshes_when_identity_is_fresh_and_face_present() -> None:
    async def go() -> None:
        state = PerceptionState()
        now = time.time()
        state.state["dev-1"] = {
            "last_face_id": "brett",
            "last_face_recognized_t": now - 1.0,
            "face_present": True,
        }
        xiaozhi = FakeXiaozhi()

        async def body() -> None:
            # Wait one interval-tick + a margin
            await asyncio.sleep(0.15)
            assert any(
                c["device_id"] == "dev-1"
                for c in xiaozhi.set_face_identified_calls
            )

        await _spin(state, xiaozhi, body, interval=0.05)

    asyncio.run(go())


def test_skips_when_identity_ttl_expired() -> None:
    async def go() -> None:
        state = PerceptionState()
        now = time.time()
        state.state["dev-1"] = {
            "last_face_id": "brett",
            "last_face_recognized_t": now - 999.0,  # old → ttl expired
            "face_present": True,
        }
        xiaozhi = FakeXiaozhi()

        async def body() -> None:
            await asyncio.sleep(0.15)
            assert xiaozhi.set_face_identified_calls == []

        await _spin(state, xiaozhi, body, interval=0.05, ttl=30.0)

    asyncio.run(go())


def test_skips_when_face_lost_for_too_long() -> None:
    async def go() -> None:
        state = PerceptionState()
        now = time.time()
        state.state["dev-1"] = {
            "last_face_id": "brett",
            "last_face_recognized_t": now - 1.0,
            "face_present": False,
            "last_face_lost_t": now - 10.0,  # lost ages ago
        }
        xiaozhi = FakeXiaozhi()

        async def body() -> None:
            await asyncio.sleep(0.15)
            assert xiaozhi.set_face_identified_calls == []

        await _spin(state, xiaozhi, body, interval=0.05, quiet=2.0)

    asyncio.run(go())


def test_refreshes_when_face_recently_lost_within_quiet_window() -> None:
    async def go() -> None:
        state = PerceptionState()
        now = time.time()
        state.state["dev-1"] = {
            "last_face_id": "brett",
            "last_face_recognized_t": now - 1.0,
            "face_present": False,
            "last_face_lost_t": now - 0.5,  # lost recently
        }
        xiaozhi = FakeXiaozhi()

        async def body() -> None:
            await asyncio.sleep(0.15)
            assert any(
                c["device_id"] == "dev-1"
                for c in xiaozhi.set_face_identified_calls
            )

        await _spin(state, xiaozhi, body, interval=0.05, quiet=2.0)

    asyncio.run(go())
