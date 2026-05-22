"""SleepDreamer — schedule, fire, cancel-on-state-change."""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from consumers import SleepDreamer
from consumers.sleep_dreamer import split_dream_text
from logs import NdjsonWriter
from perception import PerceptionEvent, PerceptionState

from ._fakes import let_consumer_settle


@dataclass
class _FakeNarrative:
    """Drop-in for NarrativeLLMClient. Returns canned text + records calls."""
    calls: list[dict[str, Any]] = field(default_factory=list)
    response: str | None = (
        "Paragraph one of the dream.\n\n"
        "Paragraph two — perception is strange, time bends.\n\n"
        "SUMMARY: Dotty dreams of bending time."
    )

    async def chat(
        self,
        user_prompt: str,
        *,
        system_prompt: str,
        model: str | None = None,
        max_tokens: int = 1200,
        temperature: float = 0.9,
    ) -> str | None:
        self.calls.append(
            {
                "user_prompt": user_prompt,
                "system_prompt": system_prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        return self.response


def _consumer(state, narrative, td: Path, *, count: int, window: float) -> SleepDreamer:
    return SleepDreamer(
        state,
        narrative,
        NdjsonWriter(td, "dreams", ZoneInfo("UTC")),
        window_seconds=window,
        count_per_night=count,
        inspirations=("Dune",),
    )


def test_split_dream_text_with_summary() -> None:
    raw = "para one\n\npara two\nSUMMARY: short summary"
    full, summary = split_dream_text(raw)
    assert summary == "short summary"
    assert "SUMMARY" not in full
    assert "para one" in full


def test_split_dream_text_without_summary() -> None:
    raw = "para one\n\npara two"
    full, summary = split_dream_text(raw)
    assert summary is None
    assert full == raw


def test_sleep_scheduling_fires_dreams_and_writes_ndjson() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            state = PerceptionState()
            narrative = _FakeNarrative()
            # window=0.5s, count=2 → fires at 1/3, 2/3 → 0.17, 0.33 s
            consumer = _consumer(state, narrative, tdp, count=2, window=0.5)
            task = asyncio.create_task(consumer.run())
            try:
                await let_consumer_settle()
                state.broadcast(
                    PerceptionEvent(
                        device_id="dev-1",
                        name="state_changed",
                        data={"state": "sleep"},
                        ts=0.0,
                    )
                )
                # Allow scheduled tasks to fire
                await asyncio.sleep(0.6)
                files = list(tdp.glob("dreams-*.ndjson"))
                assert len(files) == 1
                lines = files[0].read_text(encoding="utf-8").splitlines()
                # Both dreams should have fired
                assert len(lines) == 2
                records = [json.loads(line) for line in lines]
                for r in records:
                    assert r["type"] == "dream"
                    assert r["device"] == "dev-1"
                    assert r["seed"] == "Dune"
                    assert "bending time" in r["summary"]
                # Narrative LLM called twice (once per dream)
                assert len(narrative.calls) == 2
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())


def test_non_sleep_transition_cancels_pending_dreams() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            state = PerceptionState()
            narrative = _FakeNarrative()
            # Long window so dreams won't fire before we cancel
            consumer = _consumer(state, narrative, tdp, count=3, window=10.0)
            task = asyncio.create_task(consumer.run())
            try:
                await let_consumer_settle()
                state.broadcast(
                    PerceptionEvent(
                        device_id="dev-1",
                        name="state_changed",
                        data={"state": "sleep"},
                        ts=0.0,
                    )
                )
                await let_consumer_settle()
                state.broadcast(
                    PerceptionEvent(
                        device_id="dev-1",
                        name="state_changed",
                        data={"state": "idle"},
                        ts=0.1,
                    )
                )
                await asyncio.sleep(0.2)
                # No dreams should have fired
                assert narrative.calls == []
                files = list(tdp.glob("dreams-*.ndjson"))
                assert files == []
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())


def test_dream_with_no_llm_response_is_skipped() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            state = PerceptionState()
            narrative = _FakeNarrative(response=None)
            consumer = _consumer(state, narrative, tdp, count=1, window=0.2)
            task = asyncio.create_task(consumer.run())
            try:
                await let_consumer_settle()
                state.broadcast(
                    PerceptionEvent(
                        device_id="dev-1",
                        name="state_changed",
                        data={"state": "sleep"},
                        ts=0.0,
                    )
                )
                await asyncio.sleep(0.3)
                # LLM was called but no record was written
                assert len(narrative.calls) == 1
                files = list(tdp.glob("dreams-*.ndjson"))
                assert files == []
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
