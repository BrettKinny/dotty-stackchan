"""Tests for the perception-feed relay, the no-wait inject helper, the
last-seen fallback, and the voice-tool inventory.

The dashboard's live pieces all pointed at producers that moved out of the
bridge in #36 / #111: the activity feed opened an SSE route the bridge never
served, and every inject action sat for 8 s on a turn stream nothing publishes
to. Handlers are invoked directly (not via TestClient) so neither CSRF nor a
live dotty-behaviour is in the path.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.requests import Request

_state_dir = Path(tempfile.mkdtemp(prefix="dotty-feed-state-"))
os.environ.setdefault("DOTTY_KID_MODE_STATE", str(_state_dir / "kid-mode"))
os.environ.setdefault("DOTTY_SMART_MODE_STATE", str(_state_dir / "smart-mode"))

_repo_root = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("bridge_app", _repo_root / "bridge.py")
assert _spec is not None and _spec.loader is not None
bridge_app = importlib.util.module_from_spec(_spec)
sys.modules["bridge_app"] = bridge_app
_spec.loader.exec_module(bridge_app)


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


bridge_app.app.router.lifespan_context = _noop_lifespan

import bridge.dashboard as dash  # noqa: E402


def _req(path: str = "/ui/perception/feed", method: str = "GET") -> Request:
    async def _receive():
        # Never signals http.disconnect — the stream ends on upstream EOF.
        await asyncio.sleep(3600)

    return Request(
        {"type": "http", "method": method, "path": path,
         "headers": [], "query_string": b""},
        _receive,
    )


class _FakeUpstream:
    def __init__(self, lines):
        self._lines = lines
        self.closed = False

    def iter_lines(self, chunk_size=None):
        return iter(self._lines)

    def close(self):
        self.closed = True


async def _drain(resp) -> bytes:
    return b"".join([chunk async for chunk in resp.body_iterator])


class _StateSaver(unittest.TestCase):
    def setUp(self):
        saved = dict(dash._state)
        self.addCleanup(lambda: dash._state.update(saved))


class PerceptionFeedProxyTests(_StateSaver):

    def test_relays_upstream_lines_and_closes(self):
        upstream = _FakeUpstream([b'data: {"name":"face_detected"}', b"", b": keepalive", b""])
        dash._state["perception_feed_opener"] = lambda: upstream
        resp = asyncio.run(dash.perception_feed_proxy(_req()))
        self.assertEqual(resp.media_type, "text/event-stream")
        body = asyncio.run(_drain(resp))
        self.assertEqual(
            body,
            b'retry: 5000\n\ndata: {"name":"face_detected"}\n\n: keepalive\n\n',
        )
        self.assertTrue(upstream.closed)

    def test_upstream_down_ends_stream_without_error(self):
        def _boom():
            raise ConnectionError("simulated")

        dash._state["perception_feed_opener"] = _boom
        resp = asyncio.run(dash.perception_feed_proxy(_req()))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(asyncio.run(_drain(resp)), b"retry: 5000\n\n")

    def test_page_subscribes_to_the_relay_route(self):
        html = (_repo_root / "bridge/templates/dashboard.html").read_text()
        self.assertIn("new EventSource('/ui/perception/feed')", html)
        self.assertNotIn("EventSource('/api/perception/feed')", html)


class InjectNoWaitTests(_StateSaver):

    def test_returns_without_waiting_for_a_turn(self):
        async def _inject(text):
            return {"ok": True}

        dash._state["inject_to_device"] = _inject

        async def _run():
            return await asyncio.wait_for(
                dash._inject_or_error(_req("/ui/actions/say", "POST"), "hi", label="hi"),
                timeout=1.0,
            )

        body = asyncio.run(_run()).body.decode("utf-8")
        self.assertIn("sent to Dotty", body)
        self.assertNotIn("no reply", body)

    def test_inject_failure_surfaces_error(self):
        async def _inject(text):
            return {"ok": False, "error": "no device connected"}

        dash._state["inject_to_device"] = _inject
        resp = asyncio.run(
            dash._inject_or_error(_req("/ui/actions/say", "POST"), "hi", label="hi")
        )
        self.assertIn("no device connected", resp.body.decode("utf-8"))


class LastSeenFallbackTests(_StateSaver):

    def setUp(self):
        super().setUp()
        original = dash._log_last_voice_ts
        dash._log_last_voice_ts = lambda: None
        self.addCleanup(lambda: setattr(dash, "_log_last_voice_ts", original))

    def test_falls_back_to_perception_last_chat(self):
        dash._state["perception_state_getter"] = lambda: {
            "dev-1": {"last_chat_t": 100.0}, "dev-2": {"last_chat_t": 250.0},
        }
        self.assertEqual(dash._stackchan_last_seen(), 250.0)

    def test_none_when_no_chat_recorded(self):
        dash._state["perception_state_getter"] = lambda: {"dev-1": {}}
        self.assertIsNone(dash._stackchan_last_seen())


class VoiceToolInventoryTests(unittest.TestCase):

    def test_matches_dotty_pi_ext_tools(self):
        shipped = set()
        for src in (_repo_root / "dotty-pi-ext/src/tools").glob("*.ts"):
            shipped.update(re.findall(r'^\s*name: "(\w+)"', src.read_text(), re.M))
        self.assertEqual({t["name"] for t in dash._VOICE_TOOLS}, shipped)


if __name__ == "__main__":
    unittest.main()
