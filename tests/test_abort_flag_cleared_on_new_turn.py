"""A device/admin abort must not silence every later turn on the connection.

AI-assisted (Claude). Observed on the physical robot 2026-10-05: after one
`abort` frame, ASR and the LLM kept running but no reply was ever spoken,
because nothing on the `nointent` chat path cleared `conn.client_abort`.
"""
import asyncio
import json
import types
from unittest.mock import AsyncMock, MagicMock, patch

from tests.test_asr_name_corrections import _module


def _start_turn(text, **conn_overrides):
    conn = types.SimpleNamespace(
        logger=MagicMock(), need_bind=False, max_output_size=0,
        client_is_speaking=False, current_state="idle", session_id="fixture",
        websocket=types.SimpleNamespace(send=AsyncMock()),
        executor=MagicMock(), chat=MagicMock(),
        _dotty_toggles_synced=True, client_abort=False,
    )
    for key, value in conn_overrides.items():
        setattr(conn, key, value)
    flag_at_submit = []
    conn.executor.submit.side_effect = lambda *a, **k: flag_at_submit.append(conn.client_abort)
    with patch.object(_module, "handle_user_intent", new=AsyncMock(return_value=False)), \
         patch.object(_module, "send_stt_message", new=AsyncMock()), \
         patch.object(_module, "_mcp_call_tool", new=AsyncMock()):
        asyncio.run(_module.startToChat(conn, json.dumps({"content": text})))
    return conn, flag_at_submit


def test_new_turn_after_abort_is_not_submitted_with_abort_still_set():
    conn, flag_at_submit = _start_turn("what is your name", client_abort=True)
    assert flag_at_submit == [False]
    assert conn.client_abort is False


def test_turn_handled_by_intent_does_not_touch_the_flag():
    conn = types.SimpleNamespace(
        logger=MagicMock(), need_bind=False, max_output_size=0,
        client_is_speaking=False, current_state="idle", session_id="fixture",
        websocket=types.SimpleNamespace(send=AsyncMock()),
        executor=MagicMock(), chat=MagicMock(),
        _dotty_toggles_synced=True, client_abort=True,
    )
    with patch.object(_module, "handle_user_intent", new=AsyncMock(return_value=True)), \
         patch.object(_module, "send_stt_message", new=AsyncMock()):
        asyncio.run(_module.startToChat(conn, json.dumps({"content": "goodbye"})))
    assert conn.client_abort is True
    conn.executor.submit.assert_not_called()
