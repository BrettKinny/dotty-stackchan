"""Perception package — read-only façade over the bridge's per-device
caches. The bridge.py module owns the underlying dicts; modules here
compose them into shapes consumed by the dashboard and the talk-turn
prompt builder.
"""

from .cache import PerceptionSnapshot, snapshot

__all__ = ["PerceptionSnapshot", "snapshot"]
