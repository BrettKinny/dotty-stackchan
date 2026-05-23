"""Scene-synthesis route tests via TestClient.

Tile 3 of #115 — the bridge dashboard's scene tile reads the cache
via dotty-behaviour's HTTP surface rather than a shared in-process
dict.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from main import app


def test_scene_synthesis_recent_returns_cache() -> None:
    with TestClient(app) as client:
        state = client.app.state.perception  # type: ignore[attr-defined]
        state.scene_synthesis_cache["dev-1"] = {
            "text": "Brett walked in and sat down.",
            "ts_wall": 1234567890.0,
            "face_id": "person-brett",
            "state": "idle",
        }
        resp = client.get("/api/scene-synthesis/recent")
        assert resp.status_code == 200
        body = resp.json()
        assert "dev-1" in body
        entry = body["dev-1"]
        assert entry["text"] == "Brett walked in and sat down."
        assert entry["face_id"] == "person-brett"
        assert entry["state"] == "idle"
        assert entry["ts_wall"] == 1234567890.0


def test_scene_synthesis_recent_empty_when_no_synthesis() -> None:
    with TestClient(app) as client:
        # Clear any cache entries that a previous test in the same
        # session may have left behind (TestClient reuses app state).
        state = client.app.state.perception  # type: ignore[attr-defined]
        state.scene_synthesis_cache.clear()
        resp = client.get("/api/scene-synthesis/recent")
        assert resp.status_code == 200
        assert resp.json() == {}
