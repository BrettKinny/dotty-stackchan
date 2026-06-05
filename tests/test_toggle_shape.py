"""Tests for the kid/smart-mode toggle result shape.

Audit finding: the dashboard kid/smart-mode setters persisted + hot-applied the
flip, then returned {ok:False} if the firmware didn't acknowledge — so the UI
showed "toggle failed" and the operator re-toggled into a state ping-pong, even
though the bridge state was already correct.

Fix: setters return {ok:True, device_pushed:<bool>, warning:<str|None>}
(mirroring /admin/kid-mode), and the dashboard renders a non-error success with
a stale-LED note when device_pushed is False.
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
from unittest.mock import AsyncMock

from starlette.requests import Request

_state_dir = Path(tempfile.mkdtemp(prefix="dotty-toggle-state-"))
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


def _req() -> Request:
    return Request({
        "type": "http", "method": "POST", "path": "/ui/actions/kid-mode",
        "headers": [], "query_string": b"",
    })


def _body(resp) -> str:
    return resp.body.decode("utf-8")


class SetterShapeTests(unittest.TestCase):
    """bridge.py setters: ok:True regardless of firmware ack; device_pushed
    reflects the ack; warning only present when the push failed."""

    def setUp(self):
        self._orig = bridge_app._dispatch_set_toggle

    def tearDown(self):
        bridge_app._dispatch_set_toggle = self._orig

    def test_kid_mode_no_ack_is_ok_true_device_pushed_false(self):
        bridge_app._dispatch_set_toggle = AsyncMock(return_value=False)
        res = asyncio.run(bridge_app._dashboard_set_kid_mode(True))
        self.assertTrue(res["ok"])
        self.assertFalse(res["device_pushed"])
        self.assertTrue(res["warning"])

    def test_kid_mode_ack_is_clean(self):
        bridge_app._dispatch_set_toggle = AsyncMock(return_value=True)
        res = asyncio.run(bridge_app._dashboard_set_kid_mode(False))
        self.assertTrue(res["ok"])
        self.assertTrue(res["device_pushed"])
        self.assertIsNone(res["warning"])

    def test_smart_mode_no_ack_is_ok_true_device_pushed_false(self):
        bridge_app._dispatch_set_toggle = AsyncMock(return_value=False)
        res = asyncio.run(bridge_app._dashboard_set_smart_mode(True))
        self.assertTrue(res["ok"])
        self.assertFalse(res["device_pushed"])
        self.assertTrue(res["warning"])

    def test_smart_mode_ack_is_clean(self):
        bridge_app._dispatch_set_toggle = AsyncMock(return_value=True)
        res = asyncio.run(bridge_app._dashboard_set_smart_mode(True))
        self.assertTrue(res["ok"])
        self.assertTrue(res["device_pushed"])
        self.assertIsNone(res["warning"])


class RouteRenderTests(unittest.TestCase):
    """dashboard routes: a no-ack flip renders the success alert + stale-LED
    note, NOT the 'toggle failed' error."""

    def setUp(self):
        self._kid = dash._state.get("kid_mode_setter")
        self._smart = dash._state.get("smart_mode_setter")

    def tearDown(self):
        dash._state["kid_mode_setter"] = self._kid
        dash._state["smart_mode_setter"] = self._smart

    def test_kid_route_no_ack_renders_success_with_warning(self):
        dash._state["kid_mode_setter"] = AsyncMock(
            return_value={"ok": True, "device_pushed": False, "warning": "stale"}
        )
        b = _body(asyncio.run(dash.kid_mode_set(_req(), enabled="on")))
        self.assertIn("Kid Mode", b)
        self.assertNotIn("toggle failed", b)
        self.assertIn("LED may be stale", b)

    def test_kid_route_ack_renders_success_without_warning(self):
        dash._state["kid_mode_setter"] = AsyncMock(
            return_value={"ok": True, "device_pushed": True, "warning": None}
        )
        b = _body(asyncio.run(dash.kid_mode_set(_req(), enabled="off")))
        self.assertIn("Kid Mode", b)
        self.assertNotIn("LED may be stale", b)

    def test_smart_route_no_ack_renders_success_with_warning(self):
        dash._state["smart_mode_setter"] = AsyncMock(
            return_value={"ok": True, "device_pushed": False, "warning": "stale"}
        )
        b = _body(asyncio.run(dash.smart_mode_set(_req(), enabled="on")))
        self.assertIn("Smart Mode", b)
        self.assertNotIn("toggle failed", b)
        self.assertIn("LED may be stale", b)


if __name__ == "__main__":
    unittest.main()
