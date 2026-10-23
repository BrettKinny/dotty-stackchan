"""DeviceCommand — the single seam for server→firmware MCP tool calls.

Mounted into the xiaozhi container at `core/utils/device_command.py`
(importable as `core.utils.device_command`) so both patch surfaces that
talk MCP to the device — the `/xiaozhi/admin/*` handlers in
`http_server.py` and the chat-pipeline helpers in
`receiveAudioHandle.py` — build and send tool calls through one module.

Before this seam, twelve call sites each hand-rolled the same JSON-RPC
envelope, and every shared defect was twelve defects (2026-06-06
audit): request ids were `int(time.time()*1000) % 0x7FFFFFFF`, so two
calls in the same millisecond collided; and every site fired
`conn.websocket.send()` with no coordination, racing the other senders
on the same ServerConnection (the websockets library does not allow
interleaved sends).

What this module owns:

  * **Monotonic per-connection request ids** — a plain counter stored
    on the conn, so ids are unique for the life of the connection and
    a future reply-correlation layer has something to correlate on.
  * **The MCP envelope** — one place to change the wire shape.
  * **Serialized device-bound sends** — a per-connection asyncio.Lock;
    every send routed through here is mutually exclusive with every
    other send routed through here. (Upstream xiaozhi's own chat-path
    writer does not take this lock — full serialization would mean
    patching upstream send sites; this seam is where that lands when
    it does.)

Reply correlation is deliberately NOT implemented yet: device-side MCP
replies are still fire-and-forget. `call_tool` returns the request id
so a correlation layer can be added behind this interface without
touching the twelve callers again.

State is attached to the conn object (`_dotty_mcp_next_id`,
`_dotty_send_lock`) rather than a side table so it lives and dies with
the connection — no leak across reconnects, no weakref bookkeeping.
All attachment happens on the event-loop thread with no awaits between
check and set, so initialisation cannot race.
"""

import asyncio
import json
import time

_ID_ATTR = "_dotty_mcp_next_id"
_LOCK_ATTR = "_dotty_send_lock"


def next_request_id(conn) -> int:
    """Monotonic JSON-RPC id, unique per connection lifetime."""
    current = getattr(conn, _ID_ATTR, 1)
    setattr(conn, _ID_ATTR, current + 1)
    return current


def mcp_envelope(conn, tool: str, arguments: dict, request_id: int) -> str:
    """Serialize one MCP tools/call frame for `conn`."""
    return json.dumps({
        "session_id": getattr(conn, "session_id", ""),
        "type": "mcp",
        "payload": {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
            "id": request_id,
        },
    })


def _send_lock(conn) -> asyncio.Lock:
    lock = getattr(conn, _LOCK_ATTR, None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(conn, _LOCK_ATTR, lock)
    return lock


async def send_serialized(conn, message) -> None:
    """Send one frame (str or bytes) on the device WebSocket, mutually
    exclusive with every other send routed through this module."""
    async with _send_lock(conn):
        await conn.websocket.send(message)


async def call_tool(conn, tool: str, arguments: dict) -> int:
    """Build + send one MCP tools/call. Returns the request id.

    Fire-and-forget at the protocol level (no reply wait — see module
    docstring), but the send itself is serialized against other
    device-bound sends from this module.
    """
    request_id = next_request_id(conn)
    await send_serialized(conn, mcp_envelope(conn, tool, arguments, request_id))
    return request_id


# ---------------------------------------------------------------------------
# Ending a listening session.
#
# The xiaozhi protocol has no server→device "listen stop" frame: a `tts stop`
# only takes the firmware from Speaking back to Listening in auto mode, and the
# only `system` command the firmware honours is `reboot`. The one clean way to
# make the device stop listening is the one the no-voice timeout already uses
# (`no_voice_close_connect` → `conn.close()`): close the WebSocket. The
# firmware's OnAudioChannelClosed handler then drops to kDeviceStateIdle, which
# closes the mic and clears the listening pip; the channel reopens lazily on
# the next wake word, head-pet hold, or perception event.
# ---------------------------------------------------------------------------

_ENDING_ATTR = "_dotty_session_ending"

# Time for the firmware to apply an MCP frame sent just before the close (and
# echo its state_changed event) before the socket goes away.
DEFAULT_END_SESSION_GRACE_S = 0.5


async def end_session(conn, *, reason: str = "", grace_s: float = 0.0) -> bool:
    """Close the device's listening session the way the no-voice timeout does.

    Stops any reply in flight (the same abort path a barge-in takes), marks
    the connection so the no-voice timeout cannot start a second close, waits
    ``grace_s`` so an MCP frame sent just before has time to be applied, then
    closes the WebSocket. Returns False when a close is already under way.
    """
    if getattr(conn, _ENDING_ATTR, False) or getattr(conn, "is_exiting", False):
        return False
    setattr(conn, _ENDING_ATTR, True)
    logger = getattr(conn, "logger", None)
    if getattr(conn, "client_is_speaking", False):
        try:
            from core.handle.abortHandle import handleAbortMessage
            await handleAbortMessage(conn)
        except Exception as exc:  # pragma: no cover - logged, close still runs
            _log(logger, "warning", f"end_session abort failed: {exc}")
    # handleAbortMessage clears this; set it after so no_voice_close_connect
    # (which checks it) does not fire its own close during the grace period.
    conn.close_after_chat = True
    if grace_s > 0:
        await asyncio.sleep(grace_s)
    _log(logger, "info", f"ending listening session ({reason or 'requested'})")
    await conn.close(getattr(conn, "websocket", None))
    return True


# A sleeping robot sends no audio, so xiaozhi's inactivity check (no-voice
# time + 60 s) would close its WebSocket; asleep, it never reconnects, and the
# dashboard could no longer wake it as docs/modes.md promises (2026-10-23 A/V
# run, S05). The firmware's privacy gate keeps the mic and wake word off, so
# holding the connection open hears nothing.
SLEEP_KEEPALIVE_INTERVAL_S = 30.0
_KEEPALIVE_ATTR = "_dotty_sleep_keepalive"


def keep_alive_while_asleep(conn, *, interval_s: float = SLEEP_KEEPALIVE_INTERVAL_S):
    """Refresh ``conn.last_activity_time`` while the device reports sleep.

    One task per connection; it ends when the state leaves sleep or the
    connection stops. Must be called from the event loop.
    """
    existing = getattr(conn, _KEEPALIVE_ATTR, None)
    if existing is not None and not existing.done():
        return existing

    async def _run():
        stop = getattr(conn, "stop_event", None)
        while getattr(conn, "current_state", "") == "sleep" and not (stop and stop.is_set()):
            conn.last_activity_time = time.time() * 1000
            await asyncio.sleep(interval_s)

    task = asyncio.get_running_loop().create_task(_run())
    setattr(conn, _KEEPALIVE_ATTR, task)
    return task


def _log(logger, level: str, message: str) -> None:
    if logger is None:
        return
    try:
        getattr(logger.bind(tag=__name__), level)(message)
    except Exception:
        pass
