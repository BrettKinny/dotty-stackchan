"""Calendar/weather cache + privacy chokepoint + lazy-refresh route."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from calendar_ import (
    CalendarCache,
    Event,
    bucket_by_person,
    format_event_time,
    summarize_for_prompt,
)
from main import app


_UTC = ZoneInfo("UTC")


def _event(person: str = "_household", summary: str = "thing",
           start_iso: str = "2026-05-18T09:30:00", time: str | None = None) -> Event:
    return Event(
        person=person,
        time=time if time is not None else format_event_time(start_iso, local_tz=_UTC),
        summary=summary,
        start_iso=start_iso,
        calendar_id="cal",
    )


def test_format_event_time_all_day() -> None:
    assert format_event_time("2026-05-18", local_tz=_UTC) == "all-day"


def test_format_event_time_with_time() -> None:
    assert format_event_time("2026-05-18T09:30:00", local_tz=_UTC) == "09:30"


def test_format_event_time_empty() -> None:
    assert format_event_time("", local_tz=_UTC) == ""


def test_format_event_time_garbage_datetime_returns_empty() -> None:
    # Strings without 'T' are treated as all-day, so the broken case
    # to assert is a malformed *datetime* string (has T, fails parse).
    assert format_event_time("2026-13-99T99:99", local_tz=_UTC) == ""


def test_bucket_by_person() -> None:
    events = [
        _event(person="brett", summary="a"),
        _event(person="brett", summary="b"),
        _event(person="hudson", summary="c"),
    ]
    out = bucket_by_person(events)
    assert sorted(out.keys()) == ["brett", "hudson"]
    assert len(out["brett"]) == 2


def test_summarize_for_prompt_strips_emails_and_iso() -> None:
    ev = _event(
        person="_household",
        summary="Call alice@example.com 2026-05-18T09:00",
    )
    out = summarize_for_prompt([ev])
    line = out[0]
    assert "alice@example.com" not in line
    assert "[email]" in line
    assert "2026-05-18T09:00" not in line


def test_summarize_for_prompt_tags_non_household() -> None:
    out = summarize_for_prompt(
        [_event(person="brett", summary="lunch")],
        # person=None → include tag
    )
    assert out[0].startswith("09:30 [brett] lunch") or "[brett]" in out[0]


def test_summarize_for_prompt_filters_by_person() -> None:
    events = [
        _event(person="brett", summary="brett thing"),
        _event(person="hudson", summary="hudson thing"),
    ]
    out = summarize_for_prompt(events, person="brett")
    assert any("brett thing" in line for line in out)
    assert all("hudson thing" not in line for line in out)


def test_summarize_for_prompt_includes_household_for_person() -> None:
    events = [
        _event(person="brett", summary="brett thing"),
        _event(person="_household", summary="dinner"),
    ]
    out = summarize_for_prompt(events, person="brett")
    assert any("dinner" in line for line in out)
    assert any("brett thing" in line for line in out)


def test_summarize_for_prompt_person_match_is_case_insensitive() -> None:
    # Audit 2026-06-06: the event person comes from a free-typed
    # `[Hudson]` title prefix while identities are lowercase ids — the
    # old exact compare dropped a person's own events.
    events = [_event(person="Hudson", summary="library day")]
    out = summarize_for_prompt(events, person="hudson")
    assert any("library day" in line for line in out)


def test_summarize_for_prompt_accepts_tag_set() -> None:
    # PersonResolver.calendar_tags hands over a set of equivalent tags
    # (id, display name, calendar prefix) — any of them must match.
    events = [
        _event(person="Mum", summary="yoga"),
        _event(person="hudson", summary="hudson thing"),
    ]
    out = summarize_for_prompt(
        events, person={"maryanne", "mary anne", "mum"},
    )
    assert any("yoga" in line for line in out)
    assert all("hudson thing" not in line for line in out)


def test_summarize_for_prompt_skips_household_when_excluded() -> None:
    events = [
        _event(person="brett", summary="brett thing"),
        _event(person="_household", summary="dinner"),
    ]
    out = summarize_for_prompt(
        events, person="brett", include_household=False
    )
    assert all("dinner" not in line for line in out)


def test_cache_set_and_flush() -> None:
    c = CalendarCache()
    c.set_events([_event()], date_str="2026-05-18", now_perf=1.0)
    assert c.events != []
    assert c.calendar_date == "2026-05-18"
    c.flush_for_new_day(date_str="2026-05-19")
    assert c.events == []
    assert c.calendar_date == "2026-05-19"


def test_calendar_today_route_returns_shape() -> None:
    """With CALENDAR_IDS unset (test env), the route should respond
    with the empty-cache shape and not blow up trying to fetch."""
    with TestClient(app) as client:
        r = client.get("/api/calendar/today")
        assert r.status_code == 200
        body = r.json()
        for k in ("ok", "date", "fetched", "consecutive_failures",
                  "person", "include_household", "events", "count"):
            assert k in body
        assert body["count"] == 0
        assert body["events"] == []
