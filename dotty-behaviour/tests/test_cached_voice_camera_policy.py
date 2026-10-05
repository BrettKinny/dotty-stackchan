"""Cached voice-camera policy boundary; AI-assisted by Codex (GPT-6)."""
import asyncio
from time import perf_counter
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
import pytest

import config
from routes.voice import get_perception_state, router


def request(state):
    app = FastAPI()
    app.include_router(router)
    # Deliberately stale adult flag: the shared file must win.
    app.state.kid_mode = False
    async def fake_state():
        return state
    app.dependency_overrides[get_perception_state] = fake_state
    async def run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://synthetic") as client:
            return await client.get("/api/voice/take_photo")
    return asyncio.run(run())


@pytest.fixture
def policy(tmp_path, monkeypatch):
    path = tmp_path / "kid-mode"
    monkeypatch.setattr(config, "KID_MODE_STATE_FILE", path, raising=False)
    monkeypatch.setenv("DOTTY_KID_MODE", "false")
    return path


class ForbiddenCache:
    def values(self):
        raise AssertionError("denied camera policy must not inspect cached descriptions")


@pytest.mark.parametrize("value", ["true", "1", "yes", "", "garbage", None])
def test_denied_or_unknown_policy_never_reads_cache(policy, value):
    if value is not None:
        policy.write_text(value)
    response = request(SimpleNamespace(vision_cache=ForbiddenCache()))
    assert response.status_code == 200
    assert response.json()["denied"] is True
    assert "Kid Mode" in response.json()["description"]


def test_unreadable_or_invalid_policy_is_denied(policy):
    policy.mkdir()
    assert request(SimpleNamespace(vision_cache=ForbiddenCache())).json()["denied"] is True
    policy.rmdir()
    policy.write_bytes(b"\xff")
    assert request(SimpleNamespace(vision_cache=ForbiddenCache())).json()["denied"] is True


def test_toggle_refresh_and_adult_cache_contract(policy):
    state = SimpleNamespace(vision_cache={"fixture": {
        "description": "Synthetic cobalt cube.", "timestamp": perf_counter()}})
    for value, denied in [("false", False), ("true", True), ("0", False), ("yes", True), ("no", False)]:
        policy.write_text(value)
        response = request(state).json()
        if denied:
            assert response["denied"] is True
            assert "cobalt" not in response["description"]
        else:
            assert response == {"description": "Synthetic cobalt cube."}
