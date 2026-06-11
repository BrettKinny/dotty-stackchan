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
