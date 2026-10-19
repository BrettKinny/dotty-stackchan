"""#170: the voice wake exit survives a WebSocket reconnect in sticky states.

`startToChat` gates "wake up" on `conn.current_state`, which a new
connection used to default to `idle`. The firmware state is now remembered
per device (portal_bridge) from `state_changed` events and restored onto the
next connection before it handles any message.

AI-assisted (Claude Opus 5.5).
"""
import asyncio
import importlib.util as _ilu
import json
import pathlib
import sys
import types
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

_ROOT = pathlib.Path(__file__).parent.parent
_PATCHES = _ROOT / "custom-providers" / "xiaozhi-patches"


def _load(name, path):
    spec = _ilu.spec_from_file_location(name, path)
    module = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@contextmanager
def _stubbed(**modules):
    missing = object()
    saved = {name: sys.modules.get(name, missing) for name in modules}
    try:
        sys.modules.update(modules)
        yield
    finally:
        for name, module in saved.items():
            if module is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _ns(**attrs):
    return types.SimpleNamespace(**attrs)


portal = _load("portal_bridge_under_test", _PATCHES / "portal_bridge.py")


class _Handler:
    def __init__(self, *a, **k):
        pass


_REGISTRY_STUBS = {
    "core": _ns(), "core.handle": _ns(), "core.handle.textHandler": _ns(),
    "core.handle.textHandler.abortMessageHandler": _ns(AbortTextMessageHandler=_Handler),
    "core.handle.textHandler.helloMessageHandler": _ns(HelloTextMessageHandler=_Handler),
    "core.handle.textHandler.iotMessageHandler": _ns(IotTextMessageHandler=_Handler),
    "core.handle.textHandler.listenMessageHandler": _ns(ListenTextMessageHandler=_Handler),
    "core.handle.textHandler.mcpMessageHandler": _ns(McpTextMessageHandler=_Handler),
    "core.handle.textHandler.pingMessageHandler": _ns(PingMessageHandler=_Handler),
    "core.handle.textHandler.serverMessageHandler": _ns(ServerTextMessageHandler=_Handler),
    "core.handle.textMessageHandler": _ns(TextMessageHandler=object),
}
with _stubbed(**_REGISTRY_STUBS):
    registry = _load("text_registry_under_test",
                     _PATCHES / "textMessageHandlerRegistry.py")

_RECEIVE_STUBS = {
    "core": _ns(), "core.utils": _ns(), "core.handle": _ns(),
    "core.utils.util": _ns(audio_to_data=lambda *a, **k: None),
    "core.handle.abortHandle": _ns(handleAbortMessage=AsyncMock()),
    "core.handle.intentHandler": _ns(handle_user_intent=AsyncMock(return_value=False)),
    "core.utils.output_counter": _ns(check_device_output_limit=lambda *a: False),
    "core.handle.sendAudioHandle": _ns(send_stt_message=AsyncMock(), SentenceType=object),
    "core.utils.device_command": _ns(call_tool=AsyncMock()),
}
with _stubbed(**_RECEIVE_STUBS):
    rah = _load("receive_audio_reconnect_under_test", _ROOT / "receiveAudioHandle.py")


class _WS:
    async def send(self, message):
        pass


def _fresh_conn(device_id="dev-1"):
    """A brand-new ConnectionHandler stand-in: no current_state yet."""
    return _ns(
        headers={"device-id": device_id}, logger=MagicMock(), need_bind=False,
        max_output_size=0, client_is_speaking=False, client_listen_mode="auto",
        session_id="s", websocket=_WS(), executor=MagicMock(), chat=MagicMock(),
    )


def _state_changed(conn, state):
    event = {"type": "event", "name": "state_changed", "data": {"state": state}}
    with _stubbed(**{"core.portal_bridge": portal}), \
         patch.dict("os.environ", {"BRIDGE_URL": "", "VISION_BRIDGE_URL": ""}):
        asyncio.run(registry.EventTextMessageHandler().handle(conn, event))


def _say(conn, text):
    """Run one ASR turn; return the states dispatched via set_state."""
    mcp = AsyncMock()
    with patch.object(rah, "_mcp_call_tool", new=mcp), \
         patch.object(rah, "_sync_toggles_once", new=AsyncMock()), \
         patch.object(rah, "handle_user_intent", new=AsyncMock(return_value=False)), \
         patch.object(rah, "send_stt_message", new=AsyncMock()):
        asyncio.run(rah.startToChat(conn, json.dumps({"content": text})))
    return [c.args[2]["state"] for c in mcp.await_args_list
            if c.args[1] == "self.robot.set_state"]


class TestReconnectStateRestore(unittest.TestCase):
    def setUp(self):
        portal.device_states.clear()

    def _reconnect_in(self, state):
        old = _fresh_conn()
        _state_changed(old, state)
        self.assertEqual(old.current_state, state)
        new = _fresh_conn()
        portal.restore_device_state(new, "dev-1")  # websocket_server does this
        return new

    def test_wake_up_after_reconnect_exits_each_sticky_state_once(self):
        for state in ("security", "sleep", "story_time"):
            with self.subTest(state=state):
                conn = self._reconnect_in(state)
                self.assertEqual(_say(conn, "Wake up."), ["idle"])
                self.assertIn("[STATE_WAKE]", conn.executor.submit.call_args.args[1])

    def test_reconnect_while_idle_keeps_wake_up_as_conversation(self):
        conn = self._reconnect_in("idle")
        self.assertEqual(_say(conn, "Wake up."), [])
        self.assertNotIn("[STATE_WAKE]", conn.executor.submit.call_args.args[1])

    def test_unknown_device_starts_without_a_restored_state(self):
        conn = _fresh_conn("never-seen")
        self.assertIsNone(portal.restore_device_state(conn, "never-seen"))
        self.assertFalse(hasattr(conn, "current_state"))
        self.assertEqual(_say(conn, "Wake up."), [])

    def test_live_state_event_overrides_the_restored_state(self):
        conn = self._reconnect_in("security")
        _state_changed(conn, "idle")  # firmware re-emit on channel open (#21)
        self.assertEqual(_say(conn, "Wake up."), [])

    def test_state_is_tracked_without_a_relay_url(self):
        conn = _fresh_conn()
        _state_changed(conn, "sleep")
        self.assertEqual(portal.device_states, {"dev-1": "sleep"})

    def test_states_are_per_device(self):
        portal.record_device_state("dev-a", "security")
        portal.record_device_state("dev-b", "idle")
        a, b = _fresh_conn("dev-a"), _fresh_conn("dev-b")
        portal.restore_device_state(a, "dev-a")
        portal.restore_device_state(b, "dev-b")
        self.assertEqual((a.current_state, b.current_state), ("security", "idle"))


if __name__ == "__main__":
    unittest.main()
