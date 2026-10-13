"""Narrative LLM client — direct OpenAI-compatible chat completion.

Used for Dotty's internal-narrative writes (dreams, dance
reflections, scene synthesis, story summaries) where the output is
text for memory, not speech routed through the voice pipeline. The
2026-05-15 voice/coding model split keeps this path on
qwen3.6:27b-think in the `voice` matrix set, so narrative calls don't
preempt the resident voice models.

API key is optional — local llama-swap takes none; OpenRouter / a
hosted backend takes a Bearer token. Empty key → no Authorization
header.

Returns None on any failure (network error, HTTP error, malformed
response). Consumers treat None as "skip this write" rather than
crashing the loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import requests

log = logging.getLogger("dotty-behaviour.dispatch.llm")


class NarrativeLLMClient:
    def __init__(
        self,
        url: str,
        model: str,
        *,
        api_key: str = "",
        timeout_s: float = 90.0,
    ) -> None:
        # url should be the full chat-completions endpoint base — e.g.
        # http://127.0.0.1:8080/v1. We append /chat/completions here so
        # callers pass the OpenAI-compatible base URL the same way the
        # rest of the dotty stack uses.
        # An empty base (unset, or blanked by DOTTY_LOCAL_ONLY) stays
        # empty so `configured` is False and chat() skips the request.
        self._url = url.rstrip("/") + "/chat/completions" if url else ""
        self._model = model
        self._api_key = api_key
        self._timeout_s = timeout_s

    @property
    def configured(self) -> bool:
        return bool(self._url and self._model)

    def _post_sync(
        self, payload: dict[str, Any], *, timeout_s: float
    ) -> str | None:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            resp = requests.post(
                self._url, json=payload, headers=headers, timeout=timeout_s
            )
            resp.raise_for_status()
            body = resp.json()
            text = body["choices"][0]["message"]["content"]
            return (text or "").strip() or None
        except Exception:
            log.exception(
                "narrative LLM call failed (url=%s model=%s)",
                self._url,
                payload.get("model"),
            )
            return None

    async def chat(
        self,
        user_prompt: str,
        *,
        system_prompt: str,
        model: str | None = None,
        max_tokens: int = 1200,
        temperature: float = 0.9,
        timeout_s: float | None = None,
    ) -> str | None:
        """Single OpenAI-compatible chat completion. Returns the text
        content of the first choice, or None on any error."""
        if not self.configured:
            log.warning(
                "narrative LLM not configured (url=%s model=%s)",
                self._url,
                self._model,
            )
            return None
        payload = {
            "model": model or self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        eff_timeout = timeout_s if timeout_s is not None else self._timeout_s
        return await asyncio.to_thread(
            self._post_sync, payload, timeout_s=eff_timeout
        )
