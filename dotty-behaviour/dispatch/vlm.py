"""Vision Language Model client — OpenAI-compatible chat completion
with an `image_url` content block.

Lifted from bridge.py's `_call_vision_api`. Same loud-error contract:
when no API key is configured the client returns a specific sentinel
string that downstream LLMs (xiaozhi, dotty-pi) recognise as a fatal
error and refuse to confabulate around, rather than a soft-failure
message they'd happily invent a description for.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import requests

log = logging.getLogger("dotty-behaviour.dispatch.vlm")


VLM_OFFLINE_SENTINEL = (
    "ERROR: my camera is offline right now. Tell the user the vision "
    "system is unavailable and do not guess at what the photo shows."
)
VLM_NETWORK_ERROR_SENTINEL = (
    "ERROR: the vision service didn't respond. Tell the user you "
    "couldn't process the photo and do not guess at what it shows."
)


class VLMClient:
    def __init__(
        self,
        url: str,
        model: str,
        *,
        api_key: str = "",
        timeout_s: float = 15.0,
    ) -> None:
        self._url = url
        self._model = model
        self._api_key = api_key
        self._timeout_s = timeout_s

    @property
    def configured(self) -> bool:
        return bool(self._url and self._model and self._api_key)

    def _post_sync(
        self, payload: dict[str, Any], *, timeout_s: float
    ) -> str:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(
                self._url, json=payload, headers=headers, timeout=timeout_s
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception:
            log.exception("VLM call failed (url=%s)", self._url)
            return VLM_NETWORK_ERROR_SENTINEL

    async def describe_image(
        self,
        b64_image: str,
        question: str,
        *,
        system_prompt: str,
        model: str | None = None,
        max_tokens: int = 200,
        temperature: float = 0.3,
        timeout_s: float | None = None,
    ) -> str:
        """Describe a base64-encoded image. Returns the model's text
        reply on success or one of the SENTINEL strings on failure
        (offline / network). Callers MUST treat sentinel returns as
        unrecoverable — never substitute a softer fallback string."""
        if not self._api_key:
            log.error(
                "VLM call aborted — no api key set "
                "(VLM_API_KEY/VISION_API_KEY/OPENROUTER_API_KEY all empty)"
            )
            return VLM_OFFLINE_SENTINEL
        payload = {
            "model": model or self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64_image}"
                            },
                        },
                        {"type": "text", "text": question},
                    ],
                },
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        eff_timeout = timeout_s if timeout_s is not None else self._timeout_s
        return await asyncio.to_thread(
            self._post_sync, payload, timeout_s=eff_timeout
        )
