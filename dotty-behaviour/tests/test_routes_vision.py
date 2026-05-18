"""Vision route tests via TestClient with a fake VLMClient."""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient

from main import app


@dataclass
class _FakeVLM:
    calls: list[dict[str, Any]] = field(default_factory=list)
    description: str = "I see a chair."

    @property
    def configured(self) -> bool:
        return True

    async def describe_image(
        self,
        b64_image: str,
        question: str,
        *,
        system_prompt: str,
        model: str | None = None,
        max_tokens: int = 200,
        temperature: float = 0.3,
        timeout_s: float | None = None,
    ) -> str:
        self.calls.append(
            {
                "b64_image": b64_image,
                "question": question,
                "system_prompt": system_prompt,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "timeout_s": timeout_s,
            }
        )
        return self.description


def _override_vlm(client: TestClient, fake: _FakeVLM) -> None:
    """Inject the fake VLM onto app.state after lifespan boots."""
    client.app.state.vlm = fake  # type: ignore[arg-type]


def _jpeg_file(payload: bytes = b"FAKEJPEG") -> tuple[str, io.BytesIO, str]:
    return ("photo.jpg", io.BytesIO(payload), "image/jpeg")


def test_vision_explain_returns_vlm_description_and_caches_it() -> None:
    with TestClient(app) as client:
        fake = _FakeVLM(description="A red ball on the table.")
        _override_vlm(client, fake)
        r = client.post(
            "/api/vision/explain",
            files={"file": _jpeg_file()},
            data={"question": "What do you see?"},
            headers={"device-id": "dev-1"},
        )
        assert r.status_code == 200
        assert r.json() == {"description": "A red ball on the table."}

        state = client.app.state.perception
        cached = state.vision_cache["dev-1"]
        assert cached["description"] == "A red ball on the table."
        assert cached["jpeg_bytes"] == b"FAKEJPEG"
        assert cached["question"] == "What do you see?"
        assert cached["source"] == "v1"
        assert cached["room_match_person_id"] is None

        assert len(fake.calls) == 1
        assert fake.calls[0]["question"] == "What do you see?"
        # Default system prompt is the non-kid wording
        assert "young child" not in fake.calls[0]["system_prompt"]


def test_vision_explain_kid_mode_changes_system_prompt() -> None:
    with TestClient(app) as client:
        fake = _FakeVLM()
        _override_vlm(client, fake)
        client.app.state.kid_mode = True
        r = client.post(
            "/api/vision/explain",
            files={"file": _jpeg_file()},
            headers={"device-id": "dev-1"},
        )
        assert r.status_code == 200
        # Reset so other tests don't see the leak
        client.app.state.kid_mode = False
        assert "young child" in fake.calls[0]["system_prompt"]


def test_vision_explain_default_device_id_is_unknown() -> None:
    with TestClient(app) as client:
        fake = _FakeVLM()
        _override_vlm(client, fake)
        r = client.post(
            "/api/vision/explain",
            files={"file": _jpeg_file()},
        )
        assert r.status_code == 200
        state = client.app.state.perception
        assert "unknown" in state.vision_cache


def test_vision_latest_waits_then_returns_cache() -> None:
    """Open /api/vision/latest first, then POST /api/vision/explain — the
    pending GET should resolve with the description from the POST."""
    import threading

    with TestClient(app) as client:
        fake = _FakeVLM(description="A spinning top.")
        _override_vlm(client, fake)

        result: dict = {}

        def _poll() -> None:
            r = client.get("/api/vision/latest/dev-poll")
            result["status"] = r.status_code
            result["body"] = r.json()

        t = threading.Thread(target=_poll, daemon=True)
        t.start()
        # Give the GET a moment to register its waiter
        import time as _time
        _time.sleep(0.05)
        client.post(
            "/api/vision/explain",
            files={"file": _jpeg_file()},
            headers={"device-id": "dev-poll"},
        )
        t.join(timeout=3.0)
        assert not t.is_alive(), "vision/latest never returned"
        assert result["status"] == 200
        assert result["body"]["description"] == "A spinning top."


# NOTE: A wire-level "/api/vision/latest returns 404 on timeout" test
# was considered but skipped — the route's wait_for timeout is 15 s,
# and dropping that to a tunable knob would add config surface for one
# test. The signal-path is exercised by test_signal_vision_waiters_*
# below and the live container will catch a regression in seconds.


def test_signal_vision_waiters_wakes_pending_listener() -> None:
    """Unit-level test of the bus signal logic — no HTTP."""
    from perception import PerceptionState

    async def go() -> None:
        state = PerceptionState()
        event = state.register_vision_waiter("dev-1")
        # Signal in the background after a short delay
        async def _signal() -> None:
            await asyncio.sleep(0.01)
            state.signal_vision_waiters("dev-1")

        task = asyncio.create_task(_signal())
        await asyncio.wait_for(event.wait(), timeout=0.5)
        await task
        state.unregister_vision_waiter("dev-1", event)

    asyncio.run(go())


def test_unregister_vision_waiter_after_signal_is_idempotent() -> None:
    from perception import PerceptionState

    state = PerceptionState()
    event = state.register_vision_waiter("dev-1")
    state.signal_vision_waiters("dev-1")
    state.unregister_vision_waiter("dev-1", event)
    # second unregister is a no-op
    state.unregister_vision_waiter("dev-1", event)
