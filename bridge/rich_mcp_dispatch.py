"""Per-connection dispatch shim for the rich-MCP tool surface.

Builds a ``ws_send_func(tool_name, arguments)`` async callable from a
``conn`` (the xiaozhi-server ``ConnectionHandler`` instance) so the
dispatch surface in :mod:`bridge.rich_mcp` stays bridge-agnostic.

The MCP frame shape mirrors the existing helpers in
``receiveAudioHandle.py`` (``_send_led_color``, ``_send_led_multi``,
``_send_head_angles``):

    {
        "session_id": <conn.session_id>,
        "type": "mcp",
        "payload": {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "self.<tool>",
                "arguments": {...},
            },
            "id": <int>,
        },
    }

Bridge wiring deferred
----------------------
TODO: This factory ships scaffolded but is **not yet imported** by
``bridge.py`` or ``receiveAudioHandle.py`` — a concurrent agent owns
those files for this commit. Wire-up in the follow-up commit:

    from bridge.rich_mcp import RichMCPToolSurface
    from bridge.rich_mcp_dispatch import build_ws_send_func

    surface = RichMCPToolSurface(kid_mode_provider=lambda: KID_MODE)

    # In the per-connection LLM tool-use handler:
    ws_send = build_ws_send_func(conn)
    out = await surface.dispatch(tool_name, arguments, ws_send)

The dispatch shape is awaited but does NOT block on a tool result — the
firmware's MCP response is delivered over the same WS as a separate
frame and is handled by the existing tool-result wiring. The async
return value here is whatever the underlying send returns (typically
``None``); :mod:`bridge.rich_mcp` wraps it in ``{"ok": True, ...}``.

Defensive: every send is try/except guarded so a transport failure
returns a benign None rather than propagating into the LLM turn.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Awaitable, Callable

log = logging.getLogger("zeroclaw-bridge.rich_mcp_dispatch")


def build_ws_send_func(
    conn: Any,
) -> Callable[[str, dict[str, Any]], Awaitable[Any]]:
    """Return a closure that frames + sends an MCP ``tools/call``.

    Parameters
    ----------
    conn:
        The xiaozhi-server ``ConnectionHandler``-like object. Must
        expose ``conn.session_id`` (str) and ``conn.websocket.send``
        (async callable taking a JSON string). Optionally a
        ``conn.logger`` with ``.bind(tag=...).warning(msg)`` for
        warn-once logging on transport failure.

    Returns
    -------
    async (tool_name, arguments) -> None
        On success returns None (the firmware MCP response arrives
        asynchronously on the same WS). On transport failure logs and
        returns None — the caller in :mod:`bridge.rich_mcp` will see
        the (None) result and report success; if you need synchronous
        error propagation, raise from inside the closure and let the
        surface's try/except convert it to an error dict.
    """

    async def _send(tool_name: str, arguments: dict[str, Any]) -> Any:
        # The firmware advertises tools as ``self.<name>``; the bridge
        # strips the prefix for allowlist comparison (see
        # ``MCP_TOOL_ALLOWLIST`` in ``bridge.py``) and we re-add it
        # here at the WS boundary.
        full_name = tool_name if tool_name.startswith("self.") else f"self.{tool_name}"
        try:
            payload = {
                "session_id": getattr(conn, "session_id", None),
                "type": "mcp",
                "payload": {
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {
                        "name": full_name,
                        "arguments": arguments or {},
                    },
                    "id": int(time.time() * 1000) % 0x7FFFFFFF,
                },
            }
            msg = json.dumps(payload)
        except (TypeError, ValueError) as exc:
            log.warning(
                "rich_mcp_dispatch: failed to serialise call tool=%s: %s",
                tool_name, exc,
            )
            return None

        try:
            await conn.websocket.send(msg)
        except Exception as exc:
            # Mirror the warn-once pattern from _send_led_multi so an
            # offline device doesn't spam logs on every LLM turn.
            if not getattr(conn, "_rich_mcp_send_warned", False):
                try:
                    conn.logger.bind(tag="rich_mcp").warning(
                        f"rich_mcp send failed tool={tool_name}: {exc}"
                    )
                except Exception:
                    log.warning(
                        "rich_mcp send failed tool=%s: %s",
                        tool_name, exc,
                    )
                try:
                    conn._rich_mcp_send_warned = True
                except Exception:
                    pass
            return None
        return None

    return _send
