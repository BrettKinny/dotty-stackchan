"""Tests for the DeviceCommand seam (xiaozhi-patches/device_command.py)
and its wiring into the http_server admin handlers.

The seam replaces twelve hand-rolled MCP envelopes (2026-06-06 audit):
ids were `int(time.time()*1000) % 0x7FFFFFFF` (same-millisecond calls
collided) and every site called `conn.websocket.send()` with no
coordination (the websockets library forbids interleaved sends).
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

_dc_spec = _ilu.spec_from_file_location("device_command_under_test", _PATCHES / "device_command.py")
dc = _ilu.module_from_spec(_dc_spec)  # type: ignore[arg-type]
_dc_spec.loader.exec_module(dc)  # type: ignore[union-attr]


class _RecordingWS:
    def __init__(self):
        self.sent: list = []

    async def send(self, message):
        self.sent.append(message)


class _SlowWS:
    """Records how many sends overlap — the lock must keep it at 1."""

    def __init__(self):
        self.sent: list = []
        self._in_send = 0
        self.max_concurrent = 0

    async def send(self, message):
        self._in_send += 1
        self.max_concurrent = max(self.max_concurrent, self._in_send)
        await asyncio.sleep(0.005)
        self.sent.append(message)
        self._in_send -= 1


class _FakeConn:
    def __init__(self, ws=None):
        self.session_id = "sess-1"
        self.websocket = ws or _RecordingWS()


class TestRequestIds(unittest.TestCase):

    def test_ids_are_monotonic_and_collision_free(self):
        conn = _FakeConn()
        ids = [dc.next_request_id(conn) for _ in range(1000)]
        self.assertEqual(ids, list(range(1, 1001)))
        self.assertEqual(len(set(ids)), 1000)

    def test_counters_are_per_connection(self):
        a, b = _FakeConn(), _FakeConn()
        self.assertEqual(dc.next_request_id(a), 1)
        self.assertEqual(dc.next_request_id(a), 2)
        self.assertEqual(dc.next_request_id(b), 1, "fresh conn restarts at 1")


class TestEnvelope(unittest.TestCase):

    def test_wire_shape_matches_the_old_hand_rolled_envelope(self):
        conn = _FakeConn()
        frame = json.loads(dc.mcp_envelope(
            conn, "self.robot.set_state", {"state": "sleep"}, 7,
        ))
        self.assertEqual(frame, {
            "session_id": "sess-1",
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "self.robot.set_state",
                    "arguments": {"state": "sleep"},
                },
                "id": 7,
            },
        })

    def test_missing_session_id_degrades_to_empty_string(self):
        conn = types.SimpleNamespace(websocket=_RecordingWS())
        frame = json.loads(dc.mcp_envelope(conn, "t", {}, 1))
        self.assertEqual(frame["session_id"], "")


class TestCallTool(unittest.TestCase):

    def test_sends_envelope_and_returns_id(self):
        async def go():
            conn = _FakeConn()
            rid1 = await dc.call_tool(conn, "self.camera.take_photo", {"question": "q"})
            rid2 = await dc.call_tool(conn, "self.robot.set_state", {"state": "idle"})
            self.assertEqual((rid1, rid2), (1, 2))
            frames = [json.loads(m) for m in conn.websocket.sent]
            self.assertEqual(
                [f["payload"]["id"] for f in frames], [1, 2],
            )
            self.assertEqual(
                frames[0]["payload"]["params"]["name"], "self.camera.take_photo",
            )
        asyncio.run(go())

    def test_concurrent_sends_are_serialized(self):
        async def go():
            ws = _SlowWS()
            conn = _FakeConn(ws=ws)
            await asyncio.gather(*[
                dc.call_tool(conn, "self.robot.set_head_angles", {"yaw": i})
                for i in range(5)
            ])
            self.assertEqual(ws.max_concurrent, 1, "sends must never overlap")
            ids = [json.loads(m)["payload"]["id"] for m in ws.sent]
            self.assertEqual(sorted(ids), [1, 2, 3, 4, 5])
        asyncio.run(go())

    def test_send_serialized_mixes_with_call_tool_under_one_lock(self):
        async def go():
            ws = _SlowWS()
            conn = _FakeConn(ws=ws)
            await asyncio.gather(
                dc.call_tool(conn, "t", {}),
                dc.send_serialized(conn, b"\x01\x02"),
                dc.call_tool(conn, "t", {}),
            )
            self.assertEqual(ws.max_concurrent, 1)
            self.assertEqual(len(ws.sent), 3)
        asyncio.run(go())


class TestHttpServerWiring(unittest.TestCase):
    """The admin MCP handlers route through the seam: monotonic ids on
    the wire, shared conn resolution, fire-and-forget HTTP semantics."""

    @classmethod
    def setUpClass(cls):
        stubbed = (
            "config", "config.logger", "core", "core.api",
            "core.api.ota_handler", "core.api.vision_handler",
            "core.portal_bridge", "core.utils", "core.utils.device_command",
        )
        missing = object()
        cls._saved = {k: sys.modules.get(k, missing) for k in stubbed}
        cls._missing = missing

        cls.active: dict = {}
        portal = MagicMock()
        portal.active_connections = cls.active
        logger_mod = MagicMock()
        logger_mod.setup_logging = lambda: MagicMock()
        for n in ("config", "core", "core.api", "core.api.ota_handler",
                  "core.api.vision_handler"):
            sys.modules[n] = MagicMock()
        sys.modules["config.logger"] = logger_mod
        sys.modules["core.portal_bridge"] = portal
        core_utils = MagicMock()
        core_utils.device_command = dc
        sys.modules["core.utils"] = core_utils
        sys.modules["core.utils.device_command"] = dc

        spec = _ilu.spec_from_file_location(
            "http_server_dc_under_test",
            _PATCHES / "http_server.py",
        )
        cls.mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(cls.mod)  # type: ignore[union-attr]

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._saved.items():
            if v is cls._missing:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def setUp(self):
        type(self).active.clear()

    def _server(self):
        return self.mod.SimpleHttpServer(
            {"server": {"ip": "0.0.0.0", "http_port": 8003}}
        )

    @staticmethod
    def _request(data):
        class _Req:
            async def json(self):
                return data
        return _Req()

    def test_set_state_sends_seam_envelope_with_monotonic_ids(self):
        async def go():
            conn = _FakeConn()
            conn.headers = {"device-id": "dev-1"}
            type(self).active["dev-1"] = conn
            srv = self._server()
            r1 = await srv._dotty_set_state(self._request({"state": "sleep"}))
            r2 = await srv._dotty_set_state(self._request({"state": "idle"}))
            self.assertEqual((r1.status, r2.status), (200, 200))
            # Sends are _spawn()-ed fire-and-forget; let the tasks run.
            await asyncio.sleep(0.02)
            frames = [json.loads(m) for m in conn.websocket.sent]
            self.assertEqual([f["payload"]["id"] for f in frames], [1, 2])
            self.assertEqual(
                [f["payload"]["params"]["arguments"]["state"] for f in frames],
                ["sleep", "idle"],
            )
        asyncio.run(go())

    def test_resolve_conn_falls_back_to_first_device_and_503s_when_empty(self):
        conn = _FakeConn()
        conn.headers = {"device-id": "dev-a"}
        type(self).active["dev-a"] = conn
        got, err = self.mod._dotty_resolve_conn("")
        self.assertIs(got, conn)
        self.assertIsNone(err)
        type(self).active.clear()
        got, err = self.mod._dotty_resolve_conn("")
        self.assertIsNone(got)
        self.assertEqual(err.status, 503)

    def test_no_ms_truncated_ids_left_anywhere(self):
        # The collision-prone id pattern must not reappear in either
        # patch surface (the audit found it at twelve sites).
        for path in (
            _PATCHES / "http_server.py",
            pathlib.Path(__file__).parent.parent / "receiveAudioHandle.py",
        ):
            src = path.read_text(encoding="utf-8")
            self.assertNotIn(
                "% 0x7FFFFFFF", src,
                f"ms-truncated MCP id pattern resurfaced in {path.name}",
            )


if __name__ == "__main__":
    unittest.main()
