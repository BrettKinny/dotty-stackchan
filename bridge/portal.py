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
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

log = logging.getLogger("portal")

# Bridge wires its in-process message handler in via configure(). Lets the
# "Say" action invoke the same path /api/message uses without an HTTP hop.
_state: dict[str, Any] = {"send_message": None, "vision_cache": None}


def configure(*, send_message: Any = None, vision_cache: dict | None = None) -> None:
    """Register bridge state with the portal. Idempotent."""
    if send_message is not None:
        _state["send_message"] = send_message
    if vision_cache is not None:
        _state["vision_cache"] = vision_cache

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


@router.get("/logs", response_class=HTMLResponse, include_in_schema=False)
async def logs(request: Request, date: str | None = None) -> Any:
    chosen = _safe_date(date)
    entries = _read_recent_log_entries(chosen, limit=20)
    today = datetime.now().strftime("%Y-%m-%d")
    return templates.TemplateResponse(
        request, "logs.html",
        {"entries": entries, "date": chosen, "is_today": chosen == today},
    )
