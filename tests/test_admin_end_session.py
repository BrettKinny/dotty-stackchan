"""Admin idle/sleep transitions end the device's listening session.

Observed live: POST /xiaozhi/admin/set-state {"state": "idle"} changed the
StateManager state but left xiaozhi LISTENING (mic open) until the 120 s
no-voice timeout closed the WebSocket. The xiaozhi protocol has no
server->device "listen stop", so the fix closes the WebSocket the same way
that timeout does — after the set_state frame has been sent.

AI-assisted (Claude Opus 5.5).
"""
import asyncio
import importlib.util as _ilu
import json
import pathlib
import sys
import types
import unittest
from unittest.mock import MagicMock

_PATCHES = pathlib.Path(__file__).parent.parent / "custom-providers" / "xiaozhi-patches"

_dc_spec = _ilu.spec_from_file_location("device_command_end_session_under_test",
                                        _PATCHES / "device_command.py")
dc = _ilu.module_from_spec(_dc_spec)  # type: ignore[arg-type]
_dc_spec.loader.exec_module(dc)  # type: ignore[union-attr]


class _Conn:
    """Records the order of device-bound frames, aborts and the close."""

    def __init__(self, speaking=False):
        self.events: list = []
        self.session_id = "sess-1"
        self.headers = {"device-id": "dev-1"}
        self.client_is_speaking = speaking
        self.close_after_chat = False
        self.logger = MagicMock()
        conn = self

        class _WS:
            async def send(self, message):
                conn.events.append(("send", json.loads(message)))

        self.websocket = _WS()

    async def close(self, ws=None):
        self.events.append(("close", ws is self.websocket))

    def kinds(self):
        return [kind for kind, _ in self.events]


def _abort_module():
    async def handleAbortMessage(conn):
        conn.events.append(("abort", None))
        conn.close_after_chat = False
        conn.client_is_speaking = False
    return types.SimpleNamespace(handleAbortMessage=handleAbortMessage)


class _StubbedModules:
    names = (
        "config", "config.logger", "core", "core.api", "core.api.ota_handler",
        "core.api.vision_handler", "core.portal_bridge", "core.utils",
        "core.utils.device_command", "core.handle", "core.handle.abortHandle",
    )

    def __enter__(self):
        missing = object()
        self._missing = missing
        self._saved = {n: sys.modules.get(n, missing) for n in self.names}
        self.active: dict = {}
        portal = MagicMock()
        portal.active_connections = self.active
        logger_mod = MagicMock()
        logger_mod.setup_logging = lambda: MagicMock()
        for n in ("config", "core", "core.api", "core.api.ota_handler",
                  "core.api.vision_handler", "core.handle"):
            sys.modules[n] = MagicMock()
        sys.modules["config.logger"] = logger_mod
        sys.modules["core.portal_bridge"] = portal
        core_utils = MagicMock()
        core_utils.device_command = dc
        sys.modules["core.utils"] = core_utils
        sys.modules["core.utils.device_command"] = dc
        sys.modules["core.handle.abortHandle"] = _abort_module()
        spec = _ilu.spec_from_file_location("http_server_end_session_under_test",
                                            _PATCHES / "http_server.py")
        self.mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(self.mod)  # type: ignore[union-attr]
        return self

    def __exit__(self, *exc):
        for n, v in self._saved.items():
            if v is self._missing:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = v


def _request(data):
    class _Req:
        async def json(self):
            return data
    return _Req()


class TestEndSessionHelper(unittest.TestCase):
    def setUp(self):
        self.stubs = _StubbedModules().__enter__()

    def tearDown(self):
        self.stubs.__exit__(None, None, None)

    def test_closes_websocket_like_the_no_voice_timeout(self):
        conn = _Conn()
        self.assertTrue(asyncio.run(dc.end_session(conn, reason="test")))
        self.assertEqual(conn.events, [("close", True)])
        self.assertTrue(conn.close_after_chat, "no-voice timeout must not double-close")

    def test_stops_reply_in_flight_before_closing(self):
        conn = _Conn(speaking=True)
        asyncio.run(dc.end_session(conn))
        self.assertEqual(conn.kinds(), ["abort", "close"])
        self.assertTrue(conn.close_after_chat, "abort clears it; end_session restores it")

    def test_second_call_is_a_no_op(self):
        conn = _Conn()

        async def go():
            return await dc.end_session(conn), await dc.end_session(conn)

        self.assertEqual(asyncio.run(go()), (True, False))
        self.assertEqual(conn.kinds(), ["close"])

    def test_exiting_connection_is_left_alone(self):
        conn = _Conn()
        conn.is_exiting = True
        self.assertFalse(asyncio.run(dc.end_session(conn)))
        self.assertEqual(conn.events, [])


class TestSetStateEndsSession(unittest.TestCase):
    def setUp(self):
        self.stubs = _StubbedModules().__enter__()
        self.mod = self.stubs.mod
        self._grace = dc.DEFAULT_END_SESSION_GRACE_S
        dc.DEFAULT_END_SESSION_GRACE_S = 0.0

    def tearDown(self):
        dc.DEFAULT_END_SESSION_GRACE_S = self._grace
        self.stubs.__exit__(None, None, None)

    def _server(self):
        return self.mod.SimpleHttpServer({"server": {"ip": "0.0.0.0", "http_port": 8003}})

    def _post(self, handler_name, body, conn):
        self.stubs.active["dev-1"] = conn

        async def go():
            resp = await getattr(self._server(), handler_name)(_request(body))
            await asyncio.sleep(0.02)
            return resp

        return asyncio.run(go())

    def test_idle_and_sleep_send_state_then_close(self):
        for state in ("idle", "sleep"):
            with self.subTest(state=state):
                conn = _Conn()
                resp = self._post("_dotty_set_state", {"state": state}, conn)
                self.assertEqual(resp.status, 200)
                self.assertTrue(json.loads(resp.body)["end_session"])
                self.assertEqual(conn.kinds(), ["send", "close"])
                frame = conn.events[0][1]
                self.assertEqual(frame["payload"]["params"],
                                 {"name": "self.robot.set_state",
                                  "arguments": {"state": state}})

    def test_idle_while_speaking_aborts_after_state_frame(self):
        conn = _Conn(speaking=True)
        self._post("_dotty_set_state", {"state": "idle"}, conn)
        self.assertEqual(conn.kinds(), ["send", "abort", "close"])

    def test_conversational_and_sticky_states_keep_the_session(self):
        for state in ("talk", "story_time", "security", "dance"):
            with self.subTest(state=state):
                conn = _Conn()
                resp = self._post("_dotty_set_state", {"state": state}, conn)
                self.assertFalse(json.loads(resp.body)["end_session"])
                self.assertEqual(conn.kinds(), ["send"])

    def test_end_session_false_opts_out(self):
        conn = _Conn()
        self._post("_dotty_set_state", {"state": "idle", "end_session": False}, conn)
        self.assertEqual(conn.kinds(), ["send"])

    def test_end_session_must_be_bool(self):
        conn = _Conn()
        resp = self._post("_dotty_set_state", {"state": "idle", "end_session": "no"}, conn)
        self.assertEqual(resp.status, 400)
        self.assertEqual(conn.events, [])

    def test_end_session_route_closes_without_changing_state(self):
        conn = _Conn(speaking=True)
        resp = self._post("_dotty_end_session", {}, conn)
        self.assertEqual(resp.status, 200)
        self.assertEqual(conn.kinds(), ["abort", "close"])
        self.assertFalse(hasattr(conn, "_dotty_desired_state"))

    def test_end_session_route_503s_without_a_device(self):
        async def go():
            return await self._server()._dotty_end_session(_request({}))
        self.assertEqual(asyncio.run(go()).status, 503)


if __name__ == "__main__":
    unittest.main()
