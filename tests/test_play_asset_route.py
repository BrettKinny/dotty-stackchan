"""Unit tests for SimpleHttpServer._dotty_play_asset (http_server.py).

All xiaozhi-server core.* imports are mocked via sys.modules injection so
these tests run without a container.  7 cases cover the main code paths in
the route handler; decode/stream logic is integration-tested separately
via the full bridge smoke test.
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ── Mock every core.* / config.* module the server imports at load time ──────
_mock_active: dict = {}
_portal_mod = MagicMock()
_portal_mod.active_connections = _mock_active

_logger_mod = MagicMock()
_logger_mod.setup_logging.return_value = MagicMock()

for _n in (
    "config",
    "config.logger",
    "core",
    "core.api",
    "core.api.ota_handler",
    "core.api.vision_handler",
    "core.portal_bridge",
):
    sys.modules.setdefault(_n, MagicMock())

sys.modules["core.portal_bridge"] = _portal_mod
sys.modules["config.logger"] = _logger_mod

# ── Load module under test via importlib (not on the normal package path) ─────
import importlib.util as _ilu

_SERVER_PY = (
    pathlib.Path(__file__).parent.parent
    / "custom-providers"
    / "xiaozhi-patches"
    / "http_server.py"
)
_spec = _ilu.spec_from_file_location("http_server_under_test", _SERVER_PY)
_mod = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]
SimpleHttpServer = _mod.SimpleHttpServer


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_server() -> object:
    return SimpleHttpServer({"server": {"ip": "0.0.0.0", "http_port": 8003}})


class _FakeRequest:
    """Minimal stand-in for aiohttp.web.Request."""

    def __init__(self, data=None, raise_exc=None):
        self._data = data
        self._raise = raise_exc

    async def json(self):
        if self._raise:
            raise self._raise
        return self._data


def _run(coro):
    return asyncio.run(coro)


def _body(resp) -> dict:
    return json.loads(resp.body)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestPlayAssetRoute(unittest.TestCase):

    def setUp(self):
        _mock_active.clear()
        self.srv = _make_server()

    # Validation ──────────────────────────────────────────────────────────────

    def test_invalid_json_returns_400(self):
        req = _FakeRequest(raise_exc=ValueError("bad"))
        resp = _run(self.srv._dotty_play_asset(req))
        self.assertEqual(resp.status, 400)
        self.assertIn("invalid JSON", _body(resp)["error"])

    def test_missing_asset_field_returns_400(self):
        req = _FakeRequest(data={"device_id": "dev1"})
        resp = _run(self.srv._dotty_play_asset(req))
        self.assertEqual(resp.status, 400)
        self.assertIn("asset", _body(resp)["error"])

    def test_file_not_found_returns_404(self):
        req = _FakeRequest(data={"asset": "/does/not/exist/purr.opus"})
        resp = _run(self.srv._dotty_play_asset(req))
        self.assertEqual(resp.status, 404)

    def test_no_device_returns_503(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            req = _FakeRequest(data={"asset": path})
            resp = _run(self.srv._dotty_play_asset(req))
            self.assertEqual(resp.status, 503)
            self.assertIn("known", _body(resp))
        finally:
            os.unlink(path)

    # Happy-path ───────────────────────────────────────────────────────────────

    def test_valid_request_fires_task_and_returns_200(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        conn = MagicMock()
        conn.headers = {"device-id": "dev1"}
        conn.sample_rate = 16000
        conn.session_id = "sess-abc"
        conn.client_abort = False
        conn.is_exiting = False
        conn.client_is_speaking = False
        conn.websocket.send = AsyncMock()
        _mock_active["dev1"] = conn
        try:
            with patch("asyncio.create_task") as mock_ct:
                mock_ct.side_effect = lambda coro: (coro.close() or MagicMock())
                req = _FakeRequest(data={"asset": path, "device_id": "dev1"})
                resp = _run(self.srv._dotty_play_asset(req))
            self.assertEqual(resp.status, 200)
            body = _body(resp)
            self.assertTrue(body["ok"])
            self.assertEqual(body["device_id"], "dev1")
            self.assertEqual(body["asset"], path)
            mock_ct.assert_called_once()
        finally:
            os.unlink(path)
            _mock_active.clear()

    def test_named_device_id_selects_correct_connection(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        conn_a = MagicMock()
        conn_a.headers = {"device-id": "dev-a"}
        conn_b = MagicMock()
        conn_b.headers = {"device-id": "dev-b"}
        _mock_active["dev-a"] = conn_a
        _mock_active["dev-b"] = conn_b
        try:
            with patch("asyncio.create_task") as mock_ct:
                mock_ct.side_effect = lambda coro: (coro.close() or MagicMock())
                req = _FakeRequest(data={"asset": path, "device_id": "dev-b"})
                resp = _run(self.srv._dotty_play_asset(req))
            self.assertEqual(resp.status, 200)
            self.assertEqual(_body(resp)["device_id"], "dev-b")
        finally:
            os.unlink(path)
            _mock_active.clear()

    def test_unknown_device_id_returns_503(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        conn = MagicMock()
        _mock_active["dev-real"] = conn
        try:
            req = _FakeRequest(data={"asset": path, "device_id": "dev-ghost"})
            resp = _run(self.srv._dotty_play_asset(req))
            self.assertEqual(resp.status, 503)
        finally:
            os.unlink(path)
            _mock_active.clear()


if __name__ == "__main__":
    unittest.main()
