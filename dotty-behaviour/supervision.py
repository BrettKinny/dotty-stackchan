"""Lifecycle supervision for long-lived perception consumers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("dotty-behaviour.supervision")


async def supervise_consumer(
    consumer: Any,
    status: dict[str, Any],
    *,
    restart_delay_sec: float = 1.0,
) -> None:
    """Keep a consumer alive and expose failures to the health endpoint.

    Some existing consumers catch their own handler exceptions and return;
    others can raise from ``run``. Both are unexpected exits and are treated
    identically: mark the consumer degraded, wait briefly, and restart it.
    A consumer with an explicit ``enabled`` false flag is intentionally
    reported as disabled rather than restarted forever.
    """
    name = type(consumer).__name__
    if getattr(consumer, "enabled", True) is False:
        status.update(state="disabled", name=name)
        return

    while True:
        status.update(state="running", name=name)
        try:
            await consumer.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status.update(
                state="dead",
                error=f"{type(exc).__name__}: {exc}",
                restarts=int(status.get("restarts", 0)) + 1,
            )
            log.exception("consumer %s crashed; restarting", name)
        else:
            # A normal return is still a dead consumer unless it opted out
            # above. This catches consumers which swallow their own errors.
            status.update(
                state="dead",
                error="consumer exited unexpectedly",
                restarts=int(status.get("restarts", 0)) + 1,
            )
            log.error("consumer %s exited unexpectedly; restarting", name)

        await asyncio.sleep(restart_delay_sec)
