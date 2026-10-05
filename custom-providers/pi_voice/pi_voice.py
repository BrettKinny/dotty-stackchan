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
import os
import re
import threading
import time
import unicodedata
import urllib.request
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

from .pi_client import PiClient, PiClientError, make_default_pi_client


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
        build_turn_suffix,
        filter_tts_stream,
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


TAG = __name__
logger = setup_logging()


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


def _wrap_with_sandwich(user_text: str, kid_mode: bool) -> str:
    """Append the HARD CONSTRAINTS suffix to the user's text via the shared
    textUtils.build_turn_suffix contract — emoji-prefix
    rule, English-only, length caps, kid-mode topic filtering. Without
    this Dotty drifts into Chinese, multi-paragraph replies, and (in
    kid_mode) unsafe topics, since qwen3.5:4b's base behaviour doesn't
    encode any of those constraints."""
    return user_text + _VOICE_TOOL_ROUTING + build_turn_suffix(kid_mode)


def _enforce_leading_emoji(chunks: Iterator[str]) -> Iterator[str]:
    """Guarantee the firmware's leading-glyph face contract.

    Pi is prompted to start with an allowed emoji, but model compliance is not
    an output guarantee. Buffer only leading whitespace, then either pass an
    allowed emoji through or replace a missing/disallowed leading glyph with
    the neutral fallback before the model text.
    """
    leading: list[str] = []
    saw_content = False
    for chunk in chunks:
        if not chunk:
            continue
        if not saw_content:
            leading.append(chunk)
            so_far = "".join(leading).lstrip()
            if not so_far:
                continue
            saw_content = True
            if not any(so_far.startswith(emoji) for emoji in ALLOWED_EMOJIS):
                yield f"{FALLBACK_EMOJI} "
                # Do not leave a disallowed model emoji after the fallback:
                # `😐 ❤️ hello` still violates the exactly-one-face contract.
                # Consume the leading symbol plus emoji presentation/joiner
                # codepoints, while leaving ordinary punctuation and text.
                if so_far and unicodedata.category(so_far[0]) == "So":
                    end = 1
                    while end < len(so_far) and (
                        so_far[end] in ("\ufe0f", "\u200d")
                        or 0x1F3FB <= ord(so_far[end]) <= 0x1F3FF
                        or unicodedata.category(so_far[end]) == "So"
                    ):
                        end += 1
                    so_far = so_far[end:].lstrip()
            if so_far:
                yield so_far
            continue
        yield chunk

    if not saw_content:
        yield f"{FALLBACK_EMOJI} (no response)"


_REMEMBER_INTENT_RE = re.compile(
    r"^\s*(?:please\s+)?remember\s+(?:that\s+)?(?P<fact>.+?)\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_RECALL_INTENT_RE = re.compile(
    r"^\s*(?:what did i (?:tell|say to) you about|do you remember)\s+"
    r"(?P<query>.+?)\s*[?!.]?\s*$",
    re.IGNORECASE,
)
_THINK_HARD_INTENT_RE = re.compile(
    r"^\s*(?:please\s+)?think hard (?:about\s*:?)?\s*(?P<question>.+?)\s*[?!.]?\s*$",
    re.IGNORECASE,
)


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
        user_text = _last_user_text(dialogue)
        if not user_text:
            yield f"{FALLBACK_EMOJI} (empty turn)"
            return
        # Everything spoken this turn is reported to the dashboard when the
        # generator finishes — including when xiaozhi closes it early.
        spoken: list[str] = []
        errors: list[str] = []
        started = time.monotonic()
        try:
            for chunk in self._respond(user_text, errors):
                spoken.append(chunk)
                yield chunk
        finally:
            _report_to_dashboard("/api/voice/turn", {
                "request_text": user_text,
                "response_text": "".join(spoken),
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": "; ".join(errors) or None,
            })

    def _respond(self, user_text: str, errors: list[str]) -> Iterator[str]:
        """One turn's TTS chunks. Failures are appended to `errors` so
        response() can report them alongside the spoken fallback."""
        remember_intent = _REMEMBER_INTENT_RE.match(user_text)
        if remember_intent:
            fact = remember_intent.group("fact").strip()
            result = self._invoke_voice_tool("remember", {"fact": fact}, errors)
            if result == "(remembered)":
                yield f"{FALLBACK_EMOJI} I'll remember that."
            else:
                yield f"{FALLBACK_EMOJI} I couldn't save that memory."
            return
        recall_intent = _RECALL_INTENT_RE.match(user_text)
        if recall_intent:
            query = recall_intent.group("query").strip()
            result = self._invoke_voice_tool(
                "memory_lookup", {"query": query}, errors,
            )
            if result is None:
                yield f"{FALLBACK_EMOJI} I couldn't check my memory right now."
            elif result in ("(no memories found)", "(empty query)"):
                yield f"{FALLBACK_EMOJI} I don't remember anything about that yet."
            else:
                yield f"{FALLBACK_EMOJI} {result}"
            return
        think_intent = _THINK_HARD_INTENT_RE.match(user_text)
        if think_intent:
            question = think_intent.group("question").strip()
            result = self._invoke_voice_tool(
                "think_hard", {"question": question}, errors,
            )
            if result is None or result.startswith("("):
                yield f"{FALLBACK_EMOJI} I couldn't finish the deeper reasoning."
            else:
                yield f"{FALLBACK_EMOJI} {result}"
            return
        prompt = _wrap_with_sandwich(user_text, self._kid_mode)

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
