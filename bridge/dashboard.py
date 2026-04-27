"""Mobile-first admin dashboard for Dotty.

Mounted at ``/ui`` on the bridge FastAPI app. Host status cards,
conversation log tail, action endpoints, SSE turn stream.

Host probes are env-driven so this stays generic in the public template:
set ``XIAOZHI_HOST`` (and optionally ``WORKSTATION_HOST``) on the bridge
service. Cards for unset hosts render as "unknown".
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import time
import requests
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

log = logging.getLogger("dashboard")

# Bridge wires its in-process message handler in via configure(). Lets the
# "Say" action invoke the same path /api/message uses without an HTTP hop.
_state: dict[str, Any] = {
    "send_message": None,
    "vision_cache": None,
    "kid_mode_getter": None,
    "kid_mode_setter": None,
    "smart_mode_getter": None,
    "smart_mode_setter": None,
    "state_getter": None,
    "state_setter": None,
    "inject_to_device": None,
    "abort_device": None,
    "subscribe_events": None,
    "unsubscribe_events": None,
}


def configure(*, send_message: Any = None, vision_cache: dict | None = None,
              kid_mode_getter: Any = None, kid_mode_setter: Any = None,
              smart_mode_getter: Any = None, smart_mode_setter: Any = None,
              state_getter: Any = None, state_setter: Any = None,
              inject_to_device: Any = None, abort_device: Any = None,
              subscribe_events: Any = None,
              unsubscribe_events: Any = None) -> None:
    """Register bridge state with the dashboard. Idempotent."""
    if send_message is not None:
        _state["send_message"] = send_message
    if vision_cache is not None:
        _state["vision_cache"] = vision_cache
    if kid_mode_getter is not None:
        _state["kid_mode_getter"] = kid_mode_getter
    if kid_mode_setter is not None:
        _state["kid_mode_setter"] = kid_mode_setter
    if smart_mode_getter is not None:
        _state["smart_mode_getter"] = smart_mode_getter
    if smart_mode_setter is not None:
        _state["smart_mode_setter"] = smart_mode_setter
    if state_getter is not None:
        _state["state_getter"] = state_getter
    if state_setter is not None:
        _state["state_setter"] = state_setter
    if inject_to_device is not None:
        _state["inject_to_device"] = inject_to_device
    if abort_device is not None:
        _state["abort_device"] = abort_device
    if subscribe_events is not None:
        _state["subscribe_events"] = subscribe_events
    if unsubscribe_events is not None:
        _state["unsubscribe_events"] = unsubscribe_events

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


_BRIDGE_VERSION_FILE = Path(__file__).parent.parent / ".bridge-version"


def _read_bridge_version() -> str:
    """Short git SHA of the deployed bridge. Cached at module load — picks
    up changes on the next systemd restart, which `Update from GitHub` does
    automatically. Reads `.bridge-version` next to bridge.py first (written
    by `Update from GitHub` since the install dir isn't a git checkout);
    falls back to `git rev-parse` for dev installs that *are* git
    checkouts."""
    try:
        if _BRIDGE_VERSION_FILE.exists():
            v = _BRIDGE_VERSION_FILE.read_text().strip()
            if v:
                return v[:12]
    except OSError:
        pass
    try:
        import subprocess
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).parent.parent),
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return out.decode().strip() or "unknown"
    except Exception:
        return "unknown"


BRIDGE_VERSION = _read_bridge_version()

# Opt-in HTTP Basic auth. If both env vars are set, every /ui route requires
# them. Unset → no auth (preserves current LAN-only behaviour).
_DASHBOARD_USER = os.environ.get("DOTTY_DASHBOARD_USER", "")
_DASHBOARD_PASS = os.environ.get("DOTTY_DASHBOARD_PASS", "")
_basic = HTTPBasic(auto_error=False)


def _verify_dashboard_auth(
    credentials: HTTPBasicCredentials | None = Depends(_basic),
) -> None:
    if not _DASHBOARD_USER or not _DASHBOARD_PASS:
        return
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="dotty"'},
        )
    user_ok = secrets.compare_digest(credentials.username, _DASHBOARD_USER)
    pass_ok = secrets.compare_digest(credentials.password, _DASHBOARD_PASS)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="dotty"'},
        )


router = APIRouter(
    prefix="/ui", tags=["dashboard"],
    dependencies=[Depends(_verify_dashboard_auth)],
)

XIAOZHI_HOST = os.environ.get("XIAOZHI_HOST", "")
XIAOZHI_OTA_PORT = int(os.environ.get("XIAOZHI_OTA_PORT", "8003"))
XIAOZHI_WS_PORT = int(os.environ.get("XIAOZHI_WS_PORT", "8000"))
LOG_DIR = Path(os.environ.get("CONVO_LOG_DIR", "/root/zeroclaw-bridge/logs"))
VOICE_CHANNELS = ("dotty", "stackchan")

_START_TIME = time.time()

_probe_cache: dict[str, tuple[float, bool]] = {}
_PROBE_TTL = 8.0


async def _tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    if not host:
        return False
    key = f"{host}:{port}"
    now = time.monotonic()
    cached = _probe_cache.get(key)
    if cached and now - cached[0] < _PROBE_TTL:
        return cached[1]
    try:
        fut = asyncio.open_connection(host, port)
        _, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        ok = True
    except Exception:
        ok = False
    _probe_cache[key] = (now, ok)
    return ok


def _humanize_age(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _today_log_path() -> Path:
    return _log_path_for(datetime.now().strftime("%Y-%m-%d"))


def _log_path_for(date_str: str) -> Path:
    return LOG_DIR / f"convo-{date_str}.ndjson"


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _safe_date(date_str: str | None) -> str:
    """Validate ?date= query; fall back to today on anything weird."""
    if date_str and _DATE_RE.match(date_str):
        return date_str
    return datetime.now().strftime("%Y-%m-%d")


def _looks_like_xiaozhi_system_msg(text: str) -> bool:
    """Heuristic: voice-channel turns whose user payload is mostly Chinese
    are xiaozhi-server's automated wrap-up / system-injected prompts, not
    something the kid actually said. Filter them out of the dashboard log
    so the conversation history stays readable."""
    if not text:
        return False
    cjk = sum(1 for c in text if 0x4E00 <= ord(c) <= 0x9FFF)
    return cjk >= 3 and cjk / max(1, len(text)) > 0.3


def _clean_request_text(s: str) -> str:
    """Strip the wrapped `[Context] ... [User] <payload>` preamble.

    Voice turns from xiaozhi-server arrive with a long persona/context
    block prepended. The actual user utterance lives after the `[User]`
    marker, sometimes as raw text and sometimes as a JSON object with
    a `content` field. Returns the original text if no marker is found.
    """
    if not s:
        return s
    idx = s.rfind("[User]")
    if idx == -1:
        return s
    after = s[idx + len("[User]"):].strip()
    if after.startswith("{"):
        try:
            obj = json.loads(after)
            if isinstance(obj, dict) and "content" in obj:
                return str(obj["content"]).strip()
        except Exception:
            pass
    return after


def _parse_ts(ts: str) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _stackchan_last_seen() -> float | None:
    """Timestamp of the most recent voice-channel turn in today's log."""
    path = _today_log_path()
    if not path.exists():
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    last_voice_ts: float | None = None
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("channel") in VOICE_CHANNELS:
            ts = _parse_ts(rec.get("ts", ""))
            if ts is not None:
                last_voice_ts = ts
    return last_voice_ts


def _read_recent_log_entries(date_str: str, limit: int = 20) -> list[dict[str, Any]]:
    path = _log_path_for(date_str)
    if not path.exists():
        return []
    try:
        lines = path.read_bytes().splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        ts = rec.get("ts", "")
        try:
            time_str = datetime.fromisoformat(
                ts.replace("Z", "+00:00")
            ).astimezone().strftime("%H:%M:%S")
        except Exception:
            time_str = ts[-8:] if ts else "?"
        cleaned_request = _clean_request_text(rec.get("request_text") or "")
        if _looks_like_xiaozhi_system_msg(cleaned_request):
            continue
        out.append({
            "time": time_str,
            "channel": rec.get("channel") or "?",
            "request": cleaned_request[:400],
            "response": (rec.get("response_text") or "")[:1000],
            "latency_ms": rec.get("latency_ms", "?"),
            "error": rec.get("error"),
        })
        if len(out) >= limit:
            break
    return out


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request) -> Any:
    return templates.TemplateResponse(
        request, "dashboard.html",
        {"version": BRIDGE_VERSION},
    )


@router.get("/cards", response_class=HTMLResponse, include_in_schema=False)
async def cards(request: Request) -> Any:
    bridge_uptime = time.time() - _START_TIME

    xiaozhi_ota_ok, xiaozhi_ws_ok = await asyncio.gather(
        _tcp_reachable(XIAOZHI_HOST, XIAOZHI_OTA_PORT),
        _tcp_reachable(XIAOZHI_HOST, XIAOZHI_WS_PORT),
    )

    last_seen_ts = _stackchan_last_seen()
    if last_seen_ts is None:
        sc_status, sc_detail, sc_last = "unknown", "no voice activity today", ""
    else:
        age = time.time() - last_seen_ts
        if age < 600:
            sc_status, sc_detail = "ok", "active"
        elif age < 86400:
            sc_status, sc_detail = "warn", "idle"
        else:
            sc_status, sc_detail = "bad", "stale"
        sc_last = f"{_humanize_age(age)} ago"

    if not XIAOZHI_HOST:
        xiaozhi_status = "unknown"
        xiaozhi_detail = "XIAOZHI_HOST env not set"
    elif xiaozhi_ota_ok and xiaozhi_ws_ok:
        xiaozhi_status = "ok"
        xiaozhi_detail = f"OTA :{XIAOZHI_OTA_PORT} + WS :{XIAOZHI_WS_PORT}"
    elif xiaozhi_ota_ok or xiaozhi_ws_ok:
        xiaozhi_status = "warn"
        xiaozhi_detail = "partial: " + (
            f"OTA :{XIAOZHI_OTA_PORT}" if xiaozhi_ota_ok else f"WS :{XIAOZHI_WS_PORT}"
        )
    else:
        xiaozhi_status = "bad"
        xiaozhi_detail = "no ports responding"

    cards_data = [
        {"name": "StackChan", "kind": "robot", "status": sc_status,
         "detail": sc_detail, "last_seen": sc_last},
        {"name": "ZeroClaw bridge", "kind": "host", "status": "ok",
         "detail": f"bridge up {_humanize_age(bridge_uptime)}", "last_seen": ""},
        {"name": "xiaozhi-server", "kind": "host", "status": xiaozhi_status,
         "detail": xiaozhi_detail, "last_seen": ""},
    ]
    return templates.TemplateResponse(
        request, "cards.html", {"cards": cards_data}
    )


_ALLOWED_EMOJIS = ("😊", "😆", "😢", "😮", "🤔", "😠", "😐", "😍", "😴")

# Songs live on the xiaozhi-server filesystem at this absolute container path
# (host: /mnt/user/appdata/xiaozhi-server/songs/, mounted :ro). The bridge
# never touches the files itself — it asks xiaozhi to list them via the admin
# endpoint, and asks xiaozhi to play one via /xiaozhi/admin/play-asset.
_SONGS_BASE_PATH = "/opt/xiaozhi-esp32-server/config/assets/songs"
_SONG_OK_EXT = {".opus", ".ogg", ".wav", ".mp3"}


async def _xiaozhi_device_count() -> int | None:
    """Count active StackChan WS connections via the admin endpoint.
    Returns None if xiaozhi is unreachable."""
    if not XIAOZHI_HOST:
        return None
    url = f"http://{XIAOZHI_HOST}:{XIAOZHI_OTA_PORT}/xiaozhi/admin/devices"
    import urllib.request
    def _fetch() -> int | None:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status != 200:
                    return None
                data = json.loads(r.read())
                return len(data.get("devices", []))
        except Exception:
            return None
    return await asyncio.to_thread(_fetch)


@router.get("/device-status", response_class=HTMLResponse, include_in_schema=False)
async def device_status(request: Request) -> Any:
    n = await _xiaozhi_device_count()
    if n is None:
        return templates.TemplateResponse(
            request, "device_status.html",
            {"state": "unknown", "title": "xiaozhi-server unreachable"},
        )
    if n == 0:
        return templates.TemplateResponse(
            request, "device_status.html",
            {"state": "offline", "title": "Dotty offline (sleep / WiFi drop)"},
        )
    return templates.TemplateResponse(
        request, "device_status.html",
        {"state": "online", "title": f"Dotty online ({n} device)"},
    )


@router.get("/alerts/count", response_class=HTMLResponse, include_in_schema=False)
async def alerts_count(request: Request) -> Any:
    """Q6: count today's errored turns from the convo log so the header
    badge shows it."""
    today = datetime.now().strftime("%Y-%m-%d")
    path = _log_path_for(today)
    n = 0
    if path.exists():
        try:
            for line in path.read_bytes().splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("error"):
                    n += 1
        except OSError:
            pass
    return templates.TemplateResponse(
        request, "alerts_badge.html",
        {"count": n},
    )


@router.get("/alerts/detail", response_class=HTMLResponse, include_in_schema=False)
async def alerts_detail(request: Request) -> Any:
    """F13: render today's errored turns. Opened via the alerts-badge
    modal in dashboard.html."""
    today = datetime.now().strftime("%Y-%m-%d")
    path = _log_path_for(today)
    entries: list[dict[str, Any]] = []
    if path.exists():
        try:
            lines = path.read_bytes().splitlines()
        except OSError:
            lines = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not rec.get("error"):
                continue
            ts = rec.get("ts", "")
            try:
                time_str = datetime.fromisoformat(
                    ts.replace("Z", "+00:00")
                ).astimezone().strftime("%H:%M:%S")
            except Exception:
                time_str = ts[-8:] if ts else "?"
            entries.append({
                "time": time_str,
                "channel": rec.get("channel") or "?",
                "request": _clean_request_text(rec.get("request_text") or "")[:400],
                "response": (rec.get("response_text") or "")[:300],
                "error": str(rec.get("error"))[:500],
            })
    return templates.TemplateResponse(
        request, "alerts_detail.html",
        {"entries": entries},
    )


@router.post("/actions/mood", response_class=HTMLResponse, include_in_schema=False)
async def mood(request: Request, emoji: str = Form(...)) -> Any:
    if emoji not in _ALLOWED_EMOJIS:
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False, "error": "Unknown emoji."},
        )
    prompt = f"Make the {emoji} face. Reply with just '{emoji} ok'."
    return await _inject_or_error(request, prompt, label=f"make the {emoji} face")


@router.post("/actions/dance", response_class=HTMLResponse, include_in_schema=False)
async def dance(request: Request) -> Any:
    """Single 'Dance & sing' button — LLM picks the bit. Replaces the old
    macarena/sing key-driven endpoint (now collapsed into one phrase)."""
    return await _inject_or_error(
        request,
        "do a dance and sing a song",
        label="dance & sing",
    )


async def _xiaozhi_list_songs() -> tuple[list[str], str | None]:
    """Fetch the song-file list from xiaozhi's admin endpoint. Returns
    (files, error). Error is None on success."""
    if not XIAOZHI_HOST:
        return [], "XIAOZHI_HOST not set"
    url = f"http://{XIAOZHI_HOST}:{XIAOZHI_OTA_PORT}/xiaozhi/admin/songs"
    import urllib.request
    def _fetch() -> tuple[list[str], str | None]:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                data = json.loads(r.read())
                files = data.get("files") or []
                return [f for f in files if isinstance(f, str)], None
        except Exception as exc:
            return [], str(exc)
    return await asyncio.to_thread(_fetch)


@router.get("/songs", response_class=HTMLResponse, include_in_schema=False)
async def songs_list(request: Request) -> Any:
    """HTML fragment listing the songs available for direct playback."""
    files, err = await _xiaozhi_list_songs()
    return templates.TemplateResponse(
        request, "songs.html",
        {"songs": files, "error": err, "songs_dir": _SONGS_BASE_PATH},
    )


@router.post("/actions/play-song", response_class=HTMLResponse, include_in_schema=False)
async def play_song(request: Request, filename: str = Form(...)) -> Any:
    """Push a single song file to the device via xiaozhi's play-asset.
    Filename must be a basename (no slashes) with an allowed audio extension —
    the actual existence check happens server-side in play-asset."""
    import os.path as _osp
    base = _osp.basename(filename)
    if base != filename or _osp.splitext(base)[1].lower() not in _SONG_OK_EXT:
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False, "error": "Invalid song filename."},
        )
    if not XIAOZHI_HOST:
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False, "error": "XIAOZHI_HOST not set"},
        )
    asset_path = f"{_SONGS_BASE_PATH}/{base}"
    url = f"http://{XIAOZHI_HOST}:{XIAOZHI_OTA_PORT}/xiaozhi/admin/play-asset"
    def _post() -> dict:
        try:
            r = requests.post(url, json={"asset": asset_path}, timeout=3)
            if r.status_code == 200:
                return {"ok": True, "sent": base, "response": f"playing {base}"}
            if r.status_code == 503 and "no device connected" in r.text:
                return {"ok": False, "error":
                        "Dotty isn't connected right now — try again in a few seconds."}
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    result = await asyncio.to_thread(_post)
    return templates.TemplateResponse(request, "say_result.html", result)


_INJECT_WAIT_SEC = 8.0  # Q4: how long to wait for Dotty's reply before
                        #     showing "no response in time" fallback.


async def _inject_or_error(request: Request, text: str, label: str) -> Any:
    """Helper for action endpoints that fire text into xiaozhi-server's
    pipeline so the device actually speaks/emotes/runs MCP tools.

    Q4: subscribes to the bridge's event stream BEFORE injecting, then
    waits up to ~8s for the next turn so the dashboard can show what Dotty
    actually said (not just "Sent…")."""
    inject = _state.get("inject_to_device")
    if inject is None:
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False,
             "error": "Inject path not configured (xiaozhi admin patch missing)."},
        )
    subscribe = _state.get("subscribe_events")
    unsubscribe = _state.get("unsubscribe_events")
    queue = subscribe() if subscribe else None
    try:
        try:
            result = await inject(text=text)
        except Exception as exc:
            log.exception("dashboard inject failed")
            return templates.TemplateResponse(
                request, "say_result.html",
                {"ok": False, "error": f"Bridge error: {exc.__class__.__name__}"},
            )
        if not result.get("ok"):
            return templates.TemplateResponse(
                request, "say_result.html",
                {"ok": False, "error": result.get("error", "unknown injection failure")},
            )
        # Wait for the next completed turn (likely ours — single device).
        response_text = "Sent — no reply in 8s."
        if queue is not None:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_INJECT_WAIT_SEC)
                response_text = event.get("response_text") or "(no text)"
            except asyncio.TimeoutError:
                pass
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": True, "sent": label, "response": response_text},
        )
    finally:
        if queue is not None and unsubscribe is not None:
            unsubscribe(queue)


@router.post("/actions/say", response_class=HTMLResponse, include_in_schema=False)
async def say(request: Request, text: str = Form(...)) -> Any:
    text = (text or "").strip()
    if not text:
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False, "error": "Empty message — type something for Dotty to say."},
        )
    if len(text) > 500:
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False, "error": "Too long — keep it under 500 characters."},
        )
    # F17: strip ASCII C0 control chars (incl. NUL, BEL, newline, tab) and
    # DEL, then collapse whitespace runs. Stops multi-line or null-byte
    # payloads reaching the TTS pipeline.
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = " ".join(text.split())
    if not text:
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False, "error": "Message was empty after sanitisation."},
        )
    return await _inject_or_error(request, text, label=text)


def _latest_vision_entry() -> tuple[str, dict] | None:
    """Pick the most-recently captured device entry from the vision cache."""
    cache = _state.get("vision_cache") or {}
    if not cache:
        return None
    device_id, entry = max(
        cache.items(), key=lambda kv: kv[1].get("timestamp", 0.0)
    )
    return device_id, entry


@router.get("/vision/latest", response_class=HTMLResponse, include_in_schema=False)
async def vision_latest(request: Request) -> Any:
    """Render a thumbnail + description for the most recent capture, if any."""
    pick = _latest_vision_entry()
    ctx: dict[str, Any] = {"have_photo": False}
    if pick is not None:
        device_id, entry = pick
        jpeg = entry.get("jpeg_bytes")
        # `timestamp` is a perf_counter() value — relative, so use elapsed.
        elapsed = max(0.0, time.monotonic() - entry.get("timestamp", time.monotonic()))
        ctx = {
            "have_photo": jpeg is not None,
            "device_id": device_id,
            "description": entry.get("description", ""),
            "question": entry.get("question", ""),
            "age": _humanize_age(elapsed),
            "thumbnail_b64": (
                base64.b64encode(jpeg).decode("ascii") if jpeg else ""
            ),
        }
    return templates.TemplateResponse(request, "vision.html", ctx)


# Voice-daemon LLM swapped on kid-mode toggle. Same defaults as bridge.py
# so the dashboard reflects what the bridge actually loaded.
KID_MODEL_NAME = os.environ.get(
    "DOTTY_KID_MODEL", "mistralai/mistral-small-3.2-24b-instruct",
)
ADULT_MODEL_NAME = os.environ.get(
    "DOTTY_ADULT_MODEL", "anthropic/claude-sonnet-4-6",
)
# Smart-mode is an independent toggle: when ON, the bridge routes turns to
# `SMART_MODEL` instead of the kid/adult default. When OFF, the regular
# kid/adult path is used.
SMART_MODEL_NAME = os.environ.get("SMART_MODEL", "")


def _short_model(name: str) -> str:
    """Strip the provider prefix for compact dashboard display."""
    if not name:
        return ""
    return name.split("/", 1)[1] if "/" in name else name


@router.get("/kid-mode", response_class=HTMLResponse, include_in_schema=False)
async def kid_mode_partial(request: Request) -> Any:
    getter = _state.get("kid_mode_getter")
    enabled = bool(getter()) if getter else True
    return templates.TemplateResponse(
        request, "kid_mode.html",
        {"enabled": enabled, "available": getter is not None},
    )


# Phase 4 — State + Smart-mode dashboard cards. Both are LIVE-update (no
# daemon restart required). State picker pushes set_state MCP via the bridge
# helper; smart-mode toggle pushes set_toggle MCP and persists to the state
# file.

# Display order for the dashboard state picker. Slugs (sent to the firmware
# via /xiaozhi/admin/set-state) match StateManager::stateName; the order +
# short labels here are dashboard-only.
_STATES = ("idle", "talk", "story_time", "security", "dance", "sleep")
_STATE_LABELS = {
    "idle":       "Idle",
    "talk":       "Talk",
    "story_time": "Story",
    "security":   "Security",
    "sleep":      "Sleep",
    "dance":      "Dance",
}
_STATE_DESCRIPTIONS = {
    "idle":       "Ambient awareness, default",
    "talk":       "Conversation engaged",
    "story_time": "Long-running interactive story",
    "security":   "Wide deliberate scan, periodic capture",
    "sleep":      "Servos parked, ambient awareness off",
    "dance":      "Transient performance",
}


@router.get("/state", response_class=HTMLResponse, include_in_schema=False)
async def state_partial(request: Request) -> Any:
    getter = _state.get("state_getter")
    current = (getter() if getter else "idle") or "idle"
    return templates.TemplateResponse(
        request, "state.html",
        {
            "current": current,
            "available": getter is not None,
            "states": _STATES,
            "labels": _STATE_LABELS,
            "descriptions": _STATE_DESCRIPTIONS,
        },
    )


@router.post("/actions/state", response_class=HTMLResponse, include_in_schema=False)
async def state_set(request: Request, state: str = Form(...)) -> Any:
    setter = _state.get("state_setter")
    if setter is None:
        raise HTTPException(503, "state_setter not configured")
    if state not in _STATES:
        return templates.TemplateResponse(
            request, "state_result.html",
            {"ok": False, "error": f"unknown state: {state!r}", "state": state},
        )
    try:
        result = await setter(state)
        ok = bool(result.get("ok") if isinstance(result, dict) else result)
    except Exception as exc:
        log.exception("state setter failed")
        return templates.TemplateResponse(
            request, "state_result.html",
            {"ok": False, "error": str(exc), "state": state},
        )
    return templates.TemplateResponse(
        request, "state_result.html",
        {"ok": ok, "state": state, "label": _STATE_LABELS.get(state, state)},
    )


@router.get("/smart-mode", response_class=HTMLResponse, include_in_schema=False)
async def smart_mode_partial(request: Request) -> Any:
    getter = _state.get("smart_mode_getter")
    enabled = bool(getter()) if getter else False
    kid_getter = _state.get("kid_mode_getter")
    kid_enabled = bool(kid_getter()) if kid_getter else True
    default_model = KID_MODEL_NAME if kid_enabled else ADULT_MODEL_NAME
    return templates.TemplateResponse(
        request, "smart_mode.html",
        {"enabled": enabled, "available": getter is not None,
         "smart_model": _short_model(SMART_MODEL_NAME),
         "default_model": _short_model(default_model)},
    )


@router.post("/actions/smart-mode", response_class=HTMLResponse, include_in_schema=False)
async def smart_mode_set(request: Request, enabled: str = Form("")) -> Any:
    setter = _state.get("smart_mode_setter")
    if setter is None:
        raise HTTPException(503, "smart_mode_setter not configured")
    new_state = enabled.lower() in ("on", "true", "1", "yes")
    try:
        await setter(new_state)
    except Exception as exc:
        log.exception("smart_mode setter failed")
        return templates.TemplateResponse(
            request, "smart_mode_result.html",
            {"ok": False, "error": str(exc)},
        )
    return templates.TemplateResponse(
        request, "smart_mode_result.html",
        {"ok": True, "new_state": new_state},
    )


@router.post("/actions/kid-mode", response_class=HTMLResponse, include_in_schema=False)
async def kid_mode_set(request: Request, enabled: str = Form("")) -> Any:
    """Persist Kid Mode state to a file the bridge re-reads on startup,
    then trigger a self-restart via systemctl. The HTTP response returns
    before the SIGTERM hits (subprocess.Popen + small delay).
    """
    setter = _state.get("kid_mode_setter")
    if setter is None:
        raise HTTPException(503, "kid_mode_setter not configured")
    new_state = enabled.lower() in ("on", "true", "1", "yes")
    try:
        setter(new_state)
    except Exception as exc:
        log.exception("kid_mode setter failed")
        return templates.TemplateResponse(
            request, "kid_mode_result.html",
            {"ok": False, "error": str(exc)},
        )

    # Spawn a delayed self-restart so we can return the response first.
    import subprocess
    try:
        subprocess.Popen(
            ["sh", "-c", "sleep 1 && systemctl restart zeroclaw-bridge"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        log.exception("self-restart spawn failed")
        return templates.TemplateResponse(
            request, "kid_mode_result.html",
            {"ok": False, "error": f"restart failed: {exc}"},
        )

    return templates.TemplateResponse(
        request, "kid_mode_result.html",
        {"ok": True, "new_state": new_state},
    )


# --- Q3: stop / abort current TTS ----------------------------------------

@router.post("/actions/stop", response_class=HTMLResponse,
             include_in_schema=False)
async def stop(request: Request) -> Any:
    abort = _state.get("abort_device")
    if abort is None:
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False, "error": "Abort path not configured."},
        )
    try:
        result = await abort()
    except Exception as exc:
        log.exception("dashboard stop action failed")
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False, "error": f"Bridge error: {exc.__class__.__name__}"},
        )
    if not result.get("ok"):
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False, "error": result.get("error", "abort failed")},
        )
    return templates.TemplateResponse(
        request, "say_result.html",
        {"ok": True, "sent": "Stop", "response": "Aborted."},
    )


# --- P15: update bridge from GitHub --------------------------------------

GITHUB_REPO = os.environ.get(
    "DOTTY_BRIDGE_REPO", "https://github.com/BrettKinny/dotty-stackchan.git"
)
BRIDGE_INSTALL_DIR = Path(
    os.environ.get("DOTTY_BRIDGE_DIR", "/root/zeroclaw-bridge")
)


def _collect_update_preview() -> tuple[bool, dict[str, Any]]:
    """F16: shallow-clone the repo to a tmpdir, gather the commit list
    since the currently-deployed SHA. No filesystem mutation outside the
    tmp clone — caller renders the result for review."""
    import subprocess
    import tempfile
    import shutil
    work = Path(tempfile.mkdtemp(prefix="dotty-preview-"))
    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", "30", "--branch", "main",
             GITHUB_REPO, str(work)],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            return False, {"error": f"git clone failed: {proc.stderr.strip()[:300]}"}
        sha_proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(work), capture_output=True, text=True, timeout=5,
        )
        new_sha = sha_proc.stdout.strip() if sha_proc.returncode == 0 else ""
        deployed = BRIDGE_VERSION if BRIDGE_VERSION != "unknown" else ""
        commits: list[dict[str, str]] = []
        used_range = False
        if deployed:
            log_proc = subprocess.run(
                ["git", "log", "--oneline", "-30", f"{deployed}..HEAD"],
                cwd=str(work), capture_output=True, text=True, timeout=5,
            )
            if log_proc.returncode == 0 and log_proc.stdout.strip():
                used_range = True
                for line in log_proc.stdout.strip().splitlines():
                    parts = line.split(" ", 1)
                    if len(parts) == 2:
                        commits.append({"sha": parts[0], "msg": parts[1]})
        if not commits and not used_range:
            # Fallback: deployed SHA isn't in the shallow clone or unknown.
            log_proc = subprocess.run(
                ["git", "log", "--oneline", "-10"],
                cwd=str(work), capture_output=True, text=True, timeout=5,
            )
            if log_proc.returncode == 0:
                for line in log_proc.stdout.strip().splitlines():
                    parts = line.split(" ", 1)
                    if len(parts) == 2:
                        commits.append({"sha": parts[0], "msg": parts[1]})
        return True, {
            "current_sha": deployed or "unknown",
            "new_sha": new_sha or "unknown",
            "commits": commits,
            "used_range": used_range,
            "up_to_date": bool(deployed) and deployed == new_sha,
        }
    except Exception as exc:
        return False, {"error": f"preview error: {exc}"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


@router.post("/actions/preview-update",
             response_class=HTMLResponse, include_in_schema=False)
async def preview_update(request: Request) -> Any:
    """F16: render the incoming-commits review for the Update modal."""
    ok, ctx = await asyncio.to_thread(_collect_update_preview)
    return templates.TemplateResponse(
        request, "update_preview.html",
        {"ok": ok, **ctx},
    )


def _pull_and_install_bridge() -> tuple[bool, str]:
    """git-clone the public repo into a tmpdir and copy bridge.py +
    bridge/ over the install dir. Caller restarts the service."""
    import subprocess
    import tempfile
    import shutil
    work = Path(tempfile.mkdtemp(prefix="dotty-update-"))
    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", "main",
             GITHUB_REPO, str(work)],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            return False, f"git clone failed: {proc.stderr.strip()[:300]}"
        src_bridge_py = work / "bridge.py"
        src_bridge_dir = work / "bridge"
        if not src_bridge_py.exists() or not src_bridge_dir.exists():
            return False, "checkout missing bridge.py or bridge/ dir"
        # Capture the SHA so the dashboard footer reflects what loaded.
        sha = ""
        try:
            sha_proc = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(work), capture_output=True, text=True, timeout=5,
            )
            if sha_proc.returncode == 0:
                sha = sha_proc.stdout.strip()
        except Exception:
            pass
        # Atomic-ish replace: rename current then copy new in.
        dst_bridge_py = BRIDGE_INSTALL_DIR / "bridge.py"
        dst_bridge_dir = BRIDGE_INSTALL_DIR / "bridge"
        if dst_bridge_dir.exists():
            backup = BRIDGE_INSTALL_DIR / "bridge.prev"
            if backup.exists():
                shutil.rmtree(backup)
            shutil.move(str(dst_bridge_dir), str(backup))
        shutil.copytree(str(src_bridge_dir), str(dst_bridge_dir))
        if dst_bridge_py.exists():
            dst_bridge_py.rename(BRIDGE_INSTALL_DIR / "bridge.py.prev")
        shutil.copy2(str(src_bridge_py), str(dst_bridge_py))
        if sha:
            try:
                (BRIDGE_INSTALL_DIR / ".bridge-version").write_text(sha)
            except OSError:
                pass
        return True, f"Updated to {sha or 'main'}. Restarting…"
    except Exception as exc:
        return False, f"update error: {exc}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@router.post("/actions/update-bridge",
             response_class=HTMLResponse, include_in_schema=False)
async def update_bridge(request: Request) -> Any:
    ok, msg = await asyncio.to_thread(_pull_and_install_bridge)
    if not ok:
        return templates.TemplateResponse(
            request, "update_result.html",
            {"ok": False, "message": msg},
        )
    # Spawn delayed restart so the response can return first.
    import subprocess
    try:
        subprocess.Popen(
            ["sh", "-c", "sleep 1 && systemctl restart zeroclaw-bridge"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        return templates.TemplateResponse(
            request, "update_result.html",
            {"ok": False, "message": f"updated but restart failed: {exc}"},
        )
    return templates.TemplateResponse(
        request, "update_result.html",
        {"ok": True, "message": msg},
    )


# --- P16: persona switcher ------------------------------------------------

PERSONAS_DIR = Path(
    os.environ.get("DOTTY_PERSONAS_DIR", "/root/zeroclaw-bridge/personas")
)
PERSONA_STATE_FILE = Path(
    os.environ.get("DOTTY_PERSONA_STATE", "/root/zeroclaw-bridge/state/persona")
)


def _list_personas() -> list[str]:
    if not PERSONAS_DIR.is_dir():
        return []
    return sorted(p.stem for p in PERSONAS_DIR.glob("*.md"))


def _current_persona() -> str:
    if PERSONA_STATE_FILE.exists():
        try:
            v = PERSONA_STATE_FILE.read_text().strip()
            if v in _list_personas():
                return v
        except OSError:
            pass
    return "default"


@router.get("/persona/view", response_class=HTMLResponse, include_in_schema=False)
async def persona_view(request: Request, name: str = "") -> Any:
    """F6: read-only view of a persona file. Restricted to entries in
    _list_personas() so ?name=../etc/passwd is impossible."""
    available = _list_personas()
    name = (name or "").strip() or _current_persona()
    ctx: dict[str, Any] = {"name": name}
    if name not in available:
        ctx["error"] = f"Unknown persona: {name}"
        return templates.TemplateResponse(request, "persona_view.html", ctx)
    path = PERSONAS_DIR / f"{name}.md"
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        ctx["error"] = f"Read failed: {exc}"
        return templates.TemplateResponse(request, "persona_view.html", ctx)
    # Cap at 50k chars — defensive; persona files are usually small.
    ctx["content"] = content[:50_000]
    if len(content) > 50_000:
        ctx["truncated"] = True
    return templates.TemplateResponse(request, "persona_view.html", ctx)


@router.get("/persona", response_class=HTMLResponse, include_in_schema=False)
async def persona_partial(request: Request) -> Any:
    return templates.TemplateResponse(
        request, "persona.html",
        {"available": _list_personas(), "current": _current_persona(),
         "personas_dir": str(PERSONAS_DIR)},
    )


@router.post("/actions/persona", response_class=HTMLResponse,
             include_in_schema=False)
async def persona_set(request: Request, name: str = Form(...)) -> Any:
    available = _list_personas()
    if name not in available:
        return templates.TemplateResponse(
            request, "persona.html",
            {"available": available, "current": _current_persona(),
             "personas_dir": str(PERSONAS_DIR),
             "error": f"Unknown persona: {name}"},
        )
    PERSONA_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PERSONA_STATE_FILE.write_text(name)
    # Spawn delayed self-restart so the new persona is picked up by the
    # bridge's voice-wrap (it reads the state file at startup).
    import subprocess
    try:
        subprocess.Popen(
            ["sh", "-c", "sleep 1 && systemctl restart zeroclaw-bridge"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        return templates.TemplateResponse(
            request, "persona.html",
            {"available": available, "current": name,
             "personas_dir": str(PERSONAS_DIR),
             "error": f"set but restart failed: {exc}"},
        )
    return templates.TemplateResponse(
        request, "persona.html",
        {"available": available, "current": name,
         "personas_dir": str(PERSONAS_DIR), "switching": True},
    )


# --- P7: restart bridge ---------------------------------------------------

@router.post("/actions/restart-bridge",
             response_class=HTMLResponse, include_in_schema=False)
async def restart_bridge(request: Request) -> Any:
    """Spawn a delayed `systemctl restart` so the response can return
    before SIGTERM hits."""
    import subprocess
    try:
        subprocess.Popen(
            ["sh", "-c", "sleep 1 && systemctl restart zeroclaw-bridge"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        log.exception("self-restart spawn failed")
        return templates.TemplateResponse(
            request, "kid_mode_result.html",
            {"ok": False, "error": f"restart failed: {exc}"},
        )
    return templates.TemplateResponse(
        request, "restart_result.html",
        {"target": "bridge"},
    )


# --- P8: PWA manifest + icon ----------------------------------------------

_DOTTY_ICON_PATH = Path(__file__).parent / "assets" / "dotty-icon.svg"
try:
    _ICON_SVG = _DOTTY_ICON_PATH.read_text(encoding="utf-8")
except OSError:
    # Fallback to a minimal inline placeholder if the asset is missing
    _ICON_SVG = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
        '<rect width="512" height="512" rx="96" fill="#1d232a"/>'
        '<circle cx="180" cy="220" r="36" fill="#22c55e"/>'
        '<circle cx="332" cy="220" r="36" fill="#22c55e"/>'
        '<path d="M150 320 q106 80 212 0" stroke="#22c55e" stroke-width="22" '
        'stroke-linecap="round" fill="none"/>'
        '</svg>'
    )


@router.get("/icon.svg", include_in_schema=False)
async def icon() -> Response:
    return Response(content=_ICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


_DOTTY_HERO_PATH = Path(__file__).parent / "assets" / "dotty-hero.svg"
try:
    _HERO_SVG = _DOTTY_HERO_PATH.read_text(encoding="utf-8")
except OSError:
    _HERO_SVG = _ICON_SVG


@router.get("/hero.svg", include_in_schema=False)
async def hero() -> Response:
    return Response(content=_HERO_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


# F19: 180×180 PNG for iOS apple-touch-icon (iOS doesn't fully render SVG
# touch icons; an installed Add-to-Home-Screen gets a placeholder otherwise).
_APPLE_ICON_PATH = Path(__file__).parent / "assets" / "apple-touch-icon.png"
try:
    _APPLE_ICON_BYTES: bytes = _APPLE_ICON_PATH.read_bytes()
except OSError:
    _APPLE_ICON_BYTES = b""


@router.get("/apple-touch-icon.png", include_in_schema=False)
async def apple_touch_icon() -> Response:
    if not _APPLE_ICON_BYTES:
        raise HTTPException(404, "apple-touch-icon.png not bundled")
    return Response(
        content=_APPLE_ICON_BYTES, media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/manifest.json", include_in_schema=False)
async def manifest() -> JSONResponse:
    # scope must match (or be a prefix of) start_url; the previous "/ui/"
    # excluded "/ui" itself, which is what start_url resolves to. Using
    # "/ui" (no trailing slash) covers both /ui and /ui/anything as in-scope.
    return JSONResponse({
        "name": "Dotty Dashboard",
        "short_name": "Dotty",
        "start_url": "/ui",
        "scope": "/ui",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#1d232a",
        "theme_color": "#1d232a",
        "icons": [
            {"src": "/ui/icon.svg", "sizes": "any", "type": "image/svg+xml",
             "purpose": "any"},
            {"src": "/ui/apple-touch-icon.png", "sizes": "180x180",
             "type": "image/png", "purpose": "any"},
            # Raster fallbacks for Android install card / splash. Generated
            # by scripts/generate-pwa-icons.sh from the same source SVG.
            {"src": "/ui/static/icon-192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "any"},
            {"src": "/ui/static/icon-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "any"},
        ],
    })


# --- P14: system metrics --------------------------------------------------

def _read_first_line(path: str) -> str:
    try:
        with open(path) as f:
            return f.readline().strip()
    except OSError:
        return ""


def _read_memory_mb() -> tuple[int, int] | None:
    try:
        with open("/proc/meminfo") as f:
            data = f.read()
    except OSError:
        return None
    total_kb = avail_kb = 0
    for line in data.splitlines():
        if line.startswith("MemTotal:"):
            total_kb = int(line.split()[1])
        elif line.startswith("MemAvailable:"):
            avail_kb = int(line.split()[1])
    if not total_kb:
        return None
    used_mb = (total_kb - avail_kb) // 1024
    total_mb = total_kb // 1024
    return used_mb, total_mb


def _cpu_temp_c() -> float | None:
    raw = _read_first_line("/sys/class/thermal/thermal_zone0/temp")
    try:
        return int(raw) / 1000.0 if raw else None
    except ValueError:
        return None


def _proc_uptime_sec() -> float | None:
    raw = _read_first_line("/proc/uptime")
    try:
        return float(raw.split()[0]) if raw else None
    except ValueError:
        return None


def _disk_usage_root() -> tuple[int, int] | None:
    import shutil
    try:
        u = shutil.disk_usage("/")
        return u.used // (1024 ** 3), u.total // (1024 ** 3)
    except OSError:
        return None


@router.get("/metrics", response_class=HTMLResponse, include_in_schema=False)
async def metrics(request: Request) -> Any:
    cpu_c = _cpu_temp_c()
    mem = _read_memory_mb()
    disk = _disk_usage_root()
    upt = _proc_uptime_sec()
    rows = [
        {"label": "CPU temp",
         "value": f"{cpu_c:.1f} °C" if cpu_c else "n/a",
         "warn": cpu_c is not None and cpu_c >= 75},
        {"label": "Memory",
         "value": (f"{mem[0]} / {mem[1]} MB" if mem else "n/a"),
         "warn": mem is not None and mem[1] and (mem[0] / mem[1]) > 0.85},
        {"label": "Disk /",
         "value": (f"{disk[0]} / {disk[1]} GiB" if disk else "n/a"),
         "warn": disk is not None and disk[1] and (disk[0] / disk[1]) > 0.85},
        {"label": "Host uptime",
         "value": _humanize_age(upt) if upt else "n/a",
         "warn": False},
        {"label": "Bridge uptime",
         "value": _humanize_age(time.time() - _START_TIME),
         "warn": False},
    ]
    return templates.TemplateResponse(
        request, "metrics.html", {"rows": rows}
    )


# Single-page redesign: compact host dots (header placement) + system pills
# (footer placement). One endpoint, two placements, polled at the same 10s
# cadence. Replaces /ui/cards + /ui/metrics + /ui/vision/latest in the new
# layout; the older endpoints remain for compatibility but no longer have
# callers in dashboard.html.
@router.get("/status-strip", response_class=HTMLResponse, include_in_schema=False)
async def status_strip(request: Request, placement: str = "header") -> Any:
    placement = placement if placement in ("header", "footer") else "header"
    ctx: dict[str, Any] = {"placement": placement, "version": BRIDGE_VERSION}

    if placement == "header":
        bridge_uptime = time.time() - _START_TIME
        xz_ota_ok, xz_ws_ok = await asyncio.gather(
            _tcp_reachable(XIAOZHI_HOST, XIAOZHI_OTA_PORT),
            _tcp_reachable(XIAOZHI_HOST, XIAOZHI_WS_PORT),
        )
        last_seen_ts = _stackchan_last_seen()
        if last_seen_ts is None:
            sc_status = "unknown"
            sc_tip = "Dotty: no voice activity today"
        else:
            age = time.time() - last_seen_ts
            if age < 600:
                sc_status, sc_tip = "ok", f"Dotty: active {_humanize_age(age)} ago"
            elif age < 86400:
                sc_status, sc_tip = "warn", f"Dotty: idle {_humanize_age(age)} ago"
            else:
                sc_status, sc_tip = "bad", f"Dotty: stale {_humanize_age(age)} ago"

        if not XIAOZHI_HOST:
            xz_status, xz_tip = "unknown", "unraid: XIAOZHI_HOST env not set"
        elif xz_ota_ok and xz_ws_ok:
            xz_status, xz_tip = "ok", f"unraid: OTA :{XIAOZHI_OTA_PORT} + WS :{XIAOZHI_WS_PORT}"
        elif xz_ota_ok or xz_ws_ok:
            xz_status, xz_tip = "warn", "unraid: partial reachability"
        else:
            xz_status, xz_tip = "bad", "unraid: no ports responding"

        ctx["dots"] = [
            {"slug": "bridge", "label": "bridge",
             "status": "ok",      "title": f"bridge: up {_humanize_age(bridge_uptime)}"},
            {"slug": "server", "label": "server",
             "status": xz_status, "title": xz_tip.replace("unraid:", "server:")},
            {"slug": "robot",  "label": "robot",
             "status": sc_status, "title": sc_tip.replace("Dotty:", "robot:")},
        ]
    else:
        cpu_c = _cpu_temp_c()
        mem = _read_memory_mb()
        disk = _disk_usage_root()
        upt = _proc_uptime_sec()
        pick = _latest_vision_entry()
        vision_age = ""
        have_photo = False
        if pick is not None:
            _, entry = pick
            if entry.get("jpeg_bytes"):
                have_photo = True
                elapsed = max(
                    0.0,
                    time.monotonic() - entry.get("timestamp", time.monotonic()),
                )
                vision_age = _humanize_age(elapsed)
        ctx.update({
            "have_photo": have_photo,
            "vision_age": vision_age,
            "cpu_c": cpu_c,
            "cpu_warn": cpu_c is not None and cpu_c >= 75,
            "mem_pct": (
                int(round((mem[0] / mem[1]) * 100)) if mem and mem[1] else None
            ),
            "mem_warn": (
                bool(mem and mem[1] and (mem[0] / mem[1]) > 0.85)
            ),
            "disk_pct": (
                int(round((disk[0] / disk[1]) * 100)) if disk and disk[1] else None
            ),
            "disk_warn": (
                bool(disk and disk[1] and (disk[0] / disk[1]) > 0.85)
            ),
            "uptime": _humanize_age(upt) if upt else None,
        })

    return templates.TemplateResponse(request, "status_strip.html", ctx)


# Host-detail modal: clicked from the header status strip. One slug per
# host (bridge / xiaozhi / dotty); each gathers a small set of facts.
@router.get("/host/{slug}", response_class=HTMLResponse,
            include_in_schema=False)
async def host_detail(request: Request, slug: str) -> Any:
    if slug not in ("bridge", "server", "robot"):
        raise HTTPException(404, "unknown host")

    facts: list[tuple[str, str]] = []
    title = ""

    if slug == "bridge":
        title = "Bridge"
        upt = _proc_uptime_sec()
        facts = [
            ("status",    "online"),
            ("version",   BRIDGE_VERSION),
            ("uptime",    _humanize_age(time.time() - _START_TIME)),
            ("host up",   _humanize_age(upt) if upt else "n/a"),
            ("logs dir",  str(LOG_DIR)),
            ("kid model", _short_model(KID_MODEL_NAME)),
            ("adult model", _short_model(ADULT_MODEL_NAME)),
            ("smart model", _short_model(SMART_MODEL_NAME) or "(unset)"),
        ]
    elif slug == "server":
        title = "Server (xiaozhi-esp32-server)"
        ota_ok, ws_ok = await asyncio.gather(
            _tcp_reachable(XIAOZHI_HOST, XIAOZHI_OTA_PORT),
            _tcp_reachable(XIAOZHI_HOST, XIAOZHI_WS_PORT),
        )
        n = await _xiaozhi_device_count()
        facts = [
            ("host",     XIAOZHI_HOST or "(unset)"),
            ("OTA :%d" % XIAOZHI_OTA_PORT, "reachable" if ota_ok else "unreachable"),
            ("WS :%d"  % XIAOZHI_WS_PORT,  "reachable" if ws_ok  else "unreachable"),
            ("devices connected",
             "—" if n is None else f"{n}"),
        ]
    else:  # robot
        title = "Robot (StackChan)"
        last_seen_ts = _stackchan_last_seen()
        if last_seen_ts is None:
            seen = "no voice activity today"
        else:
            seen = f"{_humanize_age(time.time() - last_seen_ts)} ago"
        getter = _state.get("state_getter")
        current = (getter() if getter else None) or "idle"
        n = await _xiaozhi_device_count()
        facts = [
            ("device class", "M5Stack StackChan (ESP32-S3)"),
            ("connection",
             "online" if (n is not None and n > 0)
             else ("offline" if n == 0 else "unknown")),
            ("last seen",   seen),
            ("current state", current),
        ]

    return templates.TemplateResponse(
        request, "host_detail.html",
        {"title": title, "facts": facts, "slug": slug},
    )


# --- P13 + P12: SSE event stream for live log + error toasts -------------

@router.get("/events", include_in_schema=False)
async def events_stream(request: Request) -> StreamingResponse:
    """Server-Sent Events stream of completed conversation turns.

    Each event is one JSON object: {ts, channel, request_text, response_text,
    latency_ms, error, emoji_used}. The bridge's ConvoLogger broadcasts on
    every turn. Heartbeats every 15s keep proxies / browsers awake.
    """
    subscribe = _state.get("subscribe_events")
    unsubscribe = _state.get("unsubscribe_events")
    if subscribe is None or unsubscribe is None:
        raise HTTPException(503, "event broadcast not configured")
    queue = subscribe()

    async def gen():
        try:
            # Tell EventSource how long to wait before reconnecting on drop.
            yield "retry: 5000\n\n".encode()
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    # Strip the heavy [Context] block before pushing — clients
                    # only want the cleaned user payload.
                    event = {**event,
                             "request_text": _clean_request_text(
                                 event.get("request_text") or "")}
                    payload = json.dumps(event, ensure_ascii=False)
                    yield f"data: {payload}\n\n".encode()
                except asyncio.TimeoutError:
                    yield b": heartbeat\n\n"
        finally:
            unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/logs", response_class=HTMLResponse, include_in_schema=False)
async def logs(request: Request, date: str | None = None) -> Any:
    chosen = _safe_date(date)
    entries = _read_recent_log_entries(chosen, limit=20)
    today = datetime.now().strftime("%Y-%m-%d")
    return templates.TemplateResponse(
        request, "logs.html",
        {"entries": entries, "date": chosen, "is_today": chosen == today},
    )
