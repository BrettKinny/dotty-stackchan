"""AudioCaptionClient — wire shape + fallback semantics."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from dispatch import AUDIO_FALLBACK_DESCRIPTION, AudioCaptionClient


@dataclass
class _FakeResponse:
    status_code: int = 200
    body: dict[str, Any] = field(default_factory=dict)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self.body


@dataclass
class _Recorder:
    calls: list[dict[str, Any]] = field(default_factory=list)
    body: dict[str, Any] = field(
        default_factory=lambda: {
            "choices": [{"message": {"content": "Quiet music plays."}}]
        }
    )
    status_code: int = 200
    raise_exc: Exception | None = None

    def post(self, url: str, *, json: dict[str, Any],
             headers: dict[str, str], timeout: float) -> _FakeResponse:
        self.calls.append(
            {"url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        if self.raise_exc:
            raise self.raise_exc
        return _FakeResponse(status_code=self.status_code, body=self.body)


def _install(rec: _Recorder) -> None:
    import dispatch.audio_caption as mod
    mod.requests.post = rec.post


def test_fallback_when_no_api_key() -> None:
    rec = _Recorder()
    _install(rec)
    client = AudioCaptionClient("u", "m", api_key="")
    out = asyncio.run(
        client.caption("AAA", "wav", "what?", system_prompt="be brief")
    )
    assert out == AUDIO_FALLBACK_DESCRIPTION
    assert rec.calls == []


def test_caption_payload_shape() -> None:
    rec = _Recorder()
    _install(rec)
    client = AudioCaptionClient(
        "https://openrouter.ai/api/v1/chat/completions",
        "google/gemini-2.5-flash",
        api_key="sk-x",
    )
    asyncio.run(
        client.caption(
            "BBB", "wav", "describe the audio",
            system_prompt="you are listening",
        )
    )
    payload = rec.calls[0]["json"]
    assert payload["model"] == "google/gemini-2.5-flash"
    user_msg = payload["messages"][1]
    assert user_msg["content"][0]["type"] == "input_audio"
    assert user_msg["content"][0]["input_audio"] == {
        "data": "BBB", "format": "wav"
    }
    assert user_msg["content"][1] == {
        "type": "text", "text": "describe the audio"
    }
    assert rec.calls[0]["headers"]["Authorization"] == "Bearer sk-x"


def test_caption_returns_fallback_on_network_error() -> None:
    rec = _Recorder(raise_exc=RuntimeError("network down"))
    _install(rec)
    client = AudioCaptionClient("u", "m", api_key="k")
    out = asyncio.run(
        client.caption("AAA", "wav", "q", system_prompt="s")
    )
    assert out == AUDIO_FALLBACK_DESCRIPTION


def test_caption_returns_fallback_on_http_error() -> None:
    rec = _Recorder(status_code=500)
    _install(rec)
    client = AudioCaptionClient("u", "m", api_key="k")
    out = asyncio.run(
        client.caption("AAA", "wav", "q", system_prompt="s")
    )
    assert out == AUDIO_FALLBACK_DESCRIPTION


def test_caption_strips_response_content() -> None:
    rec = _Recorder()
    rec.body = {"choices": [{"message": {"content": "  hi there  "}}]}
    _install(rec)
    client = AudioCaptionClient("u", "m", api_key="k")
    out = asyncio.run(
        client.caption("AAA", "wav", "q", system_prompt="s")
    )
    assert out == "hi there"


def test_fallback_when_no_url() -> None:
    rec = _Recorder()
    _install(rec)
    client = AudioCaptionClient("", "m", api_key="sk-or-fake")
    out = asyncio.run(
        client.caption("AAA", "wav", "what?", system_prompt="be brief")
    )
    assert out == AUDIO_FALLBACK_DESCRIPTION
    assert rec.calls == []
