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
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

log = logging.getLogger("portal")

# Bridge wires its in-process message handler in via configure(). Lets the
# "Say" action invoke the same path /api/message uses without an HTTP hop.
_state: dict[str, Any] = {
    "send_message": None,
    "vision_cache": None,
    "kid_mode_getter": None,
    "kid_mode_setter": None,
}


def configure(*, send_message: Any = None, vision_cache: dict | None = None,
              kid_mode_getter: Any = None, kid_mode_setter: Any = None) -> None:
    """Register bridge state with the portal. Idempotent."""
    if send_message is not None:
        _state["send_message"] = send_message
    if vision_cache is not None:
        _state["vision_cache"] = vision_cache
    if kid_mode_getter is not None:
        _state["kid_mode_getter"] = kid_mode_getter
    if kid_mode_setter is not None:
        _state["kid_mode_setter"] = kid_mode_setter

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/ui", tags=["portal"])

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
    return templates.TemplateResponse(request, "dashboard.html")


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
    sender = _state.get("send_message")
    if sender is None:
        raise HTTPException(503, "send_message not configured")
    prompt = (
        f"Make the {emoji} face for me. Reply with just '{emoji} ok' "
        f"and nothing else."
    )
    try:
        result = await sender(text=prompt, channel="dotty")
    except Exception as exc:
        log.exception("portal mood action failed")
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False, "error": f"Bridge error: {exc.__class__.__name__}"},
        )
    return templates.TemplateResponse(
        request, "say_result.html",
        {"ok": True, "sent": f"make the {emoji} face",
         "response": result.get("response", "")},
    )


@router.post("/actions/dance", response_class=HTMLResponse, include_in_schema=False)
async def dance(request: Request, key: str = Form(...)) -> Any:
    pick = next((d for d in _DANCES if d["key"] == key), None)
    if pick is None:
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False, "error": "Unknown dance/song."},
        )
    sender = _state.get("send_message")
    if sender is None:
        raise HTTPException(503, "send_message not configured")
    try:
        result = await sender(text=pick["phrase"], channel="dotty")
    except Exception as exc:
        log.exception("portal dance action failed")
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False, "error": f"Bridge error: {exc.__class__.__name__}"},
        )
    return templates.TemplateResponse(
        request, "say_result.html",
        {"ok": True, "sent": pick["phrase"],
         "response": result.get("response", "")},
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
    sender = _state.get("send_message")
    if sender is None:
        raise HTTPException(503, "send_message not configured")
    prompt = (
        f"Use your audio_speaker.set_volume tool to set the volume to {value}. "
        f"Reply with just the volume emoji and 'volume {value}'."
    )
    try:
        result = await sender(text=prompt, channel="dotty")
    except Exception as exc:
        log.exception("portal volume action failed")
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False, "error": f"Bridge error: {exc.__class__.__name__}"},
        )
    return templates.TemplateResponse(
        request, "say_result.html",
        {"ok": True, "sent": f"set volume to {value}",
         "response": result.get("response", "")},
    )


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
    sender = _state.get("send_message")
    if sender is None:
        raise HTTPException(503, "send_message not configured")
    try:
        result = await sender(text=text, channel="dotty")
    except Exception as exc:
        log.exception("portal say action failed")
        return templates.TemplateResponse(
            request, "say_result.html",
            {"ok": False, "error": f"Bridge error: {exc.__class__.__name__}"},
        )
    return templates.TemplateResponse(
        request, "say_result.html",
        {"ok": True, "sent": text, "response": result.get("response", "")},
    )


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


@router.get("/kid-mode", response_class=HTMLResponse, include_in_schema=False)
async def kid_mode_partial(request: Request) -> Any:
    getter = _state.get("kid_mode_getter")
    enabled = bool(getter()) if getter else True
    return templates.TemplateResponse(
        request, "kid_mode.html",
        {"enabled": enabled, "available": getter is not None},
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
        "name": "Dotty Admin",
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


@router.get("/logs", response_class=HTMLResponse, include_in_schema=False)
async def logs(request: Request, date: str | None = None) -> Any:
    chosen = _safe_date(date)
    entries = _read_recent_log_entries(chosen, limit=20)
    today = datetime.now().strftime("%Y-%m-%d")
    return templates.TemplateResponse(
        request, "logs.html",
        {"entries": entries, "date": chosen, "is_today": chosen == today},
    )
