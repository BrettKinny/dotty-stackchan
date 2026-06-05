"""Tests for the kid-mode content filter on the dashboard say/start-story ingress.

Audit finding: `/actions/say` and `/actions/start-story` sanitised control
chars + length but never called `content_filter()`, so an operator (or any LAN
client when dashboard auth is unset) could make Dotty speak arbitrary unfiltered
text while kid-mode was on, and those turns never populated the safety ring.

Handlers are invoked directly (not via TestClient) so the CSRF middleware isn't
in the path — `content_filter` and `_inject_or_error` are stubbed so the wiring
is exercised deterministically without the real blocklist or a live xiaozhi.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse

# Redirect kid/smart state to a writable temp dir before importing bridge.
_state_dir = Path(tempfile.mkdtemp(prefix="dotty-sayfilter-state-"))
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
import bridge.text as btext  # noqa: E402


def _req() -> Request:
    return Request({
        "type": "http", "method": "POST", "path": "/ui/actions/say",
        "headers": [], "query_string": b"",
    })


def _body(resp) -> str:
    return resp.body.decode("utf-8")


async def _fake_inject(request, text, label):
    return HTMLResponse(f"INJECTED::{text}")


class SayStartStoryKidFilterTests(unittest.TestCase):

    def setUp(self):
        self._saved = (
            dash._state.get("kid_mode_getter"),
            dash._state.get("state_setter"),
            dash._state.get("state_getter"),
            btext.content_filter,
            dash._inject_or_error,
        )
        # Deterministic stub blocklist + injector.
        btext.content_filter = lambda t: "SAFE-REPLACEMENT" if "BLOCKME" in t else None
        dash._inject_or_error = _fake_inject

    def tearDown(self):
        (dash._state["kid_mode_getter"], dash._state["state_setter"],
         dash._state["state_getter"], btext.content_filter,
         dash._inject_or_error) = self._saved

    def _kid(self, on: bool):
        dash._state["kid_mode_getter"] = (lambda: on)

    # ── _kid_blocked helper ──────────────────────────────────────────────────

    def test_kid_blocked_helper(self):
        self._kid(True)
        self.assertTrue(dash._kid_blocked("x BLOCKME"))
        self.assertFalse(dash._kid_blocked("clean text"))
        self._kid(False)
        self.assertFalse(dash._kid_blocked("x BLOCKME"))
        dash._state["kid_mode_getter"] = None  # unconfigured → fail safe ON
        self.assertTrue(dash._kid_blocked("x BLOCKME"))

    # ── say ──────────────────────────────────────────────────────────────────

    def test_say_blocked_when_kid_on(self):
        self._kid(True)
        b = _body(asyncio.run(dash.say(_req(), text="please BLOCKME")))
        self.assertIn("kid-mode content filter", b)
        self.assertNotIn("INJECTED::", b)

    def test_say_injects_when_clean(self):
        self._kid(True)
        b = _body(asyncio.run(dash.say(_req(), text="hello friend")))
        self.assertIn("INJECTED::hello friend", b)

    def test_say_bypasses_filter_when_kid_off(self):
        self._kid(False)
        b = _body(asyncio.run(dash.say(_req(), text="BLOCKME anyway")))
        self.assertIn("INJECTED::", b)
        self.assertNotIn("kid-mode content filter", b)

    # ── start-story ──────────────────────────────────────────────────────────

    def test_start_story_blocked_does_not_flip_state(self):
        self._kid(True)
        flips: list[str] = []

        async def _setter(s):
            flips.append(s)

        dash._state["state_setter"] = _setter
        dash._state["state_getter"] = (lambda: "idle")
        b = _body(asyncio.run(dash.start_story(_req(), text="a tale of BLOCKME")))
        self.assertIn("kid-mode content filter", b)
        self.assertEqual(flips, [])  # never entered story_time

    def test_start_story_clean_flips_and_injects(self):
        self._kid(True)
        flips: list[str] = []

        async def _setter(s):
            flips.append(s)

        dash._state["state_setter"] = _setter
        dash._state["state_getter"] = (lambda: "idle")
        b = _body(asyncio.run(dash.start_story(_req(), text="a happy puppy")))
        self.assertIn("INJECTED::", b)
        self.assertIn("story_time", flips)


if __name__ == "__main__":
    unittest.main()
