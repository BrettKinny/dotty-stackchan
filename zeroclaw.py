import re
import uuid

import requests

from config.logger import setup_logging
from core.providers.llm.base import LLMProviderBase

TAG = __name__
logger = setup_logging()

FALLBACK_EMOJI = "😐"
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?。！？])\s+")


class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.url = config.get("url") or config.get("base_url")
        if not self.url:
            raise ValueError("ZeroClawLLM requires 'url' (e.g. http://RPI:8080/api/message)")
        self.timeout = float(config.get("timeout", 90))
        self.channel = config.get("channel", "stackchan")
        self.system_prompt = config.get("system_prompt", "")
        self.session_id = str(uuid.uuid4())

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

    def response(self, session_id, dialogue, **kwargs):
        payload = {
            "content": self._compose(dialogue),
            "channel": self.channel,
            "session_id": session_id or self.session_id,
            "metadata": {"provider": "zeroclaw"},
        }
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
