"""Perception consumer loops.

Each consumer subscribes to the bus, processes events on its own
schedule, and dispatches outbound effects through a XiaozhiAdminClient
(and where applicable a NarrativeLLMClient). All consumers expose the
same interface:

    class FooConsumer:
        def __init__(self, state: PerceptionState, xiaozhi: ..., **knobs):
            ...
        async def run(self) -> None:
            ...

main.lifespan() instantiates the configured set and launches each via
asyncio.create_task — same lifecycle the bridge uses today, just
externalised.
"""

from .face_identified_refresher import FaceIdentifiedRefresher
from .face_lost_aborter import FaceLostAborter
from .purr_player import PurrPlayer
from .sound_turner import SoundTurner
from .wake_word_turner import WakeWordTurner

__all__ = [
    "FaceIdentifiedRefresher",
    "FaceLostAborter",
    "PurrPlayer",
    "SoundTurner",
    "WakeWordTurner",
]
