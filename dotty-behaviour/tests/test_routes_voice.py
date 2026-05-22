"""voice/take_photo route tests."""

from __future__ import annotations

from time import perf_counter

from fastapi.testclient import TestClient

from household import Person
from main import app
from routes.voice import TAKE_PHOTO_FALLBACK, person_needs_review


def test_take_photo_returns_fallback_when_cache_empty() -> None:
    with TestClient(app) as client:
        state = client.app.state.perception
        state.vision_cache.clear()
        r = client.get("/api/voice/take_photo")
        assert r.status_code == 200
        assert r.json() == {"description": TAKE_PHOTO_FALLBACK}


def test_take_photo_returns_fresh_description() -> None:
    with TestClient(app) as client:
        state = client.app.state.perception
        state.vision_cache.clear()
        state.vision_cache["dev-1"] = {
            "description": "A blue chair and a wooden table.",
            "timestamp": perf_counter(),
        }
        r = client.get("/api/voice/take_photo")
        assert r.status_code == 200
        assert r.json() == {"description": "A blue chair and a wooden table."}


def test_take_photo_caps_description_at_300_chars() -> None:
    with TestClient(app) as client:
        state = client.app.state.perception
        state.vision_cache.clear()
        long = "A " + ("very long description " * 50)
        state.vision_cache["dev-1"] = {
            "description": long,
            "timestamp": perf_counter(),
        }
        r = client.get("/api/voice/take_photo")
        assert len(r.json()["description"]) == 300


def test_take_photo_picks_freshest_across_devices() -> None:
    with TestClient(app) as client:
        state = client.app.state.perception
        state.vision_cache.clear()
        now = perf_counter()
        state.vision_cache["dev-old"] = {
            "description": "Old description.",
            "timestamp": now - 10.0,
        }
        state.vision_cache["dev-new"] = {
            "description": "Fresh description.",
            "timestamp": now,
        }
        r = client.get("/api/voice/take_photo")
        assert r.json() == {"description": "Fresh description."}


def test_take_photo_returns_fallback_when_stale() -> None:
    with TestClient(app) as client:
        state = client.app.state.perception
        state.vision_cache.clear()
        state.vision_cache["dev-1"] = {
            "description": "Stale description.",
            "timestamp": perf_counter() - 999.0,
        }
        r = client.get("/api/voice/take_photo")
        assert r.json() == {"description": TAKE_PHOTO_FALLBACK}


# --- #53 per-person memory kid-safety classifier ---------------------------


class _FakeHousehold:
    """Minimal stand-in for HouseholdRegistry — only `.get()` is used by
    the classifier. Keyed lowercase, matching the real registry."""

    def __init__(self, people: dict[str, Person]) -> None:
        self._people = {k.lower(): v for k, v in people.items()}

    def get(self, person_id: str) -> Person | None:
        return self._people.get((person_id or "").lower())


def test_person_needs_review_adult_by_age() -> None:
    hh = _FakeHousehold({"brett": Person(id="brett", display_name="Brett", age=40)})
    assert person_needs_review(hh, "brett") is False


def test_person_needs_review_minor_by_age() -> None:
    hh = _FakeHousehold({"kid": Person(id="kid", display_name="Kid", age=7)})
    assert person_needs_review(hh, "kid") is True


def test_person_needs_review_adult_by_relation() -> None:
    hh = _FakeHousehold(
        {"mum": Person(id="mum", display_name="Mum", relation="parent")}
    )
    assert person_needs_review(hh, "mum") is False


def test_person_needs_review_child_relation() -> None:
    hh = _FakeHousehold(
        {"son": Person(id="son", display_name="Son", relation="son")}
    )
    assert person_needs_review(hh, "son") is True


def test_person_needs_review_unknown_person() -> None:
    assert person_needs_review(_FakeHousehold({}), "ghost") is True


def test_person_needs_review_sparse_entry() -> None:
    # No age, no relation — cannot confirm adult, so route to review.
    hh = _FakeHousehold({"x": Person(id="x", display_name="X")})
    assert person_needs_review(hh, "x") is True


def test_person_needs_review_none_household() -> None:
    assert person_needs_review(None, "anyone") is True


def test_person_review_status_endpoint() -> None:
    with TestClient(app) as client:
        client.app.state.household = _FakeHousehold({
            "dad": Person(id="dad", display_name="Dad", relation="parent"),
            "kiddo": Person(id="kiddo", display_name="Kiddo", age=6),
        })
        r = client.get(
            "/api/voice/person_review_status", params={"person_id": "Dad"}
        )
        assert r.status_code == 200
        assert r.json() == {"person_id": "dad", "needs_review": False}

        r2 = client.get(
            "/api/voice/person_review_status", params={"person_id": "kiddo"}
        )
        assert r2.json() == {"person_id": "kiddo", "needs_review": True}

        r3 = client.get(
            "/api/voice/person_review_status", params={"person_id": "stranger"}
        )
        assert r3.json() == {"person_id": "stranger", "needs_review": True}
