"""ProactiveGreeter tests — bus integration + cooldown + LLM fallback."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from calendar_ import CalendarCache, Event
from greeter import CalendarFacade, ProactiveGreeter
from household import HouseholdRegistry
from perception import PerceptionEvent, PerceptionState

from ._fakes import let_consumer_settle


_UTC = ZoneInfo("UTC")


class _RecordingTTS:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.fail = False

    async def __call__(self, device_id: str, text: str) -> None:
        if self.fail:
            raise RuntimeError("tts down")
        self.calls.append((device_id, text))


def _llm_factory(reply: str | None = "Hey there!"):
    calls: list[str] = []

    async def _llm(prompt: str) -> str:
        calls.append(prompt)
        if reply is None:
            raise RuntimeError("llm down")
        return reply

    return _llm, calls


def _household_with(td: Path, yaml_text: str) -> HouseholdRegistry:
    path = td / "household.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return HouseholdRegistry(path=path)


def _make(
    td: Path,
    *,
    llm,
    tts: _RecordingTTS,
    household=None,
    kid_mode: bool = False,
    cache: CalendarCache | None = None,
    clock_value: float = 1_700_000_000.0,
) -> tuple[ProactiveGreeter, PerceptionState]:
    state = PerceptionState()
    cache = cache or CalendarCache()
    facade = CalendarFacade(cache, household_bucket="_household")
    g = ProactiveGreeter(
        state,
        llm,
        facade,
        tts,
        lambda: kid_mode,
        household_registry=household,
        tz=_UTC,
        clock=lambda: clock_value,
        state_path=td / "greeter_state.json",
    )
    return g, state


async def _drive(consumer, body):
    task = asyncio.create_task(consumer.run())
    try:
        await let_consumer_settle()
        await body()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_face_recognized_fires_llm_greeting() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            household = _household_with(
                tdp,
                """
people:
  brett:
    display_name: Brett
""",
            )
            llm, llm_calls = _llm_factory("Hey Brett, library day!")
            tts = _RecordingTTS()
            g, state = _make(tdp, llm=llm, tts=tts, household=household)

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
                await asyncio.sleep(0.02)
                assert tts.calls == [("dev-1", "Hey Brett, library day!")]
                assert len(llm_calls) == 1
                assert "Brett" in llm_calls[0]

            await _drive(g, body)

    asyncio.run(go())


def test_greeting_prompt_includes_own_calendar_events_despite_case() -> None:
    # Audit 2026-06-06 (confirmed 2/3): identity is a lowercase person id
    # ("hudson") but the calendar event's person tag comes from the
    # `[Hudson]` title prefix uncased — the old exact compare dropped the
    # person's own events from their greeting prompt.
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            household = _household_with(
                tdp,
                """
people:
  hudson:
    display_name: Hudson
""",
            )
            cache = CalendarCache()
            cache.events = [
                Event(
                    person="Hudson", time="10:00", summary="library day",
                    start_iso="2026-06-11T10:00:00", calendar_id="cal",
                )
            ]
            llm, llm_calls = _llm_factory("Morning Hudson, library day!")
            tts = _RecordingTTS()
            g, state = _make(
                tdp, llm=llm, tts=tts, household=household, cache=cache,
            )

            async def body() -> None:
                state.broadcast(
                    PerceptionEvent(
                        device_id="dev-1",
                        name="face_recognized",
                        data={"identity": "hudson"},
                        ts=time.time(),
                    )
                )
                await let_consumer_settle()
                await asyncio.sleep(0.02)
                assert len(llm_calls) == 1
                assert "library day" in llm_calls[0]

            await _drive(g, body)

    asyncio.run(go())


def test_unknown_face_skipped_by_default() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            llm, _ = _llm_factory()
            tts = _RecordingTTS()
            g, state = _make(tdp, llm=llm, tts=tts)

            async def body() -> None:
                state.broadcast(
                    PerceptionEvent(
                        device_id="dev-1",
                        name="face_recognized",
                        data={"identity": "unknown"},
                        ts=time.time(),
                    )
                )
                await let_consumer_settle()
                assert tts.calls == []

            await _drive(g, body)

    asyncio.run(go())


def test_llm_failure_falls_back_to_template() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            household = _household_with(
                tdp,
                """
people:
  brett:
    display_name: Brett
""",
            )
            llm, _ = _llm_factory(reply=None)  # raises
            tts = _RecordingTTS()
            g, state = _make(tdp, llm=llm, tts=tts, household=household)

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
                await asyncio.sleep(0.02)
                # Template fallback: "Good <window>, Brett!"
                assert len(tts.calls) == 1
                msg = tts.calls[0][1]
                assert msg.startswith("Good ")
                assert "Brett" in msg

            await _drive(g, body)

    asyncio.run(go())


def test_cooldown_blocks_repeat_greeting() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            household = _household_with(
                tdp,
                """
people:
  brett:
    display_name: Brett
""",
            )
            llm, _ = _llm_factory("hi")
            tts = _RecordingTTS()
            g, state = _make(tdp, llm=llm, tts=tts, household=household)

            async def body() -> None:
                for _ in range(2):
                    state.broadcast(
                        PerceptionEvent(
                            device_id="dev-1",
                            name="face_recognized",
                            data={"identity": "brett"},
                            ts=time.time(),
                        )
                    )
                    await let_consumer_settle()
                    await asyncio.sleep(0.02)
                # Cooldown is 4h default → second greet suppressed
                assert len(tts.calls) == 1

            await _drive(g, body)

    asyncio.run(go())


def test_state_persists_across_instances() -> None:
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        llm, _ = _llm_factory("hi")
        tts = _RecordingTTS()
        g, _ = _make(tdp, llm=llm, tts=tts)
        assert g._take_slot("brett", event_ts=None) is True
        path = tdp / "greeter_state.json"
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert any("brett" in bucket for bucket in data.values())


def test_calendar_facade_passes_events_through() -> None:
    cache = CalendarCache()
    cache.events = [
        Event(
            person="brett", time="09:30", summary="library",
            start_iso="2026-05-18T09:30:00", calendar_id="cal",
        )
    ]
    facade = CalendarFacade(cache, household_bucket="_household")
    assert facade.get_events()[0]["summary"] == "library"
    summary = facade.summarize_for_prompt(
        facade.get_events(), person="brett"
    )
    assert any("library" in line for line in summary)


def test_face_detected_promoted_when_use_face_detected_enabled() -> None:
    import os
    async def go() -> None:
        os.environ["GREETER_USE_FACE_DETECTED"] = "1"
        os.environ["GREETER_GREET_UNKNOWN"] = "1"
        try:
            with tempfile.TemporaryDirectory() as td:
                tdp = Path(td)
                llm, _ = _llm_factory("hi")
                tts = _RecordingTTS()
                g, state = _make(tdp, llm=llm, tts=tts)

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
                    await asyncio.sleep(0.02)
                    assert len(tts.calls) == 1
                    assert "haven" in tts.calls[0][1].lower() or \
                           "met" in tts.calls[0][1].lower()

                await _drive(g, body)
        finally:
            os.environ.pop("GREETER_USE_FACE_DETECTED", None)
            os.environ.pop("GREETER_GREET_UNKNOWN", None)

    asyncio.run(go())
