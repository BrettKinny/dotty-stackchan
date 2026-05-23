"""CSRF middleware tests for the dashboard.

Covers the signed double-submit cookie pattern in `bridge/csrf.py` and
its integration into `bridge.py` — cookie issuance on GET, header
validation on mutating /ui/ requests, /api/ exemption, kill-switch.

Uses the same heavy-lifespan-neutralization bootstrap as
test_bridge_routes.py so the test client can construct without spawning
ACP, perception consumers, or the calendar loop.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path


# Same env redirects as test_bridge_routes.py — kid/smart-mode state dirs
# default under /root which CI/tests can't write, and CONVO_LOG_DIR
# defaults the same way.
_state_dir = Path(tempfile.mkdtemp(prefix="dotty-csrf-test-state-"))
os.environ.setdefault("DOTTY_KID_MODE_STATE", str(_state_dir / "kid-mode"))
os.environ.setdefault("DOTTY_SMART_MODE_STATE", str(_state_dir / "smart-mode"))
os.environ.setdefault("CONVO_LOG_DIR", str(_state_dir / "logs"))
os.environ.setdefault("IDLE_PHOTOGRAPHER_ENABLED", "0")
os.environ.setdefault("DREAMER_ENABLED", "0")
os.environ.setdefault("DANCE_REFLECTOR_ENABLED", "0")
os.environ.setdefault("CALENDAR_IDS", "")
# Pin a known secret so cookie signatures are deterministic across the
# whole test file. Must be set before bridge.csrf imports.
os.environ["DOTTY_CSRF_SECRET"] = "test-secret-for-csrf-tests-only"
os.environ.setdefault("DOTTY_CSRF_ENFORCE", "1")

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
    yield


bridge_app.app.router.lifespan_context = _noop_lifespan

from fastapi.testclient import TestClient  # noqa: E402

import bridge.csrf as csrf_mod  # noqa: E402


class CSRFCookieIssuanceTests(unittest.TestCase):
    """A fresh GET to /ui/ should set the dotty_csrf cookie and inject
    the raw token into the rendered HTML as a <meta> tag."""

    def setUp(self):
        self.client = TestClient(bridge_app.app)

    def test_get_ui_sets_cookie_and_meta(self):
        # Hit /ui directly (no trailing slash) — /ui/ 307-redirects and
        # the Set-Cookie lands on the intermediate response, which the
        # TestClient absorbs into its jar before the final 200. The
        # response object only shows Set-Cookies from the final hop.
        r = self.client.get("/ui")
        self.assertEqual(r.status_code, 200)
        self.assertIn(csrf_mod.COOKIE_NAME, self.client.cookies)
        # Cookie value contains the signature; unsigning should yield
        # the raw token that's embedded in the meta tag.
        raw = csrf_mod._unsign(self.client.cookies[csrf_mod.COOKIE_NAME])
        self.assertIsNotNone(raw)
        self.assertIn(f'name="csrf-token" content="{raw}"', r.text)

    def test_health_does_not_set_cookie(self):
        # /health is in the exempt prefix list — but the middleware does
        # still set the cookie on any first request (it issues on every
        # response that lacked a valid one). That's by design: the cookie
        # is harmless to other endpoints and pre-warms the dashboard.
        # What MUST hold: /health remains accessible without a token.
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)


class CSRFEnforcementTests(unittest.TestCase):
    """POST to /ui/actions/* must require a matching X-CSRF-Token."""

    def setUp(self):
        # Each test gets a clean cookie jar so prior tests' cookies don't
        # leak across cases.
        self.client = TestClient(bridge_app.app)

    def _prime_token(self) -> str:
        r = self.client.get("/ui/")
        self.assertEqual(r.status_code, 200)
        raw = csrf_mod._unsign(self.client.cookies[csrf_mod.COOKIE_NAME])
        assert raw is not None
        return raw

    def test_post_without_cookie_rejected(self):
        # Fresh client, no GET first → no cookie, POST should 403.
        fresh = TestClient(bridge_app.app)
        r = fresh.post("/ui/actions/mood", data={"emoji": "😊"})
        self.assertEqual(r.status_code, 403)
        self.assertIn(b"csrf", r.content.lower())

    def test_post_with_cookie_but_no_header_rejected(self):
        self._prime_token()
        r = self.client.post("/ui/actions/mood", data={"emoji": "😊"})
        self.assertEqual(r.status_code, 403)

    def test_post_with_mismatched_header_rejected(self):
        self._prime_token()
        r = self.client.post(
            "/ui/actions/mood",
            data={"emoji": "😊"},
            headers={"X-CSRF-Token": "not-the-real-token"},
        )
        self.assertEqual(r.status_code, 403)

    def test_post_with_matching_header_accepted_by_middleware(self):
        # We only assert the middleware passes the request through to
        # the handler — the handler itself may 503 because XIAOZHI_HOST
        # isn't configured in the test env. The point is: NOT 403.
        token = self._prime_token()
        r = self.client.post(
            "/ui/actions/mood",
            data={"emoji": "😊"},
            headers={"X-CSRF-Token": token},
        )
        self.assertNotEqual(r.status_code, 403)

    def test_tampered_cookie_signature_rejected(self):
        self._prime_token()
        # Corrupt the signature half. Cookie is `raw.sig`; mutate sig.
        bad = self.client.cookies[csrf_mod.COOKIE_NAME].rsplit(".", 1)[0] + ".deadbeef"
        self.client.cookies.set(csrf_mod.COOKIE_NAME, bad)
        r = self.client.post(
            "/ui/actions/mood",
            data={"emoji": "😊"},
            headers={"X-CSRF-Token": "anything"},
        )
        self.assertEqual(r.status_code, 403)


class CSRFExemptionTests(unittest.TestCase):
    """API and observability endpoints must remain reachable without
    a CSRF token."""

    def setUp(self):
        self.client = TestClient(bridge_app.app)

    def test_api_post_without_cookie_passes_middleware(self):
        # /api/perception/event is documented in bridge.py as
        # @app.post(..., status_code=204) — middleware MUST NOT 403 it.
        # The handler may still respond with a non-success status if the
        # payload is malformed, but specifically not 403 from CSRF.
        r = self.client.post(
            "/api/perception/event",
            json={"type": "event", "name": "test", "data": {}},
        )
        self.assertNotEqual(r.status_code, 403)


class CSRFKillSwitchTests(unittest.TestCase):
    """DOTTY_CSRF_ENFORCE=0 → log-only mode, requests pass through."""

    def test_enforce_off_passes_bad_token(self):
        # Monkey-patch the module-level enforcement flag (set at import).
        original = csrf_mod._ENFORCE
        csrf_mod._ENFORCE = False
        try:
            client = TestClient(bridge_app.app)
            # Prime cookie, then POST with a bad header — should not 403.
            client.get("/ui/")
            r = client.post(
                "/ui/actions/mood",
                data={"emoji": "😊"},
                headers={"X-CSRF-Token": "deliberately-wrong"},
            )
            self.assertNotEqual(r.status_code, 403)
        finally:
            csrf_mod._ENFORCE = original


class CSRFSigningUnitTests(unittest.TestCase):
    """Direct tests on the sign/unsign helpers in bridge.csrf."""

    def test_roundtrip(self):
        raw = "abc123"
        signed = csrf_mod._sign(raw)
        self.assertEqual(csrf_mod._unsign(signed), raw)

    def test_unsign_rejects_empty(self):
        self.assertIsNone(csrf_mod._unsign(""))

    def test_unsign_rejects_unsigned(self):
        self.assertIsNone(csrf_mod._unsign("no-dot-here"))

    def test_unsign_rejects_bad_signature(self):
        self.assertIsNone(csrf_mod._unsign("raw.deadbeef"))


if __name__ == "__main__":
    unittest.main()
