"""Boundary tests for bridge.py's FastAPI routes — phase 1.

Background: bridge.py exposes 13 HTTP routes but only /health was
exercised through unit tests and none through the actual HTTP boundary.
This file lands TestClient-based tests for the 6 lowest-dependency
routes — health, perception event/state, calendar today, voice
memory_log, voice remember. The remaining 7 (vision, audio, message,
voice escalate, SSE feed, message/stream) need ACPClient + LLM
fakes and will land in follow-up commits.

Import wiring:
  - bridge.py is the FastAPI app; the `bridge` package also exists
    (bridge/__init__.py for submodules), so `import bridge` resolves
    to the package. We load bridge.py explicitly via importlib under
    the module name `bridge_app` to avoid the collision.
  - The app's lifespan spawns ~11 perception consumers + an ACP
    subprocess + a calendar poll. None of that is needed for route
    boundary tests, so we replace `app.router.lifespan_context` with
    a no-op async context manager BEFORE constructing TestClient.
  - `acp` (module-level ACPClient) is stubbed at the attribute level
    so /health reads sane values.
  - `_refresh_caches` is patched to a no-op so /api/calendar/today
    doesn't hit the network.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Import bridge.py as `bridge_app` and neutralise its heavy lifespan.
# ---------------------------------------------------------------------------

# Env-var skips for background loops that the lifespan would otherwise
# spawn. These have no effect at module-import time but keep the lifespan
# tidy in case it ever does fire during a test run.
os.environ.setdefault("IDLE_PHOTOGRAPHER_ENABLED", "0")
os.environ.setdefault("DREAMER_ENABLED", "0")
os.environ.setdefault("DANCE_REFLECTOR_ENABLED", "0")
os.environ.setdefault("CALENDAR_IDS", "")  # short-circuits _fetch_calendar_events
os.environ.setdefault("ZEROCLAW_BIN", "/bin/true")  # never spawned

_repo_root = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "bridge_app", _repo_root / "bridge.py",
)
assert _spec is not None and _spec.loader is not None
bridge_app = importlib.util.module_from_spec(_spec)
sys.modules["bridge_app"] = bridge_app
_spec.loader.exec_module(bridge_app)


@asynccontextmanager
async def _noop_lifespan(_app):
    """No-op lifespan that bypasses ACP spawn, perception consumers,
    proactive greeter, and the calendar poll loop."""
    yield


bridge_app.app.router.lifespan_context = _noop_lifespan


# ---------------------------------------------------------------------------
# Per-test acp stub + cache reset helpers
# ---------------------------------------------------------------------------

class _StubProc:
    """Stand-in for asyncio subprocess. /health reads only `returncode`."""
    def __init__(self, alive: bool = True):
        self.returncode = None if alive else 1


def _install_acp_stub(*, alive: bool = True, sid: str | None = None, turns: int = 0):
    """Replace the module-level `acp` with a stub bare enough for /health
    and the voice memory-write endpoints to read sensible values."""
    acp_stub = MagicMock()
    acp_stub._proc = _StubProc(alive=alive) if alive is not None else None
    acp_stub._sid = sid
    acp_stub._sid_turns = turns
    bridge_app.acp = acp_stub
    return acp_stub


def _reset_perception_state():
    bridge_app._perception_state.clear()


# Module-level TestClient — cheap to reuse; tests reset module state
# (_perception_state, acp stub) in setUp instead of rebuilding the client.
from fastapi.testclient import TestClient  # noqa: E402
client = TestClient(bridge_app.app)

# Separate client for the 500-error tests. TestClient defaults to
# raise_server_exceptions=True (handler exceptions propagate to the
# caller), which is great for debugging but defeats assertions about
# the HTTP status of an exception path. This variant returns 500
# the same way a real client would observe.
client_no_raise = TestClient(bridge_app.app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

class HealthTests(unittest.TestCase):
    def test_alive_acp_no_session(self):
        _install_acp_stub(alive=True, sid=None, turns=0)
        r = client.get("/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["service"], "zeroclaw-bridge")
        self.assertTrue(body["acp_running"])
        self.assertFalse(body["cached_session"])
        self.assertEqual(body["session_turns"], 0)

    def test_dead_acp(self):
        _install_acp_stub(alive=False, sid=None, turns=0)
        r = client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["acp_running"])

    def test_cached_session_reported(self):
        _install_acp_stub(alive=True, sid="sess-42", turns=7)
        body = client.get("/health").json()
        self.assertTrue(body["cached_session"])
        self.assertEqual(body["session_turns"], 7)


# ---------------------------------------------------------------------------
# /api/perception/event + /api/perception/state
# ---------------------------------------------------------------------------

class PerceptionEventStateTests(unittest.TestCase):
    def setUp(self):
        _install_acp_stub()
        _reset_perception_state()

    def test_post_event_returns_204_and_updates_state(self):
        r = client.post(
            "/api/perception/event",
            json={
                "device_id": "dotty-aa:bb",
                "name": "face_detected",
                "data": {},
            },
        )
        self.assertEqual(r.status_code, 204)
        # State must reflect the event for this device.
        state = client.get("/api/perception/state").json()
        self.assertIn("dotty-aa:bb", state)

    def test_state_with_device_id_param_returns_single_device(self):
        client.post("/api/perception/event", json={
            "device_id": "alpha", "name": "face_detected", "data": {},
        })
        client.post("/api/perception/event", json={
            "device_id": "beta", "name": "face_detected", "data": {},
        })
        body = client.get("/api/perception/state?device_id=alpha").json()
        # Per-device query: only `alpha` is keyed in the response.
        self.assertEqual(set(body.keys()), {"alpha"})

    def test_state_annotates_sensor_age_and_staleness(self):
        client.post("/api/perception/event", json={
            "device_id": "x", "name": "face_detected", "data": {},
        })
        entry = client.get("/api/perception/state").json()["x"]
        # Annotations always present per the route docstring.
        self.assertIn("sensor_age_s", entry)
        self.assertIn("sensor_stale", entry)
        self.assertFalse(entry["sensor_stale"])  # fresh event

    def test_state_missing_device_returns_stale(self):
        body = client.get("/api/perception/state?device_id=ghost").json()
        ghost = body["ghost"]
        # sensor_age_s is float('inf') over Python but JSON-encodes to None
        # (FastAPI uses allow_nan=False, then coerces). The actionable signal
        # is sensor_stale, which must be True for a never-seen device.
        self.assertTrue(ghost["sensor_stale"])

    def test_post_event_validation_missing_name(self):
        # `name` is required (no default).
        r = client.post(
            "/api/perception/event",
            json={"device_id": "x", "data": {}},
        )
        self.assertEqual(r.status_code, 422)


# ---------------------------------------------------------------------------
# /api/calendar/today
# ---------------------------------------------------------------------------

class CalendarTodayTests(unittest.TestCase):
    """Calendar route reads _calendar_cache after a lazy refresh. We stub
    _refresh_caches to a no-op so no network is hit, then mutate the
    cache directly to drive each scenario."""

    def setUp(self):
        _install_acp_stub()
        # Reset cache to a known state.
        bridge_app._calendar_cache.update({
            "date": "2026-05-17",
            "fetched": 1715900000.0,
            "consecutive_failures": 0,
            "events": [],
        })

    def test_empty_cache(self):
        with patch.object(bridge_app, "_refresh_caches",
                          new=AsyncMock(return_value=None)):
            r = client.get("/api/calendar/today")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["events"], [])
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["date"], "2026-05-17")
        self.assertIsNone(body["person"])
        self.assertTrue(body["include_household"])

    def test_person_filter_passed_through(self):
        with patch.object(bridge_app, "_refresh_caches",
                          new=AsyncMock(return_value=None)):
            r = client.get(
                "/api/calendar/today?person=alex&include_household=false",
            )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["person"], "alex")
        self.assertFalse(body["include_household"])

    def test_refresh_failure_propagates_500(self):
        with patch.object(
            bridge_app, "_refresh_caches",
            new=AsyncMock(side_effect=RuntimeError("calendar API down")),
        ):
            r = client_no_raise.get("/api/calendar/today")
        self.assertEqual(r.status_code, 500)


# ---------------------------------------------------------------------------
# /api/voice/memory_log + /api/voice/remember
# ---------------------------------------------------------------------------

class VoiceMemoryLogTests(unittest.TestCase):
    """Both endpoints fire-and-forget via _spawn(asyncio.to_thread(...)).
    Patch the blocking store fn to a no-op so no disk writes happen, and
    assert it was invoked with the expected args."""

    def setUp(self):
        _install_acp_stub()

    def test_memory_log_204_and_calls_store(self):
        with patch.object(bridge_app, "_voice_memory_store_blocking") as mock_store:
            r = client.post(
                "/api/voice/memory_log",
                json={
                    "user": "hello",
                    "assistant": "hi there",
                    "session_id": "s-1",
                },
            )
        self.assertEqual(r.status_code, 204)
        self.assertEqual(mock_store.call_count, 1)
        kwargs = mock_store.call_args.kwargs
        self.assertEqual(kwargs["namespace"], "voice")
        self.assertEqual(kwargs["category"], "conversation")
        self.assertEqual(kwargs["session_id"], "s-1")
        self.assertIn("hello", kwargs["content"])
        self.assertIn("hi there", kwargs["content"])

    def test_memory_log_skips_empty_pair(self):
        with patch.object(bridge_app, "_voice_memory_store_blocking") as mock_store:
            r = client.post(
                "/api/voice/memory_log",
                json={"user": "", "assistant": "", "session_id": None},
            )
        self.assertEqual(r.status_code, 204)
        mock_store.assert_not_called()

    def test_memory_log_truncates_long_inputs(self):
        long_user = "u" * 2000
        long_assistant = "a" * 5000
        with patch.object(bridge_app, "_voice_memory_store_blocking") as mock_store:
            client.post("/api/voice/memory_log", json={
                "user": long_user, "assistant": long_assistant,
            })
        kwargs = mock_store.call_args.kwargs
        # Per the route: user is truncated to 500 chars, assistant to 1000.
        # Verify by extracting the segments from the "user: X | assistant: Y" envelope.
        content = kwargs["content"]
        user_seg = content.split(" | assistant: ", 1)[0].removeprefix("user: ")
        assistant_seg = content.split(" | assistant: ", 1)[1]
        self.assertEqual(len(user_seg), 500)
        self.assertEqual(len(assistant_seg), 1000)


class VoiceRememberTests(unittest.TestCase):
    def setUp(self):
        _install_acp_stub()

    def test_remember_204_and_calls_store_with_core_category(self):
        with patch.object(bridge_app, "_voice_memory_store_blocking") as mock_store:
            r = client.post(
                "/api/voice/remember",
                json={"fact": "birthday is March 4", "session_id": "s-1"},
            )
        self.assertEqual(r.status_code, 204)
        self.assertEqual(mock_store.call_count, 1)
        kwargs = mock_store.call_args.kwargs
        self.assertEqual(kwargs["category"], "core")
        self.assertEqual(kwargs["namespace"], "voice")
        self.assertEqual(kwargs["content"], "birthday is March 4")
        # Higher importance than conversation logs per the route.
        self.assertGreater(kwargs["importance"], 0.5)

    def test_remember_skips_empty_fact(self):
        with patch.object(bridge_app, "_voice_memory_store_blocking") as mock_store:
            r = client.post("/api/voice/remember", json={"fact": "   "})
        self.assertEqual(r.status_code, 204)
        mock_store.assert_not_called()

    def test_remember_truncates_to_300_chars(self):
        with patch.object(bridge_app, "_voice_memory_store_blocking") as mock_store:
            client.post(
                "/api/voice/remember",
                json={"fact": "x" * 1000},
            )
        kwargs = mock_store.call_args.kwargs
        self.assertEqual(len(kwargs["content"]), 300)


if __name__ == "__main__":
    unittest.main()
