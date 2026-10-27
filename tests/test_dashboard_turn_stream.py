"""/ui/events turn stream: history replay and reconnect resume (#183).

PiVoiceLLM reports each turn to /api/voice/turn and the bridge fans it out
on /ui/events. A page opened (or reloaded) after a turn used to show an
empty Turns tab until the next one; the stream now replays recent turns,
and a reconnecting EventSource gets only what it missed via Last-Event-ID.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.requests import Request

_state_dir = Path(tempfile.mkdtemp(prefix="dotty-turns-state-"))
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


def _req(last_event_id: str | None = None) -> Request:
    async def _receive():
        await asyncio.sleep(3600)

    headers = []
    if last_event_id is not None:
        headers.append((b"last-event-id", last_event_id.encode()))
    return Request(
        {"type": "http", "method": "GET", "path": "/ui/events",
         "headers": headers, "query_string": b""},
        _receive,
    )


def _frames(raw: bytes) -> list[dict]:
    """Parse SSE frames into {id, data} dicts (comments / retry skipped)."""
    out = []
    for block in raw.decode().split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in block.splitlines()
            if ": " in line and not line.startswith(":")
        )
        if "data" in fields:
            out.append({"id": fields.get("id"), "data": json.loads(fields["data"])})
    return out


async def _read(resp, n_frames: int, *, publish=None, timeout: float = 2.0) -> bytes:
    """Collect body chunks until n_frames data frames have arrived."""
    it = resp.body_iterator
    buf = b""
    try:
        async def _collect():
            nonlocal buf
            published = False
            async for chunk in it:
                buf += chunk
                frames = buf.count(b"data: ")
                if publish is not None and not published and frames >= n_frames - 1:
                    published = True
                    publish()
                if frames >= n_frames:
                    return
        await asyncio.wait_for(_collect(), timeout=timeout)
    finally:
        await it.aclose()
    return buf


class TurnStreamTests(unittest.TestCase):

    def setUp(self):
        bridge_app._dashboard_recent_turns.clear()
        self.addCleanup(bridge_app._dashboard_recent_turns.clear)
        saved = dict(dash._state)
        self.addCleanup(lambda: dash._state.update(saved))
        dash._state.update(
            recent_turns_getter=bridge_app._dashboard_recent_turns_getter,
            subscribe_events=bridge_app._dashboard_subscribe_events,
            unsubscribe_events=bridge_app._dashboard_unsubscribe_events,
        )

    def _publish(self, text: str, error: str | None = None) -> None:
        bridge_app._dashboard_publish_turn({
            "ts": 1.0, "channel": "dotty", "request_text": text,
            "response_text": "😊 ok", "latency_ms": 10, "error": error,
        })

    def test_turn_ids_are_strictly_increasing(self):
        for i in range(5):
            self._publish(f"t{i}")
        ids = [t["id"] for t in bridge_app._dashboard_recent_turns_getter()]
        self.assertEqual(ids, sorted(set(ids)))

    def test_fresh_page_gets_recent_history_flagged_as_replay(self):
        self._publish("first")
        self._publish("second", error="pi rpc timeout")

        async def go():
            resp = await dash.events_stream(_req())
            return _frames(await _read(resp, 2))

        frames = asyncio.run(go())
        self.assertEqual([f["data"]["request_text"] for f in frames], ["first", "second"])
        self.assertTrue(all(f["data"]["replay"] for f in frames))
        self.assertEqual([int(f["id"]) for f in frames],
                         [t["id"] for t in bridge_app._dashboard_recent_turns_getter()])

    def test_replay_is_capped(self):
        for i in range(dash._EVENTS_REPLAY_MAX + 5):
            self._publish(f"t{i}")

        async def go():
            resp = await dash.events_stream(_req())
            return _frames(await _read(resp, dash._EVENTS_REPLAY_MAX))

        frames = asyncio.run(go())
        self.assertEqual(len(frames), dash._EVENTS_REPLAY_MAX)
        self.assertEqual(frames[-1]["data"]["request_text"],
                         f"t{dash._EVENTS_REPLAY_MAX + 4}")

    def test_reconnect_resumes_after_last_event_id(self):
        self._publish("seen")
        seen_id = bridge_app._dashboard_recent_turns_getter()[-1]["id"]
        self._publish("missed")

        async def go():
            resp = await dash.events_stream(_req(str(seen_id)))
            return _frames(await _read(resp, 1))

        frames = asyncio.run(go())
        self.assertEqual([f["data"]["request_text"] for f in frames], ["missed"])

    def test_live_turn_follows_replay_without_flag_or_duplicate(self):
        self._publish("old")

        async def go():
            resp = await dash.events_stream(_req())
            raw = await _read(resp, 2, publish=lambda: self._publish("live", error="boom"))
            return _frames(raw)

        frames = asyncio.run(go())
        self.assertEqual([f["data"]["request_text"] for f in frames], ["old", "live"])
        self.assertTrue(frames[0]["data"]["replay"])
        self.assertNotIn("replay", frames[1]["data"])
        self.assertEqual(frames[1]["data"]["error"], "boom")

    def test_page_skips_toast_for_replayed_turns(self):
        html = (_repo_root / "bridge/templates/dashboard.html").read_text()
        self.assertIn("if (data && data.replay) return;", html)


if __name__ == "__main__":
    unittest.main()
