"""Dance choreography definitions and executor for Dotty."""

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Callable

HEAD = "head"
LED = "led"

BEAT_MS = 582  # Macarena ~103 BPM

# Audio reaches the device ~150 ms after we queue Opus packets (encoding +
# WebSocket transit + device decode buffer). Delay choreography start by this
# amount so servo movements land on the audible beat. Tune by ear/video.
AUDIO_LATENCY_OFFSET_MS = 150

# Timeline: (time_ms, action_type, params)
# Servo: yaw -128..128, pitch 0..90, speed 100..1000
# LED: r/g/b 0..168
MACARENA_TIMELINE: list[tuple[int, str, dict]] = [
    # --- Intro: center + purple LED ---
    (0,                 LED,  {"r": 168, "g": 0,   "b": 168}),
    (0,                 HEAD, {"yaw": 0,    "pitch": 30,  "speed": 200}),

    # --- Verse 1 (16 beats) ---
    # Beat 1-2: right arm out (look right)
    (BEAT_MS * 2,       HEAD, {"yaw": -45,  "pitch": 20,  "speed": 300}),
    (BEAT_MS * 2,       LED,  {"r": 168, "g": 0,   "b": 0}),
    # Beat 3-4: left arm out (look left)
    (BEAT_MS * 4,       HEAD, {"yaw": 45,   "pitch": 20,  "speed": 300}),
    (BEAT_MS * 4,       LED,  {"r": 0,   "g": 0,   "b": 168}),
    # Beat 5-6: flip right (tilt up-right)
    (BEAT_MS * 6,       HEAD, {"yaw": -30,  "pitch": 60,  "speed": 250}),
    (BEAT_MS * 6,       LED,  {"r": 0,   "g": 168, "b": 0}),
    # Beat 7-8: flip left (tilt up-left)
    (BEAT_MS * 8,       HEAD, {"yaw": 30,   "pitch": 60,  "speed": 250}),
    (BEAT_MS * 8,       LED,  {"r": 168, "g": 168, "b": 0}),
    # Beat 9-10: right on left shoulder (right-down)
    (BEAT_MS * 10,      HEAD, {"yaw": -40,  "pitch": 10,  "speed": 300}),
    (BEAT_MS * 10,      LED,  {"r": 168, "g": 0,   "b": 168}),
    # Beat 11-12: left on right shoulder (left-down)
    (BEAT_MS * 12,      HEAD, {"yaw": 40,   "pitch": 10,  "speed": 300}),
    (BEAT_MS * 12,      LED,  {"r": 0,   "g": 168, "b": 168}),
    # Beat 13-14: right behind head (up-right)
    (BEAT_MS * 14,      HEAD, {"yaw": -50,  "pitch": 70,  "speed": 250}),
    (BEAT_MS * 14,      LED,  {"r": 168, "g": 100, "b": 0}),
    # Beat 15-16: left behind head (up-left)
    (BEAT_MS * 16,      HEAD, {"yaw": 50,   "pitch": 70,  "speed": 250}),
    (BEAT_MS * 16,      LED,  {"r": 100, "g": 0,   "b": 168}),

    # --- "Heeey Macarena!" (4 beats): hip wiggle = rapid yaw oscillation ---
    (BEAT_MS * 18,      HEAD, {"yaw": -60,  "pitch": 30,  "speed": 500}),
    (BEAT_MS * 18,      LED,  {"r": 168, "g": 0,   "b": 168}),
    (BEAT_MS * 18 + 290, HEAD, {"yaw": 60,  "pitch": 30,  "speed": 500}),
    (BEAT_MS * 19,      HEAD, {"yaw": -60,  "pitch": 30,  "speed": 500}),
    (BEAT_MS * 19 + 290, HEAD, {"yaw": 60,  "pitch": 30,  "speed": 500}),
    (BEAT_MS * 20,      HEAD, {"yaw": -60,  "pitch": 30,  "speed": 500}),
    (BEAT_MS * 20 + 290, HEAD, {"yaw": 60,  "pitch": 30,  "speed": 500}),

    # --- Jump turn (2 beats): fast sweep then center ---
    (BEAT_MS * 21,      HEAD, {"yaw": -128, "pitch": 45,  "speed": 800}),
    (BEAT_MS * 21,      LED,  {"r": 168, "g": 168, "b": 168}),
    (BEAT_MS * 22,      HEAD, {"yaw": 128,  "pitch": 45,  "speed": 800}),
    (BEAT_MS * 23,      HEAD, {"yaw": 0,    "pitch": 30,  "speed": 400}),

    # --- Verse 2 (repeat with slight variation) ---
    (BEAT_MS * 24,      HEAD, {"yaw": -50,  "pitch": 25,  "speed": 300}),
    (BEAT_MS * 24,      LED,  {"r": 168, "g": 0,   "b": 0}),
    (BEAT_MS * 26,      HEAD, {"yaw": 50,   "pitch": 25,  "speed": 300}),
    (BEAT_MS * 26,      LED,  {"r": 0,   "g": 0,   "b": 168}),
    (BEAT_MS * 28,      HEAD, {"yaw": -35,  "pitch": 65,  "speed": 250}),
    (BEAT_MS * 28,      LED,  {"r": 0,   "g": 168, "b": 0}),
    (BEAT_MS * 30,      HEAD, {"yaw": 35,   "pitch": 65,  "speed": 250}),
    (BEAT_MS * 30,      LED,  {"r": 168, "g": 168, "b": 0}),
    (BEAT_MS * 32,      HEAD, {"yaw": -45,  "pitch": 15,  "speed": 300}),
    (BEAT_MS * 32,      LED,  {"r": 168, "g": 0,   "b": 168}),
    (BEAT_MS * 34,      HEAD, {"yaw": 45,   "pitch": 15,  "speed": 300}),
    (BEAT_MS * 34,      LED,  {"r": 0,   "g": 168, "b": 168}),
    (BEAT_MS * 36,      HEAD, {"yaw": -55,  "pitch": 75,  "speed": 250}),
    (BEAT_MS * 36,      LED,  {"r": 168, "g": 100, "b": 0}),
    (BEAT_MS * 38,      HEAD, {"yaw": 55,   "pitch": 75,  "speed": 250}),
    (BEAT_MS * 38,      LED,  {"r": 100, "g": 0,   "b": 168}),

    # --- Second "Heeey Macarena!" wiggle ---
    (BEAT_MS * 40,      HEAD, {"yaw": -70,  "pitch": 30,  "speed": 500}),
    (BEAT_MS * 40,      LED,  {"r": 168, "g": 0,   "b": 168}),
    (BEAT_MS * 40 + 290, HEAD, {"yaw": 70,  "pitch": 30,  "speed": 500}),
    (BEAT_MS * 41,      HEAD, {"yaw": -70,  "pitch": 30,  "speed": 500}),
    (BEAT_MS * 41 + 290, HEAD, {"yaw": 70,  "pitch": 30,  "speed": 500}),
    (BEAT_MS * 42,      HEAD, {"yaw": -70,  "pitch": 30,  "speed": 500}),
    (BEAT_MS * 42 + 290, HEAD, {"yaw": 70,  "pitch": 30,  "speed": 500}),

    # --- Final jump turn + bow ---
    (BEAT_MS * 43,      HEAD, {"yaw": -128, "pitch": 45,  "speed": 800}),
    (BEAT_MS * 43,      LED,  {"r": 168, "g": 168, "b": 168}),
    (BEAT_MS * 44,      HEAD, {"yaw": 128,  "pitch": 45,  "speed": 800}),
    (BEAT_MS * 45,      HEAD, {"yaw": 0,    "pitch": 80,  "speed": 300}),  # bow
    (BEAT_MS * 45,      LED,  {"r": 168, "g": 0,   "b": 168}),

    # --- Return to neutral ---
    (BEAT_MS * 47,      HEAD, {"yaw": 0,    "pitch": 0,   "speed": 200}),
    (BEAT_MS * 47,      LED,  {"r": 0,   "g": 0,   "b": 0}),
]

DANCE_REGISTRY: dict[str, dict] = {
    "macarena": {
        "timeline": MACARENA_TIMELINE,
        "audio_file": "config/assets/songs/macarena.wav",
        "intro": "Ooh yeah! It's Macarena time! Let's dance!",
        "outro": "Heeey Macarena! Ayyy! That was so much fun!",
        "duration_ms": BEAT_MS * 48,
    },
}

DEFAULT_DANCE = "macarena"


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
