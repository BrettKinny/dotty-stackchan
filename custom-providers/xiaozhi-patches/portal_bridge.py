"""Shared registry of active StackChan device WebSocket handlers.

Populated by the patched WebSocketServer when devices connect/disconnect;
read by the patched HTTP server's /xiaozhi/admin/inject-text route so
the Dotty admin dashboard can fire `startToChat` against an active device
connection (which is what the bridge needs to make the device actually
speak / emote / fire MCP tools — the bridge has no WS to the device).

This file is mounted into the container at /opt/xiaozhi-esp32-server/core/
"""

from typing import Any

# device_id -> ConnectionHandler. Single-process asyncio so plain dict ops
# are race-free for our purposes.
active_connections: dict[str, Any] = {}
