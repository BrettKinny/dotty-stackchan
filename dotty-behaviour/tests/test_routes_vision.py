"""Vision route tests via TestClient with a fake VLMClient."""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient

from main import app


@dataclass
class _FakeVLM:
    calls: list[dict[str, Any]] = field(default_factory=list)
    description: str = "I see a chair."

    @property
    def configured(self) -> bool:
        return True

    async def describe_image(
        self,
        b64_image: str,
        question: str,
        *,
        system_prompt: str,
        model: str | None = None,
        max_tokens: int = 200,
        temperature: float = 0.3,
        timeout_s: float | None = None,
    ) -> str:
        self.calls.append(
            {
                "b64_image": b64_image,
                "question": question,
                "system_prompt": system_prompt,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "timeout_s": timeout_s,
            }
        )
        return self.description


def _override_vlm(client: TestClient, fake: _FakeVLM) -> None:
    """Inject the fake VLM onto app.state after lifespan boots."""
    client.app.state.vlm = fake  # type: ignore[arg-type]


def _jpeg_file(payload: bytes = b"FAKEJPEG") -> tuple[str, io.BytesIO, str]:
    return ("photo.jpg", io.BytesIO(payload), "image/jpeg")


def test_vision_explain_returns_vlm_description_and_caches_it() -> None:
    with TestClient(app) as client:
        fake = _FakeVLM(description="A red ball on the table.")
        _override_vlm(client, fake)
        r = client.post(
            "/api/vision/explain",
            files={"file": _jpeg_file()},
            data={"question": "What do you see?"},
            headers={"device-id": "dev-1"},
        )
        assert r.status_code == 200
        assert r.json() == {"description": "A red ball on the table."}

        state = client.app.state.perception
        cached = state.vision_cache["dev-1"]
        assert cached["description"] == "A red ball on the table."
        assert cached["jpeg_bytes"] == b"FAKEJPEG"
        assert cached["question"] == "What do you see?"
        assert cached["source"] == "v1"
        assert cached["room_match_person_id"] is None

        assert len(fake.calls) == 1
        assert fake.calls[0]["question"] == "What do you see?"
        # Default system prompt is the non-kid wording
        assert "young child" not in fake.calls[0]["system_prompt"]


def test_vision_explain_kid_mode_changes_system_prompt() -> None:
    with TestClient(app) as client:
        fake = _FakeVLM()
        _override_vlm(client, fake)
        client.app.state.kid_mode = True
        r = client.post(
            "/api/vision/explain",
            files={"file": _jpeg_file()},
            headers={"device-id": "dev-1"},
        )
        assert r.status_code == 200
        # Reset so other tests don't see the leak
        client.app.state.kid_mode = False
        assert "young child" in fake.calls[0]["system_prompt"]


def test_vision_explain_default_device_id_is_unknown() -> None:
    with TestClient(app) as client:
        fake = _FakeVLM()
        _override_vlm(client, fake)
        r = client.post(
            "/api/vision/explain",
            files={"file": _jpeg_file()},
        )
        assert r.status_code == 200
        state = client.app.state.perception
        assert "unknown" in state.vision_cache


def test_vision_latest_waits_then_returns_cache() -> None:
    """Open /api/vision/latest first, then POST /api/vision/explain — the
    pending GET should resolve with the description from the POST."""
    import threading

    with TestClient(app) as client:
        fake = _FakeVLM(description="A spinning top.")
        _override_vlm(client, fake)

        result: dict = {}

        def _poll() -> None:
            r = client.get("/api/vision/latest/dev-poll")
            result["status"] = r.status_code
            result["body"] = r.json()

        t = threading.Thread(target=_poll, daemon=True)
        t.start()
        # Give the GET a moment to register its waiter
        import time as _time
        _time.sleep(0.05)
        client.post(
            "/api/vision/explain",
            files={"file": _jpeg_file()},
            headers={"device-id": "dev-poll"},
        )
        t.join(timeout=3.0)
        assert not t.is_alive(), "vision/latest never returned"
        assert result["status"] == 200
        assert result["body"]["description"] == "A spinning top."


# NOTE: A wire-level "/api/vision/latest returns 404 on timeout" test
# was considered but skipped — the route's wait_for timeout is 15 s,
# and dropping that to a tunable knob would add config surface for one
# test. The signal-path is exercised by test_signal_vision_waiters_*
# below and the live container will catch a regression in seconds.


def test_signal_vision_waiters_wakes_pending_listener() -> None:
    """Unit-level test of the bus signal logic — no HTTP."""
    from perception import PerceptionState

    async def go() -> None:
        state = PerceptionState()
        event = state.register_vision_waiter("dev-1")
        # Signal in the background after a short delay
        async def _signal() -> None:
            await asyncio.sleep(0.01)
            state.signal_vision_waiters("dev-1")

        task = asyncio.create_task(_signal())
        await asyncio.wait_for(event.wait(), timeout=0.5)
        await task
        state.unregister_vision_waiter("dev-1", event)

    asyncio.run(go())


def test_unregister_vision_waiter_after_signal_is_idempotent() -> None:
    from perception import PerceptionState

    state = PerceptionState()
    event = state.register_vision_waiter("dev-1")
    state.signal_vision_waiters("dev-1")
    state.unregister_vision_waiter("dev-1", event)
    # second unregister is a no-op
    state.unregister_vision_waiter("dev-1", event)


# ---------------------------------------------------------------------------
# Room-view sentinel path (#101 — restores PR #93's named-greet path)
# ---------------------------------------------------------------------------


@dataclass
class _FakePerson:
    id: str
    display_name: str
    appearance: str = ""


@dataclass
class _FakeHousehold:
    """Minimal HouseholdRegistry stand-in for room_view tests.

    Carries real `id` fields distinct from display names — the audit
    found the previous fake derived ids as `display_name.lower()`,
    which masked the id-vs-display-name parsing bug in production.
    """

    people: list[_FakePerson] = field(default_factory=list)

    def render_roster_for_vlm(self, *, max_line_chars: int = 80) -> str:
        return "\n".join(
            f"  {p.display_name}: {p.appearance}"
            for p in self.people if p.appearance
        )

    def iter(self):  # noqa: A003 — matches HouseholdRegistry method
        return tuple(self.people)

    def get(self, person_id: str):  # matches HouseholdRegistry method
        if not person_id:
            return None
        for p in self.people:
            if p.id == person_id.lower():
                return p
        return None

    def roster_ids_with_appearance(self) -> set[str]:
        return {p.id for p in self.people if p.appearance}


def _override_household(client: TestClient, fake: _FakeHousehold) -> None:
    client.app.state.household = fake  # type: ignore[arg-type]


def test_room_view_sentinel_matches_roster_and_broadcasts_face_recognized() -> None:
    """The full happy path: sentinel question → roster-aware VLM call →
    parse → cache with source=room_view + room_match_person_id →
    face_recognized event broadcast on the perception bus."""
    with TestClient(app) as client:
        fake_vlm = _FakeVLM(
            description=(
                "DESC: adult with goatee and dark sweater | "
                "NAME: Brett | MOOD: engaged"
            ),
        )
        _override_vlm(client, fake_vlm)
        _override_household(client, _FakeHousehold(people=[
            _FakePerson("brett", "Brett", appearance="tall, dark hair, goatee"),
            _FakePerson("hudson", "Hudson", appearance="small child, blond"),
        ]))

        state = client.app.state.perception
        bus_q = state.subscribe()
        try:
            r = client.post(
                "/api/vision/explain",
                files={"file": _jpeg_file()},
                data={"question": "__ROOM_VIEW_V1__"},
                headers={"device-id": "dev-rv"},
            )
            assert r.status_code == 200

            cached = state.vision_cache["dev-rv"]
            assert cached["source"] == "room_view"
            assert cached["room_match_person_id"] == "brett"
            assert cached["description"] == (
                "adult with goatee and dark sweater"
            )

            # System prompt for VLM call was the roster-aware one.
            assert "ONLY by names from the list" in (
                fake_vlm.calls[0]["system_prompt"]
            )
            # Roster substituted into the question.
            assert "Brett: tall, dark hair, goatee" in fake_vlm.calls[0][
                "question"
            ]

            # face_recognized broadcast landed on the bus.
            event = bus_q.get_nowait()
            assert event.name == "face_recognized"
            assert event.device_id == "dev-rv"
            assert event.data == {
                "identity": "brett", "source": "room_view",
            }

            # Mood plumbed into perception state.
            assert state.state["dev-rv"]["face_mood"] == "engaged"
            # Identity mirrored into per-device state (face_identified_
            # refresher reads last_face_id to keep pixel 6 green past
            # its 4 s firmware timeout). Bug caught during the #102
            # bench sweep — broadcast alone wasn't enough; we also need
            # to call update_state the way /api/perception/event does.
            assert state.state["dev-rv"]["last_face_id"] == "brett"
            assert state.state["dev-rv"]["last_face_recognized_t"] > 0
        finally:
            state.unsubscribe(bus_q)


def test_room_view_no_match_does_not_broadcast() -> None:
    """Off-roster name → cache description but no face_recognized event."""
    with TestClient(app) as client:
        fake_vlm = _FakeVLM(
            description=(
                "DESC: stranger in a red jacket | "
                "NAME: unknown | MOOD: neutral"
            ),
        )
        _override_vlm(client, fake_vlm)
        _override_household(client, _FakeHousehold(people=[
            _FakePerson("brett", "Brett", appearance="tall, dark hair"),
        ]))

        state = client.app.state.perception
        bus_q = state.subscribe()
        try:
            r = client.post(
                "/api/vision/explain",
                files={"file": _jpeg_file()},
                data={"question": "__ROOM_VIEW_V1__"},
                headers={"device-id": "dev-rv2"},
            )
            assert r.status_code == 200

            cached = state.vision_cache["dev-rv2"]
            assert cached["source"] == "room_view"
            assert cached["room_match_person_id"] is None
            assert cached["description"] == "stranger in a red jacket"

            # No face_recognized event broadcast.
            assert bus_q.empty()
        finally:
            state.unsubscribe(bus_q)


def test_room_view_cooldown_blocks_second_call() -> None:
    """Within DOTTY_IDLE_VISION_COOLDOWN_SEC of the previous capture, the
    second sentinel call skips the VLM entirely and caches the
    no-one-in-view sentinel."""
    with TestClient(app) as client:
        fake_vlm = _FakeVLM(
            description=(
                "DESC: tall adult | NAME: Brett | MOOD: engaged"
            ),
        )
        _override_vlm(client, fake_vlm)
        _override_household(client, _FakeHousehold(people=[
            _FakePerson("brett", "Brett", appearance="tall, dark hair"),
        ]))

        state = client.app.state.perception

        # First call — VLM fires.
        r1 = client.post(
            "/api/vision/explain",
            files={"file": _jpeg_file()},
            data={"question": "__ROOM_VIEW_V1__"},
            headers={"device-id": "dev-cd"},
        )
        assert r1.status_code == 200
        assert len(fake_vlm.calls) == 1

        # Second call within cooldown — should skip the VLM and cache
        # the no-person sentinel.
        r2 = client.post(
            "/api/vision/explain",
            files={"file": _jpeg_file()},
            data={"question": "__ROOM_VIEW_V1__"},
            headers={"device-id": "dev-cd"},
        )
        assert r2.status_code == 200
        assert r2.json() == {"description": "no one in view"}
        # VLM was NOT called again.
        assert len(fake_vlm.calls) == 1
        cached = state.vision_cache["dev-cd"]
        assert cached["source"] == "room_view"
        assert cached["room_match_person_id"] is None


def test_room_view_with_empty_registry_falls_back_to_v1() -> None:
    """Sentinel question + empty roster → v1 path (no VLM system-prompt
    swap, no face_recognized event), but `source` still 'room_view' so
    the dashboard attributes the capture correctly."""
    with TestClient(app) as client:
        fake_vlm = _FakeVLM(description="Adult in dark sweater.")
        _override_vlm(client, fake_vlm)
        _override_household(client, _FakeHousehold(people=[]))  # empty

        state = client.app.state.perception
        bus_q = state.subscribe()
        try:
            r = client.post(
                "/api/vision/explain",
                files={"file": _jpeg_file()},
                data={"question": "__ROOM_VIEW_V1__"},
                headers={"device-id": "dev-empty"},
            )
            assert r.status_code == 200
            cached = state.vision_cache["dev-empty"]
            assert cached["source"] == "room_view"
            assert cached["room_match_person_id"] is None
            # Fallback v1 question substituted, NOT the sentinel.
            assert "approximate age range" in fake_vlm.calls[0]["question"]
            # Default (non-roster) system prompt was used.
            assert "ONLY by names from the list" not in (
                fake_vlm.calls[0]["system_prompt"]
            )
            # No face_recognized event.
            assert bus_q.empty()
        finally:
            state.unsubscribe(bus_q)


# ---------------------------------------------------------------------------
# /api/vision/cache + /api/vision/photo/{device_id} (Tile 2 of #115 — the
# bridge dashboard's vision card + /ui/vision/large modal consume these.)
# ---------------------------------------------------------------------------


def test_vision_cache_route_returns_metadata_only() -> None:
    """The JSON dump must strip jpeg_bytes — base64-expanding raw image
    bytes through the JSON encoder is wasteful and the dashboard fetches
    the binary separately via /api/vision/photo/{device_id}."""
    with TestClient(app) as client:
        fake = _FakeVLM(description="A toy robot.")
        _override_vlm(client, fake)
        # Seed the cache through the explain endpoint so the entry shape
        # matches production (rather than hand-building it on app.state).
        r = client.post(
            "/api/vision/explain",
            files={"file": _jpeg_file(b"BINARYDATA")},
            data={"question": "What do you see?"},
            headers={"device-id": "dev-meta"},
        )
        assert r.status_code == 200

        resp = client.get("/api/vision/cache")
        assert resp.status_code == 200
        body = resp.json()
        assert "dev-meta" in body
        entry = body["dev-meta"]
        assert entry["description"] == "A toy robot."
        assert entry["question"] == "What do you see?"
        assert entry["source"] == "v1"
        # Critical: jpeg_bytes must NOT appear in the JSON payload.
        assert "jpeg_bytes" not in entry
        # Other metadata fields are preserved.
        assert "timestamp" in entry
        assert "wall_ts" in entry


def test_vision_cache_route_empty_when_no_captures() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/vision/cache")
        assert resp.status_code == 200
        assert resp.json() == {}


def test_vision_photo_route_serves_binary_jpeg() -> None:
    with TestClient(app) as client:
        fake = _FakeVLM()
        _override_vlm(client, fake)
        r = client.post(
            "/api/vision/explain",
            files={"file": _jpeg_file(b"RAWJPEGBYTES")},
            headers={"device-id": "dev-photo"},
        )
        assert r.status_code == 200

        resp = client.get("/api/vision/photo/dev-photo")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.content == b"RAWJPEGBYTES"


def test_vision_photo_route_404_when_missing() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/vision/photo/no-such-device")
        assert resp.status_code == 404
        assert resp.json() == {"error": "no photo"}
