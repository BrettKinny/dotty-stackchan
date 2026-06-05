"""Tests for the bridge's X-Admin-Token wiring on /xiaozhi/admin/* calls.

Admin-auth epic (bridge caller). bridge.py and bridge.dashboard each expose a
`_xiaozhi_admin_headers()` helper that returns the X-Admin-Token header when
DOTTY_ADMIN_TOKEN is set (read into a module-level `_ADMIN_TOKEN`) and an empty
dict otherwise. The helper is threaded through every admin call site.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_state_dir = Path(tempfile.mkdtemp(prefix="dotty-adminhdr-state-"))
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


@dataclass
class _Resp:
    status_code: int = 200
    text: str = ""

    def json(self) -> dict:
        return {}


class _PostRecorder:
    def __init__(self) -> None:
        self.headers: dict[str, str] | None = None

    def __call__(self, url: str, *, json: Any = None, headers: Any = None,
                 timeout: float = 0) -> _Resp:
        self.headers = headers
        return _Resp()


class BridgeAdminHeaderTests(unittest.TestCase):

    def setUp(self):
        self._tok = bridge_app._ADMIN_TOKEN
        self._host = bridge_app._XIAOZHI_HOST
        self._post = bridge_app.requests.post

    def tearDown(self):
        bridge_app._ADMIN_TOKEN = self._tok
        bridge_app._XIAOZHI_HOST = self._host
        bridge_app.requests.post = self._post

    def test_helper_returns_header_when_set(self):
        bridge_app._ADMIN_TOKEN = "tok"
        self.assertEqual(bridge_app._xiaozhi_admin_headers(), {"X-Admin-Token": "tok"})

    def test_helper_empty_when_unset(self):
        bridge_app._ADMIN_TOKEN = ""
        self.assertEqual(bridge_app._xiaozhi_admin_headers(), {})

    def test_dispatch_abort_sends_header_when_set(self):
        bridge_app._ADMIN_TOKEN = "tok"
        bridge_app._XIAOZHI_HOST = "127.0.0.1"
        rec = _PostRecorder()
        bridge_app.requests.post = rec
        asyncio.run(bridge_app._dispatch_abort("dev-1"))
        self.assertEqual(rec.headers, {"X-Admin-Token": "tok"})

    def test_dispatch_abort_no_header_when_unset(self):
        bridge_app._ADMIN_TOKEN = ""
        bridge_app._XIAOZHI_HOST = "127.0.0.1"
        rec = _PostRecorder()
        bridge_app.requests.post = rec
        asyncio.run(bridge_app._dispatch_abort("dev-1"))
        self.assertEqual(rec.headers, {})


class DashboardAdminHeaderTests(unittest.TestCase):

    def setUp(self):
        self._tok = dash._ADMIN_TOKEN

    def tearDown(self):
        dash._ADMIN_TOKEN = self._tok

    def test_helper_returns_header_when_set(self):
        dash._ADMIN_TOKEN = "tok"
        self.assertEqual(dash._xiaozhi_admin_headers(), {"X-Admin-Token": "tok"})

    def test_helper_empty_when_unset(self):
        dash._ADMIN_TOKEN = ""
        self.assertEqual(dash._xiaozhi_admin_headers(), {})


if __name__ == "__main__":
    unittest.main()
