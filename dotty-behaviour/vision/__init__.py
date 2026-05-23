"""Vision support — VLM-side helpers that aren't FastAPI/asyncio bound.

The route module (`routes/vision.py`) handles HTTP + cache + bus
orchestration. Anything pure (prompt templates, parsers, sentinels)
lives here so it can be unit-tested without spinning up FastAPI.
"""

from .room_view import (
    ROOM_VIEW_MOODS,
    ROOM_VIEW_NO_PERSON,
    ROOM_VIEW_SENTINEL,
    ROOM_VIEW_SYSTEM_PROMPT,
    build_room_view_question,
    parse_room_view_response,
)

__all__ = [
    "ROOM_VIEW_MOODS",
    "ROOM_VIEW_NO_PERSON",
    "ROOM_VIEW_SENTINEL",
    "ROOM_VIEW_SYSTEM_PROMPT",
    "build_room_view_question",
    "parse_room_view_response",
]
