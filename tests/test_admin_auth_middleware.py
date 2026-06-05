"""Unit tests for the /xiaozhi/admin/* auth middleware (http_server.py).

The middleware gates the admin surface behind X-Admin-Token when
DOTTY_ADMIN_TOKEN is set, and is a transparent no-op when it isn't (so the
patch is backward-compatible until a token is provisioned across all callers).
core.* imports are stubbed so the module loads without a container; aiohttp is
real.
"""
import asyncio
import importlib.util as _ilu
import os
import pathlib
import sys
import unittest
from unittest.mock import MagicMock

for _n in (
    "config", "config.logger", "core", "core.api", "core.api.ota_handler",
    "core.api.vision_handler", "core.portal_bridge",
):
    sys.modules.setdefault(_n, MagicMock())
sys.modules["config.logger"].setup_logging = lambda: MagicMock()  # type: ignore[attr-defined]
sys.modules["core.portal_bridge"].active_connections = {}  # type: ignore[attr-defined]

_SERVER_PY = (
    pathlib.Path(__file__).parent.parent
    / "custom-providers" / "xiaozhi-patches" / "http_server.py"
)
_spec = _ilu.spec_from_file_location("http_server_auth_under_test", _SERVER_PY)
_mod = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

_mw = _mod._dotty_admin_auth_middleware
_SENTINEL = object()


async def _pass_handler(request):
    return _SENTINEL


class _Req:
    def __init__(self, path, token_header=None):
        self.path = path
        self.headers = {} if token_header is None else {"X-Admin-Token": token_header}


def _run(req):
    return asyncio.run(_mw(req, _pass_handler))


class AdminAuthMiddlewareTests(unittest.TestCase):

    def setUp(self):
        self._prev = os.environ.get("DOTTY_ADMIN_TOKEN")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("DOTTY_ADMIN_TOKEN", None)
        else:
            os.environ["DOTTY_ADMIN_TOKEN"] = self._prev

    # ── token unset → enforcement off (backward compatible) ──────────────────

    def test_unset_token_passes_admin_route(self):
        os.environ.pop("DOTTY_ADMIN_TOKEN", None)
        self.assertIs(_run(_Req("/xiaozhi/admin/say")), _SENTINEL)

    def test_blank_token_passes_admin_route(self):
        os.environ["DOTTY_ADMIN_TOKEN"] = "   "
        self.assertIs(_run(_Req("/xiaozhi/admin/say")), _SENTINEL)

    # ── token set → enforced on admin routes ─────────────────────────────────

    def test_set_token_correct_header_passes(self):
        os.environ["DOTTY_ADMIN_TOKEN"] = "s3cret"
        self.assertIs(_run(_Req("/xiaozhi/admin/say", token_header="s3cret")), _SENTINEL)

    def test_set_token_missing_header_401(self):
        os.environ["DOTTY_ADMIN_TOKEN"] = "s3cret"
        resp = _run(_Req("/xiaozhi/admin/inject-text"))
        self.assertEqual(resp.status, 401)

    def test_set_token_wrong_header_401(self):
        os.environ["DOTTY_ADMIN_TOKEN"] = "s3cret"
        resp = _run(_Req("/xiaozhi/admin/set-state", token_header="nope"))
        self.assertEqual(resp.status, 401)

    # ── non-admin paths are never gated ──────────────────────────────────────

    def test_set_token_does_not_gate_ota(self):
        os.environ["DOTTY_ADMIN_TOKEN"] = "s3cret"
        self.assertIs(_run(_Req("/xiaozhi/ota/")), _SENTINEL)

    def test_set_token_does_not_gate_vision(self):
        os.environ["DOTTY_ADMIN_TOKEN"] = "s3cret"
        self.assertIs(_run(_Req("/mcp/vision/explain")), _SENTINEL)


if __name__ == "__main__":
    unittest.main()
