"""Audio route tests via TestClient with a fake AudioCaptionClient."""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient

from main import app
from routes.audio import audio_format_from_upload
from fastapi import UploadFile


@dataclass
class _FakeAudio:
    calls: list[dict[str, Any]] = field(default_factory=list)
    description: str = "Music plays softly."

    @property
    def configured(self) -> bool:
        return True

    async def caption(
        self,
        b64_audio: str,
        audio_format: str,
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
                "b64_audio": b64_audio,
                "audio_format": audio_format,
                "question": question,
                "system_prompt": system_prompt,
                "timeout_s": timeout_s,
            }
        )
        return self.description


def _wav_file(payload: bytes = b"RIFFFAKEWAV") -> tuple[str, io.BytesIO, str]:
    return ("clip.wav", io.BytesIO(payload), "audio/wav")


def test_audio_explain_caches_and_returns_caption() -> None:
    with TestClient(app) as client:
        fake = _FakeAudio(description="Two people laughing.")
        client.app.state.audio_caption = fake  # type: ignore[arg-type]
        r = client.post(
            "/api/audio/explain",
            files={"file": _wav_file()},
            data={"question": "Describe what you hear."},
            headers={"device-id": "dev-1"},
        )
        assert r.status_code == 200
        assert r.json() == {"description": "Two people laughing."}

        state = client.app.state.perception
        cached = state.audio_cache["dev-1"]
        assert cached["description"] == "Two people laughing."
        assert cached["question"] == "Describe what you hear."
        assert cached["source"] == "audio_explain"

        assert len(fake.calls) == 1
        assert fake.calls[0]["audio_format"] == "wav"


def test_audio_explain_broadcasts_audio_captioned_event() -> None:
    """The route should fan a synthetic `audio_captioned` event onto
    the perception bus so dashboards see the caption immediately."""
    with TestClient(app) as client:
        fake = _FakeAudio(description="Glass shatters.")
        client.app.state.audio_caption = fake  # type: ignore[arg-type]
        state = client.app.state.perception

        seen: list = []

        async def _drain() -> None:
            q = state.subscribe()
            try:
                ev = await asyncio.wait_for(q.get(), timeout=2.0)
                seen.append(ev)
            finally:
                state.unsubscribe(q)

        import threading

        t = threading.Thread(target=lambda: asyncio.run(_drain()), daemon=True)
        t.start()
        # Tiny wait so the subscribe happens before the POST.
        import time as _time
        _time.sleep(0.05)
        r = client.post(
            "/api/audio/explain",
            files={"file": _wav_file()},
            headers={"device-id": "dev-1"},
        )
        assert r.status_code == 200
        t.join(timeout=2.0)
        assert seen, "no perception event observed"
        assert seen[0].name == "audio_captioned"
        assert seen[0].data["preview"] == "Glass shatters."


def test_audio_explain_default_question() -> None:
    with TestClient(app) as client:
        fake = _FakeAudio()
        client.app.state.audio_caption = fake  # type: ignore[arg-type]
        r = client.post(
            "/api/audio/explain",
            files={"file": _wav_file()},
            headers={"device-id": "dev-x"},
        )
        assert r.status_code == 200
        assert fake.calls[0]["question"] == "Describe what you hear."


def test_audio_format_from_upload_content_type() -> None:
    f = UploadFile(filename="x.bin", file=io.BytesIO(b""),
                   headers={"content-type": "audio/mpeg"})
    assert audio_format_from_upload(f) == "mp3"


def test_audio_format_from_upload_filename_fallback() -> None:
    f = UploadFile(filename="clip.opus", file=io.BytesIO(b""))
    assert audio_format_from_upload(f) == "opus"


def test_audio_format_from_upload_default_wav() -> None:
    f = UploadFile(filename="mystery", file=io.BytesIO(b""))
    assert audio_format_from_upload(f) == "wav"


# ---------------------------------------------------------------------------
# /api/audio/cache (Tile 4 of #115 — bridge dashboard reads via HTTP.)
# ---------------------------------------------------------------------------


def test_audio_cache_route_returns_cache() -> None:
    with TestClient(app) as client:
        state = client.app.state.perception  # type: ignore[attr-defined]
        state.audio_cache.clear()
        state.audio_cache["dev-a"] = {
            "description": "A dog barks outside.",
            "timestamp": 1.23,
            "wall_ts": 1234567890.0,
            "question": "What do you hear?",
            "source": "audio_explain",
        }
        resp = client.get("/api/audio/cache")
        assert resp.status_code == 200
        body = resp.json()
        assert "dev-a" in body
        entry = body["dev-a"]
        assert entry["description"] == "A dog barks outside."
        assert entry["question"] == "What do you hear?"
        assert entry["source"] == "audio_explain"
        assert entry["wall_ts"] == 1234567890.0


def test_audio_cache_route_empty_when_no_captures() -> None:
    with TestClient(app) as client:
        state = client.app.state.perception  # type: ignore[attr-defined]
        state.audio_cache.clear()
        resp = client.get("/api/audio/cache")
        assert resp.status_code == 200
        assert resp.json() == {}
