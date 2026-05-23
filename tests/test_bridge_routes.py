"""Boundary tests for bridge.py's FastAPI routes.

Post-#111 surface: bridge.py is the dashboard host. Its voice + perception
endpoints were ripped in #113; the only HTTP boundary left worth testing
at the bridge level is `/health` (the dashboard's /ui/* router is covered
by tests/test_dashboard_csrf.py).

Import wiring:
  - bridge.py is the FastAPI app; the `bridge` package also exists
    (bridge/__init__.py for submodules), so `import bridge` resolves
    to the package. We load bridge.py explicitly via importlib under
    the module name `bridge_app` to avoid the collision.
  - The slim post-#111 app no longer spawns the ACP subprocess /
    perception consumers / calendar poll, so the heavy lifespan
    neutralisation that earlier revisions of this file performed is
    no longer required. A no-op lifespan is still installed for
    defence-in-depth.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path


# ---------------------------------------------------------------------------
# Import bridge.py as `bridge_app`.
# ---------------------------------------------------------------------------

# State files (kid-mode + smart-mode) default to /root/zeroclaw-bridge/state/...
# which the CI runner can neither read (/root is 700) nor write. Redirect both
# to a writable temp dir before import.
_state_dir = Path(tempfile.mkdtemp(prefix="dotty-bridge-test-state-"))
os.environ.setdefault("DOTTY_KID_MODE_STATE", str(_state_dir / "kid-mode"))
os.environ.setdefault("DOTTY_SMART_MODE_STATE", str(_state_dir / "smart-mode"))

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
    """No-op lifespan. The post-#111 bridge has no background spawn,
    but keep this in place so future additions can't sneak network /
    subprocess work into a unit-test import path."""
    yield


bridge_app.app.router.lifespan_context = _noop_lifespan


from fastapi.testclient import TestClient  # noqa: E402
client = TestClient(bridge_app.app)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

class HealthTests(unittest.TestCase):
    """The post-#111 /health is a minimal liveness probe — `{status, service}`.
    The ACP / session fields the pre-#36 surface carried are gone with the
    rest of the ZeroClaw path."""

    def test_returns_ok_status(self):
        r = client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_reports_service_name(self):
        body = client.get("/health").json()
        self.assertEqual(body["service"], "dotty-bridge")

    def test_no_legacy_acp_fields(self):
        """Regression guard: if someone re-adds ACP-shaped fields here,
        the dashboard contract has drifted — investigate before relaxing
        this test."""
        body = client.get("/health").json()
        for legacy_key in ("acp_running", "cached_session", "session_turns"):
            self.assertNotIn(legacy_key, body)


if __name__ == "__main__":
    unittest.main()
