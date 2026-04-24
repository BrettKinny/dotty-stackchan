import json
import os
import re
import time
import uuid

import requests

_DBG = os.environ.get("ZEROCLAW_STREAM_DEBUG") == "1"

from config.logger import setup_logging
from core.providers.llm.base import LLMProviderBase

TAG = __name__
logger = setup_logging()

FALLBACK_EMOJI = "😐"
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?。！？])\s+")


class LLMProvider(LLMProviderBase):
    """xiaozhi LLM provider that delegates to the ZeroClaw bridge.

    Supports two bridge endpoints:
      * `/api/message`         — buffered JSON response; sentence-chunked
                                 locally before yielding.
      * `/api/message/stream`  — NDJSON (one chunk per LLM token). Yielded as
                                 they arrive so xiaozhi starts TTS on the
                                 first sentence. Auto-detected by URL ending
                                 in `/stream`.
    """

    def __init__(self, config):
        self.url = config.get("url") or config.get("base_url")
        if not self.url:
            raise ValueError("ZeroClawLLM requires 'url' (e.g. http://RPI:8080/api/message)")
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
        prompt_source = None
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
        return {
            "content": self._compose(dialogue),
            "channel": self.channel,
            "session_id": session_id or self.session_id,
            "metadata": {"provider": "zeroclaw"},
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
        try:
            if _DBG:
                logger.bind(tag=TAG).info(f"strdbg {_ms():7.0f}ms POST begin url={self.url}")
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
                        f"strdbg {_ms():7.0f}ms line[{line_idx}] len={len(line) if line else 0} "
                        f"head={(line or '')[:60]!r}"
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
                                f"strdbg {_ms():7.0f}ms yield content={content[:40]!r}"
                            )
                        yield content
                elif etype == "final":
                    # All chunks already yielded; the `content` field on final
                    # is the concatenation and doesn't need re-emitting.
                    if _DBG:
                        logger.bind(tag=TAG).info(f"strdbg {_ms():7.0f}ms final (return)")
                    return
                elif etype == "error":
                    msg = evt.get("message") or f"{FALLBACK_EMOJI} Stream error."
                    if not any_chunk:
                        yield msg
                    return
            if not any_chunk:
                yield f"{FALLBACK_EMOJI} (no response)"
        except requests.exceptions.Timeout:
            logger.bind(tag=TAG).warning("ZeroClaw bridge stream timeout")
            yield f"{FALLBACK_EMOJI} Sorry, I'm thinking too slowly right now."
        except requests.exceptions.ConnectionError:
            logger.bind(tag=TAG).error(f"ZeroClaw bridge unreachable: {self.url}")
            yield f"{FALLBACK_EMOJI} My brain is offline. Please check the ZeroClaw bridge."
        except Exception as exc:
            logger.bind(tag=TAG).exception("ZeroClaw bridge error (stream)")
            yield f"{FALLBACK_EMOJI} Something went wrong: {exc}"

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
            text = f"{FALLBACK_EMOJI} My brain is offline. Please check the ZeroClaw bridge."
        except Exception as exc:
            logger.bind(tag=TAG).exception("ZeroClaw bridge error")
            text = f"{FALLBACK_EMOJI} Something went wrong: {exc}"

        chunks = self._chunk(text)
        if not chunks:
            yield f"{FALLBACK_EMOJI} (no response)"
            return
        last = len(chunks) - 1
        for i, chunk in enumerate(chunks):
            yield chunk + (" " if i < last else "")
