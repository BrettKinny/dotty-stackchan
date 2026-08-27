"""Health endpoint — Docker healthcheck target."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from config import VERSION

router = APIRouter()


@router.get("/health")
async def health(request: Request):
    """Report process health plus the lifecycle of each consumer.

    A process can be serving HTTP while a perception worker has silently
    exited. Surface that as HTTP 503 so Docker and ``dotty doctor`` can act
    on the real failure rather than a green but inert daemon.
    """
    statuses = getattr(request.app.state, "consumer_status", {})
    dead = [
        name for name, status in statuses.items()
        if status.get("state") not in ("starting", "running", "disabled")
    ]
    # Also catch a supervisor task that died before it could update its map.
    for task in getattr(request.app.state, "consumer_tasks", []):
        if task.done() and not task.cancelled():
            name = task.get_name()
            if name not in dead:
                dead.append(name)
    body = {
        "status": "degraded" if dead else "ok",
        "service": "dotty-behaviour",
        "version": VERSION,
        "consumers": statuses,
    }
    if dead:
        body["dead_consumers"] = dead
        return JSONResponse(status_code=503, content=body)
    return body
