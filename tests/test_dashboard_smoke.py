"""Tests for scripts/dashboard_smoke.py.

Parser edge cases use small fixtures; an end-to-end run renders the real
templates through the bridge app (TestClient) so a template change that
breaks a marker the checker relies on fails here, not on the tower.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_state_dir = Path(tempfile.mkdtemp(prefix="dotty-smoke-state-"))
os.environ.setdefault("DOTTY_KID_MODE_STATE", str(_state_dir / "kid-mode"))
os.environ.setdefault("DOTTY_SMART_MODE_STATE", str(_state_dir / "smart-mode"))

_repo_root = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


smoke = _load("dashboard_smoke", _repo_root / "scripts/dashboard_smoke.py")
bridge_app = _load("bridge_app", _repo_root / "bridge.py")


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


bridge_app.app.router.lifespan_context = _noop_lifespan

import bridge.dashboard as dash  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

R = smoke.Response


def _html(body: str, status: int = 200, ctype: str = "text/html") -> "smoke.Response":
    return R(status, ctype, body.encode())


class ParserFixtureTests(unittest.TestCase):

    def test_non_200_fails_everywhere(self):
        for parser in (smoke.parse_state, smoke.parse_kid_mode, smoke.parse_tools,
                       smoke.parse_status_strip, smoke.parse_songs):
            self.assertEqual(parser(_html("", 500))[0], smoke.FAIL)

    def test_status_strip(self):
        dots = "".join(
            f'<span data-dot="{d}" data-status="{s}" data-title="{t}"></span>'
            for d, s, t in (("bridge", "ok", "bridge: up"),
                            ("server", "ok", "server: ok"),
                            ("robot", "warn", "robot: perception sensors stale (1/1 dev)")))
        verdict, detail = smoke.parse_status_strip(_html(dots))
        self.assertEqual(verdict, smoke.WARN)
        self.assertIn("robot=warn", detail)
        self.assertIn("sensors stale", detail)
        verdict, detail = smoke.parse_status_strip(_html(dots.replace("warn", "ok")))
        self.assertEqual((verdict, detail), (smoke.PASS, "bridge=ok server=ok robot=ok"))
        self.assertEqual(smoke.parse_status_strip(_html(dots[:60]))[0], smoke.FAIL)

    def test_state(self):
        self.assertEqual(smoke.parse_state(_html('<div data-state="security">')),
                         (smoke.PASS, "security"))
        self.assertEqual(smoke.parse_state(_html('<div data-state="bogus">'))[0], smoke.FAIL)
        self.assertEqual(smoke.parse_state(_html("<div>"))[0], smoke.FAIL)

    def test_kid_and_smart_mode(self):
        self.assertEqual(
            smoke.parse_kid_mode(_html('<div data-kid-mode="on" data-kid-mode-saved="yes">')),
            (smoke.PASS, "on"))
        self.assertEqual(
            smoke.parse_kid_mode(_html('<div data-kid-mode="off" data-kid-mode-saved="no">'))[0],
            smoke.WARN)
        self.assertEqual(smoke.parse_kid_mode(_html("<div>"))[0], smoke.FAIL)
        self.assertEqual(smoke.parse_smart_mode(_html('<div data-smart-mode="off">')),
                         (smoke.PASS, "off"))

    def test_songs(self):
        two = '<input name="filename" value="a.opus"><input name="filename" value="b.mp3">'
        self.assertEqual(smoke.parse_songs(_html(two)), (smoke.PASS, "2 songs"))
        self.assertEqual(smoke.parse_songs(_html("No songs yet."))[0], smoke.WARN)
        self.assertEqual(
            smoke.parse_songs(_html('<div class="text-error/80">timed out</div>')),
            (smoke.WARN, "xiaozhi: timed out"))

    def test_tools_require_all_seven(self):
        full = "".join(f"<span>{n}</span>" for n in smoke.VOICE_TOOLS)
        self.assertEqual(smoke.parse_tools(_html(full)), (smoke.PASS, "7 tools"))
        verdict, detail = smoke.parse_tools(_html(full.replace(">play_song<", "><")))
        self.assertEqual(verdict, smoke.FAIL)
        self.assertIn("play_song", detail)

    def test_errors_chip(self):
        self.assertEqual(smoke.parse_alerts_chip(_html("Errors"))[0], smoke.PASS)
        self.assertEqual(smoke.parse_alerts_chip(_html("⚠Errors (3)")),
                         (smoke.WARN, "3 errored turns today"))

    def test_vision(self):
        self.assertEqual(smoke.parse_vision_photo(_html("", 404))[0], smoke.WARN)
        self.assertEqual(smoke.parse_vision_photo(R(200, "image/jpeg", b"\xff\xd8"))[0],
                         smoke.PASS)
        self.assertEqual(smoke.parse_vision_photo(R(200, "application/json", b"{}"))[0],
                         smoke.FAIL)
        self.assertEqual(smoke.parse_vision_large(_html("No recent photo."))[0], smoke.WARN)

    def test_robot_host_modal(self):
        body = ("<h3>Robot</h3><dl><dt>Connection</dt><dd>online</dd>"
                "<dt>Current state</dt><dd>sleep</dd>"
                "<dt>Perception</dt><dd>live (5s since last)</dd></dl>")
        verdict, detail = smoke.parse_host("robot")(_html(body))
        self.assertEqual(verdict, smoke.PASS)
        self.assertIn("state=sleep", detail)
        stale = body.replace("live (5s since last)", "stale (1/1 dev, oldest 9m)")
        self.assertEqual(smoke.parse_host("robot")(_html(stale))[0], smoke.WARN)

    def test_turn_stream(self):
        sse = "text/event-stream"
        none = R(200, sse, b"retry: 5000\n\n: heartbeat\n\n")
        self.assertEqual(smoke.parse_sse(none, count_turns=True)[0], smoke.WARN)
        two = R(200, sse, (
            b'retry: 5000\n\nid: 1\ndata: {"request_text":"hi","error":null,"replay":true}\n\n'
            b'id: 2\ndata: {"request_text":"x","error":"pi rpc timeout","replay":true}\n\n'))
        self.assertEqual(smoke.parse_sse(two, count_turns=True),
                         (smoke.PASS, "2 recent turns replayed (1 errored)"))
        self.assertEqual(smoke.parse_sse(R(200, "text/html", b"retry: 1"))[0], smoke.FAIL)
        self.assertEqual(smoke.parse_sse(R(200, sse, b"data: {}"))[0], smoke.FAIL)
        bad = R(200, sse, b"retry: 5000\n\ndata: {not json\n\n")
        self.assertEqual(smoke.parse_sse(bad, count_turns=True)[0], smoke.FAIL)

    def test_table_and_exit_code(self):
        results = [smoke.Result("a", "/a", smoke.PASS, "200", "fine"),
                   smoke.Result("b", "/b", smoke.WARN, "200", "hmm")]
        table = smoke.format_table(results)
        self.assertIn("check", table.splitlines()[0])
        self.assertIn("1 pass, 1 warn, 0 fail", table)
        self.assertEqual(smoke.exit_code(results), 0)
        self.assertEqual(smoke.exit_code(results, strict=True), 1)
        results.append(smoke.Result("c", "/c", smoke.FAIL, "500", "HTTP 500"))
        self.assertEqual(smoke.exit_code(results), 1)

    def test_unreachable_and_parser_crash_become_fail_rows(self):
        def boom(path, stream):
            raise ConnectionRefusedError("nope")
        results = smoke.run(boom)
        self.assertTrue(all(r.verdict == smoke.FAIL for r in results))
        self.assertEqual(len(results), len(smoke.CHECKS))

    def test_tool_list_matches_dashboard(self):
        self.assertEqual(tuple(t["name"] for t in dash._VOICE_TOOLS), smoke.VOICE_TOOLS)


class EndToEndAgainstBridgeApp(unittest.TestCase):
    """Run every check against the real templates rendered by bridge.py."""

    def setUp(self):
        saved = dict(dash._state)
        self.addCleanup(lambda: dash._state.update(saved))
        robot = "aa:bb:cc:dd:ee:ff"
        pstate = {robot: {"current_state": "story_time", "last_event_t": time.time(),
                          "sensor_age_s": 3.0, "sensor_stale": False, "sensor_seen": True}}
        dash._state.update(
            perception_state_getter=lambda: pstate,
            state_getter=lambda: "story_time",
            kid_mode_getter=lambda: True,
            kid_mode_saved_getter=lambda: True,
            smart_mode_getter=lambda: False,
            recent_turns_getter=lambda: [],
            vision_cache_getter=lambda: {},
            audio_cache_getter=lambda: {},
            scene_synthesis_cache_getter=lambda: {},
            perception_recent_getter=lambda device_id, limit=None: [],
        )
        original = dash._xiaozhi_connected_devices
        self.addCleanup(lambda: setattr(dash, "_xiaozhi_connected_devices", original))

        async def _connected():
            return [robot]

        dash._xiaozhi_connected_devices = _connected
        client = TestClient(bridge_app.app)
        self.methods: list[str] = []

        def fetch(path: str, stream: bool) -> "smoke.Response":
            self.methods.append("GET")
            if stream:  # SSE never ends; serve what a fresh connect sends
                return R(200, "text/event-stream; charset=utf-8",
                         b"retry: 5000\n\n: heartbeat\n\n")
            r = client.get(path)
            return R(r.status_code, r.headers.get("content-type", ""), r.content)

        self.results = {r.name: r for r in smoke.run(fetch)}

    def test_no_row_fails_against_the_real_templates(self):
        failing = {n: r.detail for n, r in self.results.items() if r.verdict == smoke.FAIL}
        self.assertEqual(failing, {})

    def test_key_values_are_parsed(self):
        self.assertEqual(self.results["state"].detail, "story_time")
        self.assertEqual(self.results["kid mode"].detail, "on")
        self.assertEqual(self.results["smart mode"].detail, "off")
        self.assertEqual(self.results["tools inventory"].detail, "7 tools")
        # No XIAOZHI_HOST in the test env, so only the server dot is grey.
        self.assertIn("bridge=ok server=unknown robot=ok",
                      self.results["status strip"].detail)
        self.assertIn("state=story_time", self.results["host: robot"].detail)
        # No photo and no xiaozhi in the test env: reachable but worth a look.
        self.assertEqual(self.results["vision photo"].verdict, smoke.WARN)
        self.assertEqual(self.results["songs"].verdict, smoke.WARN)


class _SSEHandler(BaseHTTPRequestHandler):
    methods: list[str] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).methods.append("GET")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b'retry: 5000\n\nid: 7\ndata: {"request_text":"hi"}\n\n')
        self.wfile.flush()
        time.sleep(5)  # an idle stream: the fetcher must give up on its own


class FetcherTests(unittest.TestCase):

    def test_stream_window_bounds_an_idle_stream(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        fetch = smoke.make_fetcher(f"http://127.0.0.1:{server.server_port}",
                                   stream_window=0.5)
        started = time.monotonic()
        resp = fetch("/ui/events", True)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(smoke.parse_sse(resp, count_turns=True),
                         (smoke.PASS, "1 recent turns replayed (0 errored)"))
        self.assertEqual(_SSEHandler.methods, ["GET"])


if __name__ == "__main__":
    unittest.main()
