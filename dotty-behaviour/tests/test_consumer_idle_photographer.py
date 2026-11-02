"""IdlePhotographer — silent take_photo + notability gate."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

from consumers import IdlePhotographer
from logs import NdjsonWriter
from perception import PerceptionState

from ._fakes import FakeXiaozhi


_UTC = ZoneInfo("UTC")


def _consumer(td: Path, state, xiaozhi, *, jaccard=0.7, camera_denied=lambda: False) -> IdlePhotographer:
    return IdlePhotographer(
        state, xiaozhi,
        NdjsonWriter(td, "perception", _UTC),
        sleep_min_sec=0.01,
        sleep_max_sec=0.02,
        result_wait_sec=0.05,
        notable_jaccard=jaccard,
        question="Describe what you see.",
        camera_denied=camera_denied,
    )


async def _drive(consumer: IdlePhotographer, body) -> None:
    task = asyncio.create_task(consumer.run())
    try:
        # Give the loop one cycle to start + sleep + cycle through
        await asyncio.sleep(0.0)
        await body()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_skips_when_not_idle() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            state = PerceptionState()
            state.state["dev-1"] = {"current_state": "talk"}
            state.vision_cache["dev-1"] = {"wall_ts": 0.0}
            xiaozhi = FakeXiaozhi()
            consumer = _consumer(tdp, state, xiaozhi)

            async def body() -> None:
                await asyncio.sleep(0.1)
                assert xiaozhi.take_photo_calls == []

            await _drive(consumer, body)

    asyncio.run(go())


def test_skips_when_face_present() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            state = PerceptionState()
            state.state["dev-1"] = {
                "current_state": "idle", "face_present": True
            }
            state.vision_cache["dev-1"] = {"wall_ts": 0.0}
            xiaozhi = FakeXiaozhi()
            consumer = _consumer(tdp, state, xiaozhi)

            async def body() -> None:
                await asyncio.sleep(0.1)
                assert xiaozhi.take_photo_calls == []

            await _drive(consumer, body)

    asyncio.run(go())


def test_fires_take_photo_and_writes_notable_record() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            state = PerceptionState()
            state.state["dev-1"] = {"current_state": "idle"}
            state.vision_cache["dev-1"] = {"wall_ts": 0.0}
            xiaozhi = FakeXiaozhi()

            # Simulate the firmware → /api/vision/explain cache fill
            # mid-cycle: the consumer dispatches take_photo, then sleeps
            # RESULT_WAIT_SEC, then reads vision_cache. We just need a
            # fresh entry to be there by the time it reads.
            async def _populate_after_dispatch() -> None:
                # Wait for the first take_photo dispatch to land
                for _ in range(50):
                    if xiaozhi.take_photo_calls:
                        break
                    await asyncio.sleep(0.005)
                state.vision_cache["dev-1"] = {
                    "description": "A red ball sits on a tidy wooden table.",
                    "wall_ts": time.time(),
                    "timestamp": perf_counter(),
                }

            consumer = _consumer(tdp, state, xiaozhi)

            async def body() -> None:
                t = asyncio.create_task(_populate_after_dispatch())
                await asyncio.sleep(0.15)
                await t
                assert len(xiaozhi.take_photo_calls) >= 1
                files = list(tdp.glob("perception-*.ndjson"))
                assert len(files) == 1
                record = json.loads(
                    files[0].read_text(encoding="utf-8").splitlines()[0]
                )
                assert record["type"] == "perception"
                assert record["mode"] == "idle"
                assert "red ball" in record["text"]

            await _drive(consumer, body)

    asyncio.run(go())


def test_skips_record_when_not_notable() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            state = PerceptionState()
            state.state["dev-1"] = {
                "current_state": "idle",
                "last_idle_perception_text":
                    "A red ball sits on a tidy wooden table.",
            }
            state.vision_cache["dev-1"] = {"wall_ts": 0.0}
            xiaozhi = FakeXiaozhi()

            async def _populate_after_dispatch() -> None:
                for _ in range(50):
                    if xiaozhi.take_photo_calls:
                        break
                    await asyncio.sleep(0.005)
                state.vision_cache["dev-1"] = {
                    "description": "A red ball sits on a tidy wooden table.",
                    "wall_ts": time.time(),
                    "timestamp": perf_counter(),
                }

            consumer = _consumer(tdp, state, xiaozhi)

            async def body() -> None:
                t = asyncio.create_task(_populate_after_dispatch())
                await asyncio.sleep(0.15)
                await t
                files = list(tdp.glob("perception-*.ndjson"))
                # Identical description → not notable → no write
                assert files == []

            await _drive(consumer, body)

    asyncio.run(go())


def test_skips_when_take_photo_returns_false() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            state = PerceptionState()
            state.state["dev-1"] = {"current_state": "idle"}
            state.vision_cache["dev-1"] = {"wall_ts": 0.0}
            xiaozhi = FakeXiaozhi()
            xiaozhi.take_photo_result = False
            consumer = _consumer(tdp, state, xiaozhi)

            async def body() -> None:
                await asyncio.sleep(0.1)
                # take_photo was called but no follow-up
                assert len(xiaozhi.take_photo_calls) >= 1
                files = list(tdp.glob("perception-*.ndjson"))
                assert files == []

            await _drive(consumer, body)

    asyncio.run(go())


def test_kid_mode_takes_no_photo():
    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            state = PerceptionState()
            state.state["dev"] = {"current_state": "idle"}
            xiaozhi = FakeXiaozhi()
            consumer = _consumer(Path(td), state, xiaozhi, camera_denied=lambda: True)
            await consumer._one_cycle()
            assert xiaozhi.take_photo_calls == []

    asyncio.run(scenario())
