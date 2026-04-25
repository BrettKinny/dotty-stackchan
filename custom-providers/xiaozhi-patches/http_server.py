import asyncio
from aiohttp import web
from config.logger import setup_logging
from core.api.ota_handler import OTAHandler
from core.api.vision_handler import VisionHandler
# DOTTY-PATCH: shared registry populated by the patched WebSocketServer
# and consumed by the /xiaozhi/admin/inject-text route below. Lets the
# Dotty admin portal fire `startToChat` against an active device WS.
from core.portal_bridge import active_connections as _dotty_active_connections

TAG = __name__


class SimpleHttpServer:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging()
        self.ota_handler = OTAHandler(config)
        self.vision_handler = VisionHandler(config)

    def _get_websocket_url(self, local_ip: str, port: int) -> str:
        """获取websocket地址"""
        server_config = self.config["server"]
        websocket_config = server_config.get("websocket")
        if websocket_config and "你" not in websocket_config:
            return websocket_config
        else:
            return f"ws://{local_ip}:{port}/xiaozhi/v1/"

    # DOTTY-PATCH ------------------------------------------------------------
    async def _dotty_inject_text(self, request: "web.Request") -> "web.Response":
        """POST /xiaozhi/admin/inject-text

        Body: {"text": "...", "device_id": "<optional>"}

        Routes the text through xiaozhi-server's normal post-ASR pipeline
        for the named (or first available) active device. The device
        will speak/emote/dispatch MCP tools as if the user had said it.
        Fire-and-forget — returns immediately, the chat task runs async.
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        text = (data.get("text") or "").strip()
        device_id = data.get("device_id", "") or ""
        if not text:
            return web.json_response({"error": "text required"}, status=400)
        if device_id:
            conn = _dotty_active_connections.get(device_id)
        else:
            conn = next(iter(_dotty_active_connections.values()), None)
        if conn is None:
            return web.json_response(
                {"error": "no device connected", "known": list(_dotty_active_connections)},
                status=503,
            )
        # Lazy import to avoid pulling the chat pipeline at server startup.
        from core.handle.receiveAudioHandle import startToChat
        asyncio.create_task(startToChat(conn, text))
        return web.json_response({
            "ok": True,
            "device_id": getattr(conn, "headers", {}).get("device-id", "") or device_id,
        })

    async def _dotty_list_devices(self, request: "web.Request") -> "web.Response":
        """GET /xiaozhi/admin/devices — list connected device-ids."""
        return web.json_response({"devices": list(_dotty_active_connections)})

    async def _dotty_abort(self, request: "web.Request") -> "web.Response":
        """POST /xiaozhi/admin/abort  Body: {"device_id": "<optional>"}

        Stops current TTS, drains queues, sends the device-side stop
        frame — same path xiaozhi-server takes on barge-in. Fire-and-forget.
        """
        try:
            data = await request.json()
        except Exception:
            data = {}
        device_id = (data.get("device_id") or "").strip() if isinstance(data, dict) else ""
        if device_id:
            conn = _dotty_active_connections.get(device_id)
        else:
            conn = next(iter(_dotty_active_connections.values()), None)
        if conn is None:
            return web.json_response(
                {"error": "no device connected",
                 "known": list(_dotty_active_connections)},
                status=503,
            )
        from core.handle.abortHandle import handleAbortMessage
        asyncio.create_task(handleAbortMessage(conn))
        return web.json_response({
            "ok": True,
            "device_id": (getattr(conn, "headers", {}) or {}).get("device-id", "") or device_id,
        })
    # END DOTTY-PATCH --------------------------------------------------------

    async def start(self):
        try:
            server_config = self.config["server"]
            read_config_from_api = self.config.get("read_config_from_api", False)
            host = server_config.get("ip", "0.0.0.0")
            port = int(server_config.get("http_port", 8003))

            if port:
                app = web.Application()

                if not read_config_from_api:
                    app.add_routes(
                        [
                            web.get("/xiaozhi/ota/", self.ota_handler.handle_get),
                            web.post("/xiaozhi/ota/", self.ota_handler.handle_post),
                            web.options(
                                "/xiaozhi/ota/", self.ota_handler.handle_options
                            ),
                            web.get(
                                "/xiaozhi/ota/download/{filename}",
                                self.ota_handler.handle_download,
                            ),
                            web.options(
                                "/xiaozhi/ota/download/{filename}",
                                self.ota_handler.handle_options,
                            ),
                        ]
                    )
                app.add_routes(
                    [
                        web.get("/mcp/vision/explain", self.vision_handler.handle_get),
                        web.post(
                            "/mcp/vision/explain", self.vision_handler.handle_post
                        ),
                        web.options(
                            "/mcp/vision/explain", self.vision_handler.handle_options
                        ),
                        # DOTTY-PATCH: admin routes for portal text injection.
                        web.post(
                            "/xiaozhi/admin/inject-text", self._dotty_inject_text
                        ),
                        web.get(
                            "/xiaozhi/admin/devices", self._dotty_list_devices
                        ),
                        web.post(
                            "/xiaozhi/admin/abort", self._dotty_abort
                        ),
                    ]
                )

                runner = web.AppRunner(app)
                await runner.setup()
                site = web.TCPSite(runner, host, port)
                await site.start()

                while True:
                    await asyncio.sleep(3600)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"HTTP服务器启动失败: {e}")
            import traceback
            self.logger.bind(tag=TAG).error(f"错误堆栈: {traceback.format_exc()}")
            raise
