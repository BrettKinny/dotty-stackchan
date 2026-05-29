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

import os
from typing import Iterator

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
    from core.utils.textUtils import build_turn_suffix  # type: ignore
except ImportError:  # pragma: no cover — dev workstation fallback
    import importlib.util as _ilu
    from pathlib import Path as _Path

    _tu_path = _Path(__file__).resolve().parents[1] / "textUtils.py"
    _spec = _ilu.spec_from_file_location("dotty_textUtils", _tu_path)
    assert _spec is not None and _spec.loader is not None
    _tu = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_tu)
    build_turn_suffix = _tu.build_turn_suffix  # type: ignore[attr-defined]


TAG = __name__
logger = setup_logging()


def _read_kid_mode() -> bool:
    return os.environ.get("DOTTY_KID_MODE", "true").lower() in ("1", "true", "yes")


def _last_user_text(dialogue: list[dict]) -> str:
    """Find the most recent user-turn content. xiaozhi's dialogue is a
    list of {role, content} dicts in chronological order; the last user
    entry is the utterance we want pi to react to."""
    for msg in reversed(dialogue):
        if msg.get("role") == "user":
            return str(msg.get("content") or "")
    return ""


def _wrap_with_sandwich(user_text: str, kid_mode: bool) -> str:
    """Append the HARD CONSTRAINTS suffix to the user's text via the shared
    textUtils.build_turn_suffix contract — emoji-prefix
    rule, English-only, length caps, kid-mode topic filtering. Without
    this Dotty drifts into Chinese, multi-paragraph replies, and (in
    kid_mode) unsafe topics, since qwen3.5:4b's base behaviour doesn't
    encode any of those constraints."""
    return user_text + build_turn_suffix(kid_mode)


class LLMProvider(LLMProviderBase):
    """xiaozhi-server LLM provider backed by the dotty-pi container."""

    def __init__(self, config: dict, *, client: PiClient | None = None):
        self._container = config.get("container_name") or os.environ.get(
            "DOTTY_PI_CONTAINER", "dotty-pi",
        )
        # KID_MODE is a process-start snapshot. Toggling kid_mode requires
        # a container restart, which already happens via the bridge's
        # existing restart path.
        self._kid_mode = _read_kid_mode()
        # `client` is injected by tests; production passes None to get
        # the env-configured default.
        self._client: PiClient = client if client is not None else make_default_pi_client()
        self._first_turn = True
        msg = f"PiVoiceLLM ready (container={self._container} kid_mode={self._kid_mode})"
        try:
            logger.bind(tag=TAG).info(msg)  # type: ignore[attr-defined]
        except AttributeError:
            logger.info(msg)

    # xiaozhi-server's voice loop calls this as a sync generator.
    # Each yielded string becomes a TTS chunk.
    def response(self, session_id, dialogue, **kwargs) -> Iterator[str]:
        user_text = _last_user_text(dialogue)
        if not user_text:
            yield "(empty turn)"
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
            for chunk in self._client.iter_turn_text(prompt):
                yield chunk
        except PiClientError as exc:
            logger.error("PiVoiceLLM turn failed: %s", exc)
            for line in self._client.recent_stderr()[-5:]:
                logger.error("  pi.stderr: %s", line)
            yield "(brain offline — try again in a moment)"

    def close(self) -> None:
        """xiaozhi may call this on shutdown — make sure pi cleans up."""
        self._client.close()
