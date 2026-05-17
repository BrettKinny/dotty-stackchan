"""pi_voice — xiaozhi-server custom LLM provider that routes voice turns
through the dotty-pi container per #36. Replaces zeroclaw / tier1_slim
once the cutover lands.

Public surface:
- LLMProvider — implements xiaozhi's LLMProviderBase response() generator.
- PiClient   — long-lived `pi --mode rpc` client (filtered thinking_delta,
               auto-cancelled extension_ui_request).
"""

from .pi_client import (  # noqa: F401
    PiClient,
    PiClientError,
    default_subprocess_factory,
    local_exec_subprocess_factory,
    make_default_pi_client,
)
from .pi_voice import LLMProvider, _wrap_with_sandwich  # noqa: F401
