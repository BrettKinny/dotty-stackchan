"""Outbound HTTP clients consumed by perception consumers and routes.

Two clients live here:

* `XiaozhiAdminClient` — xiaozhi-server's `/xiaozhi/admin/*` surface
  (abort, inject-text, say, set-head-angles, set-state, set-toggle,
  set-face-identified, play-asset). Consumers fire actions through
  this instead of calling `requests.post` inline; tests stub by
  patching `requests`.

* `NarrativeLLMClient` — direct llama-swap (or OpenAI-compatible)
  chat-completion call used by scene_synthesis, sleep_dreamer, and
  dance_reflector for their introspective text writes. Bypasses pi
  because these aren't voice output — they're narrative for memory.

The 2026-05-15 cutover model split lives in the daemon config: voice
turns go through PiVoiceLLM/qwen3.5:4b, narrative writes hit
qwen3.6:27b-think on the same llama-swap proxy. The narrative path
preempts neither voice model (both are in the `voice` matrix set).
"""

from .llm import NarrativeLLMClient
from .vlm import (
    VLM_NETWORK_ERROR_SENTINEL,
    VLM_OFFLINE_SENTINEL,
    VLMClient,
)
from .xiaozhi import XiaozhiAdminClient

__all__ = [
    "NarrativeLLMClient",
    "VLM_NETWORK_ERROR_SENTINEL",
    "VLM_OFFLINE_SENTINEL",
    "VLMClient",
    "XiaozhiAdminClient",
]
