"""Cache + privacy chokepoint for calendar data.

`summarize_for_prompt` is the *single* sanctioned site for calendar →
prompt injection. Every other caller (proactive greeter, calendar HTTP
route) must funnel through it so the same privacy contract holds:
no ISO timestamps, no email addresses, no calendar IDs, ever.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TypedDict
from zoneinfo import ZoneInfo


class Event(TypedDict):
    person: str
    time: str
    summary: str
    start_iso: str
    calendar_id: str


_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")


def _fold_person_tag(value: str) -> str:
    """Case/whitespace-fold a person tag for comparison."""
    return " ".join((value or "").lower().split())
_ISO_TS_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}"
    r"(?:T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
)


def format_event_time(start_iso: str, *, local_tz: ZoneInfo) -> str:
    """Render `start_iso` as a short local clock string for prompts."""
    if not start_iso:
        return ""
    if "T" not in start_iso:
        return "all-day"
    try:
        dt = datetime.fromisoformat(start_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=local_tz)
        return dt.astimezone(local_tz).strftime("%H:%M")
    except ValueError:
        return ""


def bucket_by_person(events: list[Event]) -> dict[str, list[Event]]:
    out: dict[str, list[Event]] = {}
    for ev in events:
        out.setdefault(ev["person"], []).append(ev)
    return out


def summarize_for_prompt(
    events: list[Event],
    *,
    person: str | set[str] | None = None,
    include_household: bool = True,
    household_bucket: str = "_household",
) -> list[str]:
    """**Single privacy chokepoint** for calendar → prompt injection.

    Strips ISO timestamps, emails, calendar IDs. Emits only short
    `HH:MM summary` / `all-day summary` strings. Every prompt /
    response that surfaces calendar data MUST go through here.

    `person` filters to one person's events. It accepts either a single
    name or a set of equivalent tags (id, display name, calendar
    prefix — see PersonResolver.calendar_tags). Matching is case- and
    whitespace-insensitive: the event's person comes from a free-typed
    `[Name]` title prefix, so `[Hudson]` must match identity `hudson`
    (audit 2026-06-06: the old exact compare dropped a person's own
    events from their greeting).
    """
    if person is None:
        wanted: set[str] | None = None
    elif isinstance(person, str):
        wanted = {_fold_person_tag(person)}
    else:
        wanted = {_fold_person_tag(p) for p in person}
    out: list[str] = []
    for ev in events:
        if wanted is not None:
            if _fold_person_tag(ev["person"]) not in wanted and not (
                include_household and ev["person"] == household_bucket
            ):
                continue
        time_label = ev["time"] or ""
        clean_summary = _ISO_TS_RE.sub("", ev["summary"])
        clean_summary = _EMAIL_RE.sub("[email]", clean_summary)
        clean_summary = " ".join(clean_summary.split())
        if not clean_summary:
            continue
        if ev["person"] != household_bucket and wanted is None:
            tag = f"[{ev['person']}] "
        else:
            tag = ""
        if time_label:
            out.append(f"{time_label} {tag}{clean_summary}".strip())
        else:
            out.append(f"{tag}{clean_summary}".strip())
    return out


class CalendarCache:
    """Combined weather + calendar cache, lazily refreshed.

    Owned as a singleton on app.state by main.lifespan(). The poll
    loop owns refresh; routes/proactive_greeter call read methods
    that return last-fetched data without IO.
    """

    def __init__(self) -> None:
        self.weather_text: str = ""
        self.weather_fetched_perf: float = 0.0
        self.events: list[Event] = []
        self.by_person: dict[str, list[Event]] = {}
        self.calendar_fetched_perf: float = 0.0
        self.calendar_date: str = ""
        self.calendar_failures: int = 0

    def set_events(self, events: list[Event], *, date_str: str,
                   now_perf: float) -> None:
        self.events = events
        self.by_person = bucket_by_person(events)
        self.calendar_fetched_perf = now_perf
        self.calendar_date = date_str
        self.calendar_failures = 0

    def flush_for_new_day(self, *, date_str: str) -> None:
        """Drop yesterday's events eagerly when the local day rolls so
        even a failed refresh on day-roll yields an empty cache rather
        than yesterday's data."""
        self.events = []
        self.by_person = {}
        self.calendar_date = date_str

    def set_weather(self, text: str, *, now_perf: float) -> None:
        if text:
            self.weather_text = text
        self.weather_fetched_perf = now_perf
