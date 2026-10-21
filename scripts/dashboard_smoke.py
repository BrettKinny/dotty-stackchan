#!/usr/bin/env python3
"""Read-only smoke check for the Dotty admin dashboard (bridge.py, /ui).

Fetches every page, fragment and stream the dashboard uses and checks each
returns 200 with its key content: State, Kid Mode, Smart Mode, Perception,
Vision, Songs, Memory, the voice tools inventory, the Turns stream, the
status strip and the host modals. GET only — it never posts, toggles,
injects or opens the robot's mic. Stdlib only, so it runs anywhere.

    python3 scripts/dashboard_smoke.py                       # default host
    python3 scripts/dashboard_smoke.py http://<XIAOZHI_HOST>:8081
    DOTTY_DASHBOARD_USER=u DOTTY_DASHBOARD_PASS=p python3 scripts/dashboard_smoke.py

Each row is PASS, WARN (reachable and well-formed, but reporting something
worth a look: robot dot amber, no photo yet, xiaozhi unreachable) or FAIL
(non-200, wrong content, unreachable). Exit status is 1 if any row FAILs
(or WARNs, with --strict), else 0.
"""

from __future__ import annotations

import argparse
import base64
import html
import http.client
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass
from typing import Callable

# No built-in host: pass the URL or set DOTTY_DASHBOARD_URL (e.g.
# http://<XIAOZHI_HOST>:8081).
DEFAULT_BASE_URL = os.environ.get("DOTTY_DASHBOARD_URL", "")

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

STATES = ("idle", "talk", "story_time", "security", "sleep", "dance")
# Keep in step with dotty-pi-ext/src/tools/ (tests pin bridge/dashboard.py's
# _VOICE_TOOLS to that directory, and this list to _VOICE_TOOLS).
VOICE_TOOLS = (
    "memory_lookup", "remember", "recall_person", "remember_person",
    "think_hard", "take_photo", "play_song",
)


@dataclass
class Response:
    status: int
    content_type: str
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@dataclass
class Result:
    name: str
    path: str
    verdict: str
    http: str
    detail: str


Fetcher = Callable[[str, bool], Response]


def _attr(text: str, name: str) -> str | None:
    m = re.search(rf'{re.escape(name)}="([^"]*)"', text)
    return html.unescape(m.group(1)) if m else None


def _need_200(resp: Response) -> tuple[str, str] | None:
    if resp.status != 200:
        return FAIL, f"HTTP {resp.status}"
    return None


# --- Parsers: Response -> (verdict, detail). Pure, unit-tested. -----------

def parse_health(resp: Response) -> tuple[str, str]:
    if (bad := _need_200(resp)):
        return bad
    try:
        data = json.loads(resp.text)
    except ValueError:
        return FAIL, "not JSON"
    if data.get("status") != "ok":
        return FAIL, f"status={data.get('status')!r}"
    return PASS, f"service={data.get('service', '?')}"


def parse_page(resp: Response) -> tuple[str, str]:
    if (bad := _need_200(resp)):
        return bad
    t = resp.text
    missing = [label for label, marker in (
        ("csrf meta", 'name="csrf-token"'),
        ("state card", 'id="state-card-body"'),
        ("kid card", 'id="kid-card-body"'),
        ("turn stream", "EventSource('/ui/events')"),
        ("perception feed", "EventSource('/ui/perception/feed')"),
    ) if marker not in t]
    if missing:
        return FAIL, "missing " + ", ".join(missing)
    return PASS, "dashboard shell"


def parse_status_strip(resp: Response) -> tuple[str, str]:
    if (bad := _need_200(resp)):
        return bad
    dots = {
        m.group(1): (m.group(2), html.unescape(m.group(3)))
        for m in re.finditer(
            r'data-dot="([^"]+)" data-status="([^"]+)" data-title="([^"]*)"',
            resp.text,
        )
    }
    missing = [d for d in ("bridge", "server", "robot") if d not in dots]
    if missing:
        return FAIL, "missing dots: " + ", ".join(missing)
    summary = " ".join(f"{d}={dots[d][0]}" for d in ("bridge", "server", "robot"))
    off = [d for d in ("bridge", "server", "robot") if dots[d][0] != "ok"]
    if off:
        return WARN, summary + " | " + "; ".join(dots[d][1] for d in off)
    return PASS, summary


def parse_state(resp: Response) -> tuple[str, str]:
    if (bad := _need_200(resp)):
        return bad
    state = _attr(resp.text, "data-state")
    if state not in STATES:
        return FAIL, f"data-state={state!r}"
    if "State control not available" in resp.text:
        return WARN, f"{state} (state control not available)"
    return PASS, state


def parse_kid_mode(resp: Response) -> tuple[str, str]:
    if (bad := _need_200(resp)):
        return bad
    mode = _attr(resp.text, "data-kid-mode")
    if mode not in ("on", "off"):
        return FAIL, f"data-kid-mode={mode!r}"
    if _attr(resp.text, "data-kid-mode-saved") == "no":
        return WARN, f"{mode} (default, no saved policy)"
    return PASS, mode


def parse_smart_mode(resp: Response) -> tuple[str, str]:
    if (bad := _need_200(resp)):
        return bad
    mode = _attr(resp.text, "data-smart-mode")
    if mode not in ("on", "off"):
        return FAIL, f"data-smart-mode={mode!r}"
    return PASS, mode


def parse_perception(resp: Response) -> tuple[str, str]:
    if (bad := _need_200(resp)):
        return bad
    t = resp.text
    if 'id="perception-card-body"' not in t or "Perception" not in t:
        return FAIL, "perception card missing"
    if "Sleeping" in t:
        return PASS, "sleeping"
    return PASS, "sight + sound"


def parse_vision_large(resp: Response) -> tuple[str, str]:
    if (bad := _need_200(resp)):
        return bad
    if 'src="/ui/vision/photo' in resp.text:
        return PASS, "photo shown"
    if "No recent photo" in resp.text:
        return WARN, "no recent photo"
    return FAIL, "neither photo nor empty state"


def parse_vision_photo(resp: Response) -> tuple[str, str]:
    if resp.status == 404:
        return WARN, "no photo cached"
    if (bad := _need_200(resp)):
        return bad
    if not resp.content_type.startswith("image/"):
        return FAIL, f"content-type {resp.content_type!r}"
    return PASS, f"{len(resp.body)} bytes"


def parse_songs(resp: Response) -> tuple[str, str]:
    if (bad := _need_200(resp)):
        return bad
    t = resp.text
    n = t.count('name="filename"')
    if n:
        return PASS, f"{n} songs"
    if "No songs yet" in t:
        return WARN, "catalogue empty"
    if "text-error" in t:
        msg = re.sub(r"<[^>]+>", "", t).strip()
        return WARN, f"xiaozhi: {msg[:80]}"
    return FAIL, "unrecognised songs fragment"


def parse_memory(resp: Response) -> tuple[str, str]:
    if (bad := _need_200(resp)):
        return bad
    t = resp.text
    if "Person memory" not in t:
        return FAIL, "memory card missing"
    if "Memory store not available" in t:
        return WARN, "memory store not available"
    pending = re.search(r"(\d+) awaiting review", t)
    return PASS, f"{pending.group(1)} awaiting review" if pending else "no pending"


def parse_tools(resp: Response) -> tuple[str, str]:
    if (bad := _need_200(resp)):
        return bad
    missing = [n for n in VOICE_TOOLS if f">{n}<" not in resp.text]
    if missing:
        return FAIL, "missing " + ", ".join(missing)
    return PASS, f"{len(VOICE_TOOLS)} tools"


def parse_safety(resp: Response) -> tuple[str, str]:
    if (bad := _need_200(resp)):
        return bad
    if "Content filter" not in resp.text:
        return FAIL, "safety card missing"
    return PASS, f"{resp.text.count('<li')} recent hits"


def parse_alerts_chip(resp: Response) -> tuple[str, str]:
    if (bad := _need_200(resp)):
        return bad
    t = resp.text
    if "Errors" not in t:
        return FAIL, "errors chip missing"
    m = re.search(r"Errors \((\d+)\)", t)
    if m:
        return WARN, f"{m.group(1)} errored turns today"
    return PASS, "no errored turns today"


def parse_ok_200(label: str) -> Callable[[Response], tuple[str, str]]:
    def _parse(resp: Response) -> tuple[str, str]:
        if (bad := _need_200(resp)):
            return bad
        return PASS, label
    return _parse


def parse_device_status(resp: Response) -> tuple[str, str]:
    if (bad := _need_200(resp)):
        return bad
    state = _attr(resp.text, "data-device-state")
    if state == "online":
        return PASS, _attr(resp.text, "title") or "online"
    if state in ("offline", "unknown"):
        return WARN, _attr(resp.text, "title") or state
    return FAIL, f"data-device-state={state!r}"


def _facts(text: str) -> dict[str, str]:
    pairs = re.findall(r"<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", text, re.S)
    return {html.unescape(k.strip()): html.unescape(v.strip()) for k, v in pairs}


def parse_host(slug: str) -> Callable[[Response], tuple[str, str]]:
    def _parse(resp: Response) -> tuple[str, str]:
        if (bad := _need_200(resp)):
            return bad
        facts = _facts(resp.text)
        if not facts:
            return FAIL, "no facts"
        if slug == "robot":
            state = facts.get("Current state", "?")
            perception = facts.get("Perception", "?")
            detail = (f"{facts.get('Connection', '?')}, state={state}, "
                      f"perception={perception}")
            if state not in STATES:
                return FAIL, detail
            return (WARN if perception.startswith("stale") else PASS), detail
        if slug == "server":
            return PASS, f"devices={facts.get('Devices connected', '?')}"
        return PASS, (f"kid={facts.get('Kid Mode', '?')} "
                      f"smart={facts.get('Smart Mode', '?')}")
    return _parse


def parse_sse(resp: Response, *, count_turns: bool = False) -> tuple[str, str]:
    """An SSE stream must open with 200, text/event-stream and the retry
    hint. For /ui/events, count the replayed turns it sends on connect."""
    if (bad := _need_200(resp)):
        return bad
    if not resp.content_type.startswith("text/event-stream"):
        return FAIL, f"content-type {resp.content_type!r}"
    t = resp.text
    if not t.startswith("retry: "):
        return FAIL, "no retry hint"
    if not count_turns:
        n = t.count("\ndata: ") + t.startswith("data: ")
        return PASS, f"stream open, {n} events in window"
    turns = errors = 0
    for line in t.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            data = json.loads(line[6:])
        except ValueError:
            return FAIL, "malformed turn frame"
        turns += 1
        errors += bool(data.get("error"))
    if not turns:
        return WARN, "stream open, no turns since bridge start"
    return PASS, f"{turns} recent turns replayed ({errors} errored)"


def security_path(robot_modal: Response) -> str | None:
    """The robot modal's scene panel polls /ui/security/recent/<device>."""
    m = re.search(r'hx-get="(/ui/security/recent/[^"]+)"', robot_modal.text)
    return html.unescape(m.group(1)) if m else None


# name, path, parser, is_stream
CHECKS: list[tuple[str, str, Callable[[Response], tuple[str, str]], bool]] = [
    ("health", "/health", parse_health, False),
    ("dashboard page", "/ui", parse_page, False),
    ("status strip", "/ui/status-strip?placement=header", parse_status_strip, False),
    ("device status", "/ui/device-status", parse_device_status, False),
    ("version chip", "/ui/version-chip", parse_ok_200("rendered"), False),
    ("state", "/ui/state", parse_state, False),
    ("kid mode", "/ui/kid-mode", parse_kid_mode, False),
    ("smart mode", "/ui/smart-mode", parse_smart_mode, False),
    ("perception", "/ui/perception", parse_perception, False),
    ("vision modal", "/ui/vision/large", parse_vision_large, False),
    ("vision photo", "/ui/vision/photo", parse_vision_photo, False),
    ("songs", "/ui/songs", parse_songs, False),
    ("memory", "/ui/memory", parse_memory, False),
    ("tools inventory", "/ui/tools-inventory", parse_tools, False),
    ("content filter", "/ui/safety/recent", parse_safety, False),
    ("errors chip", "/ui/alerts/count?chip=1", parse_alerts_chip, False),
    ("errors report", "/ui/alerts/detail", parse_ok_200("rendered"), False),
    ("host: bridge", "/ui/host/bridge", parse_host("bridge"), False),
    ("host: server", "/ui/host/server", parse_host("server"), False),
    ("host: robot", "/ui/host/robot", parse_host("robot"), False),
    ("turns stream", "/ui/events", lambda r: parse_sse(r, count_turns=True), True),
    ("perception feed", "/ui/perception/feed", parse_sse, True),
    ("htmx asset", "/ui/static/htmx.min.js", parse_ok_200("served"), False),
    ("manifest", "/ui/manifest.json", parse_ok_200("served"), False),
]


def make_fetcher(base_url: str, *, timeout: float = 5.0,
                 stream_window: float = 2.0,
                 auth: tuple[str, str] | None = None) -> Fetcher:
    """GET-only fetcher. Streams are read for ``stream_window`` seconds and
    then torn down with a socket shutdown, so the server sees the client
    go away at once: an SSE handler left running on a half-open socket can
    otherwise hold a single-worker bridge and starve the next request."""
    base = base_url.rstrip("/")
    headers = {"User-Agent": "dotty-dashboard-smoke/1"}
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"

    def fetch_page(path: str) -> Response:
        req = urllib.request.Request(base + path, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return Response(resp.status, resp.headers.get("Content-Type", ""),
                                resp.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return Response(exc.code, exc.headers.get("Content-Type", ""),
                                exc.read() or b"")

    def fetch_stream(path: str) -> Response:
        parts = urlsplit(base)
        conn_cls = (http.client.HTTPSConnection if parts.scheme == "https"
                    else http.client.HTTPConnection)
        conn = conn_cls(parts.hostname or "", parts.port, timeout=timeout)
        sock: socket.socket | None = None
        resp: http.client.HTTPResponse | None = None
        try:
            conn.connect()
            sock = conn.sock
            conn.request("GET", parts.path + path, headers={
                **headers, "Accept": "text/event-stream", "Connection": "close",
            })
            resp = conn.getresponse()
            ctype = resp.headers.get("Content-Type", "")
            body = b""
            deadline = time.monotonic() + stream_window
            while (remaining := deadline - time.monotonic()) > 0:
                if sock is not None:
                    sock.settimeout(remaining)
                try:
                    chunk = resp.read1(65536)
                except (TimeoutError, OSError, http.client.HTTPException):
                    break  # window over; a timed-out socket is unusable
                if not chunk:
                    break
                body += chunk
            return Response(resp.status, ctype, body)
        finally:
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            if resp is not None:
                resp.close()
            conn.close()
            if sock is not None:
                sock.close()

    def fetch(path: str, stream: bool) -> Response:
        return fetch_stream(path) if stream else fetch_page(path)

    return fetch


# Fragments whose stable data attribute was added after 7844b30. Against an
# older bridge the attribute is simply absent: say so instead of failing.
NEW_MARKERS: dict[str, tuple[str, str]] = {
    "kid mode": ("data-kid-mode", "80fae86"),
    "smart mode": ("data-smart-mode", "80fae86"),
    "status strip": ("data-dot", "cf183ee"),
}


def running_commit(version_chip: Response | None) -> str:
    """The bridge build the version chip links to, or "unknown"."""
    if version_chip is None:
        return "unknown"
    m = re.search(r"/commit/([0-9a-fA-F]{7,40})", version_chip.text)
    return m.group(1)[:12] if m else "unknown"


def _old_build(result: Result, resp: Response | None, commit: str) -> Result:
    marker = NEW_MARKERS.get(result.name)
    if (marker is None or result.verdict != FAIL or resp is None
            or resp.status != 200 or marker[0] in resp.text):
        return result
    return Result(result.name, result.path, WARN, result.http,
                  f"no {marker[0]}: old dashboard build? running {commit}, "
                  f"attribute added in {marker[1]}")


def run(fetch: Fetcher) -> list[Result]:
    """Run every check. Plain fetches go first and the SSE streams last,
    one at a time, so a stream that lingers server-side cannot starve the
    page and fragment checks."""
    results: list[Result] = []
    responses: dict[str, Response | None] = {}

    def _run(checks) -> None:
        for name, path, parser, stream in checks:
            result, resp = _check(fetch, name, path, parser, stream)
            results.append(result)
            responses[name] = resp

    _run([c for c in CHECKS if not c[3]])
    # The robot modal's scene panel is only rendered once a photo exists.
    robot_modal = responses.get("host: robot")
    sec = security_path(robot_modal) if robot_modal else None
    if sec:
        results.append(_check(fetch, "robot scene panel", sec,
                              parse_ok_200("rendered"), False)[0])
    _run([c for c in CHECKS if c[3]])
    commit = running_commit(responses.get("version chip"))
    return [_old_build(r, responses.get(r.name), commit) for r in results]


def _check(fetch: Fetcher, name: str, path: str,
           parser: Callable[[Response], tuple[str, str]],
           stream: bool) -> tuple[Result, Response | None]:
    try:
        resp = fetch(path, stream)
    except Exception as exc:
        return Result(name, path, FAIL, "-", f"unreachable: {exc}"), None
    try:
        verdict, detail = parser(resp)
    except Exception as exc:  # a parser bug must not hide the other rows
        verdict, detail = FAIL, f"parse error: {exc}"
    return Result(name, path, verdict, str(resp.status), detail), resp


def format_table(results: list[Result]) -> str:
    headers = ("check", "path", "result", "http", "detail")
    rows = [(r.name, r.path, r.verdict, r.http, r.detail) for r in results]
    widths = [max(len(h), *(len(row[i]) for row in rows)) if rows else len(h)
              for i, h in enumerate(headers[:-1])]
    def fmt(row) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(row[:-1], widths)) + "  " + row[-1]
    lines = [fmt(headers), fmt(tuple("-" * w for w in widths) + ("-" * 6,))]
    lines += [fmt(row) for row in rows]
    counts = {v: sum(r.verdict == v for r in results) for v in (PASS, WARN, FAIL)}
    lines.append("")
    lines.append(f"{counts[PASS]} pass, {counts[WARN]} warn, {counts[FAIL]} fail")
    return "\n".join(lines)


def exit_code(results: list[Result], *, strict: bool = False) -> int:
    bad = {FAIL, WARN} if strict else {FAIL}
    return 1 if any(r.verdict in bad for r in results) else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("base_url", nargs="?", default=DEFAULT_BASE_URL,
                    help="dashboard base URL, e.g. http://<XIAOZHI_HOST>:8081 "
                         "(default: $DOTTY_DASHBOARD_URL)")
    ap.add_argument("--timeout", type=float, default=5.0,
                    help="per-request timeout in seconds")
    ap.add_argument("--stream-window", type=float, default=2.0,
                    help="seconds to read each SSE stream before closing it")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 on WARN rows too")
    args = ap.parse_args(argv)
    if not args.base_url:
        ap.error("pass the dashboard base URL or set DOTTY_DASHBOARD_URL")
    user = os.environ.get("DOTTY_DASHBOARD_USER", "")
    password = os.environ.get("DOTTY_DASHBOARD_PASS", "")
    fetch = make_fetcher(args.base_url, timeout=args.timeout,
                         stream_window=args.stream_window,
                         auth=(user, password) if user and password else None)
    results = run(fetch)
    print(f"Dotty dashboard smoke check: {args.base_url}\n")
    print(format_table(results))
    return exit_code(results, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
