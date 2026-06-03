"""PiClient — long-lived RPC client for the dotty-pi container.

Owns a single `pi --mode rpc` process spawned via `docker exec -i` and
multiplexes turns over its stdin/stdout. Per #36 Step-5 invariants:

  1. **Spawn once.** The pi process is started lazily on the first
     turn and reused across all subsequent turns. Between turns we
     issue `new_session` to clear state without re-spawning — that
     recovers the per-turn startup tax (1.2-1.8 s warm spawn in the
     spike report) that an in-process HTTP provider wouldn't have paid.

  2. **Auto-cancel `extension_ui_request`.** Dialog methods (`select`,
     `confirm`, `input`, `editor`) block pi until the client sends
     back an `extension_ui_response`. In Dotty's voice-only world
     there's no user to answer dialogs mid-turn, so we cancel
     immediately. Fire-and-forget UI methods (`notify`, `setStatus`,
     etc.) need no response — we just drop them.

  3. **Filter `thinking_delta`.** Pi's reasoning models emit
     `assistantMessageEvent.type = "thinking_delta"` while reasoning,
     then `text_delta` for the user-visible answer. Per the spike
     telemetry: ~19 thinking deltas vs 3 text deltas per turn. Dotty
     must NEVER speak its reasoning — only `text_delta` reaches the
     `iter_turn_text` generator.

Wire format follows pi's `docs/rpc.md` (JSONL framing — one JSON object
per stdout/stdin line, no embedded newlines).

This is the production-grade replacement for the spike-only
probes/pi-longlived-spike.py — same protocol, but designed to live
inside xiaozhi-server (synchronous generator interface, no asyncio).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from queue import Empty, Queue
from typing import Callable, Iterable, Iterator, List, Optional


logger = logging.getLogger(__name__)


_DEFAULT_PI_FLAGS = (
    "--mode", "rpc",
    "--provider", "ollama",
    "--model", "qwen3.5:4b",
    "--no-session",
    "--no-context-files",
    "--offline",
    "--no-skills",
    "--no-prompt-templates",
    "--no-themes",
    "--thinking", "off",
)


def default_subprocess_factory(
    docker_host_user: str,
    docker_host: str,
    container: str,
    pi_args: Iterable[str],
) -> subprocess.Popen:
    """Spawn pi via ssh + docker exec. Replace in tests via PiClient's
    `subprocess_factory` parameter."""
    cmd = [
        "ssh", f"{docker_host_user}@{docker_host}",
        "docker", "exec", "-i", container, "pi", *pi_args,
    ]
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )


def local_exec_subprocess_factory(
    container: str,
    pi_args: Iterable[str],
) -> subprocess.Popen:
    """For when xiaozhi-server runs on the same host as dotty-pi (the
    target deployment). Skips the ssh hop."""
    cmd = ["docker", "exec", "-i", container, "pi", *pi_args]
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )


SubprocessFactory = Callable[[], subprocess.Popen]


class PiClientError(Exception):
    pass


class PiClient:
    """Long-lived JSONL client for `pi --mode rpc`.

    Thread model:
      - Main thread owns the public interface (`send_turn`, `new_session`,
        `close`). Calls are serialized — only one turn in flight at a time.
      - Reader thread continuously consumes stdout lines, parses JSON,
        and routes them: events into the current turn's queue, UI requests
        into the auto-cancel path. Started on first connect, stopped on close.
      - Stderr drain thread captures pi's diagnostic output into a ring
        buffer surfaced on errors.

    Per-turn lifecycle (`iter_turn_text`):
      1. Caller pushes a `prompt` command.
      2. Reader thread routes events; this generator yields only the
         user-visible `text_delta`s until it sees `agent_end`.
      3. Generator exits; caller can run another turn.
    """

    def __init__(
        self,
        subprocess_factory: SubprocessFactory,
        *,
        turn_timeout_sec: float = 120.0,
        stderr_ring_size: int = 200,
    ):
        self._spawn = subprocess_factory
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stderr_reader: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._event_queue: Queue = Queue()
        self._stderr_ring: List[str] = []
        self._stderr_ring_size = stderr_ring_size
        self._turn_timeout_sec = turn_timeout_sec
        self._next_req_id = 0
        self._closed = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def _ensure_started(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            logger.info("PiClient: spawning pi process")
            self._proc = self._spawn()
            assert self._proc.stdout is not None
            assert self._proc.stderr is not None
            self._event_queue = Queue()
            self._reader = threading.Thread(
                target=self._read_stdout,
                name="pi-stdout-reader",
                daemon=True,
            )
            self._reader.start()
            self._stderr_reader = threading.Thread(
                target=self._read_stderr,
                name="pi-stderr-reader",
                daemon=True,
            )
            self._stderr_reader.start()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                except Exception:
                    logger.exception("PiClient: error during close")
            self._proc = None

    # ------------------------------------------------------------------
    # public turn interface
    # ------------------------------------------------------------------

    def new_session(self) -> None:
        """Clear pi's session state without re-spawning. Call between
        Dotty voice turns so each turn starts fresh."""
        self._ensure_started()
        req_id = self._next_id("nsess")
        self._send({"id": req_id, "type": "new_session"})
        # Drain until we see the matching `response` frame, dropping any
        # leftover events from a prior turn that the reader queued.
        deadline = time.time() + 10.0
        while time.time() < deadline:
            try:
                frame = self._event_queue.get(timeout=0.5)
            except Empty:
                continue
            if not isinstance(frame, dict):
                continue
            if (
                frame.get("type") == "response"
                and frame.get("command") == "new_session"
            ):
                return
        raise PiClientError("new_session timed out waiting for response")

    def iter_turn_text(self, prompt: str) -> Iterator[str]:
        """Send a `prompt` command and yield user-visible text deltas
        until `agent_end`. Thinking deltas and any other event types
        are silently dropped — the caller's only job is to forward what
        comes out of this iterator to TTS."""
        self._ensure_started()
        req_id = self._next_id("turn")
        self._send({"id": req_id, "type": "prompt", "message": prompt})

        deadline = time.time() + self._turn_timeout_sec
        saw_accept = False
        while time.time() < deadline:
            try:
                frame = self._event_queue.get(timeout=1.0)
            except Empty:
                if self._proc is None or self._proc.poll() is not None:
                    raise PiClientError("pi process exited mid-turn")
                continue

            if not isinstance(frame, dict):
                continue

            ftype = frame.get("type")

            # Accept-ack — exactly one per command. Required before events.
            if (
                ftype == "response"
                and frame.get("command") == "prompt"
                and frame.get("id") == req_id
            ):
                if not frame.get("success", False):
                    raise PiClientError(
                        f"pi rejected prompt: {frame.get('error', 'unknown')}"
                    )
                saw_accept = True
                continue

            if ftype == "message_update":
                ame = frame.get("assistantMessageEvent")
                if isinstance(ame, dict) and ame.get("type") == "text_delta":
                    delta = ame.get("delta")
                    if isinstance(delta, str) and delta:
                        yield delta
                # thinking_delta, thinking_start, thinking_end and any
                # other message_update sub-types are filtered out here.
                continue

            if ftype == "agent_end":
                if not saw_accept:
                    raise PiClientError("agent_end before prompt-accept")
                return

            # Unknown frame — drop silently. Logging at debug level keeps
            # production logs clean while leaving a trail when needed.
            logger.debug("PiClient: unhandled frame type=%r", ftype)

        raise PiClientError(
            f"turn timed out after {self._turn_timeout_sec}s"
        )

    # ------------------------------------------------------------------
    # diagnostics
    # ------------------------------------------------------------------

    def recent_stderr(self) -> List[str]:
        """Snapshot of pi's recent stderr — useful when raising errors."""
        return list(self._stderr_ring)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _next_id(self, prefix: str) -> str:
        self._next_req_id += 1
        return f"{prefix}-{self._next_req_id}"

    def _send(self, cmd: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise PiClientError("send before connect")
        line = (json.dumps(cmd) + "\n").encode("utf-8")
        try:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except BrokenPipeError as exc:
            raise PiClientError(f"stdin closed: {exc}") from exc

    def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("PiClient: dropped non-JSON stdout: %r", line[:120])
                    continue
                self._route_frame(frame)
        except Exception:
            if not self._closed:
                logger.exception("PiClient: stdout reader crashed")

    def _route_frame(self, frame: object) -> None:
        if isinstance(frame, dict) and frame.get("type") == "extension_ui_request":
            self._handle_ui_request(frame)
            return
        self._event_queue.put(frame)

    def _handle_ui_request(self, req: dict) -> None:
        """Invariant 2: auto-cancel dialog methods, drop fire-and-forget."""
        method = req.get("method", "")
        if method in {"select", "confirm", "input", "editor"}:
            req_id = req.get("id", "")
            logger.info(
                "PiClient: auto-cancelling extension_ui_request method=%s id=%s",
                method, req_id,
            )
            try:
                self._send({
                    "type": "extension_ui_response",
                    "id": req_id,
                    "cancelled": True,
                })
            except PiClientError:
                logger.exception("PiClient: failed to send UI cancel")
            return
        # Fire-and-forget: notify, setStatus, setWidget, setTitle,
        # set_editor_text — drop silently. They don't block pi.
        logger.debug("PiClient: dropping fire-and-forget UI method=%s", method)

    def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                self._stderr_ring.append(line)
                if len(self._stderr_ring) > self._stderr_ring_size:
                    self._stderr_ring.pop(0)
        except Exception:
            if not self._closed:
                logger.exception("PiClient: stderr reader crashed")


# Convenience factory that wires the env-var-defaults into a
# PiClient ready to be used by xiaozhi-server.
def make_default_pi_client() -> PiClient:
    container = os.environ.get("DOTTY_PI_CONTAINER", "dotty-pi")
    pi_args: list[str] = list(_DEFAULT_PI_FLAGS)
    extra = os.environ.get("DOTTY_PI_EXTRA_FLAGS", "").split()
    if extra:
        pi_args.extend(extra)
    return PiClient(
        subprocess_factory=lambda: local_exec_subprocess_factory(
            container=container, pi_args=pi_args,
        ),
    )
