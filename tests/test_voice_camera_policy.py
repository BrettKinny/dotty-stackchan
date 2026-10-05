"""Voice-only camera privacy regressions; AI-assisted by Codex (GPT-6)."""
import asyncio
import json
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.test_asr_name_corrections import _module


@pytest.fixture
def policy(tmp_path, monkeypatch):
    path = tmp_path / "kid-mode"
    monkeypatch.setattr(_module, "_KID_MODE_STATE_FILE", str(path))
    monkeypatch.setenv("DOTTY_KID_MODE", "false")
    monkeypatch.setattr(_module, "VISION_BRIDGE_URL", "http://synthetic.invalid")
    return path


@pytest.mark.parametrize("value, denied", [("true", True), ("1", True), ("yes", True),
    ("false", False), ("0", False), ("no", False), (" FALSE\n", False),
    ("", True), ("garbage", True)])
def test_camera_policy_uses_explicit_current_state(policy, value, denied):
    policy.write_text(value)
    assert _module._camera_access_denied() is denied


def test_missing_unreadable_or_invalid_policy_denies_even_with_adult_env(policy):
    assert _module._camera_access_denied() is True
    policy.mkdir()
    assert _module._camera_access_denied() is True
    policy.rmdir()
    policy.write_bytes(b"\xff")
    assert _module._camera_access_denied() is True


def test_camera_policy_refreshes_without_process_restart(policy):
    for value, denied in [("false", False), ("true", True), ("false", False)]:
        policy.write_text(value)
        assert _module._camera_access_denied() is denied


def test_physical_capture_boundary_denies_before_dispatch(policy):
    policy.write_text("true")
    conn = types.SimpleNamespace(logger=MagicMock(), headers={"device-id": "fixture"})
    dispatch = AsyncMock(side_effect=AssertionError("camera dispatch must not happen"))
    with patch.object(_module, "_mcp_call_tool", dispatch):
        with pytest.raises(PermissionError, match="Kid Mode"):
            asyncio.run(_module._handle_vision(conn, "What do you see?"))
    dispatch.assert_not_awaited()


def test_voice_denial_never_becomes_successful_photo_prompt(policy):
    policy.write_text("true")
    conn = types.SimpleNamespace(logger=MagicMock(), need_bind=False, max_output_size=0,
        client_is_speaking=False, current_state="idle", session_id="fixture",
        websocket=types.SimpleNamespace(send=AsyncMock()), executor=MagicMock(),
        chat=MagicMock(), headers={"device-id": "fixture"}, _dotty_toggles_synced=True)
    dispatch = AsyncMock(side_effect=AssertionError("camera dispatch must not happen"))
    with patch.object(_module, "handle_user_intent", AsyncMock(return_value=False)), \
         patch.object(_module, "send_stt_message", AsyncMock()), \
         patch.object(_module, "_mcp_call_tool", dispatch):
        asyncio.run(_module.startToChat(conn, json.dumps({"content": "What do you see?"})))
    dispatch.assert_not_awaited()
    prompt = conn.executor.submit.call_args.args[1]
    assert "Kid Mode" in prompt
    assert "just used your camera" not in prompt
    assert "photo shows" not in prompt


def test_adult_physical_capture_preserves_result(policy, monkeypatch):
    policy.write_text("false")
    conn = types.SimpleNamespace(logger=MagicMock(), headers={"device-id": "fixture"})
    response = types.SimpleNamespace(status_code=200, json=lambda: {"description": "Synthetic cube."})
    async def run():
        loop = asyncio.get_running_loop()
        async def synthetic_executor(*args):
            return response
        dispatch = AsyncMock()
        with patch.object(loop, "run_in_executor", synthetic_executor), \
             patch.object(_module, "_mcp_call_tool", dispatch):
            assert await _module._handle_vision(conn, "What do you see?") == "Synthetic cube."
        dispatch.assert_awaited_once_with(conn, "self.camera.take_photo", {"question": "What do you see?"})
    # requests is imported by the real function, but its client must not run.
    monkeypatch.setitem(__import__("sys").modules, "requests", types.SimpleNamespace(get=None))
    asyncio.run(run())
