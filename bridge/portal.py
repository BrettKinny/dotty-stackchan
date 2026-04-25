"""Mobile-first admin portal for Dotty.

Mounted at ``/ui`` on the bridge FastAPI app. Read-only MVP: host status
cards, conversation log tail. No state-mutating actions.

Host probes are env-driven so this stays generic in the public template:
set ``UNRAID_HOST`` (and optionally ``WORKSTATION_HOST``) on the bridge
service. Cards for unset hosts render as "unknown".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

log = logging.getLogger("portal")

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
    return LOG_DIR / f"convo-{datetime.now().strftime('%Y-%m-%d')}.ndjson"


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


def _read_recent_log_entries(limit: int = 20) -> list[dict[str, Any]]:
    path = _today_log_path()
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
        out.append({
            "time": time_str,
            "channel": rec.get("channel") or "?",
            "request": (rec.get("request_text") or "")[:240],
            "response": (rec.get("response_text") or "")[:600],
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


@router.get("/logs", response_class=HTMLResponse, include_in_schema=False)
async def logs(request: Request) -> Any:
    entries = _read_recent_log_entries(limit=20)
    return templates.TemplateResponse(
        request, "logs.html", {"entries": entries}
    )
