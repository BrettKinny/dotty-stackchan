import asyncio
from aiohttp import web
from config.logger import setup_logging
from core.api.ota_handler import OTAHandler
from core.api.vision_handler import VisionHandler
# DOTTY-PATCH: shared registry populated by the patched WebSocketServer
# and consumed by the /xiaozhi/admin/inject-text route below. Lets the
# Dotty admin dashboard fire `startToChat` against an active device WS.
from core.portal_bridge import active_connections as _dotty_active_connections

TAG = __name__

# DOTTY-PATCH: pin fire-and-forget tasks so asyncio's weakref doesn't
# GC them mid-flight. Use _spawn() in place of bare asyncio.create_task
# wherever the returned Task isn't kept by the caller.
_BACKGROUND_TASKS: set = set()


def _spawn(coro, *, name: str | None = None):
    t = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(t)
    t.add_done_callback(_BACKGROUND_TASKS.discard)
    return t


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
        _spawn(startToChat(conn, text), name="inject_text_chat")
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
        _spawn(handleAbortMessage(conn), name="inject_abort")
        return web.json_response({
            "ok": True,
            "device_id": (getattr(conn, "headers", {}) or {}).get("device-id", "") or device_id,
        })

    async def _dotty_set_head_angles(self, request: "web.Request") -> "web.Response":
        """POST /xiaozhi/admin/set-head-angles
        Body: {"device_id": "<optional>", "yaw": int, "pitch": int, "speed": int}

        Direct MCP self.robot.set_head_angles call against the named
        (or first available) device. Phase 1.6 ambient perception
        consumer (sound-direction head-turn) calls this from the
        bridge.
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        device_id = (data.get("device_id") or "").strip()
        try:
            yaw = int(data.get("yaw", 0))
            pitch = int(data.get("pitch", 0))
            speed = int(data.get("speed", 250))
        except (TypeError, ValueError):
            return web.json_response({"error": "yaw/pitch/speed must be ints"}, status=400)
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
        import json
        import time
        msg = json.dumps({
            "session_id": getattr(conn, "session_id", ""),
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "self.robot.set_head_angles",
                    "arguments": {"yaw": yaw, "pitch": pitch, "speed": speed},
                },
                "id": int(time.time() * 1000) % 0x7FFFFFFF,
            },
        })
        _spawn(conn.websocket.send(msg), name="set_head_angles_send")
        return web.json_response({
            "ok": True,
            "device_id": (getattr(conn, "headers", {}) or {}).get("device-id", "") or device_id,
            "yaw": yaw, "pitch": pitch, "speed": speed,
        })

    async def _dotty_set_state(self, request: "web.Request") -> "web.Response":
        """POST /xiaozhi/admin/set-state
        Body: {"device_id": "<optional>", "state": "<idle|talk|story_time|security|sleep|dance>"}

        Direct MCP self.robot.set_state call. Phase 4 — voice phrases and the
        dashboard route through here (and through the bridge) so the state pip
        and idle profile flip without a daemon restart.
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        device_id = (data.get("device_id") or "").strip()
        state = (data.get("state") or "").strip()
        if state not in ("idle", "talk", "story_time", "security", "sleep", "dance"):
            return web.json_response({"error": f"unknown state: {state!r}"}, status=400)
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
        import json
        import time
        msg = json.dumps({
            "session_id": getattr(conn, "session_id", ""),
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "self.robot.set_state",
                    "arguments": {"state": state},
                },
                "id": int(time.time() * 1000) % 0x7FFFFFFF,
            },
        })
        _spawn(conn.websocket.send(msg), name="set_state_send")
        return web.json_response({
            "ok": True,
            "device_id": (getattr(conn, "headers", {}) or {}).get("device-id", "") or device_id,
            "state": state,
        })

    async def _dotty_set_toggle(self, request: "web.Request") -> "web.Response":
        """POST /xiaozhi/admin/set-toggle
        Body: {"device_id": "<optional>", "name": "<kid_mode|smart_mode>", "enabled": bool}

        Direct MCP self.robot.set_toggle call. Phase 4 — toggles compose with
        State; this endpoint flips them without disturbing the active state.
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        device_id = (data.get("device_id") or "").strip()
        name = (data.get("name") or "").strip()
        if name not in ("kid_mode", "smart_mode"):
            return web.json_response({"error": f"unknown toggle: {name!r}"}, status=400)
        enabled = bool(data.get("enabled"))
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
        import json
        import time
        msg = json.dumps({
            "session_id": getattr(conn, "session_id", ""),
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "self.robot.set_toggle",
                    "arguments": {"name": name, "enabled": enabled},
                },
                "id": int(time.time() * 1000) % 0x7FFFFFFF,
            },
        })
        _spawn(conn.websocket.send(msg), name="set_toggle_send")
        return web.json_response({
            "ok": True,
            "device_id": (getattr(conn, "headers", {}) or {}).get("device-id", "") or device_id,
            "name": name, "enabled": enabled,
        })

    async def _dotty_take_photo(self, request: "web.Request") -> "web.Response":
        """POST /xiaozhi/admin/take-photo
        Body: {"device_id": "<optional>", "question": str}

        Direct MCP self.camera.take_photo call. The firmware grabs a JPEG
        and POSTs it to the bridge's /api/vision/explain, which runs the
        VLM and caches the description. Used by bridge/security_watch.py
        to drive the security-state photo cycle (every 20 s) — same MCP
        tool the voice "what do you see?" path already uses, just routed
        through an admin endpoint instead of the chat pipeline.
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        device_id = (data.get("device_id") or "").strip()
        question = (data.get("question") or "").strip()
        if not question:
            return web.json_response({"error": "question required"}, status=400)
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
        import json
        import time
        msg = json.dumps({
            "session_id": getattr(conn, "session_id", ""),
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "self.camera.take_photo",
                    "arguments": {"question": question},
                },
                "id": int(time.time() * 1000) % 0x7FFFFFFF,
            },
        })
        _spawn(conn.websocket.send(msg), name="take_photo_send")
        return web.json_response({
            "ok": True,
            "device_id": (getattr(conn, "headers", {}) or {}).get("device-id", "") or device_id,
            "question": question,
        })

    async def _dotty_list_songs(self, request: "web.Request") -> "web.Response":
        """GET /xiaozhi/admin/songs — list audio files mounted at
        /opt/xiaozhi-esp32-server/config/assets/songs/.

        Files are sorted by name; only canonical audio extensions are
        returned (opus, ogg, wav, mp3) so junk files in the mount don't
        leak through. Used by the dashboard to populate its song picker.
        """
        import os as _os
        base = "/opt/xiaozhi-esp32-server/config/assets/songs"
        try:
            names = sorted(_os.listdir(base))
        except OSError as exc:
            return web.json_response({"error": str(exc), "base": base}, status=500)
        ok_ext = {".opus", ".ogg", ".wav", ".mp3"}
        files = [n for n in names if _os.path.splitext(n)[1].lower() in ok_ext]
        return web.json_response({"base": base, "files": files})

    async def _dotty_play_asset(self, request: "web.Request") -> "web.Response":
        """POST /xiaozhi/admin/play-asset
        Body: {"device_id": "<optional>", "asset": "/abs/path/to/file"}

        Decodes the named audio asset (WAV, Opus, MP3, etc.) and streams
        it to the device as Opus 60 ms frames using the same push path as
        dance/singing mode.  Returns 200 immediately; playback is
        fire-and-forget.  Respects conn.client_abort for barge-in.
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        asset_path = (data.get("asset") or "").strip()
        device_id = (data.get("device_id") or "").strip()
        if not asset_path:
            return web.json_response({"error": "asset required"}, status=400)
        import os as _os
        if not _os.path.exists(asset_path):
            return web.json_response(
                {"error": f"asset not found: {asset_path}"}, status=404
            )
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
        resolved_id = (getattr(conn, "headers", {}) or {}).get("device-id", "") or device_id

        async def _dispatch() -> None:
            import json as _json
            import numpy as _np
            from math import gcd as _gcd
            from scipy import signal as _sp_sig
            from pydub import AudioSegment as _AS
            from core.utils import opus_encoder_utils as _oeu

            def _decode() -> list:
                ext = _os.path.splitext(asset_path)[1].lower().lstrip(".")
                fmt = {"opus": "ogg", "ogg": "ogg", "wav": "wav", "mp3": "mp3"}.get(ext)
                kw: dict = {"parameters": ["-nostdin"]}
                if fmt:
                    kw["format"] = fmt
                audio = _AS.from_file(asset_path, **kw).set_channels(1).set_sample_width(2)
                src_rate = audio.frame_rate
                pcm = _np.frombuffer(audio.raw_data, dtype=_np.int16)
                tgt = conn.sample_rate
                if src_rate != tgt:
                    g = _gcd(src_rate, tgt)
                    pcm = _sp_sig.resample_poly(
                        pcm.astype(_np.float32), tgt // g, src_rate // g
                    )
                    pcm = _np.clip(pcm, -32768, 32767).astype(_np.int16)
                enc = _oeu.OpusEncoderUtils(
                    sample_rate=tgt, channels=1, frame_size_ms=60
                )
                fsz = int(tgt * 60 / 1000) * 2
                raw = pcm.tobytes()
                pkts: list = []

                def _collect(b: bytes) -> None:
                    if b:
                        pkts.append(b)

                for i in range(0, len(raw), fsz):
                    chunk = raw[i: i + fsz]
                    if len(chunk) < fsz:
                        chunk += b"\x00" * (fsz - len(chunk))
                    enc.encode_pcm_to_opus_stream(
                        chunk,
                        end_of_stream=(i + fsz >= len(raw)),
                        callback=_collect,
                    )
                enc.close()
                return pkts

            try:
                pkts = await asyncio.get_running_loop().run_in_executor(None, _decode)
            except Exception as exc:
                self.logger.bind(tag=TAG).warning(
                    f"play-asset decode failed {asset_path!r}: {exc}"
                )
                return

            conn.client_abort = False
            conn.client_is_speaking = True
            sent = 0
            try:
                await conn.websocket.send(_json.dumps({
                    "type": "tts",
                    "state": "sentence_start",
                    "text": "",
                    "session_id": conn.session_id,
                }))
                for pkt in pkts:
                    if conn.client_abort or conn.is_exiting:
                        self.logger.bind(tag=TAG).info(
                            f"play-asset aborted after {sent}/{len(pkts)} packets"
                        )
                        break
                    await conn.websocket.send(pkt)
                    sent += 1
                    await asyncio.sleep(0.06)
            except Exception as exc:
                self.logger.bind(tag=TAG).warning(f"play-asset stream error: {exc}")
            finally:
                conn.client_is_speaking = False
                try:
                    await conn.websocket.send(_json.dumps({
                        "type": "tts",
                        "state": "stop",
                        "session_id": conn.session_id,
                    }))
                except Exception:
                    pass
                self.logger.bind(tag=TAG).info(
                    f"play-asset complete device={resolved_id} "
                    f"asset={_os.path.basename(asset_path)} "
                    f"sent={sent}/{len(pkts)}"
                )

        _spawn(_dispatch(), name="play_asset_dispatch")
        return web.json_response({"ok": True, "device_id": resolved_id, "asset": asset_path})

    async def _dotty_say(self, request: "web.Request") -> "web.Response":
        """POST /xiaozhi/admin/say
        Body: {"text": "...", "device_id": "<optional>"}

        Speaks `text` verbatim on the device by pushing it onto the
        device's existing TTS text queue — the same queue the LLM
        chat-path uses for tool-call responses. Bypasses ASR + LLM
        entirely; nothing is appended to dialogue history. Used by
        Layer 6 ProactiveGreeter so a server-generated greeting plays
        as Dotty's speech instead of being treated as fake user input.

        Implementation note: the obvious-looking `conn.tts.to_tts(text)`
        path doesn't work — its `asyncio.run(text_to_speak(...))` wrapper
        misbehaves with providers (e.g. EdgeTTS) whose `text_to_speak`
        is structured to spawn a background task and return None
        synchronously. Each retry erroring on the return value still
        leaves the side-effect tasks running, producing 3-5× duplicate
        playback. `tts_one_sentence` is the canonical "say this" API
        that goes through the same priority threads chat turns use.
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        text = (data.get("text") or "").strip()
        device_id = (data.get("device_id") or "").strip()
        if not text:
            return web.json_response({"error": "text required"}, status=400)
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
        if not getattr(conn, "tts", None):
            return web.json_response(
                {"error": "device has no tts provider"}, status=503,
            )
        resolved_id = (getattr(conn, "headers", {}) or {}).get("device-id", "") or device_id

        # Lazy imports — keep module load cheap and the dependency on
        # xiaozhi-server's TTS DTO module local to this handler.
        import uuid as _uuid
        from core.providers.tts.dto.dto import (
            ContentType as _ContentType,
            SentenceType as _SentenceType,
            TTSMessageDTO as _TTSMessageDTO,
        )

        sentence_id = _uuid.uuid4().hex

        def _enqueue() -> None:
            try:
                # The consumer thread filters with
                # `message.sentence_id != self.conn.sentence_id` and
                # drops anything that doesn't match — so we stamp the
                # conn with our new id BEFORE putting messages on the
                # queue. This will pre-empt any in-flight TTS for this
                # conn, which is acceptable for server-pushed greetings
                # (they shouldn't race a chat turn in normal operation).
                #
                # Frame the utterance with FIRST/MIDDLE/LAST: FIRST inits
                # consumer state, MIDDLE carries the text into the
                # buffer, LAST triggers `_process_remaining_text_stream`
                # which is the actual flush. Skipping LAST is the bug
                # that left "this should play once." stuck in the buffer
                # behind a mid-string comma.
                conn.sentence_id = sentence_id
                for st, body in (
                    (_SentenceType.FIRST, ""),
                    (_SentenceType.MIDDLE, text),
                    (_SentenceType.LAST, ""),
                ):
                    conn.tts.tts_text_queue.put(_TTSMessageDTO(
                        sentence_id=sentence_id,
                        sentence_type=st,
                        content_type=_ContentType.TEXT,
                        content_detail=body,
                    ))
            except Exception as exc:
                self.logger.bind(tag=TAG).warning(
                    f"say enqueue failed text={text[:60]!r}: {exc}"
                )

        # The puts are sync on a thread-safe queue, but we hop a thread
        # so the aiohttp loop never blocks on producer-side contention.
        await asyncio.to_thread(_enqueue)
        self.logger.bind(tag=TAG).info(
            f"say queued device={resolved_id} sid={sentence_id[:8]} "
            f"text={text[:60]!r}"
        )
        return web.json_response({
            "ok": True, "device_id": resolved_id, "sentence_id": sentence_id,
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
                        # DOTTY-PATCH: admin routes for dashboard text injection.
                        web.post(
                            "/xiaozhi/admin/inject-text", self._dotty_inject_text
                        ),
                        web.get(
                            "/xiaozhi/admin/devices", self._dotty_list_devices
                        ),
                        web.post(
                            "/xiaozhi/admin/abort", self._dotty_abort
                        ),
                        web.post(
                            "/xiaozhi/admin/set-head-angles",
                            self._dotty_set_head_angles,
                        ),
                        web.post(
                            "/xiaozhi/admin/set-state",
                            self._dotty_set_state,
                        ),
                        web.post(
                            "/xiaozhi/admin/set-toggle",
                            self._dotty_set_toggle,
                        ),
                        web.post(
                            "/xiaozhi/admin/take-photo",
                            self._dotty_take_photo,
                        ),
                        web.post(
                            "/xiaozhi/admin/play-asset",
                            self._dotty_play_asset,
                        ),
                        web.get(
                            "/xiaozhi/admin/songs",
                            self._dotty_list_songs,
                        ),
                        web.post(
                            "/xiaozhi/admin/say",
                            self._dotty_say,
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
