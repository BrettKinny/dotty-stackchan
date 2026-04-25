"""Dance choreography definitions and executor for Dotty.

Two-tier model:

  CHOREOGRAPHIES: dict[str, factory] - factory(beat_ms, duration_ms) → timeline
  DANCE_REGISTRY: dict[str, dict] - presets that pair an audio source (MIDI/WAV)
                                    with a named choreography + tempo + intros

Some choreographies are hand-authored for a specific song (macarena_moves);
others are generic factories that adapt to any BPM/duration (head_bob, etc.).
The executor converts the timeline (absolute ms) to wall-clock servo+LED calls.
"""

import asyncio
from typing import TYPE_CHECKING, Callable as _Callable

if TYPE_CHECKING:
    from typing import Callable

HEAD = "head"
LED = "led"

# Default beat for choreographies that aren't paired with a specific song.
BEAT_MS = 582  # ~103 BPM

# Audio reaches the device ~150 ms after we queue Opus packets (encoding +
# WebSocket transit + device decode buffer). Delay choreography start by this
# amount so servo movements land on the audible beat. Tune by ear/video.
AUDIO_LATENCY_OFFSET_MS = 150


# --------------------------------------------------------------------------
# Macarena — hand-tuned for BEAT_MS=582 (103 BPM); not BPM-portable
# --------------------------------------------------------------------------

_MACARENA_TIMELINE: list[tuple[int, str, dict]] = [
    (0,                  LED,  {"r": 168, "g": 0,   "b": 168}),
    (0,                  HEAD, {"yaw": 0,    "pitch": 30, "speed": 200}),

    # Verse 1 (16 beats) — arms-out poses every 2 beats
    (BEAT_MS * 2,        HEAD, {"yaw": -45,  "pitch": 20, "speed": 300}),
    (BEAT_MS * 2,        LED,  {"r": 168, "g": 0,   "b": 0}),
    (BEAT_MS * 4,        HEAD, {"yaw": 45,   "pitch": 20, "speed": 300}),
    (BEAT_MS * 4,        LED,  {"r": 0,   "g": 0,   "b": 168}),
    (BEAT_MS * 6,        HEAD, {"yaw": -30,  "pitch": 60, "speed": 250}),
    (BEAT_MS * 6,        LED,  {"r": 0,   "g": 168, "b": 0}),
    (BEAT_MS * 8,        HEAD, {"yaw": 30,   "pitch": 60, "speed": 250}),
    (BEAT_MS * 8,        LED,  {"r": 168, "g": 168, "b": 0}),
    (BEAT_MS * 10,       HEAD, {"yaw": -40,  "pitch": 10, "speed": 300}),
    (BEAT_MS * 10,       LED,  {"r": 168, "g": 0,   "b": 168}),
    (BEAT_MS * 12,       HEAD, {"yaw": 40,   "pitch": 10, "speed": 300}),
    (BEAT_MS * 12,       LED,  {"r": 0,   "g": 168, "b": 168}),
    (BEAT_MS * 14,       HEAD, {"yaw": -50,  "pitch": 70, "speed": 250}),
    (BEAT_MS * 14,       LED,  {"r": 168, "g": 100, "b": 0}),
    (BEAT_MS * 16,       HEAD, {"yaw": 50,   "pitch": 70, "speed": 250}),
    (BEAT_MS * 16,       LED,  {"r": 100, "g": 0,   "b": 168}),

    # "Heeey Macarena!" hip wiggle (4 beats)
    (BEAT_MS * 18,       HEAD, {"yaw": -60,  "pitch": 30, "speed": 500}),
    (BEAT_MS * 18,       LED,  {"r": 168, "g": 0,   "b": 168}),
    (BEAT_MS * 18 + 290, HEAD, {"yaw": 60,   "pitch": 30, "speed": 500}),
    (BEAT_MS * 19,       HEAD, {"yaw": -60,  "pitch": 30, "speed": 500}),
    (BEAT_MS * 19 + 290, HEAD, {"yaw": 60,   "pitch": 30, "speed": 500}),
    (BEAT_MS * 20,       HEAD, {"yaw": -60,  "pitch": 30, "speed": 500}),
    (BEAT_MS * 20 + 290, HEAD, {"yaw": 60,   "pitch": 30, "speed": 500}),

    # Jump turn (2 beats)
    (BEAT_MS * 21,       HEAD, {"yaw": -128, "pitch": 45, "speed": 800}),
    (BEAT_MS * 21,       LED,  {"r": 168, "g": 168, "b": 168}),
    (BEAT_MS * 22,       HEAD, {"yaw": 128,  "pitch": 45, "speed": 800}),
    (BEAT_MS * 23,       HEAD, {"yaw": 0,    "pitch": 30, "speed": 400}),

    # Verse 2 (variation)
    (BEAT_MS * 24,       HEAD, {"yaw": -50,  "pitch": 25, "speed": 300}),
    (BEAT_MS * 24,       LED,  {"r": 168, "g": 0,   "b": 0}),
    (BEAT_MS * 26,       HEAD, {"yaw": 50,   "pitch": 25, "speed": 300}),
    (BEAT_MS * 26,       LED,  {"r": 0,   "g": 0,   "b": 168}),
    (BEAT_MS * 28,       HEAD, {"yaw": -35,  "pitch": 65, "speed": 250}),
    (BEAT_MS * 28,       LED,  {"r": 0,   "g": 168, "b": 0}),
    (BEAT_MS * 30,       HEAD, {"yaw": 35,   "pitch": 65, "speed": 250}),
    (BEAT_MS * 30,       LED,  {"r": 168, "g": 168, "b": 0}),
    (BEAT_MS * 32,       HEAD, {"yaw": -45,  "pitch": 15, "speed": 300}),
    (BEAT_MS * 32,       LED,  {"r": 168, "g": 0,   "b": 168}),
    (BEAT_MS * 34,       HEAD, {"yaw": 45,   "pitch": 15, "speed": 300}),
    (BEAT_MS * 34,       LED,  {"r": 0,   "g": 168, "b": 168}),
    (BEAT_MS * 36,       HEAD, {"yaw": -55,  "pitch": 75, "speed": 250}),
    (BEAT_MS * 36,       LED,  {"r": 168, "g": 100, "b": 0}),
    (BEAT_MS * 38,       HEAD, {"yaw": 55,   "pitch": 75, "speed": 250}),
    (BEAT_MS * 38,       LED,  {"r": 100, "g": 0,   "b": 168}),

    # Second wiggle
    (BEAT_MS * 40,       HEAD, {"yaw": -70,  "pitch": 30, "speed": 500}),
    (BEAT_MS * 40,       LED,  {"r": 168, "g": 0,   "b": 168}),
    (BEAT_MS * 40 + 290, HEAD, {"yaw": 70,   "pitch": 30, "speed": 500}),
    (BEAT_MS * 41,       HEAD, {"yaw": -70,  "pitch": 30, "speed": 500}),
    (BEAT_MS * 41 + 290, HEAD, {"yaw": 70,   "pitch": 30, "speed": 500}),
    (BEAT_MS * 42,       HEAD, {"yaw": -70,  "pitch": 30, "speed": 500}),
    (BEAT_MS * 42 + 290, HEAD, {"yaw": 70,   "pitch": 30, "speed": 500}),

    # Final jump turn + bow + neutral
    (BEAT_MS * 43,       HEAD, {"yaw": -128, "pitch": 45, "speed": 800}),
    (BEAT_MS * 43,       LED,  {"r": 168, "g": 168, "b": 168}),
    (BEAT_MS * 44,       HEAD, {"yaw": 128,  "pitch": 45, "speed": 800}),
    (BEAT_MS * 45,       HEAD, {"yaw": 0,    "pitch": 80, "speed": 300}),
    (BEAT_MS * 45,       LED,  {"r": 168, "g": 0,   "b": 168}),
    (BEAT_MS * 47,       HEAD, {"yaw": 0,    "pitch": 0,  "speed": 200}),
    (BEAT_MS * 47,       LED,  {"r": 0,   "g": 0,   "b": 0}),
]


def _macarena_moves(beat_ms: int, duration_ms: int) -> list[tuple[int, str, dict]]:
    return _MACARENA_TIMELINE


# --------------------------------------------------------------------------
# Generic BPM-adaptive choreographies
# --------------------------------------------------------------------------

# Rainbow palette — reused by color_party and bouncy_party.
_RAINBOW = [
    (168, 0, 0), (168, 80, 0), (168, 168, 0), (0, 168, 0),
    (0, 168, 168), (0, 0, 168), (100, 0, 168), (168, 0, 168),
]


def _head_bob(beat_ms: int, duration_ms: int) -> list[tuple[int, str, dict]]:
    """Gentle nodding side-to-side; cool blue LED."""
    timeline = [(0, LED, {"r": 0, "g": 80, "b": 168})]
    n_beats = max(1, duration_ms // beat_ms)
    for i in range(n_beats):
        yaw = -20 if i % 2 == 0 else 20
        timeline.append((i * beat_ms, HEAD, {"yaw": yaw, "pitch": 30, "speed": 250}))
    timeline.append((duration_ms, HEAD, {"yaw": 0, "pitch": 0, "speed": 200}))
    timeline.append((duration_ms, LED, {"r": 0, "g": 0, "b": 0}))
    return timeline


def _color_party(beat_ms: int, duration_ms: int) -> list[tuple[int, str, dict]]:
    """Lively yaw + cycling rainbow LEDs every 2 beats; pitch nod every 4."""
    timeline: list[tuple[int, str, dict]] = []
    n_beats = max(1, duration_ms // beat_ms)
    for i in range(n_beats):
        t = i * beat_ms
        # Yaw oscillates each beat; widening sweep on every 4th beat.
        if i % 4 == 3:
            timeline.append((t, HEAD, {"yaw": -60 if i % 8 < 4 else 60, "pitch": 50, "speed": 500}))
        else:
            yaw = (-30, 30, -45, 45)[i % 4]
            timeline.append((t, HEAD, {"yaw": yaw, "pitch": 25, "speed": 350}))
        if i % 2 == 0:
            r, g, b = _RAINBOW[(i // 2) % len(_RAINBOW)]
            timeline.append((t, LED, {"r": r, "g": g, "b": b}))
    timeline.append((duration_ms, HEAD, {"yaw": 0, "pitch": 0, "speed": 200}))
    timeline.append((duration_ms, LED, {"r": 0, "g": 0, "b": 0}))
    return timeline


def _bouncy_party(beat_ms: int, duration_ms: int) -> list[tuple[int, str, dict]]:
    """Fast head bouncing + pitch flicks; rainbow rapid-fire."""
    timeline: list[tuple[int, str, dict]] = []
    n_beats = max(1, duration_ms // beat_ms)
    for i in range(n_beats):
        t = i * beat_ms
        # Two yaw flicks per beat for energetic feel.
        timeline.append((t, HEAD, {"yaw": -40, "pitch": 60, "speed": 700}))
        timeline.append((t + beat_ms // 2, HEAD, {"yaw": 40, "pitch": 20, "speed": 700}))
        # LED change each beat.
        r, g, b = _RAINBOW[i % len(_RAINBOW)]
        timeline.append((t, LED, {"r": r, "g": g, "b": b}))
    timeline.append((duration_ms, HEAD, {"yaw": 0, "pitch": 0, "speed": 200}))
    timeline.append((duration_ms, LED, {"r": 0, "g": 0, "b": 0}))
    return timeline


def _sleepy_sway(beat_ms: int, duration_ms: int) -> list[tuple[int, str, dict]]:
    """Slow gentle sway + warm dim LEDs; for slow songs."""
    timeline = [(0, LED, {"r": 100, "g": 40, "b": 0})]
    # One sway every 4 beats.
    n_beats = max(1, duration_ms // beat_ms)
    for i in range(0, n_beats, 4):
        t = i * beat_ms
        yaw = -15 if (i // 4) % 2 == 0 else 15
        timeline.append((t, HEAD, {"yaw": yaw, "pitch": 25, "speed": 150}))
    timeline.append((duration_ms, HEAD, {"yaw": 0, "pitch": 0, "speed": 150}))
    timeline.append((duration_ms, LED, {"r": 0, "g": 0, "b": 0}))
    return timeline


def _look_around(beat_ms: int, duration_ms: int) -> list[tuple[int, str, dict]]:
    """Dramatic slow head turns + pose holds; deep blue/white pulses."""
    poses = [
        ({"yaw": 0,   "pitch": 60,  "speed": 250}, (0, 0, 168)),
        ({"yaw": -60, "pitch": 40,  "speed": 200}, (168, 168, 168)),
        ({"yaw": 60,  "pitch": 40,  "speed": 200}, (168, 168, 168)),
        ({"yaw": 0,   "pitch": 80,  "speed": 250}, (0, 0, 100)),
        ({"yaw": -80, "pitch": 20,  "speed": 200}, (168, 168, 168)),
        ({"yaw": 80,  "pitch": 20,  "speed": 200}, (168, 168, 168)),
    ]
    timeline: list[tuple[int, str, dict]] = []
    n_beats = max(1, duration_ms // beat_ms)
    pose_every = 3  # change pose every 3 beats
    pi = 0
    for i in range(0, n_beats, pose_every):
        t = i * beat_ms
        head, color = poses[pi % len(poses)]
        timeline.append((t, HEAD, head))
        r, g, b = color
        timeline.append((t, LED, {"r": r, "g": g, "b": b}))
        pi += 1
    timeline.append((duration_ms, HEAD, {"yaw": 0, "pitch": 0, "speed": 200}))
    timeline.append((duration_ms, LED, {"r": 0, "g": 0, "b": 0}))
    return timeline


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

ChoreographyFactory = _Callable[[int, int], list[tuple[int, str, dict]]]

CHOREOGRAPHIES: dict[str, ChoreographyFactory] = {
    "macarena_moves": _macarena_moves,
    "head_bob": _head_bob,
    "color_party": _color_party,
    "bouncy_party": _bouncy_party,
    "sleepy_sway": _sleepy_sway,
    "look_around": _look_around,
}


# Each preset pairs a song (audio_file MIDI/WAV) with a choreography.
# `audio_tempo_bpm` rewrites the MIDI's set_tempo events at render time so the
# music matches the choreography's intended pace; `duration_ms` trims the
# rendered audio AND bounds the choreography's note count.
DANCE_REGISTRY: dict[str, dict] = {
    "macarena": {
        "choreography": "macarena_moves",
        "audio_file": "config/assets/songs/macarena.mid",
        "audio_tempo_bpm": 103,  # MIDI ships at 62 BPM; 103 matches BEAT_MS=582
        "intro": "Ooh yeah! It's Macarena time! Let's dance!",
        "outro": "Heeey Macarena! Ayyy! That was so much fun!",
        "duration_ms": BEAT_MS * 48,
    },
    "tetris": {
        "choreography": "bouncy_party",
        "audio_file": "config/assets/songs/tetris.mid",
        "audio_tempo_bpm": 130,  # Korobeiniki traditional uptempo
        "intro": "Tetris time! Watch me bop!",
        "outro": "Game over, but in a fun way!",
        "duration_ms": 30000,
    },
    "mountain_king": {
        "choreography": "look_around",
        "audio_file": "config/assets/songs/mountain_king.mid",
        "audio_tempo_bpm": 120,
        "intro": "In the hall of the mountain king!",
        "outro": "Phew! That was creepy!",
        "duration_ms": 35000,
    },
    "star_wars": {
        "choreography": "look_around",
        "audio_file": "config/assets/songs/star_wars.mid",
        "audio_tempo_bpm": 108,
        "intro": "May the force be with us!",
        "outro": "And that's the way of the Jedi!",
        "duration_ms": 30000,
    },
    "pirates": {
        "choreography": "bouncy_party",
        "audio_file": "config/assets/songs/pirates.mid",
        "audio_tempo_bpm": 130,
        "intro": "Yo ho ho! Pirate time!",
        "outro": "Arrr! Land ho!",
        "duration_ms": 30000,
    },
    "mario": {
        "choreography": "color_party",
        "audio_file": "config/assets/songs/mario.mid",
        "audio_tempo_bpm": 200,
        "intro": "It's-a me, Dotty! Let's go!",
        "outro": "Wahoo! Mario style!",
        "duration_ms": 25000,
    },
}

DEFAULT_DANCE = "macarena"


def resolve_timeline(dance: dict) -> list[tuple[int, str, dict]]:
    """Build the absolute-ms timeline for a dance preset.

    Generic choreographies adapt to the preset's BPM (computed from
    audio_tempo_bpm) and duration_ms; macarena_moves ignores both.
    """
    bpm = dance.get("audio_tempo_bpm")
    beat_ms = int(60_000 / bpm) if bpm else BEAT_MS
    duration_ms = dance.get("duration_ms", beat_ms * 48)
    factory = CHOREOGRAPHIES.get(dance.get("choreography", "macarena_moves"))
    if factory is None:
        factory = CHOREOGRAPHIES["macarena_moves"]
    timeline = factory(beat_ms, duration_ms)
    timeline.sort(key=lambda t: t[0])
    return timeline


async def execute_choreography(
    conn: object,
    timeline: list[tuple[int, str, dict]],
    send_head_fn: "Callable",
    send_led_fn: "Callable",
    audio_latency_offset_ms: int = AUDIO_LATENCY_OFFSET_MS,
) -> None:
    """Walk a timed choreography timeline, sending MCP commands at each mark."""
    loop = asyncio.get_event_loop()
    if audio_latency_offset_ms > 0:
        await asyncio.sleep(audio_latency_offset_ms / 1000.0)
    start = loop.time()

    for t_ms, action_type, params in timeline:
        now_ms = (loop.time() - start) * 1000
        delay = t_ms - now_ms
        if delay > 0:
            await asyncio.sleep(delay / 1000.0)

        if getattr(conn, "client_abort", False) or getattr(conn, "is_exiting", False):
            break

        if action_type == HEAD:
            await send_head_fn(conn, params["yaw"], params["pitch"], params.get("speed", 150))
        elif action_type == LED:
            await send_led_fn(conn, params["r"], params["g"], params["b"])
