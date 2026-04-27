import json
import os
import pathlib
import re
import time
import uuid

import requests

# Person ids in household.yaml are lowercase, short, alnum + underscore
# + hyphen. Anything not matching this is treated as user text by the
# v1/v2 marker disambiguator in `_payload`.
_PERSON_ID_RE = re.compile(r"^[a-z0-9_-]{0,32}$")


def _looks_like_person_id(s: str) -> bool:
    """True for the v2 marker's `<person_id_or_blank>` slot — empty
    string OR a short alnum/underscore/hyphen token. Deliberately
    accepts the empty string so the v2 shape with no roster match
    still parses as v2 (otherwise an empty second line would silently
    revert to v1)."""
    return bool(_PERSON_ID_RE.match(s))

_DBG = os.environ.get("ZEROCLAW_STREAM_DEBUG") == "1"

from config.logger import setup_logging
from core.providers.llm.base import LLMProviderBase
from core.utils.textUtils import FALLBACK_EMOJI, _SENTENCE_BOUNDARY

TAG = __name__
logger = setup_logging()

# personas/ is mounted at parents[4]/personas relative to this file
# inside the xiaozhi-server package tree (/opt/xiaozhi-esp32-server/).
try:
    _PERSONAS_BASE: pathlib.Path | None = pathlib.Path(__file__).parents[4] / "personas"
except IndexError:
    _PERSONAS_BASE = None


def _load_persona_prompt() -> str:
    """Load persona from the PERSONA env var at call time.

    Returns empty string if PERSONA is unset, whitespace-only, or the
    corresponding file is unreadable. PERSONA_DIR overrides the base
    directory (default: parents[4]/personas relative to this file).
    """
    name = os.environ.get("PERSONA", "").strip()
    if not name:
        return ""
    persona_dir = os.environ.get("PERSONA_DIR", "")
    if persona_dir:
        base = pathlib.Path(persona_dir)
    elif _PERSONAS_BASE is not None:
        base = _PERSONAS_BASE
    else:
        return ""
    try:
        return (base / f"{name}.md").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class LLMProvider(LLMProviderBase):
    """xiaozhi LLM provider that delegates to the ZeroClaw bridge.

    Supports two bridge endpoints:
      * `/api/message`         -- buffered JSON response; sentence-chunked
                                  locally before yielding.
      * `/api/message/stream`  -- NDJSON (one chunk per LLM token). Yielded
                                  as they arrive so xiaozhi starts TTS on
                                  the first sentence. Auto-detected by URL
                                  ending in `/stream`.
    """

    def __init__(self, config):
        self.url = config.get("url") or config.get("base_url")
        if not self.url:
            raise ValueError(
                "ZeroClawLLM requires 'url' (e.g. http://<zeroclaw-host>:8080/api/message)"
            )
        self.timeout = float(config.get("timeout", 90))
        self.channel = config.get("channel", "dotty")
        self.system_prompt = config.get("system_prompt", "")
        self.session_id = str(uuid.uuid4())
        self._streaming = self.url.rstrip("/").endswith("/stream")

    def _last_user_text(self, dialogue):
        for msg in reversed(dialogue):
            if msg.get("role") == "user":
                return msg.get("content", "") or ""
        return ""

    def _compose(self, dialogue):
        user_text = self._last_user_text(dialogue)
        # PERSONA env: load from file at request time for zero-restart hot-swap.
        prompt_source = _load_persona_prompt()
        if not prompt_source:
            for msg in dialogue:
                if msg.get("role") == "system" and msg.get("content"):
                    prompt_source = msg["content"]
                    break
        if not prompt_source:
            prompt_source = self.system_prompt
        if prompt_source:
            return f"[Context] {prompt_source.strip()}\n\n[User] {user_text}"
        return user_text

    def _chunk(self, text):
        text = (text or "").strip()
        if not text:
            return []
        pieces = [p.strip() for p in _SENTENCE_BOUNDARY.split(text)]
        return [p for p in pieces if p]

    def _payload(self, session_id, dialogue):
        # Marker detection runs on the raw user text. _compose() prepends
        # "[Context] ...\n\n[User] ", so a check on the composed string would
        # never match -- startswith() would see the prefix, not the marker.
        user_text = self._last_user_text(dialogue)
        metadata = {"provider": "zeroclaw"}
        stripped_user = user_text

        # Description-based identity (Layer 4 server-side, no storage).
        # receiveAudioHandle prepends to the user text when it has a
        # fresh VLM-generated description of who's in front of the
        # camera. The marker has two shapes:
        #
        #   v1 (description only):
        #       "[ROOM_VIEW]\n<description>\n<user text>"
        #
        #   v2 (description + matched roster id):
        #       "[ROOM_VIEW]\n<description>\n<person_id_or_blank>\n<user text>"
        #
        # We accept both. v2 is the room_view + roster identification
        # path: the bridge's room_view VLM call returns a `(desc,
        # person_id)` tuple, and the xiaozhi side passes the matched
        # id along on the second line. Empty string on the id line is
        # the explicit "no roster match" signal (vs. v1's no-line-at-
        # all). Validation against the registry happens bridge-side in
        # SpeakerResolver — we just shuttle the value across.
        if stripped_user.startswith("[ROOM_VIEW]\n"):
            tail = stripped_user[len("[ROOM_VIEW]\n"):]
            lines = tail.split("\n", 2)
            # lines[0] = description; lines[1] = person_id (v2) or
            # the start of user text (v1); lines[2] = user text (v2).
            if len(lines) >= 3:
                desc, second, rest = lines[0], lines[1], lines[2]
                # Heuristic: a person_id is a short, alnum-or-underscore
                # token. Anything else is treated as the start of v1
                # user text and we fall back to v1 parsing.
                if _looks_like_person_id(second):
                    metadata["room_description"] = desc
                    if second:
                        metadata["room_match_person_id"] = second
                    stripped_user = rest
                else:
                    metadata["room_description"] = desc
                    stripped_user = second + ("\n" + rest if rest else "")
            elif len(lines) == 2:
                metadata["room_description"] = lines[0]
                stripped_user = lines[1]
            else:
                metadata["room_description"] = lines[0]
                stripped_user = ""

        if stripped_user.startswith("[SMART_MODE]\n"):
            stripped_user = stripped_user[len("[SMART_MODE]\n"):]
            metadata["smart_mode"] = True
        elif stripped_user.startswith("[SMART_MODE_ACK] "):
            stripped_user = stripped_user[len("[SMART_MODE_ACK] "):]
        if stripped_user != user_text:
            dialogue = [dict(msg) for msg in dialogue]
            for msg in reversed(dialogue):
                if msg.get("role") == "user":
                    msg["content"] = stripped_user
                    break
        content = self._compose(dialogue)
        return {
            "content": content,
            "channel": self.channel,
            "session_id": session_id or self.session_id,
            "metadata": metadata,
        }

    def response(self, session_id, dialogue, **kwargs):
        payload = self._payload(session_id, dialogue)
        if self._streaming:
            yield from self._response_stream(payload)
        else:
            yield from self._response_buffered(payload)

    def _response_stream(self, payload):
        t0 = time.perf_counter() if _DBG else 0.0

        def _ms():
            return (time.perf_counter() - t0) * 1000.0

        resp = None
        try:
            if _DBG:
                logger.bind(tag=TAG).info(
                    f"strdbg {_ms():7.0f}ms POST begin url={self.url}"
                )
            resp = requests.post(
                self.url,
                json=payload,
                timeout=self.timeout,
                headers={"content-type": "application/json"},
                stream=True,
            )
            resp.raise_for_status()
            if _DBG:
                logger.bind(tag=TAG).info(
                    f"strdbg {_ms():7.0f}ms headers ok status={resp.status_code}"
                )
            any_chunk = False
            line_idx = 0
            for line in resp.iter_lines(decode_unicode=True):
                if _DBG:
                    logger.bind(tag=TAG).info(
                        f"strdbg {_ms():7.0f}ms line[{line_idx}]"
                        f" len={len(line) if line else 0}"
                        f" head={(line or '')[:60]!r}"
                    )
                line_idx += 1
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except Exception:
                    logger.bind(tag=TAG).warning(
                        f"ZeroClaw stream non-JSON line: {line[:200]!r}"
                    )
                    continue
                etype = evt.get("type")
                if etype == "chunk":
                    content = evt.get("content") or ""
                    if content:
                        any_chunk = True
                        if _DBG:
                            logger.bind(tag=TAG).info(
                                f"strdbg {_ms():7.0f}ms yield"
                                f" content={content[:40]!r}"
                            )
                        yield content
                elif etype == "final":
                    if _DBG:
                        logger.bind(tag=TAG).info(
                            f"strdbg {_ms():7.0f}ms final (return)"
                        )
                    return
                elif etype == "error":
                    msg = evt.get("message") or f"{FALLBACK_EMOJI} Stream error."
                    if not any_chunk:
                        yield msg
                    return
            if not any_chunk:
                yield f"{FALLBACK_EMOJI} (no response)"
        except GeneratorExit:
            logger.bind(tag=TAG).info("ZeroClaw stream aborted (barge-in)")
        except requests.exceptions.Timeout:
            logger.bind(tag=TAG).warning("ZeroClaw bridge stream timeout")
            yield f"{FALLBACK_EMOJI} Sorry, I'm thinking too slowly right now."
        except requests.exceptions.ConnectionError:
            logger.bind(tag=TAG).error(f"ZeroClaw bridge unreachable: {self.url}")
            yield (
                f"{FALLBACK_EMOJI} My brain is offline."
                " Please check the ZeroClaw bridge."
            )
        except Exception:  # noqa: BLE001
            logger.bind(tag=TAG).exception("ZeroClaw bridge error (stream)")
            yield f"{FALLBACK_EMOJI} Something went wrong, please try again."
        finally:
            if resp is not None:
                resp.close()

    def _response_buffered(self, payload):
        try:
            resp = requests.post(
                self.url,
                json=payload,
                timeout=self.timeout,
                headers={"content-type": "application/json"},
            )
            resp.raise_for_status()
            body = resp.json()
            text = body.get("response", "").strip()
            if not text:
                text = f"{FALLBACK_EMOJI} (empty response)"
        except requests.exceptions.Timeout:
            logger.bind(tag=TAG).warning("ZeroClaw bridge timeout")
            text = f"{FALLBACK_EMOJI} Sorry, I'm thinking too slowly right now."
        except requests.exceptions.ConnectionError:
            logger.bind(tag=TAG).error(f"ZeroClaw bridge unreachable: {self.url}")
            text = (
                f"{FALLBACK_EMOJI} My brain is offline."
                " Please check the ZeroClaw bridge."
            )
        except Exception:  # noqa: BLE001
            logger.bind(tag=TAG).exception("ZeroClaw bridge error")
            text = f"{FALLBACK_EMOJI} Something went wrong, please try again."

        chunks = self._chunk(text)
        if not chunks:
            yield f"{FALLBACK_EMOJI} (no response)"
            return
        last = len(chunks) - 1
        for i, chunk in enumerate(chunks):
            yield chunk + (" " if i < last else "")
