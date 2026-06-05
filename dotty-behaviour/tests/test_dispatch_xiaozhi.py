"""XiaozhiAdminClient — assert URLs, payloads, and configured/failure
semantics. `requests.post` is monkey-patched so no network IO."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from dispatch import XiaozhiAdminClient


@dataclass
class _FakeResponse:
    status_code: int = 200
    text: str = ""


@dataclass
class _Recorder:
    calls: list[dict[str, Any]] = field(default_factory=list)
    status_code: int = 200
    raise_exc: Exception | None = None
    last_headers: dict[str, str] | None = None

    def post(
        self, url: str, *, json: dict[str, Any], timeout: float,
        headers: dict[str, str] | None = None,
    ) -> _FakeResponse:
        # headers recorded separately so existing exact-match `calls` asserts
        # (url/json/timeout) keep passing.
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        self.last_headers = headers
        if self.raise_exc:
            raise self.raise_exc
        return _FakeResponse(status_code=self.status_code, text="")


def _client_with_recorder(monkeypatch_target_module, *, status: int = 200,
                          raise_exc: Exception | None = None) -> tuple[
    XiaozhiAdminClient, _Recorder
]:
    rec = _Recorder(status_code=status, raise_exc=raise_exc)
    monkeypatch_target_module.requests.post = rec.post
    client = XiaozhiAdminClient("127.0.0.1", 8003, timeout_s=2.5)
    return client, rec


def test_not_configured_returns_false_without_posting() -> None:
    import dispatch.xiaozhi as mod

    rec = _Recorder()
    mod.requests.post = rec.post
    client = XiaozhiAdminClient("", 8003)
    ok = asyncio.run(client.abort("dev-1"))
    assert ok is False
    assert rec.calls == []


def test_abort_url_and_payload() -> None:
    import dispatch.xiaozhi as mod

    client, rec = _client_with_recorder(mod)
    ok = asyncio.run(client.abort("dev-1"))
    assert ok is True
    assert rec.calls == [
        {
            "url": "http://127.0.0.1:8003/xiaozhi/admin/abort",
            "json": {"device_id": "dev-1"},
            "timeout": 2.5,
        }
    ]


def test_inject_text_payload_includes_text_and_device() -> None:
    import dispatch.xiaozhi as mod

    client, rec = _client_with_recorder(mod)
    asyncio.run(client.inject_text("dev-1", "Hi there!"))
    assert rec.calls[0]["url"].endswith("/xiaozhi/admin/inject-text")
    assert rec.calls[0]["json"] == {"text": "Hi there!", "device_id": "dev-1"}


def test_say_payload_includes_text_and_device() -> None:
    import dispatch.xiaozhi as mod

    client, rec = _client_with_recorder(mod)
    asyncio.run(client.say("dev-1", "Good morning."))
    assert rec.calls[0]["url"].endswith("/xiaozhi/admin/say")
    assert rec.calls[0]["json"] == {"text": "Good morning.", "device_id": "dev-1"}


def test_set_head_angles_payload() -> None:
    import dispatch.xiaozhi as mod

    client, rec = _client_with_recorder(mod)
    asyncio.run(client.set_head_angles("dev-1", yaw=-30, pitch=10, speed=120))
    assert rec.calls[0]["url"].endswith("/xiaozhi/admin/set-head-angles")
    assert rec.calls[0]["json"] == {
        "device_id": "dev-1",
        "yaw": -30,
        "pitch": 10,
        "speed": 120,
    }


def test_set_state_payload() -> None:
    import dispatch.xiaozhi as mod

    client, rec = _client_with_recorder(mod)
    asyncio.run(client.set_state("dev-1", "security"))
    assert rec.calls[0]["url"].endswith("/xiaozhi/admin/set-state")
    assert rec.calls[0]["json"] == {
        "device_id": "dev-1", "state": "security"
    }


def test_set_face_identified_payload() -> None:
    import dispatch.xiaozhi as mod

    client, rec = _client_with_recorder(mod)
    asyncio.run(client.set_face_identified("dev-1"))
    assert rec.calls[0]["url"].endswith("/xiaozhi/admin/set-face-identified")
    assert rec.calls[0]["json"] == {"device_id": "dev-1"}


def test_set_toggle_payload() -> None:
    import dispatch.xiaozhi as mod

    client, rec = _client_with_recorder(mod)
    asyncio.run(client.set_toggle("dev-1", "kid_mode", True))
    assert rec.calls[0]["url"].endswith("/xiaozhi/admin/set-toggle")
    assert rec.calls[0]["json"] == {
        "device_id": "dev-1", "name": "kid_mode", "enabled": True
    }


def test_play_asset_payload() -> None:
    import dispatch.xiaozhi as mod

    client, rec = _client_with_recorder(mod)
    asyncio.run(client.play_asset("dev-1", "/assets/purr.opus"))
    assert rec.calls[0]["url"].endswith("/xiaozhi/admin/play-asset")
    assert rec.calls[0]["json"] == {
        "device_id": "dev-1", "asset": "/assets/purr.opus"
    }


def test_4xx_returns_false() -> None:
    import dispatch.xiaozhi as mod

    client, _ = _client_with_recorder(mod, status=400)
    ok = asyncio.run(client.abort("dev-1"))
    assert ok is False


def test_network_exception_returns_false_not_raise() -> None:
    import dispatch.xiaozhi as mod

    client, _ = _client_with_recorder(
        mod, raise_exc=RuntimeError("connection refused")
    )
    ok = asyncio.run(client.abort("dev-1"))
    assert ok is False


def test_admin_token_header_sent_when_set() -> None:
    import os

    import dispatch.xiaozhi as mod

    rec = _Recorder()
    mod.requests.post = rec.post
    prev = os.environ.get("DOTTY_ADMIN_TOKEN")
    os.environ["DOTTY_ADMIN_TOKEN"] = "s3cret"
    try:
        client = XiaozhiAdminClient("127.0.0.1", 8003)
        asyncio.run(client.abort("dev-1"))
        assert rec.last_headers == {"X-Admin-Token": "s3cret"}
    finally:
        if prev is None:
            os.environ.pop("DOTTY_ADMIN_TOKEN", None)
        else:
            os.environ["DOTTY_ADMIN_TOKEN"] = prev


def test_no_admin_token_header_when_unset() -> None:
    import os

    import dispatch.xiaozhi as mod

    rec = _Recorder()
    mod.requests.post = rec.post
    prev = os.environ.get("DOTTY_ADMIN_TOKEN")
    os.environ.pop("DOTTY_ADMIN_TOKEN", None)
    try:
        client = XiaozhiAdminClient("127.0.0.1", 8003)
        asyncio.run(client.abort("dev-1"))
        assert rec.last_headers == {}
    finally:
        if prev is not None:
            os.environ["DOTTY_ADMIN_TOKEN"] = prev
