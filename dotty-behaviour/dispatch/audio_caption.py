"""Audio captioning client — OpenAI-style chat completion with an
`input_audio` content block (Gemini family on OpenRouter accepts this
shape; override the model + URL via env if your account routes audio
elsewhere).

Lifted from bridge.py's `_call_audio_caption_api`. Same soft-fail
contract: returns the literal string "I couldn't quite hear that
clearly." on any failure path — the audio caption is best-effort and
downstream consumers (security_cycle, scene_synthesis, dashboard)
treat the fallback as "no useful audio" without further wrapping.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import requests

log = logging.getLogger("dotty-behaviour.dispatch.audio_caption")


AUDIO_FALLBACK_DESCRIPTION = "I couldn't quite hear that clearly."


class AudioCaptionClient:
    def __init__(
        self,
        url: str,
        model: str,
        *,
        api_key: str = "",
        timeout_s: float = 20.0,
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
            log.exception("audio caption call failed (url=%s)", self._url)
            return AUDIO_FALLBACK_DESCRIPTION

    async def caption(
        self,
        b64_audio: str,
        audio_format: str,
        question: str,
        *,
        system_prompt: str,
        model: str | None = None,
        max_tokens: int = 200,
        temperature: float = 0.3,
        timeout_s: float | None = None,
    ) -> str:
        """Caption a base64-encoded audio clip. ``audio_format`` is one
        of wav/mp3/opus/flac (the multipart route picks this from
        content-type/filename). Returns the model's text reply on
        success or AUDIO_FALLBACK_DESCRIPTION on any failure."""
        if not self._api_key:
            log.warning("audio caption: api key not set; returning fallback")
            return AUDIO_FALLBACK_DESCRIPTION
        if not self._url:
            log.warning(
                "audio caption: no endpoint configured "
                "(AUDIO_CAPTION_API_URL empty, or blanked by "
                "DOTTY_LOCAL_ONLY); returning fallback"
            )
            return AUDIO_FALLBACK_DESCRIPTION
        payload = {
            "model": model or self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": b64_audio,
                                "format": audio_format,
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
