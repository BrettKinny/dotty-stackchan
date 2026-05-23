"""Dotty bridge — admin dashboard service (FastAPI on :8081, served at /ui).

Post-#36 incarnation: bridge.py owns the dashboard, the localhost-only
`/admin/*` mutation surface, Prometheus `/metrics`, and a small set of
xiaozhi-admin passthrough helpers used by the dashboard buttons (set-state,
inject-text, abort, kid-mode/smart-mode toggles). Everything else — voice
turns, perception consumers, vision/audio captioning — moved to dotty-pi
and dotty-behaviour in the #36 cutover. See docs/cutover-behaviour.md.

History: the file used to be ~6000 lines of voice/perception code wrapped
around the dashboard mount. Issue #111 ripped the dead code in three
commits (ACP/voice, perception consumers, VLM/vision/audio) so the
containerized deploy in PR #109 doesn't double-fire consumers already
running in dotty-behaviour.
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# Sibling import shim — custom-providers/textUtils.py is the canonical
# home for safety/format constants (also bind-mounted into the xiaozhi
# container as core.utils.textUtils). Bridge runs outside the container
# so it imports it as a sibling. Drop this if/when bridge becomes a
# proper package.
sys.path.insert(0, str(Path(__file__).parent / "custom-providers"))

import requests
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from textUtils import (  # noqa: F401  (re-exported for downstream tools)
    ALLOWED_EMOJIS,
    FALLBACK_EMOJI,
    build_turn_suffix,
)

from bridge.csrf import CSRFMiddleware
from bridge.text import (  # noqa: F401  (used by bridge.dashboard via re-imports)
    CONTENT_FILTER_REPLACEMENT,
    MAX_SENTENCES,
    clean_for_tts,
    content_filter,
    ensure_emoji_prefix,
    strip_extra_emojis,
    truncate_sentences,
)

try:
    from bridge.metrics import (
        dotty_kid_mode_active,
        metrics_app,
    )
    _METRICS_AVAILABLE = True
except Exception:  # pragma: no cover
    _METRICS_AVAILABLE = False
    metrics_app = None  # type: ignore[assignment]


def _safe_metric(fn, *args, **kwargs) -> None:
    """Run a metrics-mutating callable, swallowing any exception."""
    try:
        fn(*args, **kwargs)
    except Exception:
        logging.getLogger("dotty-bridge").debug(
            "metric update raised; ignoring", exc_info=True,
        )


# ---------------------------------------------------------------------------
# Kid-mode + smart-mode state files
# ---------------------------------------------------------------------------

_KID_STATE_FILE = Path(
    os.environ.get("DOTTY_KID_MODE_STATE", "/root/zeroclaw-bridge/state/kid-mode")
)
_SMART_STATE_FILE = Path(
    os.environ.get("DOTTY_SMART_MODE_STATE", "/root/zeroclaw-bridge/state/smart-mode")
)


def _read_kid_mode() -> bool:
    """State file overrides env var so the dashboard can flip kid-mode and
    survive a restart without editing the unit. Format: "true" or "false"."""
    if _KID_STATE_FILE.exists():
        try:
            v = _KID_STATE_FILE.read_text().strip().lower()
            if v in ("true", "1", "yes"):
                return True
            if v in ("false", "0", "no"):
                return False
        except OSError:
            pass
    return os.environ.get("DOTTY_KID_MODE", "true").lower() in ("1", "true", "yes")


def _write_kid_mode(enabled: bool) -> None:
    _KID_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _KID_STATE_FILE.write_text("true" if enabled else "false")
    if _METRICS_AVAILABLE:
        _safe_metric(dotty_kid_mode_active.set, 1 if enabled else 0)


def _apply_kid_mode(enabled: bool) -> None:
    """Rebind every kid_mode-derived module global in one atomic pass.

    Called once at module import and again on each dashboard / admin
    toggle. Each rebinding is a single STORE_GLOBAL — readers see either
    the old or new value, never a torn intermediate. Kept slim post-#36:
    only the bits the live dashboard / admin surface reads."""
    global KID_MODE, VOICE_TURN_SUFFIX
    KID_MODE = enabled
    VOICE_TURN_SUFFIX = build_turn_suffix(enabled)


KID_MODE: bool = False
VOICE_TURN_SUFFIX: str = ""
_apply_kid_mode(_read_kid_mode())
if _METRICS_AVAILABLE:
    _safe_metric(dotty_kid_mode_active.set, 1 if KID_MODE else 0)


def _read_smart_mode() -> bool:
    if _SMART_STATE_FILE.exists():
        try:
            v = _SMART_STATE_FILE.read_text().strip().lower()
            if v in ("true", "1", "yes"):
                return True
            if v in ("false", "0", "no"):
                return False
        except OSError:
            pass
    return False


def _write_smart_mode(enabled: bool) -> None:
    _SMART_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SMART_STATE_FILE.write_text("true" if enabled else "false")


# ---------------------------------------------------------------------------
# Tier1Slim hot-swap env (smart-mode flip target on the live xiaozhi-server)
# ---------------------------------------------------------------------------
# DOTTY_VOICE_PROVIDER=tier1slim is the post-#36 voice provider; smart_mode
# flips its model/url/api_key via /xiaozhi/admin/set-tier1slim-model rather
# than restarting a daemon. Legacy "zeroclaw" config.toml-rewrite path was
# retired with the rest of the ZeroClaw stack — keep the helper for parity
# but the only live target is tier1slim.

SMART_MODEL = os.environ.get("SMART_MODEL", "anthropic/claude-sonnet-4-6")
DOTTY_VOICE_PROVIDER = os.environ.get("DOTTY_VOICE_PROVIDER", "tier1slim").strip().lower()

TIER1SLIM_LOCAL_URL = os.environ.get(
    "TIER1SLIM_LOCAL_URL", "http://localhost:8080/v1",
)
TIER1SLIM_LOCAL_API_KEY = os.environ.get("TIER1SLIM_LOCAL_API_KEY", "dotty-voice")
TIER1SLIM_LOCAL_MODEL = os.environ.get("TIER1SLIM_LOCAL_MODEL", "qwen3.5:4b")
TIER1SLIM_CLOUD_URL = os.environ.get(
    "TIER1SLIM_CLOUD_URL", "https://openrouter.ai/api/v1",
)
TIER1SLIM_CLOUD_API_KEY = os.environ.get(
    "TIER1SLIM_CLOUD_API_KEY", os.environ.get("OPENROUTER_API_KEY", ""),
)


def _tier1slim_target_for_smart_mode(enabled: bool) -> tuple[str, str, str]:
    """Return (model, url, api_key) for the Tier1Slim runtime swap given
    the desired smart_mode state. ON → cloud, OFF → local llama-swap."""
    if enabled:
        return (SMART_MODEL, TIER1SLIM_CLOUD_URL, TIER1SLIM_CLOUD_API_KEY)
    return (TIER1SLIM_LOCAL_MODEL, TIER1SLIM_LOCAL_URL, TIER1SLIM_LOCAL_API_KEY)


# ---------------------------------------------------------------------------
# xiaozhi-server admin passthrough helpers — dashboard buttons
# ---------------------------------------------------------------------------
# These wrap POSTs at /xiaozhi/admin/* on the local xiaozhi-server. The
# dashboard never talks to xiaozhi directly; it goes through these so the
# bridge can layer state-file + metric updates around the call.

_XIAOZHI_HOST = os.environ.get("XIAOZHI_HOST", "")
_XIAOZHI_HTTP_PORT = int(os.environ.get("XIAOZHI_OTA_PORT", "8003"))


async def _dispatch_abort(device_id: str) -> None:
    """Send xiaozhi admin abort to stop in-flight TTS for a device."""
    if not _XIAOZHI_HOST:
        return
    url = f"http://{_XIAOZHI_HOST}:{_XIAOZHI_HTTP_PORT}/xiaozhi/admin/abort"
    payload = {"device_id": device_id}

    def _post() -> None:
        try:
            r = requests.post(url, json=payload, timeout=3)
            if r.status_code >= 400:
                log.warning("abort %s: %s", r.status_code, r.text[:200])
        except Exception as exc:
            log.warning("abort failed: %s", exc)

    await asyncio.to_thread(_post)


async def _dispatch_set_state(device_id: str, state: str) -> bool:
    """Fire MCP self.robot.set_state via /xiaozhi/admin/set-state.
    Valid state: idle / talk / story_time / security / sleep / dance.
    Returns True on 2xx."""
    if not _XIAOZHI_HOST:
        log.warning("set_state: XIAOZHI_HOST not set")
        return False
    url = f"http://{_XIAOZHI_HOST}:{_XIAOZHI_HTTP_PORT}/xiaozhi/admin/set-state"
    payload = {"device_id": device_id, "state": state}

    def _post() -> bool:
        try:
            r = requests.post(url, json=payload, timeout=3)
            if r.status_code >= 400:
                log.warning("set_state %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except Exception as exc:
            log.warning("set_state failed: %s", exc)
            return False

    return await asyncio.to_thread(_post)


async def _dispatch_set_toggle(device_id: str, name: str, enabled: bool) -> bool:
    """Fire MCP self.robot.set_toggle via /xiaozhi/admin/set-toggle.
    Toggle name must be one of: kid_mode / smart_mode."""
    if not _XIAOZHI_HOST:
        log.warning("set_toggle: XIAOZHI_HOST not set")
        return False
    url = f"http://{_XIAOZHI_HOST}:{_XIAOZHI_HTTP_PORT}/xiaozhi/admin/set-toggle"
    payload = {"device_id": device_id, "name": name, "enabled": enabled}

    def _post() -> bool:
        try:
            r = requests.post(url, json=payload, timeout=3)
            if r.status_code >= 400:
                log.warning("set_toggle %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except Exception as exc:
            log.warning("set_toggle failed: %s", exc)
            return False

    return await asyncio.to_thread(_post)


async def _dispatch_set_tier1slim_model(
    model: str, url: str = "", api_key: str = "",
) -> bool:
    """Hot-swap the running Tier1Slim provider's model (and optionally url /
    api_key) via the xiaozhi-server admin endpoint. Used by smart_mode flip.
    Returns True on 2xx."""
    if not _XIAOZHI_HOST:
        log.warning("set_tier1slim_model: XIAOZHI_HOST not set")
        return False
    url_endpoint = (
        f"http://{_XIAOZHI_HOST}:{_XIAOZHI_HTTP_PORT}/xiaozhi/admin/set-tier1slim-model"
    )
    payload: dict[str, str] = {"model": model}
    if url:
        payload["url"] = url
    if api_key:
        payload["api_key"] = api_key

    def _post() -> bool:
        try:
            r = requests.post(url_endpoint, json=payload, timeout=3)
            if r.status_code >= 400:
                log.warning("set_tier1slim_model %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except Exception as exc:
            log.warning("set_tier1slim_model failed: %s", exc)
            return False

    return await asyncio.to_thread(_post)


# ---------------------------------------------------------------------------
# MCP tool permission policy — edited by /admin/safety
# ---------------------------------------------------------------------------
# Tools the firmware advertises via WebSocket handshake. The voice/MCP path
# that read this list lived inside ZeroClaw and is gone post-#36; the
# allowlist literal is kept here because /admin/safety mutates it in-place
# and external operator scripts may still treat bridge.py as the source of
# truth. Markers below bound the literal so the rewrite stays deterministic.
# === ADMIN_ALLOWLIST_START ===
MCP_TOOL_ALLOWLIST: set[str] = {
    "get_device_status",
    "audio_speaker.set_volume",
    "screen.set_brightness",
    "screen.set_theme",
    "robot.get_head_angles",
    "robot.set_head_angles",
    "robot.set_led_color",
    "robot.create_reminder",
    "robot.get_reminders",
    "robot.stop_reminder",
}
# === ADMIN_ALLOWLIST_END ===


# ---------------------------------------------------------------------------
# Per-person memory — dashboard /ui/memory backing store
# ---------------------------------------------------------------------------
# brain.db is the FTS5-only memory store written by dotty-pi-ext voice
# tools. The dashboard's /ui/memory page lists every per-person row
# (approved + pending review) and exposes approve / redact actions. The
# bridge does NOT write to brain.db — it's a read + lifecycle-mutate
# surface for the dashboard. See bridge/MEMORY-INDEX.md / brain-db-fts-only.md.

_VOICE_MEMORY_DB = Path(os.environ.get(
    "VOICE_MEMORY_DB", "/root/.zeroclaw/workspace/memory/brain.db",
))


def _voice_memory_person_records_blocking() -> list[dict]:
    """List every per-person memory row — approved (`person:<id>`) and
    pending review (`person_pending:<id>`). Powers the /ui/memory page (#53).
    Read-only. Empty list on error / missing db."""
    import sqlite3
    if not _VOICE_MEMORY_DB.exists():
        return []
    try:
        conn = sqlite3.connect(
            f"file:{_VOICE_MEMORY_DB}?mode=ro", uri=True, timeout=2,
        )
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT id, content, namespace, importance,
                       created_at, updated_at
                FROM memories
                WHERE substr(namespace, 1, 7) = 'person:'
                   OR substr(namespace, 1, 15) = 'person_pending:'
                ORDER BY namespace, importance DESC, updated_at DESC
                """
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception:
        log.exception("voice memory person records list failed")
        return []


def _voice_memory_approve_blocking(mem_id: str) -> bool:
    """Promote `person_pending:<id>` → `person:<id>`. Returns False if
    the row is missing or not in a pending namespace (safe double-approve)."""
    import sqlite3
    from datetime import datetime
    from zoneinfo import ZoneInfo
    if not _VOICE_MEMORY_DB.exists() or not mem_id:
        return False
    prefix = "person_pending:"
    now = datetime.now(ZoneInfo("UTC")).isoformat()
    try:
        conn = sqlite3.connect(str(_VOICE_MEMORY_DB), timeout=5)
        try:
            cur = conn.execute(
                "SELECT namespace FROM memories WHERE id = ?", (mem_id,),
            )
            row = cur.fetchone()
            if row is None:
                return False
            namespace = row[0] or ""
            if not namespace.startswith(prefix):
                return False
            approved = "person:" + namespace[len(prefix):]
            conn.execute(
                "UPDATE memories SET namespace = ?, updated_at = ? "
                "WHERE id = ?",
                (approved, now, mem_id),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        log.exception("voice memory approve failed (id=%s)", mem_id)
        return False


def _voice_memory_delete_blocking(mem_id: str) -> bool:
    """Delete a memory row by id — the /ui/memory redact action. FTS5
    triggers drop the matching index row. False if nothing matched."""
    import sqlite3
    if not _VOICE_MEMORY_DB.exists() or not mem_id:
        return False
    try:
        conn = sqlite3.connect(str(_VOICE_MEMORY_DB), timeout=5)
        try:
            cur = conn.execute(
                "DELETE FROM memories WHERE id = ?", (mem_id,),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception:
        log.exception("voice memory delete failed (id=%s)", mem_id)
        return False


# ---------------------------------------------------------------------------
# Logging + FastAPI app
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dotty-bridge")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan slimmed to dashboard-only on the #111 rip.

    The voice / perception / VLM machinery that used to spin up here moved
    to dotty-pi and dotty-behaviour in the #36 cutover; PR #109 then
    containerized this file, and #111 ripped the dormant code paths so the
    container doesn't double-fire consumers. Today the only startup work is
    reconciling the live xiaozhi-server's Tier1Slim provider against the
    persisted smart_mode bit so the bridge owns the source of truth.
    """
    try:
        await _reconcile_voice_model_at_startup()
    except Exception:
        log.exception("startup voice-model reconcile raised — continuing anyway")
    yield


async def _reconcile_voice_model_at_startup() -> None:
    """Bridge owns the source of truth for which model the voice provider
    runs on (smart_mode state file → DEFAULT vs SMART). Push the desired
    state at the live Tier1Slim provider so the next turn lands on the
    right model. Best-effort: failures are logged, not fatal."""
    if DOTTY_VOICE_PROVIDER != "tier1slim":
        log.info(
            "startup reconcile: DOTTY_VOICE_PROVIDER=%r ≠ 'tier1slim' — skipping",
            DOTTY_VOICE_PROVIDER,
        )
        return
    smart = _read_smart_mode()
    t_model, t_url, t_api = _tier1slim_target_for_smart_mode(smart)
    ok = await _dispatch_set_tier1slim_model(t_model, t_url, t_api)
    if ok:
        log.info(
            "startup tier1slim reconcile: pushed model=%r (smart_mode=%s)",
            t_model, smart,
        )
    else:
        log.warning(
            "startup tier1slim reconcile failed: model=%r (smart_mode=%s) "
            "— next dashboard flip will retry",
            t_model, smart,
        )


app = FastAPI(title="Dotty Bridge", lifespan=lifespan)
app.add_middleware(CSRFMiddleware)

# Prometheus exposition. Mounted as an ASGI sub-app so it shares the
# bridge's listener — keep that listener LAN-only (bind 0.0.0.0 on a
# private network or 127.0.0.1 + a reverse proxy). NEVER expose /metrics
# to the public internet; it leaks operational details about the host.
if _METRICS_AVAILABLE and metrics_app is not None:
    try:
        app.mount("/metrics", metrics_app())
        log.info("Prometheus /metrics mounted")
    except Exception:
        log.exception("metrics mount failed — /metrics will be unavailable")

try:
    from bridge.dashboard import router as _dashboard_router, configure as _configure_dashboard
    app.include_router(_dashboard_router)
except Exception:
    log.exception("dashboard mount failed — admin UI at /ui will be unavailable")
    _configure_dashboard = None  # type: ignore[assignment]

# Vendored JS/CSS + PWA icons for the dashboard. Served same-origin so we
# can attach SRI to the <script>/<link> tags and drop the third-party
# CDNs. Re-build the tailwind bundle with `npm run build:css` after editing
# templates.
try:
    from fastapi.staticfiles import StaticFiles as _StaticFiles
    _STATIC_DIR = Path(__file__).parent / "bridge" / "static"
    if _STATIC_DIR.is_dir():
        app.mount("/ui/static", _StaticFiles(directory=str(_STATIC_DIR)), name="ui-static")
    else:
        log.warning("dashboard static dir missing at %s — vendored assets will 404", _STATIC_DIR)
except Exception:
    log.exception("dashboard static mount failed — vendored assets at /ui/static will be unavailable")


@app.get("/health")
async def health() -> dict:
    """Liveness probe. Reports just the service name + ok status — the
    ACP/voice fields the pre-#36 health surface carried are gone with
    the rest of the ZeroClaw path."""
    return {"status": "ok", "service": "dotty-bridge"}


# ---------------------------------------------------------------------------
# Dashboard wiring
# ---------------------------------------------------------------------------
# bridge/dashboard.py is the actual /ui router; it pulls a small set of
# callables out of bridge.py via configure() so it can flip kid/smart-mode
# state files, push admin commands at xiaozhi-server, and read brain.db
# for the memory panel. The perception / VLM / audio caches the pre-#36
# dashboard read live in dotty-behaviour now — bridge.py exposes empty
# stubs so the dashboard renders without errors (the perception card just
# shows no data until the dashboard ports to dotty-behaviour).

# Empty perception/vision/audio caches — dashboard reads these but the
# writers (vision_explain, audio_explain, scene_synthesis_loop) all moved
# to dotty-behaviour in #36. Kept as empty dicts so the perception card
# templates don't 500 — they just render "no data".
_vision_cache: dict[str, dict] = {}
_audio_cache: dict[str, dict] = {}
_scene_synthesis_cache: dict[str, dict] = {}
_perception_state: dict[str, dict] = {}


def _dashboard_perception_state_getter() -> dict:
    """Empty perception snapshot — dotty-behaviour owns the live bus."""
    return {}


def _dashboard_perception_recent_getter(device_id: str, limit: int | None = None) -> list[dict]:
    """Empty recent-events ring — dotty-behaviour owns the live bus."""
    return []


def _dashboard_state_getter() -> str:
    """Return the current State of Dotty. Without a perception bus the
    bridge can't know — fall through to 'idle' so the dashboard renders
    safely. Real state is mirrored on dotty-behaviour."""
    return "idle"


def _dashboard_last_user_line_getter(device_id: str) -> dict | None:
    """Empty — voice-turn transcripts now flow through dotty-pi."""
    return None


def _dashboard_sound_balance_series() -> list[float]:
    """Empty sound-balance series — sound_event handling moved to
    dotty-behaviour with the rest of the perception bus."""
    return []


def _dashboard_vision_failures_last_hour() -> dict[str, int]:
    """Empty vision-failure counters — VLM dispatch moved to dotty-behaviour."""
    return {}


def _identity_display_name(identity: str) -> str | None:
    """Resolve a household person_id to its display_name. With the
    HouseholdRegistry write/read path moved to dotty-behaviour, this
    always returns None on the bridge side — name resolution happens in
    dotty-behaviour's consumers before the event lands in the dashboard
    pipe (currently unwired)."""
    return None


# Stub SSE plumbing — kept so /ui/events still wakes the queue handler
# (sends heartbeats) when the browser subscribes. No producer is wired in
# bridge.py post-#111; convo turns are owned by dotty-pi now.
_dashboard_event_listeners: list[asyncio.Queue] = []


def _dashboard_subscribe_events() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _dashboard_event_listeners.append(q)
    return q


def _dashboard_unsubscribe_events(q: asyncio.Queue) -> None:
    try:
        _dashboard_event_listeners.remove(q)
    except ValueError:
        pass


if _configure_dashboard is not None:
    async def _dashboard_set_kid_mode(enabled: bool) -> dict:
        """Flip kid_mode bit + push the LED pip to the firmware. Guardrails
        used to live in the bridge's voice path; post-#36 kid_mode is just
        a stored bit + LED — the actual content-filtering lives in
        custom-providers/textUtils.py and the persona prompts."""
        _write_kid_mode(enabled)
        _apply_kid_mode(enabled)
        ok = await _dispatch_set_toggle("", "kid_mode", enabled)
        if not ok:
            return {
                "ok": False,
                "error": "firmware did not acknowledge — LED + on-device toggle stale; bridge state is flipped",
            }
        return {"ok": True}

    async def _dashboard_abort_device(*, device_id: str = "") -> dict:
        """Fire-and-forget POST to xiaozhi-server's admin abort route."""
        if not _XIAOZHI_HOST:
            return {"ok": False, "error": "XIAOZHI_HOST not set"}
        url = f"http://{_XIAOZHI_HOST}:{_XIAOZHI_HTTP_PORT}/xiaozhi/admin/abort"
        payload: dict = {}
        if device_id:
            payload["device_id"] = device_id

        def _post() -> dict:
            try:
                r = requests.post(url, json=payload, timeout=3)
                if r.status_code == 200:
                    return {"ok": True, **r.json()}
                if r.status_code == 503 and "no device connected" in r.text:
                    return {"ok": False, "error": "Dotty isn't connected right now — try again in a few seconds."}
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        return await asyncio.to_thread(_post)

    async def _dashboard_inject_to_device(*, text: str, device_id: str = "") -> dict:
        """POST to xiaozhi-server's /admin/inject-text so the named (or
        first-available) device runs the text through its post-ASR
        pipeline — intent detection, MCP tools, TTS."""
        if not _XIAOZHI_HOST:
            return {"ok": False, "error": "XIAOZHI_HOST not set"}
        url = f"http://{_XIAOZHI_HOST}:{_XIAOZHI_HTTP_PORT}/xiaozhi/admin/inject-text"
        payload = {"text": text}
        if device_id:
            payload["device_id"] = device_id

        def _post() -> dict:
            try:
                r = requests.post(url, json=payload, timeout=3)
                if r.status_code == 200:
                    return {"ok": True, **r.json()}
                if r.status_code == 503 and "no device connected" in r.text:
                    return {"ok": False, "error": "Dotty isn't connected right now — try again in a few seconds."}
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        return await asyncio.to_thread(_post)

    async def _dashboard_set_state(state: str) -> dict:
        ok = await _dispatch_set_state("", state)
        return {"ok": ok}

    async def _dashboard_set_smart_mode(enabled: bool) -> dict:
        """Flip smart_mode + push the LED pip + hot-swap the live
        Tier1Slim provider's model via /xiaozhi/admin/set-tier1slim-model.
        The pre-#36 ZeroClaw config.toml-rewrite path is gone."""
        _write_smart_mode(enabled)
        dispatch_ok = await _dispatch_set_toggle("", "smart_mode", enabled)
        swap_err: str | None = None
        if DOTTY_VOICE_PROVIDER == "tier1slim":
            t_model, t_url, t_api = _tier1slim_target_for_smart_mode(enabled)
            ok = await _dispatch_set_tier1slim_model(t_model, t_url, t_api)
            if not ok:
                swap_err = f"tier1slim hot-swap to {t_model!r} failed"
        else:
            log.warning(
                "smart_mode flip: DOTTY_VOICE_PROVIDER=%r is not 'tier1slim' — "
                "no model swap performed",
                DOTTY_VOICE_PROVIDER,
            )
        errors: list[str] = []
        if not dispatch_ok:
            errors.append("firmware did not acknowledge set_toggle (LED pip stale)")
        if swap_err:
            errors.append(swap_err)
        if errors:
            return {"ok": False, "error": "; ".join(errors)}
        return {"ok": True}

    async def _dashboard_memory_records() -> list[dict]:
        """All per-person memory rows (approved + pending) for /ui/memory."""
        return await asyncio.to_thread(_voice_memory_person_records_blocking)

    async def _dashboard_memory_approve(mem_id: str) -> dict:
        ok = await asyncio.to_thread(_voice_memory_approve_blocking, mem_id)
        return {"ok": ok}

    async def _dashboard_memory_redact(mem_id: str) -> dict:
        ok = await asyncio.to_thread(_voice_memory_delete_blocking, mem_id)
        return {"ok": ok}

    _configure_dashboard(
        # send_message is unused by dashboard.py post-#111 (voice turns
        # now go through dotty-pi); kept None so configure() stays
        # idempotent if the dashboard module gains a reference later.
        send_message=None,
        vision_cache=_vision_cache,
        audio_cache=_audio_cache,
        scene_synthesis_cache=_scene_synthesis_cache,
        kid_mode_getter=_read_kid_mode,
        kid_mode_setter=_dashboard_set_kid_mode,
        smart_mode_getter=_read_smart_mode,
        smart_mode_setter=_dashboard_set_smart_mode,
        state_getter=_dashboard_state_getter,
        state_setter=_dashboard_set_state,
        inject_to_device=_dashboard_inject_to_device,
        abort_device=_dashboard_abort_device,
        subscribe_events=_dashboard_subscribe_events,
        unsubscribe_events=_dashboard_unsubscribe_events,
        perception_state_getter=_dashboard_perception_state_getter,
        perception_recent_getter=_dashboard_perception_recent_getter,
        memory_records_getter=_dashboard_memory_records,
        memory_approve=_dashboard_memory_approve,
        memory_redact=_dashboard_memory_redact,
        identity_display_name=_identity_display_name,
        last_user_line_getter=_dashboard_last_user_line_getter,
        sound_balance_getter=_dashboard_sound_balance_series,
        vision_failures_getter=_dashboard_vision_failures_last_hour,
    )


# ---------------------------------------------------------------------------
# /admin/* — localhost-only runtime configuration mutations
# ---------------------------------------------------------------------------
# Editable from same-host operator scripts. The dashboard uses the
# bridge.dashboard /ui/actions/* routes (which call into _dashboard_*
# above), not these — these are the back-channel for ad-hoc CLI flips.
# Paths/units are env-configurable; defaults are placeholders since the
# zeroclaw daemon they used to point at is gone.

_ADMIN_ALLOWED_PERSONA_FILES = {
    "SOUL.md", "IDENTITY.md", "USER.md", "AGENTS.md",
    "TOOLS.md", "BOOTSTRAP.md", "HEARTBEAT.md", "MEMORY.md",
}
_ADMIN_WORKSPACE_DIR = Path(
    os.environ.get("DOTTY_PERSONA_DIR", os.environ.get("ZEROCLAW_WORKSPACE", "/root/.zeroclaw/workspace"))
)


def _admin_require_localhost(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="admin endpoints are localhost-only")


class _AdminKidModeIn(BaseModel):
    enabled: bool
    device_id: str = ""


class _AdminSmartModeIn(BaseModel):
    enabled: bool
    device_id: str = ""


class _AdminStateIn(BaseModel):
    state: str
    device_id: str = ""


class _AdminPersonaIn(BaseModel):
    file: str
    content: str


class _AdminSafetyIn(BaseModel):
    action: str
    tool: str


_admin_router = APIRouter(
    prefix="/admin", dependencies=[Depends(_admin_require_localhost)],
)


@_admin_router.post("/kid-mode")
async def _admin_kid_mode(payload: _AdminKidModeIn) -> dict:
    _write_kid_mode(payload.enabled)
    _apply_kid_mode(payload.enabled)
    pushed = await _dispatch_set_toggle(
        payload.device_id, "kid_mode", payload.enabled,
    )
    return {
        "ok": True, "enabled": payload.enabled,
        "device_pushed": pushed,
        "hot_applied": True,
    }


@_admin_router.post("/smart-mode")
async def _admin_smart_mode(payload: _AdminSmartModeIn) -> dict:
    """Flip smart_mode + push the LED pip + hot-swap the live Tier1Slim
    provider. Pre-#36 zeroclaw config.toml-rewrite path is gone."""
    _write_smart_mode(payload.enabled)
    pushed = await _dispatch_set_toggle(
        payload.device_id, "smart_mode", payload.enabled,
    )
    if DOTTY_VOICE_PROVIDER == "tier1slim":
        t_model, t_url, t_api = _tier1slim_target_for_smart_mode(payload.enabled)
        swap_ok = await _dispatch_set_tier1slim_model(t_model, t_url, t_api)
        return {
            "ok": True, "enabled": payload.enabled, "device_pushed": pushed,
            "model": t_model, "swap_ok": swap_ok, "provider": "tier1slim",
        }
    return {
        "ok": True, "enabled": payload.enabled, "device_pushed": pushed,
        "provider": DOTTY_VOICE_PROVIDER,
        "warning": "DOTTY_VOICE_PROVIDER is not 'tier1slim' — no model swap performed",
    }


@_admin_router.post("/state")
async def _admin_state(payload: _AdminStateIn) -> dict:
    """Dashboard / external trigger to set Dotty's high-level state.
    Valid: idle / talk / story_time / security / sleep / dance."""
    valid = ("idle", "talk", "story_time", "security", "sleep", "dance")
    if payload.state not in valid:
        raise HTTPException(
            status_code=400,
            detail=f"state must be one of {valid}",
        )
    pushed = await _dispatch_set_state(payload.device_id, payload.state)
    return {"ok": True, "state": payload.state, "device_pushed": pushed}


@_admin_router.post("/persona")
async def _admin_persona(payload: _AdminPersonaIn) -> dict:
    """Write a persona-file under the configured workspace dir. Edits
    used to take effect on the next zeroclaw restart; with the voice
    path moved to dotty-pi, the receiving end depends on
    DOTTY_PERSONA_DIR being aimed at a dir dotty-pi reads. Kept on the
    bridge so external operator scripts have a single localhost-only
    surface for hot-editing persona files."""
    if payload.file not in _ADMIN_ALLOWED_PERSONA_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"file must be one of {sorted(_ADMIN_ALLOWED_PERSONA_FILES)}",
        )
    target = _ADMIN_WORKSPACE_DIR / payload.file
    real = target.resolve() if target.is_symlink() or target.exists() else target
    real.parent.mkdir(parents=True, exist_ok=True)
    tmp = real.with_suffix(real.suffix + ".new")
    tmp.write_text(payload.content)
    tmp.replace(real)
    return {
        "ok": True, "file": str(target), "resolved": str(real),
        "bytes": len(payload.content),
    }


@_admin_router.post("/safety")
async def _admin_safety(payload: _AdminSafetyIn) -> dict:
    """Add / remove a tool from MCP_TOOL_ALLOWLIST. Edits the literal in
    place between the ADMIN_ALLOWLIST markers so the change persists
    across restarts. Note: the voice/MCP path that read this list lived
    in ZeroClaw and is gone — this endpoint is now a static-edit surface
    rather than a live policy mutation."""
    if payload.action not in ("add", "remove"):
        raise HTTPException(status_code=400, detail="action must be 'add' or 'remove'")
    if not re.fullmatch(r"[A-Za-z0-9._]+", payload.tool):
        raise HTTPException(status_code=400, detail="tool name has invalid chars")
    self_path = Path(__file__)
    src = self_path.read_text()
    start_marker = "# === ADMIN_ALLOWLIST_START ==="
    end_marker = "# === ADMIN_ALLOWLIST_END ==="
    if start_marker not in src or end_marker not in src:
        raise HTTPException(status_code=500, detail="allowlist markers missing")
    pre, rest = src.split(start_marker, 1)
    block, post = rest.split(end_marker, 1)
    set_re = re.compile(
        r'MCP_TOOL_ALLOWLIST:\s*set\[str\]\s*=\s*\{([^}]*)\}',
        re.DOTALL,
    )
    m_set = set_re.search(block)
    if not m_set:
        raise HTTPException(status_code=500, detail="allowlist set literal not found")
    items = set(re.findall(r'"([^"]+)"', m_set.group(1)))
    before_size = len(items)
    if payload.action == "add":
        items.add(payload.tool)
    else:
        items.discard(payload.tool)
    new_items = sorted(items)
    new_inner = "\n    " + ",\n    ".join(f'"{t}"' for t in new_items) + ",\n"
    new_block = block[: m_set.start(1)] + new_inner + block[m_set.end(1):]
    new_src = pre + start_marker + new_block + end_marker + post
    new_path = self_path.with_suffix(".py.new")
    new_path.write_text(new_src)
    import py_compile
    try:
        py_compile.compile(str(new_path), doraise=True)
    except py_compile.PyCompileError as exc:
        new_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=f"py_compile failed: {exc}")
    new_path.replace(self_path)
    return {
        "ok": True, "action": payload.action, "tool": payload.tool,
        "size_before": before_size, "size_after": len(new_items),
        "note": "bridge.py allowlist updated; restart bridge to pick up the new value in-process",
    }


app.include_router(_admin_router)


if __name__ == "__main__":
    # Entrypoint for `python bridge.py` (the container CMD). The #111 rip
    # removed this block by accident along with the legacy voice path —
    # without it the script runs the module-level FastAPI wiring then
    # exits, never starts uvicorn, and the container restart-loops with
    # "Prometheus /metrics mounted" as its last log line.
    import uvicorn
    uvicorn.run(
        app,
        host=os.environ.get("DOTTY_BRIDGE_HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8081")),
        log_level=os.environ.get("DOTTY_BRIDGE_LOG_LEVEL", "info"),
    )
