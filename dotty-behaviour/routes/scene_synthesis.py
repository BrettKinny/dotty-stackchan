"""Scene-synthesis read endpoint — GET /api/scene-synthesis/recent.

The synthesis text + face_id + state + ts_wall are written to
`PerceptionState.scene_synthesis_cache` by the `SceneSynthesisLoop`
consumer (see `consumers/scene_synthesis.py`). This route exposes
that cache as JSON for the bridge dashboard's scene tile.

Tile 3 of #115 (rewire bridge dashboard caches to dotty-behaviour).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from perception import PerceptionState

log = logging.getLogger("dotty-behaviour.routes.scene_synthesis")


def get_perception_state(request: Request) -> PerceptionState:
    state = getattr(request.app.state, "perception", None)
    if state is None:
        raise RuntimeError("PerceptionState not attached to app.state")
    return state


router = APIRouter()


@router.get("/api/scene-synthesis/recent")
async def scene_synthesis_recent(
    state: PerceptionState = Depends(get_perception_state),
) -> dict[str, dict]:
    """Per-device scene_synthesis_cache (text narrative + timestamps).

    Consumed by the bridge dashboard's scene tile via the Tile 3 (#115)
    HTTP rewire — bridge fetches this once per HTMX poll (cached for 2 s
    inside bridge to keep dotty-behaviour quiet)."""
    return state.scene_synthesis_cache
