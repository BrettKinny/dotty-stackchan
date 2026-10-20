"""PiVoiceLLM — xiaozhi-server LLM provider that routes voice turns
through the dotty-pi container instead of bridge.py.

Unlike a plain OpenAI-style provider that parses `tool_calls` and
dispatches each one xiaozhi-side, PiVoiceLLM doesn't do that: pi itself
owns the agent loop and the tool dispatch happens inside the dotty-pi-ext
extension. From xiaozhi's perspective this provider is a much simpler
shape — translate the dialogue into a single pi prompt, stream pi's
user-visible text chunks back to TTS, done.

Per #36 Step-5 contract:
  - PiVoiceLLM owns ONE PiClient — long-lived across all turns.
  - Between turns we issue `new_session` to reset pi's working state
    without re-spawning the process.
  - Thinking deltas + extension UI requests are filtered inside
    PiClient (see pi_client.py) — by the time text reaches `response()`
    only TTS-bound chunks remain.

Configuration via `data/.config.yaml`:

```yaml
selected_module:
  LLM: PiVoiceLLM

LLM:
  PiVoiceLLM:
    type: pi_voice
    container_name: dotty-pi
    # Optional — flags appended after the default ones in PiClient.
    extra_pi_flags: ""
```
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

from .pi_client import PiClient, PiClientError, make_default_pi_client
from .voice_intents import match_voice_intent, split_room_view


try:
    from config.logger import setup_logging  # type: ignore
    from core.providers.llm.base import LLMProviderBase  # type: ignore
except ImportError:  # pragma: no cover — only on dev workstation
    # Provide tiny stand-ins so this file imports cleanly during
    # extension-side unit tests. xiaozhi-server overrides both.
    class LLMProviderBase:  # type: ignore[no-redef]
        pass

    def setup_logging():  # type: ignore[no-redef]
        import logging
        return logging.getLogger("pi_voice")


# textUtils.build_turn_suffix is the source of truth — pi_voice and
# openai_compat import from it via the xiaozhi-container
# bind mount at `core.utils.textUtils`. On the dev workstation the file
# lives at `custom-providers/textUtils.py` (the dash in the dir name
# makes it unimportable as a package), so we fall back to loading it
# by absolute path. Both code paths end up with the same module.
try:
    from core.utils.textUtils import (  # type: ignore
        ALLOWED_EMOJIS,
        FALLBACK_EMOJI,
        ProviderText,
        build_turn_suffix,
        enforce_face_stream,
        filter_tts_stream,
        guard_tts_stream,
    )
except ImportError:  # pragma: no cover — dev workstation fallback
    import importlib.util as _ilu
    from pathlib import Path as _Path

    _tu_path = _Path(__file__).resolve().parents[1] / "textUtils.py"
    _spec = _ilu.spec_from_file_location("dotty_textUtils", _tu_path)
    assert _spec is not None and _spec.loader is not None
    _tu = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_tu)
    ALLOWED_EMOJIS = _tu.ALLOWED_EMOJIS  # type: ignore[attr-defined]
    FALLBACK_EMOJI = _tu.FALLBACK_EMOJI  # type: ignore[attr-defined]
    build_turn_suffix = _tu.build_turn_suffix  # type: ignore[attr-defined]
    filter_tts_stream = _tu.filter_tts_stream  # type: ignore[attr-defined]
    ProviderText = _tu.ProviderText  # type: ignore[attr-defined]
    enforce_face_stream = _tu.enforce_face_stream  # type: ignore[attr-defined]
    guard_tts_stream = _tu.guard_tts_stream  # type: ignore[attr-defined]


TAG = __name__
logger = setup_logging()


class _ForwardToProviderLog(logging.Handler):
    """Hands stdlib log records to the provider's loguru logger."""

    def __init__(self, target) -> None:
        super().__init__(logging.INFO)
        self._target = target

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + logging.Formatter().formatException(record.exc_info)
            method = getattr(self._target, record.levelname.lower(), None) or self._target.info
            method(message)
        except Exception:  # logging must never break a voice turn
            pass


def _forward_pi_client_logs(target) -> None:
    """PiClient logs through stdlib ``logging``, but xiaozhi-server prints only
    its loguru logger and leaves the stdlib root at WARNING, so INFO lines such
    as ``PiClient: tool call name=...`` (the A/V runner's tool evidence) never
    reached the container log (2026-10-20). Forward them there. On the dev
    workstation ``setup_logging`` is stdlib already, so nothing is added."""
    from . import pi_client as _pi_client

    client_logger = _pi_client.logger
    if isinstance(target, logging.Logger) or any(
        isinstance(h, _ForwardToProviderLog) for h in client_logger.handlers
    ):
        return
    client_logger.addHandler(_ForwardToProviderLog(target))
    client_logger.setLevel(logging.INFO)
    client_logger.propagate = False


_forward_pi_client_logs(logger.bind(tag=TAG) if hasattr(logger, "bind") else logger)


def _read_kid_mode() -> bool:
    """Read the shared runtime toggle, falling back to startup config."""
    state_file = Path(os.environ.get(
        "DOTTY_KID_MODE_STATE", "/var/lib/dotty-bridge/state/kid-mode",
    ))
    try:
        value = state_file.read_text().strip().lower()
        if value in ("true", "1", "yes"):
            return True
        if value in ("false", "0", "no"):
            return False
    except OSError:
        pass
    return os.environ.get("DOTTY_KID_MODE", "true").lower() in ("1", "true", "yes")


_DASHBOARD_PORT = 8081


def _dashboard_url() -> str:
    """Base URL of the bridge dashboard, or "" when it can't be worked out.

    `DOTTY_DASHBOARD_URL` wins. Otherwise assume the dashboard sits on the
    same host as dotty-behaviour (which this container already reaches via
    `BRIDGE_URL` / `VISION_BRIDGE_URL`) on its default port."""
    explicit = os.environ.get("DOTTY_DASHBOARD_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    sibling = os.environ.get("BRIDGE_URL") or os.environ.get("VISION_BRIDGE_URL", "")
    parsed = urlparse(sibling)
    if not parsed.hostname:
        return ""
    return f"{parsed.scheme or 'http'}://{parsed.hostname}:{_DASHBOARD_PORT}"


def _report_to_dashboard(path: str, payload: dict) -> None:
    """Fire-and-forget POST to the bridge dashboard.

    Runs on a daemon thread with a short timeout and swallows every error:
    a missing or slow dashboard must never delay or break a voice turn."""
    base = _dashboard_url()
    if not base:
        return
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("DOTTY_ADMIN_TOKEN", "").strip()
    if token:
        headers["X-Admin-Token"] = token
    body = json.dumps(payload).encode("utf-8")

    def _post() -> None:
        try:
            req = urllib.request.Request(
                base + path, data=body, headers=headers, method="POST",
            )
            urllib.request.urlopen(req, timeout=2).close()
        except Exception as exc:
            logger.debug("PiVoiceLLM dashboard report to %s failed: %s", path, exc)

    threading.Thread(
        target=_post, name="dotty-dashboard-report", daemon=True,
    ).start()


def _last_user_text(dialogue: list[dict]) -> str:
    """Find the most recent user-turn content. xiaozhi's dialogue is a
    list of {role, content} dicts in chronological order; the last user
    entry is the utterance we want pi to react to."""
    for msg in reversed(dialogue):
        if msg.get("role") == "user":
            return _normalise_user_content(msg.get("content"))
    return ""


def _normalise_user_content(content: object) -> str:
    """Extract xiaozhi's user text without leaking its JSON envelope to pi.

    Depending on where the dialogue was assembled, ``content`` may already be
    plain text, a ``{"content": "..."}`` mapping, or the JSON encoding of that
    mapping. Unknown JSON is kept verbatim: silently discarding or reshaping a
    genuine user utterance would be worse than passing it through.
    """
    if isinstance(content, dict):
        inner = content.get("content")
        return inner if isinstance(inner, str) else str(content or "")
    if not isinstance(content, str):
        return str(content or "")
    stripped = content.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            return content
        if isinstance(decoded, dict) and isinstance(decoded.get("content"), str):
            return decoded["content"]
    return content


_VOICE_TOOL_ROUTING = (
    "\n\nVOICE TOOL ROUTING: Decide whether to use a registered tool before "
    "composing the spoken reply. Use the matching tool when the user asks you "
    "to remember or recall, play a song, look or take a photo, or solve a "
    "question that needs careful reasoning. Call it first and use its result. "
    "Never claim an action succeeded unless its tool succeeded. The reply "
    "constraints below apply only to final spoken text, not to tool calls."
)


# Appended to the user's words for an explicit recall. dotty-pi-ext's
# turn_text.ts strips from this exact marker when logging the turn, so search
# results are never written back into memory as if the person had said them.
_MEMORY_CONTEXT = (
    "\n\nMEMORY SEARCH RESULTS (from earlier conversations; \"user\" is the "
    "person, \"assistant\" was you):\n{results}\n"
    "Answer their question from these results in your own words. If the "
    "results do not answer it, say you don't remember that yet. Never read "
    "the results out as they are written."
)


# Same contract as _MEMORY_CONTEXT: turn_text.ts strips from these markers.
_CAMERA_CONTEXT = (
    "\n\nCAMERA VIEW (what your camera saw in the last few seconds):\n{view}\n"
    "Answer from this view only. Do not add anything it does not say."
)
_ROOM_VIEW_CONTEXT = (
    "\n\nROOM VIEW (who your camera saw in front of you recently; mention it "
    "only if it helps):\n{view}"
)
_CAMERA_DENIED = "(Camera access is disabled in Kid Mode)"


# ProviderText: a fixed phrase, so kid mode may speak it before the wait.
_THINKING_PREAMBLE = ProviderText("\U0001F914 Let me think hard about that. ")


def _wrap_with_sandwich(user_text: str, kid_mode: bool) -> str:
    """Append the HARD CONSTRAINTS suffix to the user's text via the shared
    textUtils.build_turn_suffix contract — emoji-prefix
    rule, English-only, length caps, kid-mode topic filtering. Without
    this Dotty drifts into Chinese, multi-paragraph replies, and (in
    kid_mode) unsafe topics, since qwen3.5:4b's base behaviour doesn't
    encode any of those constraints."""
    return user_text + _VOICE_TOOL_ROUTING + build_turn_suffix(kid_mode)


def _enforce_leading_emoji(chunks: Iterator[str]) -> Iterator[str]:
    """Guarantee the firmware's leading-glyph face contract (#168).

    Delegates to the shared textUtils.enforce_face_stream: exactly one
    supported face at the head of the first chunk, near-miss emoji mapped
    onto the supported set, no added streaming latency.
    """
    yield from enforce_face_stream(chunks)


class LLMProvider(LLMProviderBase):
    """xiaozhi-server LLM provider backed by the dotty-pi container."""

    def __init__(self, config: dict, *, client: PiClient | None = None):
        self._container = config.get("container_name") or os.environ.get(
            "DOTTY_PI_CONTAINER", "dotty-pi",
        )
        # Initial value is logged for diagnostics. response() refreshes this
        # from the bridge/xiaozhi shared state file on every turn.
        self._kid_mode = _read_kid_mode()
        # `client` is injected by tests; production passes None to get
        # the env-configured default.
        self._client: PiClient = client if client is not None else make_default_pi_client()
        # A connection can submit multiple chat jobs to its thread pool. Pi RPC
        # is one ordered stream, so keep the complete new_session -> prompt ->
        # agent_end transaction exclusive; per-write locking cannot prevent one
        # caller from consuming another caller's response frames.
        self._turn_lock = threading.Lock()
        self._first_turn = True
        msg = f"PiVoiceLLM ready (container={self._container} kid_mode={self._kid_mode})"
        try:
            logger.bind(tag=TAG).info(msg)  # type: ignore[attr-defined]
        except AttributeError:
            logger.info(msg)

    # xiaozhi-server's voice loop calls this as a sync generator.
    # Each yielded string becomes a TTS chunk.
    def response(self, session_id, dialogue, **kwargs) -> Iterator[str]:
        with self._turn_lock:
            yield from self._response_serialized(session_id, dialogue, **kwargs)

    def _response_serialized(self, session_id, dialogue, **kwargs) -> Iterator[str]:
        """Run one complete Pi RPC transaction while ``_turn_lock`` is held."""
        self._kid_mode = _read_kid_mode()
        room_view, user_text = split_room_view(_last_user_text(dialogue))
        if not user_text.strip():
            yield f"{FALLBACK_EMOJI} (empty turn)"
            return
        # Everything spoken this turn is reported to the dashboard when the
        # generator finishes — including when xiaozhi closes it early.
        spoken: list[str] = []
        errors: list[str] = []
        started = time.monotonic()
        try:
            # Output safety for every path below (model text, direct tool
            # results, fixed phrases): one leading face, and in kid mode the
            # fail-closed blocked-content filter. See textUtils.guard_tts_stream.
            for chunk in guard_tts_stream(
                self._respond(user_text, errors, room_view), self._kid_mode,
                on_hit=self._on_filter_hit,
            ):
                spoken.append(chunk)
                yield chunk
        finally:
            _report_to_dashboard("/api/voice/turn", {
                "request_text": user_text,
                "response_text": "".join(spoken),
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": "; ".join(errors) or None,
            })

    def _respond(
        self, user_text: str, errors: list[str], room_view: str = "",
    ) -> Iterator[str]:
        """One turn's TTS chunks. Failures are appended to `errors` so
        response() can report them alongside the spoken fallback.

        Explicit tool requests (voice_intents.py) run their tool directly
        instead of trusting the 4B to call it (#165)."""
        context = ""
        intent = match_voice_intent(user_text)
        if intent is not None and intent.tool == "remember":
            result = self._invoke_voice_tool("remember", {"fact": intent.argument}, errors)
            if result == "(remembered)":
                yield f"{FALLBACK_EMOJI} I'll remember that."
            else:
                yield f"{FALLBACK_EMOJI} I couldn't save that memory."
            return
        if intent is not None and intent.tool == "memory_lookup":
            result = self._invoke_voice_tool(
                "memory_lookup", {"query": intent.argument}, errors,
            )
            if result is None:
                yield f"{FALLBACK_EMOJI} I couldn't check my memory right now."
                return
            if result in ("(no memories found)", "(empty query)"):
                yield f"{FALLBACK_EMOJI} I don't remember anything about that yet."
                return
            # The lookup is deterministic; the wording is not. Search output is
            # raw stored rows ("user: … | assistant: …"), never fit to be
            # spoken as-is, so hand it to the model as context for this turn.
            context = _MEMORY_CONTEXT.format(results=result)
        if intent is not None and intent.tool == "think_hard":
            # The reasoner unloads when idle and takes 30-60 s to come back.
            # Say so first: this chunk carries the turn's one emoji (the
            # thinking face) and reaches TTS before the wait starts.
            yield _THINKING_PREAMBLE
            result = self._invoke_voice_tool(
                "think_hard", {"question": intent.argument}, errors,
            )
            if result is None or result.startswith("("):
                yield "I couldn't finish the deeper reasoning."
            else:
                yield result
            return
        if intent is not None and intent.tool == "take_photo":
            # Reached only when xiaozhi's own vision shortcut produced nothing;
            # the 4B would otherwise describe a room it has not seen.
            result = self._invoke_voice_tool("take_photo", {}, errors)
            if result == _CAMERA_DENIED:
                yield f"{FALLBACK_EMOJI} My camera is switched off in Kid Mode."
                return
            if result is None or result.startswith("("):
                yield f"{FALLBACK_EMOJI} I can't see anything clearly right now."
                return
            context = _CAMERA_CONTEXT.format(view=result)
        if room_view and intent is None:
            context = _ROOM_VIEW_CONTEXT.format(view=room_view)
        prompt = _wrap_with_sandwich(user_text + context, self._kid_mode)

        # Reset pi state between voice turns. First turn skips this —
        # the freshly-spawned process is already clean.
        if not self._first_turn:
            try:
                self._client.new_session()
            except PiClientError:
                logger.exception("PiVoiceLLM: new_session failed, continuing")
        self._first_turn = False

        try:
            # #157: kid-mode blocked-content filter on TTS-bound output.
            # Full-turn buffered — the filter drains the pi RPC stream through
            # agent_end before making an atomic allow/replace decision.
            # Emoji enforcement precedes filtering, matching OpenAICompat: in
            # kid mode the filter still makes one atomic whole-turn decision;
            # outside it, chunks stream after the first meaningful one.
            for chunk in filter_tts_stream(
                _enforce_leading_emoji(self._client.iter_turn_text(prompt)),
                self._kid_mode,
                on_hit=self._on_filter_hit,
            ):
                yield chunk
        except PiClientError as exc:
            errors.append(str(exc) or exc.__class__.__name__)
            logger.error("PiVoiceLLM turn failed: %s", exc)
            for line in self._client.recent_stderr()[-5:]:
                logger.error("  pi.stderr: %s", line)
            yield f"{FALLBACK_EMOJI} (brain offline — try again in a moment)"

    def _invoke_voice_tool(
        self, name: str, arguments: dict[str, str],
        errors: list[str] | None = None,
    ) -> str | None:
        try:
            return self._client.invoke_voice_tool(name, arguments)
        except PiClientError as exc:
            logger.error("PiVoiceLLM direct tool %s failed: %s", name, exc)
            if errors is not None:
                errors.append(f"{name}: {exc}" if str(exc) else name)
            return None

    def _on_filter_hit(self, tier: str, match) -> None:
        logger.warning(
            "PiVoiceLLM content-filter hit tier=%s pattern=%r — turn replaced",
            tier, match.group(),
        )
        # The Prometheus counter and /ui/safety/recent ring live in the
        # bridge container; hand the hit over so the dashboard card shows it.
        _report_to_dashboard("/api/voice/filter-hit", {
            "tier": tier,
            "rule": match.group(),
            "prefix": match.string[:8],
        })

    def close(self) -> None:
        """xiaozhi may call this on shutdown — make sure pi cleans up."""
        self._client.close()
