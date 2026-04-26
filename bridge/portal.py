"""Mobile-first admin portal for Dotty.

Mounted at ``/ui`` on the bridge FastAPI app. Read-only MVP: host status
cards, conversation log tail. No state-mutating actions.

Host probes are env-driven so this stays generic in the public template:
set ``UNRAID_HOST`` (and optionally ``WORKSTATION_HOST``) on the bridge
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
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

log = logging.getLogger("portal")

# Bridge wires its in-process message handler in via configure(). Lets the
# "Say" action invoke the same path /api/message uses without an HTTP hop.
_state: dict[str, Any] = {
    "send_message": None,
    "vision_cache": None,
    "kid_mode_getter": None,
    "kid_mode_setter": None,
    "inject_to_device": None,
    "abort_device": None,
    "subscribe_events": None,
    "unsubscribe_events": None,
}


def configure(*, send_message: Any = None, vision_cache: dict | None = None,
              kid_mode_getter: Any = None, kid_mode_setter: Any = None,
              inject_to_device: Any = None, abort_device: Any = None,
              subscribe_events: Any = None,
              unsubscribe_events: Any = None) -> None:
    """Register bridge state with the portal. Idempotent."""
    if send_message is not None:
        _state["send_message"] = send_message
    if vision_cache is not None:
        _state["vision_cache"] = vision_cache
    if kid_mode_getter is not None:
        _state["kid_mode_getter"] = kid_mode_getter
    if kid_mode_setter is not None:
        _state["kid_mode_setter"] = kid_mode_setter
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


def _read_bridge_version() -> str:
    """Short git SHA of the deployed bridge. Cached at module load — picks
    up changes on the next systemd restart, which `Update from GitHub` does
    automatically."""
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
_PORTAL_USER = os.environ.get("DOTTY_PORTAL_USER", "")
_PORTAL_PASS = os.environ.get("DOTTY_PORTAL_PASS", "")
_basic = HTTPBasic(auto_error=False)


def _verify_portal_auth(
    credentials: HTTPBasicCredentials | None = Depends(_basic),
) -> None:
    if not _PORTAL_USER or not _PORTAL_PASS:
        return
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="dotty"'},
        )
    user_ok = secrets.compare_digest(credentials.username, _PORTAL_USER)
    pass_ok = secrets.compare_digest(credentials.password, _PORTAL_PASS)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="dotty"'},
        )


router = APIRouter(
    prefix="/ui", tags=["portal"],
    dependencies=[Depends(_verify_portal_auth)],
)

UNRAID_HOST = os.environ.get("UNRAID_HOST", "")
UNRAID_OTA_PORT = int(os.environ.get("UNRAID_OTA_PORT", "8003"))
UNRAID_WS_PORT = int(os.environ.get("UNRAID_WS_PORT", "8000"))
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
    something the kid actually said. Filter them out of the portal log so
    the conversation history stays readable."""
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
    rpi_uptime = time.time() - _START_TIME

    unraid_ota_ok, unraid_ws_ok = await asyncio.gather(
        _tcp_reachable(UNRAID_HOST, UNRAID_OTA_PORT),
        _tcp_reachable(UNRAID_HOST, UNRAID_WS_PORT),
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

    if not UNRAID_HOST:
        unraid_status = "unknown"
        unraid_detail = "UNRAID_HOST env not set"
    elif unraid_ota_ok and unraid_ws_ok:
        unraid_status = "ok"
        unraid_detail = f"OTA :{UNRAID_OTA_PORT} + WS :{UNRAID_WS_PORT}"
    elif unraid_ota_ok or unraid_ws_ok:
        unraid_status = "warn"
        unraid_detail = "partial: " + (
            f"OTA :{UNRAID_OTA_PORT}" if unraid_ota_ok else f"WS :{UNRAID_WS_PORT}"
        )
    else:
        unraid_status = "bad"
        unraid_detail = "no ports responding"

    cards_data = [
        {"name": "StackChan", "kind": "robot", "status": sc_status,
         "detail": sc_detail, "last_seen": sc_last},
        {"name": "RPi (bridge)", "kind": "host", "status": "ok",
         "detail": f"bridge up {_humanize_age(rpi_uptime)}", "last_seen": ""},
        {"name": "Unraid (xiaozhi)", "kind": "host", "status": unraid_status,
         "detail": unraid_detail, "last_seen": ""},
    ]
    return templates.TemplateResponse(
        request, "cards.html", {"cards": cards_data}
    )


_ALLOWED_EMOJIS = ("😊", "😆", "😢", "😮", "🤔", "😠", "😐", "😍", "😴")
_DANCES = (
    {"key": "macarena", "label": "Macarena", "phrase": "do the macarena", "icon": "💃"},
    {"key": "sing", "label": "Sing a song", "phrase": "sing a song", "icon": "🎤"},
)


async def _xiaozhi_device_count() -> int | None:
    """Count active StackChan WS connections via the admin endpoint.
    Returns None if xiaozhi is unreachable."""
    if not UNRAID_HOST:
        return None
    url = f"http://{UNRAID_HOST}:{UNRAID_OTA_PORT}/xiaozhi/admin/devices"
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


@router.get("/face", response_class=HTMLResponse, include_in_schema=False)
async def face_partial(request: Request) -> Any:
    """Show the most recent emoji Dotty used today (P9)."""
    today = datetime.now().strftime("%Y-%m-%d")
    path = _log_path_for(today)
    last_emoji = ""
    last_age = ""
    if path.exists():
        try:
            data = path.read_bytes().splitlines()
        except OSError:
            data = []
        for line in reversed(data):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            e = (rec.get("emoji_used") or "").strip()
            if e:
                last_emoji = e
                ts = _parse_ts(rec.get("ts", ""))
                if ts is not None:
                    last_age = _humanize_age(max(0.0, time.time() - ts)) + " ago"
                break
    return templates.TemplateResponse(
        request, "face.html",
        {"emoji": last_emoji, "age": last_age},
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
async def dance(request: Request, key: str = Form(...)) -> Any:
    pick = next((d for d in _DANCES if d["key"] == key), None)
    if pick is None:
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False, "error": "Unknown dance/song."},
        )
    return await _inject_or_error(request, pick["phrase"], label=pick["phrase"])


_PRESETS = {
    "bedtime": {
        "label": "Bedtime",
        "icon": "🌙",
        "volume": 15,
        "kid_mode": True,
        "summary": "volume 15 + kid mode on",
    },
    "quiet": {
        "label": "Quiet",
        "icon": "🤫",
        "volume": 10,
        "summary": "volume 10",
    },
    "loud": {
        "label": "Loud",
        "icon": "📢",
        "volume": 70,
        "summary": "volume 70",
    },
    "adult": {
        "label": "Adult mode",
        "icon": "🧠",
        "volume": 50,
        "kid_mode": False,
        "summary": "volume 50 + kid mode off",
    },
}


@router.post("/actions/preset", response_class=HTMLResponse,
             include_in_schema=False)
async def preset(request: Request, name: str = Form(...)) -> Any:
    pick = _PRESETS.get(name)
    if pick is None:
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False, "error": "Unknown preset."},
        )
    inject = _state.get("inject_to_device")
    if inject is None:
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False, "error": "Inject path not configured."},
        )
    actions_done = []
    # Volume change runs through the device pipeline (audible ack).
    if "volume" in pick:
        v = pick["volume"]
        try:
            await inject(text=(
                f"Set the speaker volume to {v} percent using your "
                f"audio_speaker.set_volume tool, then reply with just '🔊 {v}'."
            ))
            actions_done.append(f"volume {v}")
        except Exception:
            log.exception("preset inject volume failed")
    # Kid-mode change writes the state file + restart (ignore here if same).
    if "kid_mode" in pick:
        getter = _state.get("kid_mode_getter")
        setter = _state.get("kid_mode_setter")
        if getter and setter and bool(getter()) != bool(pick["kid_mode"]):
            try:
                setter(bool(pick["kid_mode"]))
                actions_done.append(
                    f"kid mode {'on' if pick['kid_mode'] else 'off'}"
                )
                # Spawn restart so kid_mode change takes effect.
                import subprocess
                subprocess.Popen(
                    ["sh", "-c",
                     "sleep 2 && systemctl restart zeroclaw-bridge"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception:
                log.exception("preset kid-mode toggle failed")
    return templates.TemplateResponse(
        request, "say_result.html",
        {"ok": True, "sent": pick["label"],
         "response": "Applied: " + ", ".join(actions_done) if actions_done
                     else "(nothing changed — already in this state)"},
    )


@router.post("/actions/volume", response_class=HTMLResponse, include_in_schema=False)
async def volume(request: Request, value: int = Form(...)) -> Any:
    """Set Dotty's speaker volume by routing the request through the LLM
    so ZeroClaw fires the existing `audio_speaker.set_volume` MCP tool.

    Caveat: this path goes through the LLM and TTS pipeline — Dotty will
    briefly acknowledge ("Volume set to N!"). A silent direct-MCP path
    would require an admin endpoint inside xiaozhi-server (tracked).
    """
    if not 0 <= value <= 100:
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False, "error": "Volume must be 0–100."},
        )
    prompt = (
        f"Set the speaker volume to {value} percent using your "
        f"audio_speaker.set_volume tool, then reply with just '🔊 {value}'."
    )
    return await _inject_or_error(request, prompt, label=f"set volume to {value}")


_INJECT_WAIT_SEC = 8.0  # Q4: how long to wait for Dotty's reply before
                        #     showing "no response in time" fallback.


async def _inject_or_error(request: Request, text: str, label: str) -> Any:
    """Helper for action endpoints that fire text into xiaozhi-server's
    pipeline so the device actually speaks/emotes/runs MCP tools.

    Q4: subscribes to the bridge's event stream BEFORE injecting, then
    waits up to ~8s for the next turn so the portal can show what Dotty
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
            log.exception("portal inject failed")
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


@router.get("/kid-mode", response_class=HTMLResponse, include_in_schema=False)
async def kid_mode_partial(request: Request) -> Any:
    getter = _state.get("kid_mode_getter")
    enabled = bool(getter()) if getter else True
    return templates.TemplateResponse(
        request, "kid_mode.html",
        {"enabled": enabled, "available": getter is not None,
         "model": KID_MODEL_NAME if enabled else ADULT_MODEL_NAME},
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
        log.exception("portal stop action failed")
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
        return True, "Updated. Restarting…"
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


@router.get("/manifest.json", include_in_schema=False)
async def manifest() -> JSONResponse:
    return JSONResponse({
        "name": "Dotty Dashboard",
        "short_name": "Dotty",
        "start_url": "/ui",
        "scope": "/ui/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#1d232a",
        "theme_color": "#1d232a",
        "icons": [
            {"src": "/ui/icon.svg", "sizes": "any", "type": "image/svg+xml",
             "purpose": "any"},
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
        {"label": "RPi uptime",
         "value": _humanize_age(upt) if upt else "n/a",
         "warn": False},
        {"label": "Bridge uptime",
         "value": _humanize_age(time.time() - _START_TIME),
         "warn": False},
    ]
    return templates.TemplateResponse(
        request, "metrics.html", {"rows": rows}
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
