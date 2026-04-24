import asyncio
import itertools
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Awaitable, Callable

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

ZEROCLAW_BIN = os.environ.get("ZEROCLAW_BIN", "/root/.cargo/bin/zeroclaw")
REQUEST_TIMEOUT_SEC = float(os.environ.get("ZEROCLAW_TIMEOUT", "90"))
INIT_TIMEOUT_SEC = float(os.environ.get("ZEROCLAW_INIT_TIMEOUT", "10"))
STOP_TIMEOUT_SEC = 2.0
SESSION_IDLE_TIMEOUT_SEC = float(os.environ.get("ZEROCLAW_SESSION_IDLE", "300"))
SESSION_MAX_TURNS = int(os.environ.get("ZEROCLAW_SESSION_MAX_TURNS", "50"))
SESSION_MAX_AGE_SEC = float(os.environ.get("ZEROCLAW_SESSION_MAX_AGE_SEC", "1800"))
FALLBACK_EMOJI = "😐"
ALLOWED_EMOJIS = ("😊", "😆", "😢", "😮", "🤔", "😠", "😐", "😍", "😴")
STACKCHAN_TURN_PREFIX = "[channel=stackchan voice-TTS]\n"
STACKCHAN_TURN_SUFFIX = (
    "\n\n---\nHARD CONSTRAINTS for THIS reply (overrides everything else):\n"
    "1. Reply in ENGLISH ONLY. Even if the user message is unclear, in another language, "
    "or you'd naturally pick Chinese — your reply is English. No Chinese, no Japanese.\n"
    "2. First character of your reply MUST be exactly one of these emojis: 😊 😆 😢 😮 🤔 😠 😐 😍 😴\n"
    "3. Length: 1-3 short sentences, TTS-friendly.\n"
    "Begin your reply now."
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zeroclaw-bridge")

app_lock = asyncio.Lock()


class MessageIn(BaseModel):
    content: str
    channel: str | None = None
    session_id: str | None = None
    metadata: dict | None = None


class MessageOut(BaseModel):
    response: str
    session_id: str


class _SessionInvalid(Exception):
    pass


class ACPClient:
    """Long-running `zeroclaw acp` child, JSON-RPC 2.0 over stdio.

    Caches one ACP sessionId across bridge requests to avoid per-turn
    workspace reload on the ZeroClaw side. Rotates the session on idle
    timeout, turn count, or wall-clock age; invalidates on session-not-found
    errors and on ACP child respawn. Serialized via asyncio.Lock because
    ACP stdio is a single channel and voice traffic is single-speaker.
    """

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._id_gen = itertools.count(1)
        self._sid: str | None = None
        self._sid_last_used: float = 0.0
        self._sid_created: float = 0.0
        self._sid_turns: int = 0

    async def _spawn(self) -> None:
        # Any respawn path invalidates the cached session — ZeroClaw has no memory of it.
        self._sid = None
        self._sid_turns = 0
        env = {**os.environ, "RUST_LOG": "error"}
        self._proc = await asyncio.create_subprocess_exec(
            ZEROCLAW_BIN, "acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        rid = next(self._id_gen)
        await self._send({"jsonrpc": "2.0", "id": rid, "method": "initialize", "params": {}})
        resp = await self._recv_matching(rid, INIT_TIMEOUT_SEC)
        caps = resp.get("result", {}).get("capabilities", {})
        log.info("ACP initialized pid=%s capabilities=%s", self._proc.pid, caps)

    async def ensure_alive(self) -> None:
        if self._proc is None or self._proc.returncode is not None:
            if self._proc is not None:
                log.warning("ACP child exited rc=%s; respawning", self._proc.returncode)
            await self._spawn()

    async def _send(self, obj: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _recv_matching(
        self,
        rid: int,
        timeout: float,
        on_event: Callable[[dict], Awaitable[None]] | None = None,
    ) -> dict:
        assert self._proc and self._proc.stdout
        loop = asyncio.get_event_loop()
        end = loop.time() + timeout
        while True:
            remaining = end - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=remaining)
            if not raw:
                raise RuntimeError("ACP child closed stdout")
            try:
                obj = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                log.warning("ACP non-JSON line ignored: %r", raw[:200])
                continue
            if obj.get("id") == rid and "method" not in obj:
                return obj
            if on_event is not None and obj.get("method") == "session/event":
                try:
                    await on_event(obj.get("params") or {})
                except Exception:
                    log.exception("session/event callback raised")

    async def _new_session(self) -> None:
        rid = next(self._id_gen)
        await self._send({"jsonrpc": "2.0", "id": rid, "method": "session/new", "params": {}})
        resp = await self._recv_matching(rid, INIT_TIMEOUT_SEC)
        if "error" in resp:
            raise RuntimeError(f"session/new: {resp['error']}")
        now = asyncio.get_event_loop().time()
        self._sid = resp["result"]["sessionId"]
        self._sid_created = now
        self._sid_last_used = now
        self._sid_turns = 0

    async def _close_session(self, sid: str) -> None:
        try:
            rid = next(self._id_gen)
            await self._send({
                "jsonrpc": "2.0", "id": rid, "method": "session/stop",
                "params": {"sessionId": sid},
            })
            try:
                await self._recv_matching(rid, STOP_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                log.debug("session/stop ack timed out (non-fatal)")
        except Exception:
            log.debug("session/stop best-effort close raised; ignoring", exc_info=True)

    def _should_rotate(self, now: float) -> tuple[bool, str | None]:
        if self._sid is None:
            return (False, None)
        if now - self._sid_last_used > SESSION_IDLE_TIMEOUT_SEC:
            return (True, "idle")
        if self._sid_turns >= SESSION_MAX_TURNS:
            return (True, "turns")
        if now - self._sid_created > SESSION_MAX_AGE_SEC:
            return (True, "age")
        return (False, None)

    @staticmethod
    def _is_session_invalid_error(err: dict) -> bool:
        msg = str(err.get("message", "")).lower()
        if "session" in msg and any(
            marker in msg for marker in ("not found", "invalid", "expired", "unknown")
        ):
            return True
        return False

    async def _do_prompt(
        self,
        text: str,
        chunk_cb: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        rid = next(self._id_gen)
        await self._send({
            "jsonrpc": "2.0", "id": rid, "method": "session/prompt",
            "params": {"sessionId": self._sid, "prompt": text},
        })

        on_event = None
        if chunk_cb is not None:
            async def on_event(params: dict) -> None:
                if params.get("type") != "chunk":
                    return
                content = params.get("content") or ""
                if content:
                    await chunk_cb(content)

        resp = await self._recv_matching(rid, REQUEST_TIMEOUT_SEC, on_event=on_event)
        if "error" in resp:
            err = resp["error"]
            if self._is_session_invalid_error(err):
                raise _SessionInvalid(str(err))
            raise RuntimeError(f"session/prompt: {err}")
        return resp.get("result", {}).get("content", "") or ""

    async def prompt(
        self,
        text: str,
        xiaozhi_sid: str | None = None,
        chunk_cb: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        async with app_lock:
            await self.ensure_alive()
            loop = asyncio.get_event_loop()
            now = loop.time()

            rotate, reason = self._should_rotate(now)
            if rotate and self._sid is not None:
                old = self._sid
                self._sid = None
                await self._close_session(old)
                log.info("session-rotated reason=%s old_sid=%s turns=%d",
                         reason, old[:8], self._sid_turns)

            t_total = perf_counter()
            new_ms = 0.0
            prompt_ms = 0.0
            stop_ms = 0.0  # always 0 in the reuse path; kept in log for continuity
            phase = "new"
            try:
                if self._sid is None:
                    t_new = perf_counter()
                    await self._new_session()
                    new_ms = (perf_counter() - t_new) * 1000.0
                reused = 0 if new_ms > 0.0 else 1

                phase = "prompt"
                t_prompt = perf_counter()
                try:
                    content = await self._do_prompt(text, chunk_cb=chunk_cb)
                except _SessionInvalid as si:
                    log.info("session-invalidated reason=%s", str(si)[:120])
                    self._sid = None
                    # re-create and retry once; count any extra new time into new_ms
                    t_new = perf_counter()
                    await self._new_session()
                    new_ms += (perf_counter() - t_new) * 1000.0
                    reused = 0
                    content = await self._do_prompt(text, chunk_cb=chunk_cb)
                prompt_ms = (perf_counter() - t_prompt) * 1000.0

                self._sid_last_used = loop.time()
                self._sid_turns += 1

                total_ms = (perf_counter() - t_total) * 1000.0
                log.info(
                    "latency_ms total=%.0f new=%.0f prompt=%.0f stop=%.0f "
                    "sid=%s reused=%d turn=%d xiaozhi_sid=%s",
                    total_ms, new_ms, prompt_ms, stop_ms,
                    (self._sid or "none")[:8], reused, self._sid_turns,
                    (xiaozhi_sid or "none")[:8],
                )
                return content
            except (BrokenPipeError, ConnectionResetError, RuntimeError, asyncio.TimeoutError):
                total_ms = (perf_counter() - t_total) * 1000.0
                log.exception(
                    "ACP call failed phase=%s latency_ms total=%.0f new=%.0f prompt=%.0f",
                    phase, total_ms, new_ms, prompt_ms,
                )
                if self._proc is not None:
                    try:
                        self._proc.kill()
                    except ProcessLookupError:
                        pass
                    self._proc = None
                self._sid = None
                raise

    async def shutdown(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            if self._sid is not None:
                await self._close_session(self._sid)
                self._sid = None
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass


acp = ACPClient()


def _ensure_emoji_prefix(text: str) -> str:
    if not text:
        return f"{FALLBACK_EMOJI} (no response)"
    stripped = text.lstrip()
    if any(stripped.startswith(e) for e in ALLOWED_EMOJIS):
        return text
    return f"{FALLBACK_EMOJI} {text}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        async with app_lock:
            await acp.ensure_alive()
    except Exception:
        log.exception("Initial ACP spawn failed — will retry on first request")
    yield
    await acp.shutdown()


app = FastAPI(title="ZeroClaw Bridge", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    proc_ok = acp._proc is not None and acp._proc.returncode is None
    return {
        "status": "ok",
        "service": "zeroclaw-bridge",
        "acp_running": proc_ok,
        "cached_session": acp._sid is not None,
        "session_turns": acp._sid_turns,
    }


@app.post("/api/message", response_model=MessageOut)
async def message(payload: MessageIn) -> MessageOut:
    session_id = payload.session_id or str(uuid.uuid4())
    log.info("msg channel=%s session=%s len=%d", payload.channel, session_id, len(payload.content))
    prompt_text = payload.content
    if payload.channel == "stackchan":
        prompt_text = STACKCHAN_TURN_PREFIX + prompt_text + STACKCHAN_TURN_SUFFIX
    try:
        raw = await asyncio.wait_for(
            acp.prompt(prompt_text, xiaozhi_sid=payload.session_id),
            timeout=REQUEST_TIMEOUT_SEC,
        )
        answer = _ensure_emoji_prefix(raw)
    except asyncio.TimeoutError:
        log.warning("ACP timeout")
        answer = f"{FALLBACK_EMOJI} I'm thinking too slowly right now, try again."
    except FileNotFoundError:
        log.exception("zeroclaw binary missing")
        answer = f"{FALLBACK_EMOJI} My AI brain is offline."
    except Exception:
        log.exception("ACP invocation failed")
        answer = f"{FALLBACK_EMOJI} Something went wrong, please try again."
    return MessageOut(response=answer, session_id=session_id)


@app.post("/api/message/stream")
async def message_stream(payload: MessageIn) -> StreamingResponse:
    """NDJSON-streaming variant of /api/message.

    Emits one JSON line per token-level chunk as the LLM produces it:
        {"type":"chunk","content":"..."}
    Ends with a single final line (after the LLM turn completes):
        {"type":"final","content":"<full text>","session_id":"..."}
    or on error:
        {"type":"error","message":"...","session_id":"..."}

    The first non-whitespace character across all emitted chunks is checked
    against ALLOWED_EMOJIS; if the LLM forgot its emoji leader, FALLBACK_EMOJI
    is prepended to the first chunk before it goes out. This keeps the face
    animation protocol intact without waiting for the full response.
    """
    session_id = payload.session_id or str(uuid.uuid4())
    log.info(
        "stream channel=%s session=%s len=%d",
        payload.channel, session_id, len(payload.content),
    )
    prompt_text = payload.content
    if payload.channel == "stackchan":
        prompt_text = STACKCHAN_TURN_PREFIX + prompt_text + STACKCHAN_TURN_SUFFIX

    queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
    state = {"seen_nonws": False}

    async def on_chunk(content: str) -> None:
        # Emoji leader enforcement on the very first non-whitespace chunk.
        if not state["seen_nonws"]:
            stripped = content.lstrip()
            if stripped:
                state["seen_nonws"] = True
                if not any(stripped.startswith(e) for e in ALLOWED_EMOJIS):
                    content = f"{FALLBACK_EMOJI} " + content
        await queue.put(("chunk", content))

    async def run_turn() -> None:
        try:
            full = await asyncio.wait_for(
                acp.prompt(
                    prompt_text,
                    xiaozhi_sid=payload.session_id,
                    chunk_cb=on_chunk,
                ),
                timeout=REQUEST_TIMEOUT_SEC,
            )
            # Fallback for providers that never stream (e.g. old openrouter path):
            # no chunks were seen, emit the full text as a single chunk with
            # emoji-prefix correction, matching legacy /api/message behavior.
            if not state["seen_nonws"]:
                full = _ensure_emoji_prefix(full)
                await queue.put(("chunk", full))
            await queue.put(("final", full))
        except asyncio.TimeoutError:
            log.warning("ACP timeout (stream)")
            await queue.put(("error", f"{FALLBACK_EMOJI} I'm thinking too slowly right now, try again."))
        except FileNotFoundError:
            log.exception("zeroclaw binary missing (stream)")
            await queue.put(("error", f"{FALLBACK_EMOJI} My AI brain is offline."))
        except Exception:
            log.exception("ACP invocation failed (stream)")
            await queue.put(("error", f"{FALLBACK_EMOJI} Something went wrong, please try again."))

    async def gen():
        task = asyncio.create_task(run_turn())
        try:
            while True:
                kind, data = await queue.get()
                if kind == "chunk":
                    yield json.dumps({"type": "chunk", "content": data}, ensure_ascii=False) + "\n"
                elif kind == "final":
                    yield json.dumps(
                        {"type": "final", "content": data, "session_id": session_id},
                        ensure_ascii=False,
                    ) + "\n"
                    break
                elif kind == "error":
                    yield json.dumps(
                        {"type": "error", "message": data, "session_id": session_id},
                        ensure_ascii=False,
                    ) + "\n"
                    break
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    return StreamingResponse(gen(), media_type="application/x-ndjson")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
