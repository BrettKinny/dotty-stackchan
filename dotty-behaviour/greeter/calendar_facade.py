"""Adapter that lets the ProactiveGreeter (lifted from bridge.py) keep
its `get_events` / `summarize_for_prompt` interface while reading from
the new CalendarCache.

The greeter expects a single object with both methods. Bridge.py
satisfied that with module-level functions + a one-off shim
(`_CalendarFacade` near the bottom of bridge.py). Same shape here,
cleaned up.
"""

from __future__ import annotations

from typing import Any

from calendar_ import CalendarCache, summarize_for_prompt


class CalendarFacade:
    def __init__(
        self,
        cache: CalendarCache,
        *,
        household_bucket: str,
    ) -> None:
        self._cache = cache
        self._household_bucket = household_bucket

    def get_events(self) -> list[Any]:
        return list(self._cache.events)

    def summarize_for_prompt(
        self,
        events: list[Any],
        *,
        person: str | set[str] | None = None,
        include_household: bool = True,
    ) -> list[str]:
        return summarize_for_prompt(
            events,
            person=person,
            include_household=include_household,
            household_bucket=self._household_bucket,
        )
