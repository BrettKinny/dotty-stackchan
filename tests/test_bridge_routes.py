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
# Dashboard render fixes (#128) — dead-card removal + honest smart-mode card
# ---------------------------------------------------------------------------


class DashboardRenderFixTests(unittest.TestCase):
    """Guards the #128 'busted render' repairs against regression.

    The zeroclaw-discord daemon card was removed (ZeroClaw retired in #36;
    the local `systemctl` query could never work inside the Unraid
    container), so its partial route must now 404. The Smart Mode card was
    relabelled to be honest about the live PiVoiceLLM path, which performs
    no model swap (Tier1Slim — the only provider that hot-swapped — was
    removed in the 2026-05-29 alignment pass; the swap is v2 scope). The
    card must render and say so rather than naming a defunct model.
    """

    def test_discord_partial_removed(self):
        # The dead /ui/discord partial route was deleted in #128.
        self.assertEqual(client.get("/ui/discord").status_code, 404)

    def test_smart_mode_partial_renders(self):
        # Route survives and renders its card chrome even with no
        # smart_mode_getter wired (available=False path).
        r = client.get("/ui/smart-mode")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Smart Mode", r.text)

    def test_smart_mode_card_is_honest_about_no_swap(self):
        # On the live PiVoiceLLM path there is no model swap, so the card
        # must describe the pending-v2 state, not name a defunct model.
        body = client.get("/ui/smart-mode").text
        self.assertIn("model-swap pending", body)


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


# ---------------------------------------------------------------------------
# Perception getters — rewired to dotty-behaviour in #115 Tile 1
# ---------------------------------------------------------------------------


class PerceptionGetterTests(unittest.TestCase):
    """Smoke tests for the dotty-behaviour-backed perception getters.

    These getters are called from the dashboard's template tiles (in
    sync render contexts), so they must:
      - degrade to the empty fallback on timeout / connection error
      - cache results for ~2s so HTMX poll fan-out doesn't hammer
        dotty-behaviour
    """

    def setUp(self) -> None:
        # Reset the per-process cache between tests so fixtures don't
        # leak across cases.
        bridge_app._dotty_behaviour_cache.clear()

    def _patch_get(self, response):
        """Monkeypatch requests.get on the bridge module to return
        ``response`` (a stub object with raise_for_status/json or an
        exception to raise)."""
        original = bridge_app.requests.get

        def fake_get(*args, **kwargs):
            if isinstance(response, Exception):
                raise response
            return response

        bridge_app.requests.get = fake_get
        self.addCleanup(lambda: setattr(bridge_app.requests, "get", original))

    def test_state_getter_returns_fetched_dict(self):
        class FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"dev-1": {"face_present": True}}

        self._patch_get(FakeResp())
        result = bridge_app._dashboard_perception_state_getter()
        self.assertEqual(result, {"dev-1": {"face_present": True}})

    def test_recent_getter_returns_fetched_list(self):
        class FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return [{"ts": 1.0, "name": "face_detected", "data": {}}]

        self._patch_get(FakeResp())
        result = bridge_app._dashboard_perception_recent_getter(
            "dev-1", limit=10
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "face_detected")

    def test_state_getter_degrades_on_timeout(self):
        import requests as _requests

        self._patch_get(_requests.exceptions.Timeout("simulated"))
        result = bridge_app._dashboard_perception_state_getter()
        self.assertEqual(result, {})

    def test_recent_getter_degrades_on_connection_error(self):
        import requests as _requests

        self._patch_get(_requests.exceptions.ConnectionError("simulated"))
        result = bridge_app._dashboard_perception_recent_getter("dev-1")
        self.assertEqual(result, [])

    def test_state_getter_caches_within_ttl(self):
        """A second call within the cache TTL should NOT hit requests.get."""
        calls = {"n": 0}

        class FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"dev-x": {"foo": 1}}

        original = bridge_app.requests.get

        def fake_get(*args, **kwargs):
            calls["n"] += 1
            return FakeResp()

        bridge_app.requests.get = fake_get
        self.addCleanup(lambda: setattr(bridge_app.requests, "get", original))

        bridge_app._dashboard_perception_state_getter()
        bridge_app._dashboard_perception_state_getter()
        bridge_app._dashboard_perception_state_getter()
        self.assertEqual(calls["n"], 1, "expected cache hit on repeat calls")


# ---------------------------------------------------------------------------
# Audio + scene-synthesis cache getters — rewired in #115 Tiles 3 + 4
# ---------------------------------------------------------------------------


class AudioAndSceneGetterTests(unittest.TestCase):
    """Smoke tests for the dotty-behaviour-backed audio_cache and
    scene_synthesis_cache getters. Same contract as the perception
    getters above — degrade to ``{}`` on failure, pass-through on
    success, share the 2 s _dotty_behaviour_cache TTL."""

    def setUp(self) -> None:
        bridge_app._dotty_behaviour_cache.clear()

    def _patch_get(self, response):
        original = bridge_app.requests.get

        def fake_get(*args, **kwargs):
            if isinstance(response, Exception):
                raise response
            return response

        bridge_app.requests.get = fake_get
        self.addCleanup(lambda: setattr(bridge_app.requests, "get", original))

    def test_audio_cache_getter_returns_fetched_dict(self):
        class FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "dev-1": {
                        "description": "Quiet hum.",
                        "wall_ts": 1700000000.0,
                        "source": "audio_explain",
                    }
                }

        self._patch_get(FakeResp())
        result = bridge_app._dashboard_audio_cache_getter()
        self.assertEqual(result["dev-1"]["description"], "Quiet hum.")
        self.assertEqual(result["dev-1"]["source"], "audio_explain")

    def test_audio_cache_getter_degrades_on_connection_error(self):
        import requests as _requests

        self._patch_get(_requests.exceptions.ConnectionError("simulated"))
        result = bridge_app._dashboard_audio_cache_getter()
        self.assertEqual(result, {})

    def test_scene_synthesis_cache_getter_returns_fetched_dict(self):
        class FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "dev-1": {
                        "text": "Brett sits down.",
                        "ts_wall": 1700000000.0,
                        "face_id": "person-brett",
                        "state": "idle",
                    }
                }

        self._patch_get(FakeResp())
        result = bridge_app._dashboard_scene_synthesis_cache_getter()
        self.assertEqual(result["dev-1"]["text"], "Brett sits down.")
        self.assertEqual(result["dev-1"]["face_id"], "person-brett")

    def test_scene_synthesis_cache_getter_degrades_on_timeout(self):
        import requests as _requests

        self._patch_get(_requests.exceptions.Timeout("simulated"))
        result = bridge_app._dashboard_scene_synthesis_cache_getter()
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# Sound-balance series getter — rewired in #115 Tile 5
# ---------------------------------------------------------------------------


class SoundBalanceGetterTests(unittest.TestCase):
    """Smoke tests for the dotty-behaviour-backed sound-balance series
    getter. Resolves the device id from the perception state getter
    (single-robot heuristic) and pulls the sparkline series."""

    def setUp(self) -> None:
        bridge_app._dotty_behaviour_cache.clear()

    def _patch_get(self, by_path):
        """Monkeypatch requests.get to dispatch on URL path. ``by_path``
        is a dict mapping path-substring → response (or Exception)."""
        original = bridge_app.requests.get

        def fake_get(url, *args, **kwargs):
            for needle, response in by_path.items():
                if needle in url:
                    if isinstance(response, Exception):
                        raise response
                    return response
            raise AssertionError(f"unexpected URL fetched: {url}")

        bridge_app.requests.get = fake_get
        self.addCleanup(lambda: setattr(bridge_app.requests, "get", original))

    def test_sound_balance_series_returns_fetched_list(self):
        class StateResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"dev-1": {"face_present": True}}

        class BalanceResp:
            def raise_for_status(self):
                return None

            def json(self):
                return [0.1, 0.5, 0.9]

        self._patch_get(
            {
                "/api/perception/state": StateResp(),
                "/api/perception/sound-balance/": BalanceResp(),
            }
        )
        result = bridge_app._dashboard_sound_balance_series()
        self.assertEqual(result, [0.1, 0.5, 0.9])

    def test_sound_balance_series_no_device_returns_empty(self):
        class EmptyStateResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {}

        self._patch_get({"/api/perception/state": EmptyStateResp()})
        result = bridge_app._dashboard_sound_balance_series()
        self.assertEqual(result, [])

    def test_sound_balance_series_degrades_on_connection_error(self):
        import requests as _requests

        class StateResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"dev-1": {"face_present": True}}

        self._patch_get(
            {
                "/api/perception/state": StateResp(),
                "/api/perception/sound-balance/": _requests.exceptions.ConnectionError(
                    "simulated"
                ),
            }
        )
        result = bridge_app._dashboard_sound_balance_series()
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
