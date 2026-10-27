"""FaceGreeter — bare + named greeting paths."""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from consumers import FaceGreeter
from household import HouseholdRegistry
from perception import PerceptionEvent, PerceptionState

from ._fakes import FakeXiaozhi, let_consumer_settle


_UTC = ZoneInfo("UTC")


def _household_with(td: Path, yaml_text: str) -> HouseholdRegistry:
    path = td / "household.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return HouseholdRegistry(path=path)


def _consumer(
    state, xiaozhi, household, *,
    bare_text="Hi!",
    bare_interval=30.0,
    hour_start=0,
    hour_end=24,
) -> FaceGreeter:
    return FaceGreeter(
        state, xiaozhi, household,
        bare_greet_text=bare_text,
        bare_min_interval_sec=bare_interval,
        bare_hour_start=hour_start,
        bare_hour_end=hour_end,
        name_template="Oh, it's {name}!",
        name_min_interval_sec=30.0,
        name_quiet_after_chat_sec=10.0,
        tz=_UTC,
    )


async def _drive(consumer, body):
    task = asyncio.create_task(consumer.run())
    try:
        await let_consumer_settle()
        await body()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_face_detected_fires_bare_hi_when_no_roster() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            household = _household_with(Path(td), "people: {}\n")
            state = PerceptionState()
            xiaozhi = FakeXiaozhi()
            consumer = _consumer(state, xiaozhi, household)

            async def body() -> None:
                state.broadcast(
                    PerceptionEvent(
                        device_id="dev-1",
                        name="face_detected",
                        data={},
                        ts=time.time(),
                    )
                )
                await let_consumer_settle()
                assert xiaozhi.inject_text_calls == [
                    {"device_id": "dev-1", "text": "Hi!"}
                ]
                # last_face_greet_t recorded
                assert state.state["dev-1"]["last_face_greet_t"] > 0

            await _drive(consumer, body)

    asyncio.run(go())


def test_face_detected_fires_bare_hi_when_roster_lacks_appearances() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            household = _household_with(
                Path(td),
                """
people:
  brett:
    display_name: Brett
""",
            )
            state = PerceptionState()
            xiaozhi = FakeXiaozhi()
            consumer = _consumer(state, xiaozhi, household)

            async def body() -> None:
                state.broadcast(
                    PerceptionEvent(
                        device_id="dev-1",
                        name="face_detected",
                        data={},
                        ts=time.time(),
                    )
                )
                await let_consumer_settle()
                assert xiaozhi.inject_text_calls == [
                    {"device_id": "dev-1", "text": "Hi!"}
                ]

            await _drive(consumer, body)

    asyncio.run(go())


def test_face_detected_suppressed_when_roster_has_appearances() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            household = _household_with(
                Path(td),
                """
people:
  brett:
    display_name: Brett
    appearance: tall
""",
            )
            state = PerceptionState()
            xiaozhi = FakeXiaozhi()
            consumer = _consumer(state, xiaozhi, household)

            async def body() -> None:
                state.broadcast(
                    PerceptionEvent(
                        device_id="dev-1",
                        name="face_detected",
                        data={},
                        ts=time.time(),
                    )
                )
                await let_consumer_settle()
                assert xiaozhi.inject_text_calls == []

            await _drive(consumer, body)

    asyncio.run(go())


def test_face_detected_within_cooldown_skipped() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            household = _household_with(Path(td), "people: {}\n")
            state = PerceptionState()
            now = time.time()
            state.state["dev-1"] = {"last_face_greet_t": now - 1.0}
            xiaozhi = FakeXiaozhi()
            consumer = _consumer(state, xiaozhi, household, bare_interval=30.0)

            async def body() -> None:
                state.broadcast(
                    PerceptionEvent(
                        device_id="dev-1",
                        name="face_detected",
                        data={},
                        ts=now,
                    )
                )
                await let_consumer_settle()
                assert xiaozhi.inject_text_calls == []

            await _drive(consumer, body)

    asyncio.run(go())


def test_face_detected_outside_hour_window_skipped() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            household = _household_with(Path(td), "people: {}\n")
            state = PerceptionState()
            xiaozhi = FakeXiaozhi()
            # Window of [25..26) is impossible → always suppressed
            consumer = _consumer(
                state, xiaozhi, household, hour_start=25, hour_end=26,
            )

            async def body() -> None:
                state.broadcast(
                    PerceptionEvent(
                        device_id="dev-1",
                        name="face_detected",
                        data={},
                        ts=time.time(),
                    )
                )
                await let_consumer_settle()
                assert xiaozhi.inject_text_calls == []

            await _drive(consumer, body)

    asyncio.run(go())


def test_empty_greet_text_no_inject() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            household = _household_with(Path(td), "people: {}\n")
            state = PerceptionState()
            xiaozhi = FakeXiaozhi()
            consumer = _consumer(state, xiaozhi, household, bare_text="")

            async def body() -> None:
                state.broadcast(
                    PerceptionEvent(
                        device_id="dev-1",
                        name="face_detected",
                        data={},
                        ts=time.time(),
                    )
                )
                await let_consumer_settle()
                assert xiaozhi.inject_text_calls == []

            await _drive(consumer, body)

    asyncio.run(go())


def test_face_recognized_says_name_and_lights_led() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            household = _household_with(
                Path(td),
                """
people:
  brett:
    display_name: Brett
""",
            )
            state = PerceptionState()
            xiaozhi = FakeXiaozhi()
            consumer = _consumer(state, xiaozhi, household)

            async def body() -> None:
                state.broadcast(
                    PerceptionEvent(
                        device_id="dev-1",
                        name="face_recognized",
                        data={"identity": "brett"},
                        ts=time.time(),
                    )
                )
                await let_consumer_settle()
                assert xiaozhi.say_calls == [
                    {"device_id": "dev-1", "text": "Oh, it's Brett!"}
                ]
                assert xiaozhi.set_face_identified_calls == [
                    {"device_id": "dev-1"}
                ]

            await _drive(consumer, body)

    asyncio.run(go())


def test_face_recognized_unknown_identity_no_op() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            household = _household_with(
                Path(td),
                """
people:
  brett:
    display_name: Brett
""",
            )
            state = PerceptionState()
            xiaozhi = FakeXiaozhi()
            consumer = _consumer(state, xiaozhi, household)

            async def body() -> None:
                state.broadcast(
                    PerceptionEvent(
                        device_id="dev-1",
                        name="face_recognized",
                        data={"identity": "unknown_person"},
                        ts=time.time(),
                    )
                )
                await let_consumer_settle()
                assert xiaozhi.say_calls == []
                assert xiaozhi.set_face_identified_calls == []

            await _drive(consumer, body)

    asyncio.run(go())


def test_face_recognized_suppressed_when_chat_fresh() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            household = _household_with(
                Path(td),
                """
people:
  brett:
    display_name: Brett
""",
            )
            state = PerceptionState()
            now = time.time()
            state.state["dev-1"] = {"last_chat_t": now - 2.0}
            xiaozhi = FakeXiaozhi()
            consumer = _consumer(state, xiaozhi, household)

            async def body() -> None:
                state.broadcast(
                    PerceptionEvent(
                        device_id="dev-1",
                        name="face_recognized",
                        data={"identity": "brett"},
                        ts=now,
                    )
                )
                await let_consumer_settle()
                assert xiaozhi.say_calls == []

            await _drive(consumer, body)

    asyncio.run(go())


def test_face_recognized_per_identity_cooldown() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            household = _household_with(
                Path(td),
                """
people:
  brett:
    display_name: Brett
""",
            )
            state = PerceptionState()
            now = time.time()
            state.state["dev-1"] = {
                "last_name_greet_t": {"brett": now - 1.0}
            }
            xiaozhi = FakeXiaozhi()
            consumer = _consumer(state, xiaozhi, household)

            async def body() -> None:
                state.broadcast(
                    PerceptionEvent(
                        device_id="dev-1",
                        name="face_recognized",
                        data={"identity": "brett"},
                        ts=now,
                    )
                )
                await let_consumer_settle()
                assert xiaozhi.say_calls == []

            await _drive(consumer, body)

    asyncio.run(go())


# The device states from docs/modes.md, and whether a proactive greet is
# allowed in each. None is a device that never reported a state.
_STATE_GREETS = [
    (None, True),
    ("idle", True),
    ("talk", True),
    ("story_time", False),
    ("security", False),
    ("sleep", False),
    ("dance", False),
]


def _set_device_state(state: PerceptionState, device_state: str | None) -> None:
    if device_state is not None:
        state.update_state(
            "dev-1", "state_changed", {"state": device_state}, 100.0
        )


@pytest.mark.parametrize("device_state,greets", _STATE_GREETS)
def test_bare_greet_follows_device_state(device_state, greets) -> None:
    """docs/modes.md allows a proactive greet only in idle and talk.

    A suppressed greet also leaves last_face_greet_t unset, so
    FaceLostAborter does not treat it as a recent greet.

    AI-assisted (Claude Opus 5.5).
    """
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            household = _household_with(Path(td), "people: {}\n")
            state = PerceptionState()
            _set_device_state(state, device_state)
            xiaozhi = FakeXiaozhi()
            consumer = _consumer(state, xiaozhi, household)

            async def body() -> None:
                state.broadcast(
                    PerceptionEvent(
                        device_id="dev-1",
                        name="face_detected",
                        data={},
                        ts=time.time(),
                    )
                )
                await let_consumer_settle()
                assert bool(xiaozhi.inject_text_calls) is greets
                stamped = state.state.get("dev-1", {}).get("last_face_greet_t")
                assert bool(stamped) is greets

            await _drive(consumer, body)

    asyncio.run(go())


@pytest.mark.parametrize("device_state,greets", _STATE_GREETS)
def test_named_greet_follows_device_state(device_state, greets) -> None:
    """The named greet obeys the same idle/talk gate as the bare one.

    AI-assisted (Claude Opus 5.5).
    """
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            household = _household_with(
                Path(td),
                """
people:
  brett:
    display_name: Brett
""",
            )
            state = PerceptionState()
            _set_device_state(state, device_state)
            xiaozhi = FakeXiaozhi()
            consumer = _consumer(state, xiaozhi, household)

            async def body() -> None:
                state.broadcast(
                    PerceptionEvent(
                        device_id="dev-1",
                        name="face_recognized",
                        data={"identity": "brett"},
                        ts=time.time(),
                    )
                )
                await let_consumer_settle()
                assert bool(xiaozhi.say_calls) is greets
                assert bool(xiaozhi.set_face_identified_calls) is greets

            await _drive(consumer, body)

    asyncio.run(go())
