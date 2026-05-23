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
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Import bridge.py as `bridge_app` and neutralise its heavy lifespan.
# ---------------------------------------------------------------------------

# State files (kid-mode + smart-mode) default to /root/zeroclaw-bridge/state/...
# which the CI runner can neither read (/root is 700) nor write. Python 3.12's
# Path.exists() raises PermissionError on stat failure (3.13+ swallows), so
# even reading the toggle at module-import time blows up. Redirect both to a
# writable temp dir before import.
_state_dir = Path(tempfile.mkdtemp(prefix="dotty-bridge-test-state-"))
os.environ.setdefault("DOTTY_KID_MODE_STATE", str(_state_dir / "kid-mode"))
os.environ.setdefault("DOTTY_SMART_MODE_STATE", str(_state_dir / "smart-mode"))
# CONVO_LOG_DIR defaults to /root/zeroclaw-bridge/logs — same problem. Used
# by /api/message and /api/message/stream via _convo_log.log_turn.
os.environ.setdefault("CONVO_LOG_DIR", str(_state_dir / "logs"))

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


# ---------------------------------------------------------------------------
# /api/voice/escalate — Tier-2 tool dispatcher
# ---------------------------------------------------------------------------

class VoiceEscalateTests(unittest.TestCase):
    def setUp(self):
        _install_acp_stub()

    def test_unknown_tool_returns_friendly_string(self):
        r = client.post("/api/voice/escalate", json={
            "tool": "not_a_tool", "args": {}, "session_id": "s",
        })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["result"], "(unknown tool: not_a_tool)")

    def test_known_tool_result_round_trips(self):
        async def fake_handler(args, session_id):
            return f"echo:{args.get('query', '')}|sid={session_id}"

        with patch.dict(
            bridge_app._VOICE_TOOLS,
            {"memory_lookup": fake_handler},
        ):
            r = client.post("/api/voice/escalate", json={
                "tool": "memory_lookup",
                "args": {"query": "birthday"},
                "session_id": "sess-1",
            })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["result"], "echo:birthday|sid=sess-1")

    def test_handler_exception_yields_failed_result(self):
        async def boom(args, session_id):
            raise RuntimeError("tool exploded")

        with patch.dict(
            bridge_app._VOICE_TOOLS,
            {"think_hard": boom},
        ):
            r = client.post("/api/voice/escalate", json={
                "tool": "think_hard", "args": {"question": "?"},
            })
        self.assertEqual(r.status_code, 200)
        # Handler exception is swallowed; client always gets 200 + result.
        self.assertEqual(r.json()["result"], "(think_hard failed)")

    def test_validation_missing_tool(self):
        r = client.post("/api/voice/escalate", json={"args": {}})
        self.assertEqual(r.status_code, 422)


# ---------------------------------------------------------------------------
# /api/vision/explain  + /api/audio/explain — VLM / ASR description routes
# ---------------------------------------------------------------------------

class VisionExplainTests(unittest.TestCase):
    """Just the simple description path. The room_view roster branch has
    its own substantial state-machine + cooldown logic and warrants its
    own dedicated test module."""

    def setUp(self):
        _install_acp_stub()
        bridge_app._vision_cache.clear()

    def test_returns_description_and_caches_it(self):
        with patch.object(
            bridge_app, "_call_vision_api",
            return_value="I see a small black robot on a desk.",
        ):
            r = client.post(
                "/api/vision/explain",
                headers={"device-id": "dotty-x1"},
                files={"file": ("photo.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
                data={"question": "What's in the photo?"},
            )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("black robot", body["description"])
        # Cache populated for the device.
        self.assertIn("dotty-x1", bridge_app._vision_cache)


class AudioExplainTests(unittest.TestCase):
    def setUp(self):
        _install_acp_stub()
        bridge_app._audio_cache.clear()

    def test_returns_caption_and_caches_it(self):
        with patch.object(
            bridge_app, "_call_audio_caption_api",
            return_value="I hear footsteps and a door closing.",
        ):
            r = client.post(
                "/api/audio/explain",
                headers={"device-id": "dotty-x1"},
                files={"file": ("clip.wav", b"RIFFfake", "audio/wav")},
                data={"question": "What's that sound?"},
            )
        self.assertEqual(r.status_code, 200)
        self.assertIn("footsteps", r.json()["description"])
        self.assertIn("dotty-x1", bridge_app._audio_cache)

    def test_caption_failure_returns_fallback_string(self):
        # _call_audio_caption_api catches its own exceptions and returns a
        # human-friendly fallback string — the route still 200s.
        with patch.object(
            bridge_app, "_call_audio_caption_api",
            return_value="I couldn't quite hear that clearly.",
        ):
            r = client.post(
                "/api/audio/explain",
                files={"file": ("clip.wav", b"RIFFfake", "audio/wav")},
            )
        self.assertEqual(r.status_code, 200)
        self.assertIn("couldn't quite hear", r.json()["description"])


# ---------------------------------------------------------------------------
# /api/message — the central voice turn
# ---------------------------------------------------------------------------

class MessageTests(unittest.TestCase):
    """Patch acp.prompt to control the LLM response, _refresh_caches to
    skip network. /api/message wraps the response in emoji-prefix +
    sentence-truncation, so we assert on the shape rather than exact text."""

    def setUp(self):
        _install_acp_stub()
        # acp.prompt is awaited; AsyncMock is required.
        bridge_app.acp.prompt = AsyncMock(return_value="😊 Hi there friend.")
        bridge_app.acp._last_phases = None

    def test_happy_path_returns_response_and_session_id(self):
        with patch.object(bridge_app, "_refresh_caches",
                          new=AsyncMock(return_value=None)):
            r = client.post("/api/message", json={
                "content": "hello",
                "channel": "dotty",
                "session_id": "s-abc",
            })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["session_id"], "s-abc")
        self.assertIn("Hi there", body["response"])
        # Emoji prefix preserved by the response pipeline.
        self.assertTrue(body["response"].lstrip().startswith("😊"))

    def test_missing_session_id_gets_auto_uuid(self):
        with patch.object(bridge_app, "_refresh_caches",
                          new=AsyncMock(return_value=None)):
            r = client.post("/api/message", json={"content": "hi"})
        self.assertEqual(r.status_code, 200)
        sid = r.json()["session_id"]
        # UUIDs are 36 chars with hyphens.
        self.assertEqual(len(sid), 36)
        self.assertEqual(sid.count("-"), 4)

    def test_acp_timeout_yields_fallback_response(self):
        bridge_app.acp.prompt = AsyncMock(side_effect=__import__("asyncio").TimeoutError())
        with patch.object(bridge_app, "_refresh_caches",
                          new=AsyncMock(return_value=None)):
            r = client.post("/api/message", json={"content": "hi"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("thinking too slowly", body["response"])

    def test_acp_exception_yields_generic_fallback(self):
        bridge_app.acp.prompt = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(bridge_app, "_refresh_caches",
                          new=AsyncMock(return_value=None)):
            r = client.post("/api/message", json={"content": "hi"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Something went wrong", r.json()["response"])

    def test_missing_emoji_prefix_gets_one_added(self):
        # When the LLM forgets its emoji, the bridge prepends FALLBACK_EMOJI.
        bridge_app.acp.prompt = AsyncMock(return_value="Hi without emoji.")
        with patch.object(bridge_app, "_refresh_caches",
                          new=AsyncMock(return_value=None)):
            r = client.post("/api/message", json={"content": "hi"})
        body = r.json()
        first_char = body["response"].lstrip()[0]
        # Must be one of the nine allowed emoji (FALLBACK_EMOJI is 😐).
        self.assertIn(first_char, bridge_app.ALLOWED_EMOJIS)


# ---------------------------------------------------------------------------
# /api/message/stream — NDJSON streaming variant
# ---------------------------------------------------------------------------

class MessageStreamTests(unittest.TestCase):
    """Stream emits one JSON line per chunk_cb invocation, then a `final`
    line. When acp.prompt returns immediately without calling chunk_cb
    (the simplest path), the route emits the full text as a single chunk
    + the final line."""

    def setUp(self):
        _install_acp_stub()
        bridge_app.acp.prompt = AsyncMock(return_value="😊 Hello.")
        bridge_app.acp._last_phases = None

    def _read_ndjson(self, raw: bytes) -> list[dict]:
        import json as _json
        return [_json.loads(line) for line in raw.decode().splitlines() if line.strip()]

    def test_ndjson_emits_chunk_then_final(self):
        with patch.object(bridge_app, "_refresh_caches",
                          new=AsyncMock(return_value=None)):
            r = client.post("/api/message/stream",
                            json={"content": "hi", "session_id": "s-1"})
        self.assertEqual(r.status_code, 200)
        records = self._read_ndjson(r.content)
        # At least one chunk and exactly one final.
        types = [rec["type"] for rec in records]
        self.assertIn("chunk", types)
        self.assertEqual(types.count("final"), 1)
        # Final carries session_id + complete content.
        final = records[-1]
        self.assertEqual(final["session_id"], "s-1")
        self.assertIn("Hello", final["content"])

    def test_stream_timeout_emits_error_frame(self):
        bridge_app.acp.prompt = AsyncMock(side_effect=__import__("asyncio").TimeoutError())
        with patch.object(bridge_app, "_refresh_caches",
                          new=AsyncMock(return_value=None)):
            r = client.post("/api/message/stream",
                            json={"content": "hi"})
        records = self._read_ndjson(r.content)
        types = [rec["type"] for rec in records]
        self.assertIn("error", types)
        err = next(r for r in records if r["type"] == "error")
        self.assertIn("thinking too slowly", err["message"])


# ---------------------------------------------------------------------------
# /api/perception/feed — SSE smoke
# ---------------------------------------------------------------------------

class PerceptionFeedTests(unittest.IsolatedAsyncioTestCase):
    """Functional test of the perception bus the SSE route consumes —
    subscribe(), broadcast(), unsubscribe().

    A full SSE round-trip through TestClient or httpx.AsyncClient hangs:
    SSE responses don't flush headers until the first body chunk, and
    the route's keepalive sits in queue.get() for 15s before its first
    yield. Driving the whole loop requires either a real HTTP server or
    a frame-level transport mock. Both are out of scope for boundary
    smoke tests, so we cover the same logic by exercising the route's
    actual dependencies directly. Tracking proper SSE coverage as a
    follow-up."""

    async def asyncSetUp(self):
        _install_acp_stub()
        _reset_perception_state()
        # Clean slate — leftover queues from earlier tests would pull events.
        bridge_app._perception_listeners.clear()

    async def test_subscribe_receives_broadcast(self):
        import asyncio as _asyncio

        queue = bridge_app._perception_subscribe()
        try:
            bridge_app._perception_broadcast({
                "name": "face_detected",
                "device_id": "dev-1",
                "ts": 1234.5,
                "data": {"hint": "test"},
            })
            event = await _asyncio.wait_for(queue.get(), timeout=1.0)
            self.assertEqual(event["name"], "face_detected")
            self.assertEqual(event["device_id"], "dev-1")
            self.assertEqual(event["data"], {"hint": "test"})
        finally:
            bridge_app._perception_unsubscribe(queue)

    async def test_unsubscribe_drops_listener(self):
        queue = bridge_app._perception_subscribe()
        self.assertIn(queue, bridge_app._perception_listeners)
        bridge_app._perception_unsubscribe(queue)
        self.assertNotIn(queue, bridge_app._perception_listeners)

    async def test_broadcast_with_no_listeners_is_noop(self):
        # Empty listeners list — broadcast must not raise.
        self.assertEqual(bridge_app._perception_listeners, [])
        bridge_app._perception_broadcast({
            "name": "face_detected", "device_id": "x", "ts": 0.0, "data": {},
        })


# ---------------------------------------------------------------------------
# /api/vision/latest/{device_id} — event-driven waiter
# ---------------------------------------------------------------------------

class VisionLatestTests(unittest.IsolatedAsyncioTestCase):
    """The endpoint pops _vision_cache[device_id], registers an asyncio.Event
    waiter, then blocks for up to 15s. To exercise the happy path we use
    httpx.AsyncClient so we can run a concurrent populate-and-fire task on
    the same event loop."""

    async def asyncSetUp(self):
        _install_acp_stub()
        bridge_app._vision_cache.clear()
        bridge_app._vision_events.clear()

    async def test_returns_cached_description_when_populated_mid_wait(self):
        import asyncio as _asyncio

        import httpx
        from httpx import ASGITransport

        async def _populate():
            # Wait long enough for the endpoint to register its waiter.
            await _asyncio.sleep(0.05)
            bridge_app._vision_cache["dev1"] = {
                "description": "saw a cat",
                "room_match_person_id": None,
            }
            for ev in bridge_app._vision_events.get("dev1", []):
                ev.set()

        transport = ASGITransport(app=bridge_app.app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as ac:
            populate_task = _asyncio.create_task(_populate())
            r = await ac.get("/api/vision/latest/dev1")
            await populate_task

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["description"], "saw a cat")
        self.assertIsNone(body["room_match_person_id"])


# ---------------------------------------------------------------------------
# /admin/reload-config — issue #77
# ---------------------------------------------------------------------------

class AdminReloadConfigTests(unittest.TestCase):
    def setUp(self):
        # Bypass the localhost guard — TestClient's client.host is
        # "testclient", which the real guard rejects with 403.
        bridge_app.app.dependency_overrides[
            bridge_app._admin_require_localhost
        ] = lambda: None
        # Disable CSRF enforcement for the duration of this test; the
        # middleware reads bridge.csrf._ENFORCE at request time.
        from bridge import csrf as _csrf
        self._csrf_mod = _csrf
        self._csrf_orig = _csrf._ENFORCE
        _csrf._ENFORCE = False
        # Seed env vars from current module globals so a baseline
        # reload reports no changes. Without this, defaults in the
        # module globals (e.g. WEATHER_LOCATION="Brisbane") don't
        # appear in os.environ and the reload reports them as changed
        # to "".
        self._env_patch = patch.dict(os.environ, {
            "WEATHER_LOCATION": str(bridge_app.WEATHER_LOCATION or ""),
            "WEATHER_TTL_SEC": str(bridge_app.WEATHER_TTL_SEC),
            "CALENDAR_TTL_SEC": str(bridge_app.CALENDAR_TTL_SEC),
            "VISION_MODEL": str(bridge_app.VISION_MODEL or ""),
            "VISION_API_KEY": str(bridge_app.VISION_API_KEY or ""),
            "VISION_API_URL": str(bridge_app.VISION_API_URL or ""),
            "VISION_TIMEOUT": str(bridge_app.VISION_TIMEOUT_SEC),
            "VLM_MODEL": str(bridge_app.VLM_MODEL or ""),
            "VLM_API_KEY": str(bridge_app.VLM_API_KEY or ""),
            "VLM_API_URL": str(bridge_app.VLM_API_URL or ""),
        })
        self._env_patch.start()

    def tearDown(self):
        bridge_app.app.dependency_overrides.clear()
        self._csrf_mod._ENFORCE = self._csrf_orig
        self._env_patch.stop()

    def test_no_changes_reports_all_unchanged(self):
        r = client.post("/admin/reload-config")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["reloaded"], [])
        self.assertIn("WEATHER_LOCATION", body["unchanged"])

    def test_changed_var_appears_in_reloaded(self):
        old_loc = bridge_app.WEATHER_LOCATION
        with patch.dict(os.environ, {"WEATHER_LOCATION": "Sydney"}):
            try:
                r = client.post("/admin/reload-config")
                self.assertEqual(r.status_code, 200)
                body = r.json()
                names = [item["name"] for item in body["reloaded"]]
                self.assertIn("WEATHER_LOCATION", names)
                entry = next(
                    item for item in body["reloaded"]
                    if item["name"] == "WEATHER_LOCATION"
                )
                self.assertEqual(entry["new"], "Sydney")
                # Live module global actually updated
                self.assertEqual(bridge_app.WEATHER_LOCATION, "Sydney")
            finally:
                # Restore the original value via another reload
                bridge_app.WEATHER_LOCATION = old_loc

    def test_secret_is_masked_in_response(self):
        with patch.dict(
            os.environ,
            {"VISION_API_KEY": "sk-test-abcd1234efgh5678"},
        ):
            r = client.post("/admin/reload-config")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            entry = next(
                (it for it in body["reloaded"] if it["name"] == "VISION_API_KEY"),
                None,
            )
            self.assertIsNotNone(entry)
            # Mask format: first4…last4
            self.assertNotIn("abcd1234efgh", entry["new"])
            self.assertTrue(entry["new"].startswith("sk-t"))
            self.assertTrue(entry["new"].endswith("5678"))


if __name__ == "__main__":
    unittest.main()
