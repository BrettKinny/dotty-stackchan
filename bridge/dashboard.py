"""Mobile-first admin dashboard for Dotty.

Mounted at ``/ui`` on the bridge FastAPI app. Host status cards,
conversation log tail, action endpoints, SSE turn stream.

Host probes are env-driven so this stays generic in the public template:
set ``XIAOZHI_HOST`` (and optionally ``WORKSTATION_HOST``) on the bridge
service. Cards for unset hosts render as "unknown".
"""

from __future__ import annotations

import asyncio
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
    "vision_cache_getter": None,
    "audio_cache_getter": None,
    "scene_synthesis_cache_getter": None,
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
    "perception_state_getter": None,
    "perception_recent_getter": None,
    "identity_display_name": None,
    "last_user_line_getter": None,
    "memory_records_getter": None,
    "memory_approve": None,
    "memory_redact": None,
    "sound_balance_getter": None,
    "vision_failures_getter": None,
}


def configure(*, send_message: Any = None, vision_cache_getter: Any = None,
              audio_cache_getter: Any = None,
              scene_synthesis_cache_getter: Any = None,
              kid_mode_getter: Any = None, kid_mode_setter: Any = None,
              smart_mode_getter: Any = None, smart_mode_setter: Any = None,
              state_getter: Any = None, state_setter: Any = None,
              inject_to_device: Any = None, abort_device: Any = None,
              subscribe_events: Any = None,
              unsubscribe_events: Any = None,
              perception_state_getter: Any = None,
              perception_recent_getter: Any = None,
              identity_display_name: Any = None,
              last_user_line_getter: Any = None,
              memory_records_getter: Any = None,
              memory_approve: Any = None,
              memory_redact: Any = None,
              sound_balance_getter: Any = None,
              vision_failures_getter: Any = None) -> None:
    """Register bridge state with the dashboard. Idempotent."""
    if send_message is not None:
        _state["send_message"] = send_message
    if vision_cache_getter is not None:
        _state["vision_cache_getter"] = vision_cache_getter
    if audio_cache_getter is not None:
        _state["audio_cache_getter"] = audio_cache_getter
    if scene_synthesis_cache_getter is not None:
        _state["scene_synthesis_cache_getter"] = scene_synthesis_cache_getter
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
    if perception_state_getter is not None:
        _state["perception_state_getter"] = perception_state_getter
    if perception_recent_getter is not None:
        _state["perception_recent_getter"] = perception_recent_getter
    if identity_display_name is not None:
        _state["identity_display_name"] = identity_display_name
    if last_user_line_getter is not None:
        _state["last_user_line_getter"] = last_user_line_getter
    if memory_records_getter is not None:
        _state["memory_records_getter"] = memory_records_getter
    if memory_approve is not None:
        _state["memory_approve"] = memory_approve
    if memory_redact is not None:
        _state["memory_redact"] = memory_redact
    if sound_balance_getter is not None:
        _state["sound_balance_getter"] = sound_balance_getter
    if vision_failures_getter is not None:
        _state["vision_failures_getter"] = vision_failures_getter


def _vision_cache_snapshot() -> dict[str, dict]:
    """Read-through accessor for the vision cache.

    Resolves the configured getter (Tile 2 of #115 wires this to
    ``bridge._dashboard_vision_cache_getter``, which fetches from
    dotty-behaviour's ``/api/vision/cache`` with a 2 s cache + 1.5 s
    timeout + circuit breaker). Returns ``{}`` if no getter has been
    wired yet, so call sites can keep ``cache.get(...)`` semantics
    without conditionals."""
    getter = _state.get("vision_cache_getter")
    if not getter:
        return {}
    try:
        result = getter()
    except Exception:
        log.warning("vision_cache_getter raised", exc_info=True)
        return {}
    return result if isinstance(result, dict) else {}


def _audio_cache_snapshot() -> dict[str, dict]:
    """Read-through accessor for the audio cache (Tile 4 of #115).

    Mirrors ``_vision_cache_snapshot``: resolves the configured getter
    (bridge wires this to ``_dashboard_audio_cache_getter``, which
    fetches from dotty-behaviour's ``/api/audio/cache``). Returns
    ``{}`` if no getter is wired or any failure occurs."""
    getter = _state.get("audio_cache_getter")
    if not getter:
        return {}
    try:
        result = getter()
    except Exception:
        log.warning("audio_cache_getter raised", exc_info=True)
        return {}
    return result if isinstance(result, dict) else {}


def _scene_synthesis_cache_snapshot() -> dict[str, dict]:
    """Read-through accessor for the scene synthesis cache (Tile 3 of #115).

    Same shape as ``_vision_cache_snapshot``/``_audio_cache_snapshot``.
    Bridge wires this to ``_dashboard_scene_synthesis_cache_getter``,
    which fetches from dotty-behaviour's
    ``/api/scene-synthesis/recent``."""
    getter = _state.get("scene_synthesis_cache_getter")
    if not getter:
        return {}
    try:
        result = getter()
    except Exception:
        log.warning("scene_synthesis_cache_getter raised", exc_info=True)
        return {}
    return result if isinstance(result, dict) else {}


# Bridge proxies the dotty-behaviour binary JPEG endpoint so the browser
# only ever talks to the bridge origin (CORS-safe, dashboard remains the
# single dashboard entry point). Read the same env var bridge.py uses so
# operators set it once.
_DOTTY_BEHAVIOUR_URL = os.environ.get(
    "DOTTY_BEHAVIOUR_URL", "http://localhost:8090"
).rstrip("/")


def _fetch_robot_photo(device_id: str) -> Response:
    """Proxy a JPEG from dotty-behaviour's /api/vision/photo/{device_id}.

    Used by both /ui/host/robot/photo/{device_id} (the host-detail modal)
    and /ui/vision/photo (the /ui/vision/large modal cache-buster URL).
    Raises HTTPException(404) when dotty-behaviour reports no photo for
    that device, HTTPException(503) on any other fetch failure so the
    modal's onerror handler can swap in a placeholder."""
    url = f"{_DOTTY_BEHAVIOUR_URL}/api/vision/photo/{device_id}"
    try:
        r = requests.get(url, timeout=2.0)
    except requests.RequestException as exc:
        log.warning("robot-photo proxy fetch failed (%s): %s", url, exc)
        raise HTTPException(503, "dotty-behaviour unreachable") from exc
    if r.status_code == 404:
        raise HTTPException(404, "no cached photo")
    if not r.ok:
        log.warning(
            "robot-photo proxy non-2xx (%s): status=%d", url, r.status_code,
        )
        raise HTTPException(503, "upstream photo fetch failed")
    return Response(
        content=r.content,
        media_type=r.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "no-store"},
    )


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
LOG_DIR = Path(os.environ.get("CONVO_LOG_DIR", "/var/lib/dotty-bridge/logs"))
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


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request) -> Any:
    return templates.TemplateResponse(
        request, "dashboard.html",
        {
            "version": BRIDGE_VERSION,
            "csrf_token": getattr(request.state, "csrf_token", ""),
            **_static_chip_context(),
        },
    )


_ALLOWED_EMOJIS = ("😊", "😆", "😢", "😮", "🤔", "😠", "😐", "😍", "😴")
_EMOJI_FACE_NAMES = {
    "😊": "happy",
    "😆": "laughing",
    "😢": "sad",
    "😮": "surprised",
    "🤔": "thinking",
    "😠": "angry",
    "😐": "neutral",
    "😍": "loving",
    "😴": "sleepy",
}

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
async def alerts_count(request: Request, chip: int = 0) -> Any:
    """Q6: count today's errored turns from the convo log.

    Two render modes share the same count so the dashboard polls one URL:
      - default: legacy floating ``alerts_badge.html`` (kept for
        compatibility — currently unused by dashboard.html since the badge
        was folded into the Errors filter chip).
      - ``?chip=1``: returns the chip's label text (``Errors`` or
        ``Errors (N)``) plus an inline script that toggles ``btn-error`` on
        the chip. innerHTML swap target is the chip itself, so the script
        runs against its own parent element.
    """
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
    if chip:
        # Don't tint the chip red — selection (`btn-primary` via setFeedFilter)
        # is the only "selected" cue; the chip just shows a warning glyph +
        # count when there are unread errors. User feedback: red without
        # selection looked permanently selected.
        if n > 0:
            return HTMLResponse(
                f'<span aria-hidden="true" class="mr-0.5">⚠</span>Errors ({n})'
            )
        return HTMLResponse("Errors")
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
    name = _EMOJI_FACE_NAMES.get(emoji, "")
    if emoji == "😠" and name == "angry":
        kid_getter = _state.get("kid_mode_getter")
        kid_on = bool(kid_getter()) if kid_getter else True
        if not kid_on:
            name = "war"
    if name:
        prompt = (
            f"Make the {emoji} face. Reply with exactly: "
            f"'{emoji} This is my {name} face.' — nothing else."
        )
    else:
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


@router.post("/actions/start-story", response_class=HTMLResponse,
             include_in_schema=False)
async def start_story(request: Request, text: str = Form(...)) -> Any:
    """Inject a story-seed prompt and (if needed) flip state to story_time.

    Reuses say_result.html — the success layout fits "you said / Dotty
    replied" naturally for a seed payload too.
    """
    text = (text or "").strip()
    if not text:
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False, "error": "Empty seed — describe a story for Dotty to start."},
        )
    if len(text) > 500:
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False, "error": "Too long — keep it under 500 characters."},
        )
    # Same sanitisation as /actions/say: strip C0 + DEL, collapse whitespace.
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = " ".join(text.split())
    if not text:
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False, "error": "Seed was empty after sanitisation."},
        )
    # Flip into story_time first if we're not already there, so the seed
    # turn lands in the right narrative state. Best-effort: a setter
    # failure shouldn't block the inject — Dotty can still start a story
    # while in idle/talk, just without the state-pip / story-mode prompt
    # tweaks the firmware applies.
    getter = _state.get("state_getter")
    setter = _state.get("state_setter")
    current = (getter() if getter else "") or ""
    if setter is not None and current != "story_time":
        try:
            await setter("story_time")
        except Exception:
            log.exception("start-story state flip failed (continuing with inject)")
    seed = f"Tell a new story about: {text}"
    return await _inject_or_error(request, seed, label=text)


def _latest_vision_entry() -> tuple[str, dict] | None:
    """Pick the most-recently captured device entry from the vision cache.

    Reads through the metadata-only getter — entries no longer carry
    jpeg_bytes locally. Callers that need the JPEG bytes must HTTP-proxy
    to dotty-behaviour via ``_fetch_robot_photo``."""
    cache = _vision_cache_snapshot()
    if not cache:
        return None
    device_id, entry = max(
        cache.items(), key=lambda kv: kv[1].get("timestamp", 0.0)
    )
    return device_id, entry


@router.get("/vision/photo", include_in_schema=False)
async def vision_photo(download: int = 0) -> Response:
    """Serve the latest captured JPEG. ?download=1 forces an attachment.

    Picks the most-recent device from the metadata cache then proxies to
    dotty-behaviour for the binary."""
    pick = _latest_vision_entry()
    if pick is None:
        raise HTTPException(status_code=404, detail="no recent capture")
    device_id, entry = pick
    resp = _fetch_robot_photo(device_id)
    if download:
        ts = int(entry.get("wall_ts") or time.time())
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(device_id)) or "device"
        filename = f"dotty-{safe_id}-{ts}.jpg"
        # Rebuild the response with the attachment header — Response
        # objects are immutable enough that re-wrapping is cleaner than
        # mutating headers in place.
        return Response(
            content=resp.body,
            media_type=resp.media_type or "image/jpeg",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )
    return resp


@router.get("/vision/large", response_class=HTMLResponse, include_in_schema=False)
async def vision_large(request: Request) -> Any:
    """Modal body for the full-size capture preview + download link."""
    pick = _latest_vision_entry()
    ctx: dict[str, Any] = {"have_photo": False}
    if pick is not None:
        device_id, entry = pick
        elapsed = max(0.0, time.monotonic() - entry.get("timestamp", time.monotonic()))
        # Cache-buster — the photo URL is stable across captures so without
        # this the browser would hand back the previous JPEG when the modal
        # reopens after a new capture.
        cb = int(entry.get("wall_ts") or time.time())
        # An entry's presence in the metadata cache implies a JPEG exists
        # in dotty-behaviour — the writer always populates jpeg_bytes
        # alongside the metadata fields. The proxy will 404 if that ever
        # changes, and the modal's onerror handler degrades gracefully.
        ctx = {
            "have_photo": True,
            "device_id": device_id,
            "description": entry.get("description", ""),
            "question": entry.get("question", ""),
            "age": _humanize_age(elapsed),
            "cache_buster": cb,
        }
    return templates.TemplateResponse(request, "vision_large.html", ctx)


# Smart-mode model-swap is v2 scope (docs/cutover-behaviour.md). These names
# describe the *intended* default/smart models for when the swap is wired;
# the Tier1Slim rollback provider that used to perform a live hot-swap was
# removed in the 2026-05-29 alignment pass, so today smart_mode is a
# toggle-only control (LED pip + flag) and the voice model is always the
# local PiVoiceLLM brain.
DEFAULT_MODEL_NAME = os.environ.get(
    "DOTTY_DEFAULT_MODEL", "mistralai/mistral-small-3.2-24b-instruct",
)
SMART_MODEL_NAME = os.environ.get("SMART_MODEL", "anthropic/claude-sonnet-4-6")


def _short_model(name: str) -> str:
    """Strip the provider prefix for compact dashboard display."""
    if not name:
        return ""
    return name.split("/", 1)[1] if "/" in name else name


def _host_detail_llm_label(smart_on: bool | None = None) -> str:
    """The 'LLM' row for host-detail modals. The live voice path is
    PiVoiceLLM (the brain runs in the dotty-pi container). smart_mode does
    NOT swap the backend model — that is v2 scope (docs/cutover-behaviour.md);
    the Tier1Slim rollback provider that used to do the swap was removed in
    the 2026-05-29 alignment pass. Mirrors smart_mode.html."""
    return "PiVoiceLLM · model-swap pending (v2)"


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


def _sound_balance_sparkline() -> dict | None:
    """Build the State-tile sound-balance sparkline (#69) — an SVG
    polyline over the last 60 s of localizer `balance` samples. Returns
    None when there's no recent sound data (fewer than 2 points)."""
    getter = _state.get("sound_balance_getter")
    series = getter() if getter else []
    if not series or len(series) < 2:
        return None
    width, height = 100.0, 24.0
    n = len(series)
    pts: list[str] = []
    for i, bal in enumerate(series):
        x = (i / (n - 1)) * width
        y = (1.0 - max(0.0, min(1.0, float(bal)))) * height
        pts.append(f"{x:.1f},{y:.1f}")
    return {
        "points": " ".join(pts),
        "last": series[-1],
        "width": width,
        "height": height,
    }


def _vision_failures_count() -> int:
    """Total vision-capture failures in the last hour (#74) — summed
    across error kinds. 0 when the getter is unwired or the window is
    clear."""
    getter = _state.get("vision_failures_getter")
    if getter is None:
        return 0
    try:
        return sum(getter().values())
    except Exception:
        return 0


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
            "sound_spark": _sound_balance_sparkline(),
        },
    )


# #65 tier 1 — voice tool inventory. Hardcoded list of the tools shipped
# by dotty-pi-ext (see dotty-pi-ext/package.json). Per the issue body the
# static list is intentional: "hardcoded; same five values until a sixth
# tool ships". Tier 2 (call counts via PiClient stdout parsing) and Tier 3
# (safety-denial counts) are deferred follow-ups.
_VOICE_TOOLS: list[dict[str, str]] = [
    {"name": "memory_lookup",
     "description": "Search Dotty's long-term memory by keyword"},
    {"name": "remember",
     "description": "Save a new memory (\"Brett's birthday is …\")"},
    {"name": "think_hard",
     "description": "Hand a hard reasoning task to the bigger 27B model"},
    {"name": "take_photo",
     "description": "Capture + VLM-describe the current camera frame"},
    {"name": "play_song",
     "description": "Play a song from Dotty's local catalogue"},
]


@router.get("/tools-inventory",
            response_class=HTMLResponse, include_in_schema=False)
async def tools_inventory_partial(request: Request) -> Any:
    """#65 tier 1 — static inventory of voice tools shipped by
    dotty-pi-ext. No backend state; the list changes only on redeploy."""
    return templates.TemplateResponse(
        request, "tools_inventory.html", {"tools": _VOICE_TOOLS},
    )


@router.get("/safety/recent", response_class=HTMLResponse, include_in_schema=False)
async def safety_recent(request: Request) -> Any:
    """#72 — recent content-filter hits from the in-memory ring (last 20).
    In-memory only; empties on a bridge restart."""
    from bridge.text import recent_content_filter_hits
    rows: list[dict[str, Any]] = []
    for hit in recent_content_filter_hits():
        ts = hit.get("ts") or 0
        try:
            time_str = datetime.fromtimestamp(ts).astimezone().strftime("%H:%M:%S")
        except Exception:
            time_str = "?"
        rows.append({
            "time": time_str,
            "tier": hit.get("tier") or "?",
            "rule": hit.get("rule") or "",
            "prefix": hit.get("prefix") or "",
        })
    return templates.TemplateResponse(
        request, "safety_recent.html", {"rows": rows},
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


@router.get("/memory", response_class=HTMLResponse, include_in_schema=False)
async def memory_partial(request: Request) -> Any:
    """#53 per-person memory review surface — approved records plus the
    kid-safety pending-review queue, grouped by person."""
    getter = _state.get("memory_records_getter")
    rows = (await getter()) if getter else []
    pending: dict[str, list] = {}
    approved: dict[str, list] = {}
    for r in rows:
        ns = r.get("namespace") or ""
        if ns.startswith("person_pending:"):
            pending.setdefault(ns[len("person_pending:"):], []).append(r)
        elif ns.startswith("person:"):
            approved.setdefault(ns[len("person:"):], []).append(r)
    return templates.TemplateResponse(
        request, "memory_list.html",
        {
            "available": getter is not None,
            "pending": pending,
            "approved": approved,
            "pending_count": sum(len(v) for v in pending.values()),
        },
    )


@router.post("/actions/memory/approve", response_class=HTMLResponse,
             include_in_schema=False)
async def memory_approve(request: Request, mem_id: str = Form(...)) -> Any:
    """Promote a pending per-person fact to readable memory."""
    fn = _state.get("memory_approve")
    if fn is None:
        raise HTTPException(503, "memory_approve not configured")
    try:
        result = await fn(mem_id)
        ok = bool(result.get("ok") if isinstance(result, dict) else result)
    except Exception as exc:
        log.exception("memory approve failed")
        return templates.TemplateResponse(
            request, "memory_result.html",
            {"ok": False, "action": "approve", "error": str(exc)},
        )
    return templates.TemplateResponse(
        request, "memory_result.html",
        {"ok": ok, "action": "approve",
         "error": None if ok else "row not found or not pending review"},
    )


@router.post("/actions/memory/redact", response_class=HTMLResponse,
             include_in_schema=False)
async def memory_redact(request: Request, mem_id: str = Form(...)) -> Any:
    """Delete a per-person memory row (approved or pending)."""
    fn = _state.get("memory_redact")
    if fn is None:
        raise HTTPException(503, "memory_redact not configured")
    try:
        result = await fn(mem_id)
        ok = bool(result.get("ok") if isinstance(result, dict) else result)
    except Exception as exc:
        log.exception("memory redact failed")
        return templates.TemplateResponse(
            request, "memory_result.html",
            {"ok": False, "action": "redact", "error": str(exc)},
        )
    return templates.TemplateResponse(
        request, "memory_result.html",
        {"ok": ok, "action": "redact",
         "error": None if ok else "row not found"},
    )


@router.get("/smart-mode", response_class=HTMLResponse, include_in_schema=False)
async def smart_mode_partial(request: Request) -> Any:
    getter = _state.get("smart_mode_getter")
    enabled = bool(getter()) if getter else False
    return templates.TemplateResponse(
        request, "smart_mode.html",
        {"enabled": enabled, "available": getter is not None,
         "smart_model": _short_model(SMART_MODEL_NAME),
         "default_model": _short_model(DEFAULT_MODEL_NAME),
         # Tier1Slim (the only provider that hot-swapped the voice model)
         # was removed in the 2026-05-29 alignment pass. smart_mode is now
         # toggle-only on the live PiVoiceLLM path; model-swap is v2 scope.
         "model_swap_active": False},
    )


def _pick_perception_device_id() -> str | None:
    """Single-device deployment helper — picks the most relevant device id
    to feed into the Perception card builder. Priority:
      1. The device with a fresh _vision_cache entry (most likely the one
         actively perceiving).
      2. Any device in _perception_state (single robot ➜ single key).
    Returns None when the bridge has not yet seen any device — the card
    then renders empty states across the board.
    """
    vc = _vision_cache_snapshot()
    if vc:
        try:
            return max(vc.items(), key=lambda kv: kv[1].get("wall_ts", 0.0))[0]
        except Exception:  # malformed cache — fall through
            pass
    psg = _state.get("perception_state_getter")
    if psg:
        try:
            states = psg() or {}
        except Exception:
            states = {}
        for did in states.keys():
            return did
    return None


def _build_perception_card_ctx(device_id: str | None) -> dict:
    """Assemble the template context for perception.html.

    Composes face state (existing 3-state dot), latest VLM image preview
    + description, latest audio caption (with the existing
    sound-direction heuristic as fallback), the most recent user voice
    line, and the most recent scene synthesis sentence. Reads only
    in-memory caches — no I/O.
    """
    psg = _state.get("perception_state_getter")
    name_lookup = _state.get("identity_display_name")
    vision_cache = _vision_cache_snapshot()
    audio_cache = _audio_cache_snapshot()
    synth_cache = _scene_synthesis_cache_snapshot()
    perception_recent = _state.get("perception_recent_getter")
    last_user_line_getter = _state.get("last_user_line_getter")
    state_getter = _state.get("state_getter")
    try:
        current_state = (state_getter() if state_getter else None) or "idle"
    except Exception:
        log.exception("perception card: state_getter failed")
        current_state = "idle"

    # --- face state (preserved from the prior 3-state implementation) ---
    face_state = "off"
    face_label = "off"
    face_color = "#374151"
    face_tip = "no face in frame"
    pstates: dict = {}
    if psg:
        try:
            pstates = psg() or {}
        except Exception:
            log.exception("perception card: perception_state_getter failed")
            pstates = {}
    dev_state: dict | None = None
    if device_id and isinstance(pstates.get(device_id), dict):
        dev_state = pstates.get(device_id)
    elif pstates:
        # Fall back to the first device — single-robot deployment.
        for v in pstates.values():
            if isinstance(v, dict):
                dev_state = v
                break
    # Identity rendering is TTL-bound on `last_face_recognized_t` so the
    # chip stays green across detector flicker (HuMan model drops the bbox
    # for ~1 s every few seconds even when a person is plainly in frame).
    # Falls back to "detected" when face_present alone is true; "off" when
    # both signals are absent. Mirrors bridge/perception/cache.py.
    if dev_state:
        identity = (dev_state.get("last_face_id") or "").strip()
        last_recog_t = dev_state.get("last_face_recognized_t") or 0.0
        identified_fresh = (
            identity
            and identity != "unknown"
            and last_recog_t
            and (time.time() - float(last_recog_t)) <= 30.0
        )
        if identified_fresh:
            name = None
            if name_lookup:
                try:
                    name = name_lookup(identity)
                except Exception:
                    name = None
            face_state = "identified"
            face_label = name or identity
            face_color = "#008c1e"
            face_tip = f"face: identified ({identity})"
        elif dev_state.get("face_present"):
            face_state = "detected"
            face_label = "detected"
            face_color = "#a88c00"
            face_tip = "face: detected, awaiting identification"

    # --- listening state (mirrors the bottom red pixel on the right ring) ---
    # Driven by the firmware's edge-only `chat_status` event — see
    # bridge.py::_dispatch_perception_event. Hue (#780000) matches the
    # physical LED's (120,0,0) so the dashboard reads as a true mirror.
    listening = bool(dev_state.get("listening")) if dev_state else False
    listening_color = "#780000" if listening else "#374151"
    listening_label = "listening" if listening else "off"
    listening_tip = (
        "xiaozhi is in LISTENING — Dotty's mic is open"
        if listening
        else "not listening"
    )

    # --- vision preview + description ---
    latest_vision: dict | None = None
    if device_id:
        vc_entry = vision_cache.get(device_id) or {}
        if vc_entry:
            wall_ts = vc_entry.get("wall_ts")
            age_label = "—"
            age_s = float("inf")
            if isinstance(wall_ts, (int, float)):
                age_s = max(0.0, time.time() - float(wall_ts))
                age_label = _humanize_age(age_s)
            # has_photo derives from entry presence — jpeg_bytes is no
            # longer in the metadata-only payload, but the explain writer
            # always populates it alongside the metadata fields.
            latest_vision = {
                "description": (vc_entry.get("description") or "").strip(),
                "source": vc_entry.get("source") or "room_view",
                "age_label": age_label,
                "stale": age_s > 55.0,
                "has_photo": True,
            }

    # --- audio caption (real description ➜ heuristic fallback) ---
    audio_text = ""
    audio_age_label = "—"
    audio_is_caption = False
    audio_stale = False
    if device_id:
        ac_entry = audio_cache.get(device_id) or {}
        if ac_entry:
            wall_ts = ac_entry.get("wall_ts")
            age_s = float("inf")
            if isinstance(wall_ts, (int, float)):
                age_s = max(0.0, time.time() - float(wall_ts))
                audio_age_label = _humanize_age(age_s)
            # Audio cache TTL on the bridge side is 120 s; flip to stale a
            # bit before that so the badge dims rather than vanishing
            # mid-glance.
            if age_s <= 120.0:
                audio_text = (ac_entry.get("description") or "").strip()
                audio_is_caption = bool(audio_text)
                audio_stale = age_s > 100.0
    if not audio_is_caption:
        # Fall back to the sound-direction/energy heuristic the robot
        # modal already uses. Same window, same wording.
        events: list[dict] = []
        if perception_recent and device_id:
            try:
                events = perception_recent(device_id, 12) or []
            except Exception:
                log.warning("perception_recent_getter raised", exc_info=True)
                events = []
        audio_text = _summarise_audio_from_perception(events)

    # --- last user voice line (post-ASR transcript) ---
    # Mirrors the audio-caption staleness convention: dim past 100 s so
    # the badge fades rather than vanishing mid-glance.
    latest_voice_line: dict | None = None
    if device_id and last_user_line_getter:
        try:
            entry = last_user_line_getter(device_id) or {}
        except Exception:
            log.warning("last_user_line_getter raised", exc_info=True)
            entry = {}
        if entry:
            text = (entry.get("text") or "").strip()
            wall_ts = entry.get("wall_ts")
            age_label = "—"
            stale = False
            if isinstance(wall_ts, (int, float)):
                age_s = max(0.0, time.time() - float(wall_ts))
                age_label = _humanize_age(age_s)
                stale = age_s > 100.0
            if text:
                latest_voice_line = {
                    "text": text,
                    "age_label": age_label,
                    "stale": stale,
                }

    # --- scene synthesis ---
    synth_entry = (synth_cache.get(device_id) or {}) if device_id else {}
    synthesis_text = (synth_entry.get("text") or "").strip()
    synthesis_age_label = "—"
    if isinstance(synth_entry.get("ts_wall"), (int, float)):
        synthesis_age_label = _humanize_age(
            max(0.0, time.time() - float(synth_entry["ts_wall"]))
        )

    return {
        "device_id": device_id or "",
        "current_state": current_state,
        "face_state": face_state,
        "face_label": face_label,
        "face_color": face_color,
        "face_tip": face_tip,
        "listening": listening,
        "listening_color": listening_color,
        "listening_label": listening_label,
        "listening_tip": listening_tip,
        "latest_vision": latest_vision,
        "audio_text": audio_text,
        "audio_age_label": audio_age_label,
        "audio_is_caption": audio_is_caption,
        "audio_stale": audio_stale,
        "latest_voice_line": latest_voice_line,
        "synthesis_text": synthesis_text,
        "synthesis_age_label": synthesis_age_label,
        "vision_failures_1h": _vision_failures_count(),
    }


@router.get("/perception", response_class=HTMLResponse, include_in_schema=False)
async def perception_partial(request: Request) -> Any:
    """Top-level Perception card — what Dotty is sensing right now.

    Layout: face status row, scene view (cached image + VLM "Sees:"),
    audio row (caption with heuristic fallback), and the most recent
    ambient synthesis sentence. Reads only in-memory caches; refreshes
    on `dotty-refresh` (SSE-driven) plus a slow polling fallback so age
    labels stay fresh during quiet stretches.
    """
    device_id = _pick_perception_device_id()
    ctx = _build_perception_card_ctx(device_id)
    return templates.TemplateResponse(request, "perception.html", ctx)


@router.post("/actions/smart-mode", response_class=HTMLResponse, include_in_schema=False)
async def smart_mode_set(request: Request, enabled: str = Form("")) -> Any:
    setter = _state.get("smart_mode_setter")
    if setter is None:
        raise HTTPException(503, "smart_mode_setter not configured")
    new_state = enabled.lower() in ("on", "true", "1", "yes")
    try:
        result = await setter(new_state)
    except Exception as exc:
        log.exception("smart_mode setter failed")
        return templates.TemplateResponse(
            request, "smart_mode_result.html",
            {"ok": False, "error": str(exc)},
        )
    if isinstance(result, dict):
        ok = bool(result.get("ok"))
        err = result.get("error")
    else:
        ok = bool(result)
        err = None
    if not ok:
        return templates.TemplateResponse(
            request, "smart_mode_result.html",
            {"ok": False, "error": err or "Smart mode toggle failed."},
        )
    return templates.TemplateResponse(
        request, "smart_mode_result.html",
        {"ok": True, "new_state": new_state},
    )


@router.post("/actions/kid-mode", response_class=HTMLResponse, include_in_schema=False)
async def kid_mode_set(request: Request, enabled: str = Form("")) -> Any:
    """Persist Kid Mode state and hot-reload the guardrail globals via the
    setter (`_dashboard_set_kid_mode` → `_apply_kid_mode`). No daemon
    restart needed since the 2026-04-29 hot-load refactor.
    """
    setter = _state.get("kid_mode_setter")
    if setter is None:
        raise HTTPException(503, "kid_mode_setter not configured")
    new_state = enabled.lower() in ("on", "true", "1", "yes")
    try:
        result = await setter(new_state)
    except Exception as exc:
        log.exception("kid_mode setter failed")
        return templates.TemplateResponse(
            request, "kid_mode_result.html",
            {"ok": False, "error": str(exc)},
        )
    if isinstance(result, dict):
        ok = bool(result.get("ok"))
        err = result.get("error")
    else:
        ok = bool(result)
        err = None
    if not ok:
        return templates.TemplateResponse(
            request, "kid_mode_result.html",
            {"ok": False, "error": err or "Kid mode toggle failed."},
        )
    return templates.TemplateResponse(
        request, "kid_mode_result.html",
        {"ok": True, "new_state": new_state},
    )


# --- Version chip (read-only) --------------------------------------------

GITHUB_REPO = os.environ.get(
    "DOTTY_BRIDGE_REPO", "https://github.com/BrettKinny/dotty-stackchan.git"
)


def _static_chip_context() -> dict[str, Any]:
    """Context for the read-only version chip: the running build linked to
    its commit on GitHub. No update detection — see version_chip()."""
    repo_url = GITHUB_REPO.removesuffix(".git")
    label = BRIDGE_VERSION if BRIDGE_VERSION and BRIDGE_VERSION != "unknown" else "unknown"
    return {
        "installed_display": f"v{label}",
        "installed_href": (
            f"{repo_url}/commit/{label}" if label != "unknown" else repo_url
        ),
        "installed_title": (
            f"Bridge build {label} — opens this commit on GitHub"
            if label != "unknown"
            else "Bridge build (unknown) — opens repo on GitHub"
        ),
        "update_available": False,
        "update_display": None,
        "update_title": None,
    }


@router.get("/version-chip",
            response_class=HTMLResponse, include_in_schema=False)
async def version_chip(request: Request) -> Any:
    """Render the static bridge-version label in the header.

    The self-update / preview / restart / reboot apparatus was removed in
    the 2026-05-29 alignment pass (review decision F): it invoked a retired
    `systemctl restart zeroclaw-bridge` unit that does not exist in the
    container and git-pulled into the dead /root/zeroclaw-bridge install
    dir, so the buttons no-op'd while reporting success. Deploys now go
    through scripts/deploy-bridge-unraid.sh (build + `compose up -d`); the
    container restart policy (`restart: unless-stopped`) handles restarts.
    The chip is now a plain version label linking to the built commit.
    """
    return templates.TemplateResponse(
        request, "version_chip.html", _static_chip_context(),
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


# Single-page redesign: compact host dots (header placement) + system pills
# (footer placement). One endpoint, two placements, polled at the same 10s
# cadence.
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
            # Binary heartbeat — voice idle is normal, no warn band.
            # Real connectivity loss surfaces via perception_state_getter
            # below, which DOES still keep its warn semantics because a
            # stale perception bus is a real bug worth flagging.
            sc_status = "ok"
            sc_tip = f"Dotty: active {_humanize_age(age)} ago"

        # Perception bus liveness — kept as a warn-only signal because a
        # stale bus genuinely indicates a hung firmware perception path
        # (face/sound events stopped flowing past PERCEPTION_STALE_THRESHOLD_S
        # in bridge.py). This is the only path that can degrade the robot
        # dot below "ok" in the redesigned binary model.
        psg = _state.get("perception_state_getter")
        if psg is not None:
            try:
                pstate = psg() or {}
            except Exception:
                pstate = {}
            stale_devs = [
                did for did, s in pstate.items()
                if s and s.get("sensor_stale")
            ]
            fresh_devs = [
                did for did, s in pstate.items()
                if s and not s.get("sensor_stale")
            ]
            if stale_devs and sc_status in ("ok", "unknown"):
                sc_status = "warn"
                sc_tip = (
                    f"Dotty: perception sensors stale "
                    f"({len(stale_devs)}/{len(pstate)} dev) — "
                    f"firmware bus may be hung"
                )
            elif fresh_devs and sc_status == "unknown":
                # The robot is connected and streaming fresh perception
                # events even though there's been no voice turn today.
                # Voice idleness is normal; perception liveness is the
                # real "robot is online" signal — so don't show grey.
                sc_status = "ok"
                sc_tip = "Dotty: online — perception active, no voice yet today"

        if not XIAOZHI_HOST:
            xz_status, xz_tip = "unknown", "unraid: XIAOZHI_HOST env not set"
        elif xz_ota_ok and xz_ws_ok:
            xz_status, xz_tip = "ok", f"unraid: OTA :{XIAOZHI_OTA_PORT} + WS :{XIAOZHI_WS_PORT}"
        elif xz_ota_ok or xz_ws_ok:
            xz_status, xz_tip = "warn", "unraid: partial reachability"
        else:
            xz_status, xz_tip = "bad", "unraid: no ports responding"

        # Face status now lives in the dedicated Perception card (`/ui/perception`)
        # rather than the hardware-reachability strip — it's a software signal,
        # not a "host is alive" signal.
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
            # Entry presence implies jpeg_bytes upstream (the metadata-
            # only getter strips the field, but the explain writer
            # always sets both fields together).
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
    extra: dict[str, Any] = {}

    if slug == "bridge":
        title = "Bridge"
        upt = _proc_uptime_sec()
        cpu_c = _cpu_temp_c()
        mem = _read_memory_mb()
        disk = _disk_usage_root()
        # Two orthogonal toggles + one active LLM. kid_mode is guardrails
        # only; the LLM row is honest about whether a model swap actually
        # happens (see _host_detail_llm_label).
        kid_getter = _state.get("kid_mode_getter")
        smart_getter = _state.get("smart_mode_getter")
        kid_on = bool(kid_getter()) if kid_getter else None
        smart_on = bool(smart_getter()) if smart_getter else None
        active_llm = _host_detail_llm_label(smart_on)
        facts = [
            ("Device",     "Raspberry Pi"),
            ("Status",     "online"),
            ("Version",    BRIDGE_VERSION),
            ("Uptime",     _humanize_age(time.time() - _START_TIME)),
            ("Host up",    _humanize_age(upt) if upt else "n/a"),
            ("Logs dir",   str(LOG_DIR)),
            ("Kid Mode",   "on" if kid_on else ("off" if kid_on is False else "unknown")),
            ("Smart Mode", "on" if smart_on else ("off" if smart_on is False else "unknown")),
            ("Active LLM", active_llm),
        ]
        # Bottom-bar diagnostics absorbed into this modal — UI-1 is
        # removing the footer rendering separately, so this becomes the
        # canonical place to read host vitals.
        extra["diagnostics"] = {
            "cpu_c": cpu_c,
            "cpu_warn": cpu_c is not None and cpu_c >= 75,
            "mem_pct": (
                int(round((mem[0] / mem[1]) * 100)) if mem and mem[1] else None
            ),
            "mem_warn": bool(mem and mem[1] and (mem[0] / mem[1]) > 0.85),
            "disk_pct": (
                int(round((disk[0] / disk[1]) * 100)) if disk and disk[1] else None
            ),
            "disk_warn": bool(disk and disk[1] and (disk[0] / disk[1]) > 0.85),
            "uptime": _humanize_age(upt) if upt else None,
        }
    elif slug == "server":
        title = "Server (xiaozhi-esp32-server)"
        ota_ok, ws_ok = await asyncio.gather(
            _tcp_reachable(XIAOZHI_HOST, XIAOZHI_OTA_PORT),
            _tcp_reachable(XIAOZHI_HOST, XIAOZHI_WS_PORT),
        )
        n = await _xiaozhi_device_count()
        # The LLM row is honest about whether a model swap actually
        # happens (see _host_detail_llm_label); kid_mode is guardrails only.
        smart_getter = _state.get("smart_mode_getter")
        smart_on = bool(smart_getter()) if smart_getter else None
        current_llm = _host_detail_llm_label(smart_on)
        # Voice-channel state — derived from the firmware state getter
        # the dashboard already reads. talk / story_time mean active.
        st_getter = _state.get("state_getter")
        firmware_state = (st_getter() if st_getter else None) or "idle"
        voice_active = firmware_state in ("talk", "story_time")
        # TTS / ASR provider names — these live in the xiaozhi-server
        # YAML on Unraid; the bridge does not currently poll for them.
        # TODO: when xiaozhi exposes /xiaozhi/admin/config (or similar),
        # query it instead of hardcoding. For now we surface the known
        # deployment values so the modal isn't blank.
        facts = [
            ("Host",     XIAOZHI_HOST or "(unset)"),
            ("OTA :%d" % XIAOZHI_OTA_PORT, "reachable" if ota_ok else "unreachable"),
            ("WS :%d"  % XIAOZHI_WS_PORT,  "reachable" if ws_ok  else "unreachable"),
            ("Devices connected", "—" if n is None else f"{n}"),
            ("Voice channel", "active" if voice_active else "idle"),
            ("Current LLM", current_llm),
            ("TTS provider", "LocalPiper"),
            ("ASR provider", "SenseVoice"),
            # TODO: surface today's xiaozhi error count once the bridge
            # tails the container log or xiaozhi exposes a metric.
            ("Recent errors today", "—"),
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
        # Perception bus liveness — separate from voice activity. Stale
        # means firmware-side face/sound events have stopped flowing
        # (face_detected, sound_event, state_changed) past the bridge's
        # PERCEPTION_STALE_THRESHOLD_S window. A live device with stale
        # perception is a useful early-warning that a perception modifier
        # has hung even though the voice path still works.
        perception_label = "no events yet"
        psg = _state.get("perception_state_getter")
        if psg is not None:
            try:
                pstate = psg() or {}
            except Exception:
                pstate = {}
            if pstate:
                stale = [
                    did for did, s in pstate.items()
                    if s and s.get("sensor_stale")
                ]
                if stale:
                    ages = [
                        s.get("sensor_age_s") for _, s in pstate.items()
                        if s and s.get("sensor_stale")
                    ]
                    finite_ages = [a for a in ages if a not in (None, float("inf"))]
                    if finite_ages:
                        oldest = max(finite_ages)
                        perception_label = (
                            f"stale ({len(stale)}/{len(pstate)} dev, "
                            f"oldest {_humanize_age(oldest)})"
                        )
                    else:
                        perception_label = (
                            f"stale ({len(stale)}/{len(pstate)} dev, never)"
                        )
                else:
                    youngest = min(
                        (s.get("sensor_age_s", float("inf"))
                         for s in pstate.values() if s),
                        default=float("inf"),
                    )
                    if youngest != float("inf"):
                        perception_label = (
                            f"live ({_humanize_age(youngest)} since last)"
                        )
                    else:
                        perception_label = "live"
        # Pick the most-recent VLM-cached entry's device id for the
        # "latest view" thumbnail. The /ui/host/robot/photo/{mac}
        # endpoint serves the JPEG bytes — we don't link directly to
        # /api/vision/latest/{mac} because that endpoint is JSON-only
        # and blocks waiting for a fresh capture.
        latest = _latest_vision_entry()
        if latest is not None:
            extra["photo_device_id"] = latest[0]
        facts = [
            ("Device class", "M5Stack StackChan (ESP32-S3)"),
            ("Connection",
             "online" if (n is not None and n > 0)
             else ("offline" if n == 0 else "unknown")),
            ("Last seen",   seen),
            ("Current state", current),
            ("Perception",  perception_label),
        ]

    return templates.TemplateResponse(
        request, "host_detail.html",
        {"title": title, "facts": facts, "slug": slug, **extra},
    )


# Robot modal photo — proxies the JPEG from dotty-behaviour's
# /api/vision/photo/{device_id} binary endpoint so the modal can render
# it with a plain <img src> and the browser only ever talks to the
# bridge origin (CORS-safe, dashboard remains the single dashboard
# entry point). We can't point the <img> at /api/vision/latest/{mac}
# because that endpoint blocks for up to 15 s waiting for a fresh
# capture and returns JSON, not an image. Returns 404 if dotty-
# behaviour reports no photo for that device, which lets the modal's
# onerror handler swap in a placeholder.
@router.get("/host/robot/photo/{device_id}", include_in_schema=False)
async def host_robot_photo(device_id: str) -> Response:
    return _fetch_robot_photo(device_id)


# --- "Scene context" panel for the Robot modal ---------------------------
# Surfaces what Dotty has lately seen and heard. Composes three sources:
#   1. _vision_cache — most-recent VLM photo + description (room_view today,
#      security_capture once the relays land).
#   2. perception ring buffer — last ~20 face_detected/face_lost/sound_event/
#      state_changed events, populated from bridge._perception_broadcast.
#   3. security_watch.RECENT_CYCLES — last few security cycles (text-only;
#      currently every cycle is errors=[photo_dispatch_failed,...] until the
#      xiaozhi-server admin route lands).
# Polled by the Robot modal every ~7 s via HTMX. Returns rendered HTML so
# the panel can be hot-swapped without client-side JSON shuffling.

_AUDIO_SUMMARY_WINDOW_S: float = 120.0  # "last 2 min"


def _summarise_audio_from_perception(events: list[dict]) -> str:
    """Heuristic: summarise sound_event entries in the last
    _AUDIO_SUMMARY_WINDOW_S seconds. Returns a one-liner suitable for the
    "What Dotty hears" line. Pure function — no I/O.

    We don't have transcripts; the firmware emits direction + balance +
    energy. We bucket direction (left/right/center) and report counts.
    """
    now = time.time()
    sounds = [
        ev for ev in events
        if ev.get("name") == "sound_event"
        and isinstance(ev.get("ts"), (int, float))
        and (now - float(ev["ts"])) <= _AUDIO_SUMMARY_WINDOW_S
    ]
    if not sounds:
        return "Quiet — no sound impulses in the last 2 min."
    by_dir: dict[str, int] = {}
    for ev in sounds:
        d = (ev.get("data") or {}).get("direction") or "unknown"
        by_dir[d] = by_dir.get(d, 0) + 1
    parts = [f"{n} from {d}" for d, n in sorted(by_dir.items(), key=lambda kv: -kv[1])]
    return f"{len(sounds)} sound impulse{'s' if len(sounds) != 1 else ''} in last 2 min — " + ", ".join(parts) + "."


def _render_perception_event(ev: dict) -> dict:
    """Format one perception event for display. Returns a dict with
    pre-computed fields the template can render flat."""
    name = ev.get("name") or ""
    data = ev.get("data") or {}
    ts = ev.get("ts")
    age_label = "—"
    if isinstance(ts, (int, float)):
        age_label = _humanize_age(max(0.0, time.time() - float(ts)))
    detail = ""
    if name == "sound_event":
        d = data.get("direction") or "?"
        e = data.get("energy")
        if e is not None:
            try:
                detail = f"from {d}, energy {int(e):,}"
            except (TypeError, ValueError):
                detail = f"from {d}"
        else:
            detail = f"from {d}"
    elif name == "state_changed":
        s = data.get("state") or "?"
        detail = f"→ {s}"
    elif name == "face_recognized":
        ident = data.get("identity") or "?"
        detail = f"as {ident}"
    elif name == "face_identified_applied":
        # Firmware accepted set_face_identified MCP — green pip lit.
        detail = "green pip lit"
    elif name == "face_identified_rejected":
        # Firmware no-op'd set_face_identified MCP (face was gone past the
        # flicker grace window). Surfaced so the dashboard shows when the
        # bridge tried but the device couldn't honour it.
        detail = "no face in frame"
    return {"name": name, "age_label": age_label, "detail": detail}


def _build_security_panel_ctx(device_id: str) -> dict:
    """Assemble the template context for security_panel.html. Pulls from
    the vision cache getter, the perception ring (via the configured
    getter), and bridge.security_watch.RECENT_CYCLES (filtered to
    device_id)."""
    cache = _vision_cache_snapshot()
    cache_entry = cache.get(device_id) or {}

    latest_vision: dict | None = None
    if cache_entry:
        wall_ts = cache_entry.get("wall_ts")
        age_label = "—"
        if isinstance(wall_ts, (int, float)):
            age_label = _humanize_age(max(0.0, time.time() - float(wall_ts)))
        # has_photo derives from entry presence — see _build_perception_
        # card_ctx for the same rationale.
        latest_vision = {
            "description": (cache_entry.get("description") or "").strip(),
            "source": cache_entry.get("source") or "room_view",
            "age_label": age_label,
            "has_photo": True,
        }

    perception_getter = _state.get("perception_recent_getter")
    perception_events: list[dict] = []
    if perception_getter is not None:
        try:
            perception_events = perception_getter(device_id, 12) or []
        except Exception:
            log.warning("perception_recent_getter raised", exc_info=True)
            perception_events = []

    audio_summary = _summarise_audio_from_perception(perception_events)
    perception_view = [_render_perception_event(e) for e in perception_events]

    # Security cycles — pull text-only records from the existing ring buffer.
    cycles_view: list[dict] = []
    try:
        from bridge import security_watch  # local import — avoid hard dep
        all_cycles = security_watch.get_recent_cycles(limit=10)
    except Exception:
        all_cycles = []
    for rec in all_cycles:
        if rec.get("device") != device_id:
            continue
        # The ts on cycle records is an isoformat string from
        # datetime.now(LOCAL_TZ); show its hh:mm:ss tail for the panel.
        ts_iso = rec.get("ts") or ""
        ts_short = ts_iso[11:19] if len(ts_iso) >= 19 else ts_iso
        cycles_view.append({
            "ts_short": ts_short,
            "photo_desc": (rec.get("photo_desc") or "").strip(),
            "audio_transcript": rec.get("audio_transcript") or "—",
            "errors": rec.get("errors") or [],
        })
        if len(cycles_view) >= 5:
            break

    state_getter = _state.get("state_getter")
    current_state = (state_getter() if state_getter else None) or "idle"

    return {
        "device_id": device_id,
        "current_state": current_state,
        "latest_vision": latest_vision,
        "audio_summary": audio_summary,
        "perception_events": perception_view,
        "cycles": cycles_view,
    }


@router.get("/security/recent/{device_id}", response_class=HTMLResponse,
            include_in_schema=False)
async def security_recent(request: Request, device_id: str) -> Any:
    """Render the Robot modal's "Scene context" panel for ``device_id``.

    HTMX swaps this in every ~7 s. Inherits dashboard auth via the router-
    level dependency. Composes from in-memory sources only — no disk I/O,
    no media bytes leave the process.
    """
    ctx = _build_security_panel_ctx(device_id)
    return templates.TemplateResponse(request, "security_panel.html", ctx)


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


