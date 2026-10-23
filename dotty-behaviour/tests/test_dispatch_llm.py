"""NarrativeLLMClient — assert request shape (URL, model, headers,
message structure) and graceful failure semantics. `requests.post` is
monkey-patched so no network IO."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from dispatch import NarrativeLLMClient


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
    response_body: dict[str, Any] = field(
        default_factory=lambda: {
            "choices": [{"message": {"content": "ok"}}]
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
        return _FakeResponse(status_code=self.status_code, body=self.response_body)


def _install(rec: _Recorder) -> None:
    import dispatch.llm as mod
    mod.requests.post = rec.post


def test_chat_posts_to_chat_completions_path() -> None:
    rec = _Recorder()
    _install(rec)
    client = NarrativeLLMClient("http://127.0.0.1:8080/v1", "qwen3.6:27b-think")
    asyncio.run(client.chat("hello", system_prompt="be brief"))
    assert rec.calls[0]["url"] == "http://127.0.0.1:8080/v1/chat/completions"


def test_chat_message_shape_and_default_model() -> None:
    rec = _Recorder()
    _install(rec)
    client = NarrativeLLMClient("http://127.0.0.1:8080/v1", "qwen3.6:27b-think")
    asyncio.run(client.chat("user text", system_prompt="sys text"))
    payload = rec.calls[0]["json"]
    assert payload["model"] == "qwen3.6:27b-think"
    assert payload["messages"] == [
        {"role": "system", "content": "sys text"},
        {"role": "user", "content": "user text"},
    ]
    assert payload["max_tokens"] == 1200
    assert payload["temperature"] == 0.9


def test_chat_per_call_model_override() -> None:
    rec = _Recorder()
    _install(rec)
    client = NarrativeLLMClient("http://127.0.0.1:8080/v1", "qwen3.6:27b-think")
    asyncio.run(
        client.chat("hi", system_prompt="x", model="override-model")
    )
    assert rec.calls[0]["json"]["model"] == "override-model"


def test_chat_includes_bearer_when_api_key_set() -> None:
    rec = _Recorder()
    _install(rec)
    client = NarrativeLLMClient(
        "https://openrouter.ai/api/v1", "x/y", api_key="sk-test"
    )
    asyncio.run(client.chat("hi", system_prompt="x"))
    assert rec.calls[0]["headers"].get("Authorization") == "Bearer sk-test"


def test_chat_omits_bearer_when_api_key_empty() -> None:
    rec = _Recorder()
    _install(rec)
    client = NarrativeLLMClient("http://127.0.0.1:8080/v1", "qwen3.6:27b-think")
    asyncio.run(client.chat("hi", system_prompt="x"))
    assert "Authorization" not in rec.calls[0]["headers"]


def test_chat_returns_stripped_content() -> None:
    rec = _Recorder()
    rec.response_body = {"choices": [{"message": {"content": "  the dream  "}}]}
    _install(rec)
    client = NarrativeLLMClient("http://127.0.0.1:8080/v1", "qwen3.6:27b-think")
    out = asyncio.run(client.chat("hi", system_prompt="x"))
    assert out == "the dream"


def test_chat_returns_none_on_empty_content() -> None:
    rec = _Recorder()
    rec.response_body = {"choices": [{"message": {"content": "   "}}]}
    _install(rec)
    client = NarrativeLLMClient("http://127.0.0.1:8080/v1", "qwen3.6:27b-think")
    out = asyncio.run(client.chat("hi", system_prompt="x"))
    assert out is None


def test_chat_returns_none_on_http_error() -> None:
    rec = _Recorder(status_code=500)
    _install(rec)
    client = NarrativeLLMClient("http://127.0.0.1:8080/v1", "qwen3.6:27b-think")
    out = asyncio.run(client.chat("hi", system_prompt="x"))
    assert out is None


def test_chat_returns_none_on_network_exception() -> None:
    rec = _Recorder(raise_exc=RuntimeError("connection refused"))
    _install(rec)
    client = NarrativeLLMClient("http://127.0.0.1:8080/v1", "qwen3.6:27b-think")
    out = asyncio.run(client.chat("hi", system_prompt="x"))
    assert out is None


def test_chat_returns_none_when_not_configured() -> None:
    rec = _Recorder()
    _install(rec)
    client = NarrativeLLMClient("", "")  # both empty → not configured
    out = asyncio.run(client.chat("hi", system_prompt="x"))
    assert out is None
    assert rec.calls == []


def test_chat_returns_none_on_malformed_response() -> None:
    rec = _Recorder()
    rec.response_body = {"oops": "no choices key"}
    _install(rec)
    client = NarrativeLLMClient("http://127.0.0.1:8080/v1", "qwen3.6:27b-think")
    out = asyncio.run(client.chat("hi", system_prompt="x"))
    assert out is None


def test_chat_returns_none_when_url_empty() -> None:
    rec = _Recorder()
    _install(rec)
    client = NarrativeLLMClient("", "qwen")  # url blanked, model set
    out = asyncio.run(client.chat("hi", system_prompt="x"))
    assert out is None
    assert rec.calls == []
