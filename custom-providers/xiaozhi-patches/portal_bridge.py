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

# Pointer to the shared LLM provider instance the WebSocketServer constructed
# at boot. Tier1Slim's runtime model/url/api_key swap (driven by the bridge's
# smart_mode flip) mutates this object in place — every existing
# ConnectionHandler shares the same provider object, so a single mutation
# affects all current and future connections on the very next call. None
# until the WebSocketServer's __init__ has run.
shared_llm: Any = None
