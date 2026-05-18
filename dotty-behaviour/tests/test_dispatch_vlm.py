"""VLMClient — wire shape + sentinel returns + configured semantics."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from dispatch import (
    VLM_NETWORK_ERROR_SENTINEL,
    VLM_OFFLINE_SENTINEL,
    VLMClient,
)


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
            "choices": [{"message": {"content": "I see a chair."}}]
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
    import dispatch.vlm as mod
    mod.requests.post = rec.post


def test_offline_sentinel_when_no_api_key() -> None:
    rec = _Recorder()
    _install(rec)
    client = VLMClient(
        "https://openrouter.ai/api/v1/chat/completions",
        "google/gemini-2.0-flash-001",
        api_key="",
    )
    out = asyncio.run(
        client.describe_image("AAAA", "what?", system_prompt="be brief")
    )
    assert out == VLM_OFFLINE_SENTINEL
    assert rec.calls == []  # no HTTP attempt


def test_describe_image_posts_correct_payload() -> None:
    rec = _Recorder()
    _install(rec)
    client = VLMClient(
        "https://openrouter.ai/api/v1/chat/completions",
        "google/gemini-2.0-flash-001",
        api_key="sk-x",
    )
    out = asyncio.run(
        client.describe_image(
            "BBBB", "What's in the photo?", system_prompt="be brief"
        )
    )
    assert out == "I see a chair."
    assert len(rec.calls) == 1
    payload = rec.calls[0]["json"]
    assert payload["model"] == "google/gemini-2.0-flash-001"
    assert payload["max_tokens"] == 200
    assert payload["temperature"] == 0.3
    user_msg = payload["messages"][1]
    assert user_msg["role"] == "user"
    # image_url + text content blocks in OpenAI vision format
    assert user_msg["content"][0]["type"] == "image_url"
    assert "base64,BBBB" in user_msg["content"][0]["image_url"]["url"]
    assert user_msg["content"][1] == {
        "type": "text",
        "text": "What's in the photo?",
    }
    assert rec.calls[0]["headers"].get("Authorization") == "Bearer sk-x"


def test_describe_image_returns_sentinel_on_http_error() -> None:
    rec = _Recorder(status_code=500)
    _install(rec)
    client = VLMClient("u", "m", api_key="k")
    out = asyncio.run(
        client.describe_image("AAA", "q", system_prompt="s")
    )
    assert out == VLM_NETWORK_ERROR_SENTINEL


def test_describe_image_returns_sentinel_on_exception() -> None:
    rec = _Recorder(raise_exc=RuntimeError("network down"))
    _install(rec)
    client = VLMClient("u", "m", api_key="k")
    out = asyncio.run(
        client.describe_image("AAA", "q", system_prompt="s")
    )
    assert out == VLM_NETWORK_ERROR_SENTINEL


def test_describe_image_per_call_model_override() -> None:
    rec = _Recorder()
    _install(rec)
    client = VLMClient("u", "default-model", api_key="k")
    asyncio.run(
        client.describe_image(
            "AAA", "q", system_prompt="s", model="override-model"
        )
    )
    assert rec.calls[0]["json"]["model"] == "override-model"


def test_configured_property() -> None:
    assert VLMClient("u", "m", api_key="k").configured is True
    assert VLMClient("u", "m", api_key="").configured is False
    assert VLMClient("", "m", api_key="k").configured is False
    assert VLMClient("u", "", api_key="k").configured is False
