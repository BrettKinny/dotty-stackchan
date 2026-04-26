import os
import re
import time
import json
import asyncio
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.utils.util import audio_to_data
from core.handle.abortHandle import handleAbortMessage
from core.handle.intentHandler import handle_user_intent
from core.utils.output_counter import check_device_output_limit
from core.handle.sendAudioHandle import send_stt_message, SentenceType

TAG = __name__

VISION_BRIDGE_URL = os.environ.get("VISION_BRIDGE_URL", "")
MIN_UTTERANCE_CHARS = int(os.environ.get("MIN_UTTERANCE_CHARS", "2"))
# Smart-mode persistent LED pixel: which ring index holds the purple
# while listen/think/talk colours animate the rest of the ring. The
# firmware MCP `self.robot.set_led_multi` tool (firmware ≥ 32163bd)
# bypasses the colour-animation tick, so we re-assert this pixel after
# every full-ring colour change.
SMART_MODE_LED_INDEX = int(os.environ.get("SMART_MODE_LED_INDEX", "0"))
# Right-ring pixel held in soft pink whenever kid mode is OFF, as a sustained
# "guards are down" indicator visible to anyone glancing at the robot. Mirrors
# the Smart Mode persistent-pixel pattern but on the opposite ring (0-5 left,
# 6-11 right) so the two indicators never collide.
KID_MODE_OFF_LED_INDEX = int(os.environ.get("KID_MODE_OFF_LED_INDEX", "6"))
KID_MODE_OFF_RGB = (168, 80, 100)
_KID_MODE_STATE_FILE = os.environ.get(
    "DOTTY_KID_MODE_STATE", "/root/zeroclaw-bridge/state/kid-mode",
)


def _read_kid_mode_state() -> bool:
    """Mirror of bridge.py's _read_kid_mode but importable from this module
    without circular-import gymnastics. Single source of truth = the same
    state file the portal writes."""
    try:
        with open(_KID_MODE_STATE_FILE, "r") as f:
            v = f.read().strip().lower()
        if v in ("true", "1", "yes"):
            return True
        if v in ("false", "0", "no"):
            return False
    except OSError:
        pass
    return os.environ.get("DOTTY_KID_MODE", "true").lower() in ("1", "true", "yes")


_LETTERS_RE = re.compile(r'[a-zA-Z一-鿿぀-ゟ゠-ヿ]')

_ASR_CORRECTIONS: dict[str, str] = {
    "doty": "Dotty",
    "dottie": "Dotty",
    "dotie": "Dotty",
    "dotti": "Dotty",
    "dody": "Dotty",
    "daughty": "Dotty",
    "haughty": "Dotty",
    "naughty": "Dotty",
    "hardy": "Dotty",
    "darty": "Dotty",
    "foto": "photo",
    "pitcher": "picture",
    "pikture": "picture",
    "storey": "story",
    "danse": "dance",
    "mornin": "morning",
    "nite": "night",
    "singah": "sing a",
}
_ASR_CORRECTION_RE = re.compile(
    r'\b(' + '|'.join(re.escape(k) for k in _ASR_CORRECTIONS) + r')\b',
    re.IGNORECASE,
)


# ---------- Fuzzy phrase corrections ----------
# Each entry: (canonical_phrase, minimum_similarity_ratio)
# The canonical phrase is what we want. If the ASR text (or a window of it)
# fuzzy-matches above the threshold, we substitute the canonical form.
# Threshold 0.7 is conservative — avoids false positives on short utterances.
_PHRASE_CORRECTIONS: list[tuple[str, float]] = [
    # Vision triggers
    ("take a photo", 0.7),
    ("take a picture", 0.7),
    ("take a photo of me", 0.7),
    ("take a picture of me", 0.7),
    # Smart-mode triggers (future)
    ("smart mode", 0.75),
    ("think harder", 0.75),
    ("big brain", 0.75),
    # Common kid requests
    ("tell me a story", 0.7),
    ("sing a song", 0.7),
    ("sing the macarena", 0.7),
    ("dance", 0.8),
    ("do the macarena", 0.7),
    # Song-name fuzzy hits
    ("play tetris", 0.7),
    ("hall of the mountain king", 0.7),
    ("star wars", 0.75),
    ("pirates of the caribbean", 0.7),
    ("super mario", 0.7),
    ("play music", 0.75),
    # Identity questions
    ("what's your name", 0.7),
    ("what is your name", 0.7),
    ("who are you", 0.75),
    # Greetings
    ("good morning", 0.7),
    ("good night", 0.7),
]


def _apply_phrase_corrections(text: str) -> str:
    """Fuzzy-match ASR text against known phrases and substitute if close enough.

    Uses a sliding window: for each canonical phrase of N words, we check every
    contiguous N-word window in the ASR text. If the best window exceeds the
    similarity threshold, we replace that window with the canonical phrase.

    Only the single best match (highest ratio) is applied per call to avoid
    cascading replacements on short utterances.
    """
    lower = text.lower().strip()
    words = lower.split()
    if len(words) < 2:
        return text  # too short to fuzzy-match phrases

    best_ratio = 0.0
    best_phrase = ""
    best_start = 0
    best_length = 0

    for canonical, threshold in _PHRASE_CORRECTIONS:
        canon_words = canonical.split()
        window_size = len(canon_words)
        if window_size > len(words):
            continue

        for i in range(len(words) - window_size + 1):
            window = " ".join(words[i : i + window_size])
            ratio = SequenceMatcher(None, window, canonical).ratio()
            if ratio >= threshold and ratio > best_ratio:
                best_ratio = ratio
                best_phrase = canonical
                best_start = i
                best_length = window_size

    if best_ratio > 0:
        # Rebuild using original-case words outside the match window,
        # substituting the canonical phrase for the matched span.
        original_words = text.split()
        # Map word indices from lower-cased split back to original split.
        # They should align since we only called .lower() without changing
        # word boundaries, but guard against edge cases.
        if len(original_words) >= best_start + best_length:
            before = " ".join(original_words[:best_start])
            after = " ".join(original_words[best_start + best_length :])
            parts = [p for p in (before, best_phrase, after) if p]
            return " ".join(parts)

    return text


def _is_noise(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) < MIN_UTTERANCE_CHARS:
        return True
    return len(_LETTERS_RE.findall(stripped)) < MIN_UTTERANCE_CHARS


def _apply_asr_corrections(text: str) -> str:
    def _repl(m):
        return _ASR_CORRECTIONS.get(m.group(0).lower(), m.group(0))
    return _ASR_CORRECTION_RE.sub(_repl, text)
VISION_PHRASES = (
    "look at", "what do you see", "what is this", "what's this",
    "take a photo", "take a picture", "can you see", "what's in front",
    "what am i holding", "what's that", "what is that", "describe what",
    "what color is", "what colour is", "how many", "do you see",
)

_SMART_MODE_PHRASES = (
    "smart mode", "think harder", "big brain",
)


def _is_smart_mode_request(text: str) -> bool:
    lower = text.lower().strip()
    return any(phrase in lower for phrase in _SMART_MODE_PHRASES)


def _strip_smart_trigger(text: str) -> str:
    lower = text.lower()
    for phrase in sorted(_SMART_MODE_PHRASES, key=len, reverse=True):
        idx = lower.find(phrase)
        if idx != -1:
            remaining = text[:idx] + text[idx + len(phrase):]
            return re.sub(r'^[\s,.\-!?]+|[\s,.\-!?]+$', '', remaining)
    return ""


async def _send_led_color(conn: "ConnectionHandler", r: int, g: int, b: int) -> None:
    try:
        msg = json.dumps({
            "session_id": conn.session_id,
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "self.robot.set_led_color",
                    "arguments": {"r": r, "g": g, "b": b},
                },
                "id": int(time.time() * 1000) % 0x7FFFFFFF,
            },
        })
        await conn.websocket.send(msg)
    except Exception:
        pass
    # Smart-mode persistent pixel: the firmware's set_led_color repaints
    # every pixel in the ring (including the smart-mode marker), so we
    # re-paint our marker pixel afterward. Only fires when the smart-mode
    # flag is active — a no-op the rest of the time.
    if getattr(conn, "smart_mode_active", False):
        await _send_led_multi(conn, SMART_MODE_LED_INDEX, 168, 0, 168)
    # Kid-mode-OFF persistent pixel: same reassert pattern, opposite ring.
    # Cache the flag on the connection so the state file is hit at most once
    # per connection — subsequent paints just check the boolean.
    if not hasattr(conn, "kid_mode_off"):
        conn.kid_mode_off = not _read_kid_mode_state()
    if conn.kid_mode_off:
        await _send_led_multi(
            conn, KID_MODE_OFF_LED_INDEX, *KID_MODE_OFF_RGB,
        )


async def _send_led_multi(
    conn: "ConnectionHandler", index: int, r: int, g: int, b: int,
) -> None:
    """Set a SINGLE pixel on the neon-ring without disturbing the rest.

    Wraps the firmware MCP `self.robot.set_led_multi` tool (firmware
    ≥ 32163bd). Index 0-5 = LeftNeonLight, 6-11 = RightNeonLight. This
    bypasses the firmware's colour-animation tick, so callers that need
    the pixel to PERSIST across a subsequent set_led_color must re-call
    this after each full-ring update. (`_send_led_color` does that
    automatically when `conn.smart_mode_active` is True.)

    Defensive: try/except guarded so an old firmware (without this MCP
    tool) degrades to the existing single-flash behaviour rather than
    crashing the LLM flow. Logs a warning on first failure per session
    so we don't noisily spam.
    """
    try:
        msg = json.dumps({
            "session_id": conn.session_id,
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "self.robot.set_led_multi",
                    "arguments": {"index": index, "r": r, "g": g, "b": b},
                },
                "id": int(time.time() * 1000) % 0x7FFFFFFF,
            },
        })
        await conn.websocket.send(msg)
    except Exception as exc:
        # The firmware may simply not support set_led_multi yet (old
        # build); log warn-once per connection so we know without
        # spamming on every re-assert.
        if not getattr(conn, "_led_multi_warned", False):
            try:
                conn.logger.bind(tag=TAG).warning(
                    f"set_led_multi failed (firmware may pre-date 32163bd): {exc}"
                )
            except Exception:
                pass
            conn._led_multi_warned = True


async def _send_head_angles(conn: "ConnectionHandler", yaw: int, pitch: int, speed: int = 150) -> None:
    try:
        msg = json.dumps({
            "session_id": conn.session_id,
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "self.robot.set_head_angles",
                    "arguments": {"yaw": yaw, "pitch": pitch, "speed": speed},
                },
                "id": int(time.time() * 1000) % 0x7FFFFFFF,
            },
        })
        await conn.websocket.send(msg)
    except Exception:
        pass


def _is_vision_request(text: str) -> bool:
    lower = text.lower().strip()
    return any(phrase in lower for phrase in VISION_PHRASES)


async def _handle_vision(conn: "ConnectionHandler", text: str) -> str | None:
    if not VISION_BRIDGE_URL:
        conn.logger.bind(tag=TAG).warning("VISION_BRIDGE_URL not set, skipping vision")
        return None

    device_id = conn.headers.get("device-id", "unknown")

    mcp_call = json.dumps({
        "session_id": conn.session_id,
        "type": "mcp",
        "payload": {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "self.camera.take_photo",
                "arguments": {"question": text},
            },
            "id": int(time.time() * 1000) % 0x7FFFFFFF,
        },
    })
    await conn.websocket.send(mcp_call)
    conn.logger.bind(tag=TAG).info(f"Vision: sent take_photo MCP call, device={device_id}")

    try:
        import requests
        url = f"{VISION_BRIDGE_URL.rstrip('/')}/api/vision/latest/{device_id}"
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: requests.get(url, timeout=20),
        )
        if resp.status_code == 200:
            description = resp.json().get("description", "")
            conn.logger.bind(tag=TAG).info(f"Vision: got description len={len(description)}")
            return description
    except Exception as exc:
        conn.logger.bind(tag=TAG).error(f"Vision: bridge poll failed: {exc}")

    return None


# ---------- Description-based identity (no storage) ----------
# Capture a one-line natural-language description of who is currently
# in front of the camera, cache it on `conn`, surface it to the bridge
# via a `[ROOM_VIEW]\n<desc>\n` prefix on the next user turn so the
# bridge can render `[Room view] ...` into the prompt. No biometric
# data is stored anywhere — the cache is per-connection and cleared on
# face_lost (see textMessageHandlerRegistry.EventTextMessageHandler).
#
# Capture is fire-and-forget: triggered by the perception relay on
# face_detected, runs in the background, beats the user's first voice
# turn most of the time. If a turn arrives before capture completes,
# that turn just goes out without a [Room view] line — no extra
# latency on the voice path.

# Sentinel placed in the multipart `question` field by the
# `take_photo` MCP call to opt in to the bridge's roster-aware
# room_view path. The actual prompt + household roster live on the
# bridge (see `bridge.py:_build_room_view_question` +
# `_ROOM_VIEW_SENTINEL`). The xiaozhi side stays roster-agnostic.
# Versioning is in the sentinel itself for future format revs.
_ROOM_VIEW_VLM_QUESTION = "__ROOM_VIEW_V1__"

# Sentinel reply the bridge emits when the frame is empty / no person
# is visible. Treated as "no description" so we don't stuff a useless
# [Room view] line into the next voice turn. Mirrors the bridge-side
# constant of the same name.
_ROOM_VIEW_NO_PERSON = "no one in view"


async def _capture_room_description_async(
    conn: "ConnectionHandler",
) -> None:
    """Background-capture the current room view description.

    Called from the perception relay on `face_detected` (when no fresh
    description is cached). Sends a `take_photo` MCP call with a
    description-focused VLM question, long-polls the bridge for the
    result, and caches it on `conn._room_description`. Best-effort —
    a failure leaves the cache empty and the next voice turn proceeds
    without `[Room view]`.
    """
    if not VISION_BRIDGE_URL:
        return
    device_id = "unknown"
    try:
        device_id = conn.headers.get("device-id", "unknown")
    except Exception:
        pass

    # Mark in-flight so concurrent face_detected events don't trigger
    # a second capture on top of an active one. Cleared in finally.
    if getattr(conn, "_room_description_in_flight", False):
        return
    conn._room_description_in_flight = True
    try:
        mcp_call = json.dumps({
            "session_id": conn.session_id,
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "self.camera.take_photo",
                    "arguments": {"question": _ROOM_VIEW_VLM_QUESTION},
                },
                "id": int(time.time() * 1000) % 0x7FFFFFFF,
            },
        })
        await conn.websocket.send(mcp_call)
        conn.logger.bind(tag=TAG).info(
            f"room_view: capture started device={device_id}"
        )
        import requests
        url = f"{VISION_BRIDGE_URL.rstrip('/')}/api/vision/latest/{device_id}"
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: requests.get(url, timeout=20),
        )
        if resp.status_code != 200:
            conn.logger.bind(tag=TAG).warning(
                f"room_view: bridge returned {resp.status_code}"
            )
            return
        body = resp.json() or {}
        description = (body.get("description") or "").strip()
        # `room_match_person_id` is added by the bridge's room_view
        # path; v1 description-only callers won't see it. Empty / None /
        # "unknown" all reduce to "no roster match this turn".
        match_raw = body.get("room_match_person_id")
        match = (match_raw or "").strip().lower() or None
        if match == "unknown":
            match = None
        if not description:
            return
        # Treat the "no one in view" sentinel as a miss so we don't
        # stuff the prompt with a useless line.
        if _ROOM_VIEW_NO_PERSON in description.lower():
            conn._room_description = None
            conn._room_match_person_id = None
            conn.logger.bind(tag=TAG).info(
                "room_view: VLM reports no person; cache cleared"
            )
            return
        conn._room_description = description
        conn._room_match_person_id = match
        conn._room_description_ts = time.time()
        conn.logger.bind(tag=TAG).info(
            f"room_view: cached len={len(description)} "
            f"match={match or '-'} "
            f"preview={description[:60]!r}"
        )
    except Exception as exc:
        conn.logger.bind(tag=TAG).warning(
            f"room_view: capture failed: {exc}"
        )
    finally:
        conn._room_description_in_flight = False


def _with_room_view_marker(
    conn: "ConnectionHandler", text: str,
) -> str:
    """Prepend the [ROOM_VIEW] marker to `text` if a fresh room
    description is cached on `conn`.

    Two shapes (zeroclaw `_payload` accepts both):

      v1 — description only:
          [ROOM_VIEW]\\n<desc>\\n<text>
      v2 — description + roster match (may be empty):
          [ROOM_VIEW]\\n<desc>\\n<person_id_or_blank>\\n<text>

    We always emit v2 when a room description is cached. The
    person_id slot is the matched roster id (e.g. `hudson`) or empty
    string for "no roster match" — both are valid v2 signals. The
    zeroclaw provider strips the marker and pushes both fields into
    request metadata; the bridge's SpeakerResolver consumes the
    person_id as a vote (see `_resolve_speaker_for_request`)."""
    desc = getattr(conn, "_room_description", None)
    if not desc:
        return text
    match = getattr(conn, "_room_match_person_id", None) or ""
    return f"[ROOM_VIEW]\n{desc}\n{match}\n{text}"


def _submit_chat(conn: "ConnectionHandler", text: str) -> None:
    """Submit `text` to the LLM via the connection's executor, with
    the room-view marker prepended automatically when a fresh
    description is cached. Single chokepoint for description
    propagation — every voice path goes through here.
    """
    conn.executor.submit(conn.chat, _with_room_view_marker(conn, text))


# ---------- Face recognition (Layer 4) ----------
# Voice-driven enrollment, recognition, and management of the family
# face DB. Compute is server-side on the bridge (see
# bridge/face_recognizer.py); these handlers just dispatch the MCP
# tool call and feed an ack prompt to the LLM. The MCP tools live on
# the firmware (`self.camera.face_enroll`, `face_recognize`, etc.) —
# until they ship, the dispatch is a no-op on the device side, but
# the voice ack still fires so the user can validate phrase matching.

# Per-household ASR fixes for proper-noun mishearings. Customise this
# to your household — without entries here, the .title()-based fallback
# still works (e.g. "brad" → "Brad"), but a known mishearing like
# "brett" being heard as "brad" would land in the DB as "Brad", not
# "Brett". The right-hand side is the canonical spelling.
_FACE_NAME_ASR_FIXES: dict[str, str] = {
    # "brad": "Brett",        # uncomment + tune for your household
    # "suzanne": "Susannah",
    # "hutson": "Hudson",
    # "annabel": "Annabelle",
}

# Patterns must be tested in order — more specific first.
_FACE_ENROLL_PATTERNS = [
    # "take a photo of me, this is X" / "take my photo, I'm X"
    re.compile(
        r"(?:take a photo of me|take my photo|take a picture of me|take a picture)"
        r"[\s,.\-!?]*"
        r"(?:this is|i'?m|i am|it'?s|its|name'?s|name is)"
        r"\s+([a-zA-Z][a-zA-Z\-']{1,31})",
        re.IGNORECASE,
    ),
    # "I'm X, take a photo of me"
    re.compile(
        r"(?:this is|i'?m|i am|it'?s|its|name'?s|name is)"
        r"\s+([a-zA-Z][a-zA-Z\-']{1,31})"
        r"[\s,.\-!?]*"
        r"(?:take a photo of me|take my photo|take a picture)",
        re.IGNORECASE,
    ),
    # "register/learn/remember my face as X"
    re.compile(
        r"(?:register|learn|remember|save|memorize|memorise)"
        r"\s+my\s+face\s+(?:as\s+)?([a-zA-Z][a-zA-Z\-']{1,31})",
        re.IGNORECASE,
    ),
]

_FACE_FORGET_NAMED_RE = re.compile(
    r"(?:forget|delete|remove|erase)"
    r"\s+([a-zA-Z][a-zA-Z\-']{1,31})(?:'s)?\s*(?:face)?",
    re.IGNORECASE,
)
_FACE_FORGET_ALL_PHRASES = (
    "forget everyone", "forget all faces", "forget all the faces",
    "forget every face",
)
_FACE_FORGET_ME_PHRASES = (
    "forget my face", "forget about me",
)

_FACE_LIST_PHRASES = (
    "who do you know", "list faces", "who have you met",
    "whose faces do you know", "do you know my face",
    "what faces do you know", "tell me whose faces",
)

# Lowercased denylist — pronouns / fillers / common ASR garbage that
# regex captures sometimes pull in despite our anchors.
_FACE_NAME_DENYLIST = {
    "me", "myself", "i", "the", "you", "him", "her", "them", "us",
    "we", "they", "everyone", "anyone", "someone", "nobody",
    "a", "an", "going", "doing", "talking", "yes", "no", "ok",
    "okay", "now", "here", "there",
}


def _normalize_face_name(raw: str) -> str | None:
    cleaned = (raw or "").strip().lower()
    if cleaned in _FACE_NAME_DENYLIST:
        return None
    if len(cleaned) < 2:
        return None
    if cleaned in _FACE_NAME_ASR_FIXES:
        return _FACE_NAME_ASR_FIXES[cleaned]
    return cleaned.title()


def _detect_face_enroll(text: str) -> str | None:
    for pat in _FACE_ENROLL_PATTERNS:
        m = pat.search(text)
        if m:
            name = _normalize_face_name(m.group(1))
            if name:
                return name
    return None


def _is_face_list_request(text: str) -> bool:
    lower = text.lower().strip()
    return any(phrase in lower for phrase in _FACE_LIST_PHRASES)


def _detect_face_forget(text: str) -> str | None:
    """Return the canonical name to forget, ``"*"`` for forget-all,
    ``"self"`` for forget-me (currently unsupported), or ``None``."""
    lower = text.lower().strip()
    if any(p in lower for p in _FACE_FORGET_ALL_PHRASES):
        return "*"
    if any(p in lower for p in _FACE_FORGET_ME_PHRASES):
        return "self"
    m = _FACE_FORGET_NAMED_RE.search(text)
    if m:
        return _normalize_face_name(m.group(1))
    return None


async def _send_face_mcp(
    conn: "ConnectionHandler", tool: str, args: dict,
) -> None:
    """Send an MCP `self.camera.<tool>` call. Best-effort: a missing
    firmware-side handler is silent (the response simply never comes
    back), so the verbal ack speaks even on a stale firmware."""
    try:
        msg = json.dumps({
            "session_id": conn.session_id,
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": f"self.camera.{tool}",
                    "arguments": args,
                },
                "id": int(time.time() * 1000) % 0x7FFFFFFF,
            },
        })
        await conn.websocket.send(msg)
    except Exception:
        conn.logger.bind(tag=TAG).warning(
            f"face MCP send failed (tool={tool})", exc_info=True,
        )


async def _handle_face_enroll(
    conn: "ConnectionHandler", text: str,
) -> bool:
    name = _detect_face_enroll(text)
    if not name:
        return False
    conn.logger.bind(tag=TAG).info(f"face_enroll intent: name={name}")
    await _send_face_mcp(conn, "face_enroll", {"name": name})
    # Optimistic verbal confirmation. The bridge logs success/failure
    # of the embedding compute; if enrollment fails (no face detected,
    # capacity reached) the user sees no error today — surfaced via a
    # follow-up endpoint poll once Phase D ships the status feedback.
    conn.executor.submit(
        conn.chat,
        f"[FACE_ENROLL_TRIGGERED] The user said \"{text}\" and you "
        f"are about to capture and remember their face as \"{name}\". "
        f"Say only: Got it — hold still for a second... I'll remember "
        f"you, {name}!",
    )
    return True


async def _handle_face_list(conn: "ConnectionHandler") -> bool:
    if not VISION_BRIDGE_URL:
        return False
    try:
        import requests
        url = f"{VISION_BRIDGE_URL.rstrip('/')}/api/face/list"
        resp = await asyncio.get_event_loop().run_in_executor(
            None, lambda: requests.get(url, timeout=5),
        )
        if resp.status_code != 200:
            conn.logger.bind(tag=TAG).warning(
                f"face_list bridge returned {resp.status_code}"
            )
            return False
        data = resp.json()
        if not data.get("ok"):
            return False
        names = data.get("names") or []
    except Exception as exc:
        conn.logger.bind(tag=TAG).error(f"face_list failed: {exc}")
        return False
    if not names:
        ack_prompt = (
            "[FACE_LIST_EMPTY] The user asked who you know but no "
            "faces are enrolled yet. Say only: I don't know anyone's "
            "face yet — say 'this is your name, take a photo of me' "
            "to teach me!"
        )
    elif len(names) == 1:
        ack_prompt = (
            f"[FACE_LIST_ONE] You know one face: {names[0]}. "
            f"Say only: I know {names[0]}!"
        )
    else:
        joined = ", ".join(names[:-1]) + f" and {names[-1]}"
        ack_prompt = (
            f"[FACE_LIST_MANY] You know these faces: {joined}. "
            f"Say only: I know {joined}!"
        )
    _submit_chat(conn, ack_prompt)
    return True


async def _handle_face_forget(
    conn: "ConnectionHandler", text: str,
) -> bool:
    target = _detect_face_forget(text)
    if not target:
        return False
    if target == "self":
        # No voice-ID today — we cannot resolve "my face" to a name.
        conn.executor.submit(
            conn.chat,
            "[FACE_FORGET_AMBIGUOUS] The user asked to forget their "
            "face but you cannot identify them from voice alone. "
            "Say only: I'm not sure which face is yours — try "
            "'forget' followed by your name.",
        )
        return True
    await _send_face_mcp(conn, "face_forget", {"name": target})
    if target == "*":
        ack = (
            "[FACE_FORGET_ALL] You just wiped every enrolled face. "
            "Say only: Okay, I've forgotten everyone's faces."
        )
    else:
        ack = (
            f"[FACE_FORGET_NAMED] You just forgot {target}'s face. "
            f"Say only: Okay, I've forgotten {target}'s face."
        )
    _submit_chat(conn, ack)
    return True


# ---------- Dance / singing mode ----------
# Singing and dancing are unified — both route to _handle_dance(), which plays
# the choreography and (if a matching audio_file exists in DANCE_REGISTRY)
# injects pre-rendered singing audio into the TTS queue.

_DANCE_PHRASES = (
    "dance", "do a dance", "let's dance", "can you dance",
    "dance for me", "dance time", "dance mode",
    # Macarena
    "do the macarena", "macarena",
    # Singing triggers — same handler, audio file decides if it sings.
    "sing a song", "sing the macarena", "sing macarena",
    "can you sing", "sing for me", "sing something",
    "let's sing",
    # Other songs in the catalog
    "play tetris", "tetris music", "play the tetris",
    "mountain king", "hall of the mountain king",
    "star wars", "play star wars", "star wars music",
    "pirates", "pirate music", "pirates of the caribbean",
    "play mario", "mario music", "super mario",
    "play music", "play a song", "music time",
)

# Short "sing" needs word-boundary matching to avoid false positives on
# words like "single" or "singapore".
_SING_WORD_RE = re.compile(r"\bsing\b", re.IGNORECASE)


def _is_dance_request(text: str) -> bool:
    lower = text.lower().strip()
    if any(phrase in lower for phrase in _DANCE_PHRASES):
        return True
    return bool(_SING_WORD_RE.search(lower))


# Map spoken-form aliases → registry key. First-match wins, longest first
# so "mountain king" beats "king".
_DANCE_ALIASES: tuple[tuple[str, str], ...] = (
    ("hall of the mountain king", "mountain_king"),
    ("mountain king", "mountain_king"),
    ("pirates of the caribbean", "pirates"),
    ("super mario", "mario"),
    ("star wars", "star_wars"),
    ("macarena", "macarena"),
    ("tetris", "tetris"),
    ("pirate", "pirates"),
    ("mario", "mario"),
)


def _detect_dance_name(text: str) -> str:
    from core.handle.dances import DANCE_REGISTRY, DEFAULT_DANCE
    import random
    lower = text.lower()
    # Direct registry-key hit (handles "macarena", "tetris" already).
    for name in DANCE_REGISTRY:
        if name in lower:
            return name
    # Aliased names (multi-word, underscored, etc.)
    for alias, name in _DANCE_ALIASES:
        if alias in lower:
            return name
    # Generic "play music" / "play a song" / "music time" → random pick.
    if any(p in lower for p in ("play music", "play a song", "music time", "play song")):
        return random.choice(list(DANCE_REGISTRY.keys()))
    return DEFAULT_DANCE


async def _handle_dance(conn: "ConnectionHandler", dance_name: str) -> None:
    from core.handle.dances import DANCE_REGISTRY, execute_choreography, resolve_timeline

    dance = DANCE_REGISTRY.get(dance_name)
    if not dance:
        return

    conn.logger.bind(tag=TAG).info(f"Dance mode: {dance_name}")

    await conn.websocket.send(json.dumps({
        "type": "llm",
        "text": "\U0001f606",
        "emotion": "laughing",
        "session_id": conn.session_id,
    }))
    await _send_led_color(conn, 168, 0, 168)

    audio_file = dance.get("audio_file")
    has_audio = bool(audio_file) and os.path.exists(audio_file)
    opus_packets = None
    if has_audio:
        try:
            ext = os.path.splitext(audio_file)[1].lower()
            if ext in (".mid", ".midi"):
                opus_packets = await _encode_midi_to_opus(
                    audio_file,
                    conn.sample_rate,
                    target_tempo_bpm=dance.get("audio_tempo_bpm"),
                    max_duration_ms=dance.get("duration_ms"),
                )
            else:
                opus_packets = await _encode_song_to_opus(audio_file, conn.sample_rate)
        except Exception as exc:
            conn.logger.bind(tag=TAG).error(f"Dance mode: audio decode failed: {exc}")
            has_audio = False

    # Only delay choreography for audio sync when we actually queued audio.
    from core.handle.dances import AUDIO_LATENCY_OFFSET_MS
    audio_offset = AUDIO_LATENCY_OFFSET_MS if has_audio else 0

    timeline = resolve_timeline(dance)
    dance_task = asyncio.create_task(
        execute_choreography(
            conn, timeline, _send_head_angles, _send_led_color,
            audio_latency_offset_ms=audio_offset,
        )
    )
    conn._dance_task = dance_task

    def _on_dance_done(task):
        async def _cleanup():
            if task.cancelled():
                await _send_head_angles(conn, 0, 0, 200)
            await _send_led_color(conn, 0, 0, 0)
        asyncio.ensure_future(_cleanup())

    dance_task.add_done_callback(_on_dance_done)

    if has_audio and opus_packets is not None:
        # Direct send: bypass tts_audio_queue and the rate controller. The
        # consumer's future.result(timeout=tts_timeout) trips on a 28-second
        # clip (tts_timeout defaults to 15s), and the upstream audio_to_data
        # hardcodes 16kHz Opus regardless of the negotiated output rate. Pace
        # by sleeping 60ms between packets, matching the device's
        # frame_duration handshake parameter.
        conn.client_abort = False
        conn.client_is_speaking = True
        asyncio.create_task(_stream_singing(conn, opus_packets))
        conn.logger.bind(tag=TAG).info(
            f"Dance mode: streaming singing audio {audio_file} "
            f"({len(opus_packets)} packets @ {conn.sample_rate}Hz)"
        )
    else:
        conn.executor.submit(
            conn.chat,
            f"[DANCE:{dance_name}] You're about to dance the {dance_name.title()}! "
            f"Say a SHORT excited one-liner intro (under 15 words). "
            f"Example: '\U0001f606 {dance['intro']}'",
        )


_MIDI_RENDER_CACHE: dict[tuple, list[bytes]] = {}
FLUID_SOUNDFONT = "/usr/share/sounds/sf2/FluidR3_GM.sf2"


async def _encode_midi_to_opus(
    midi_path: str,
    target_rate: int,
    target_tempo_bpm: float | None = None,
    max_duration_ms: int | None = None,
) -> list[bytes]:
    """Render a MIDI file to Opus 60ms frames via fluidsynth.

    Cached in-memory by (midi_path, mtime, target_rate, tempo, duration) so a
    repeat dance is instant. Optionally rewrites the MIDI's tempo events
    (`target_tempo_bpm`) so the music matches the choreography BPM.
    """
    import os as _os
    mtime = _os.path.getmtime(midi_path)
    cache_key = (midi_path, mtime, target_rate, target_tempo_bpm, max_duration_ms)
    cached = _MIDI_RENDER_CACHE.get(cache_key)
    if cached is not None:
        return cached

    def _render():
        import subprocess
        import tempfile
        import wave as _wave
        import numpy as np
        from scipy import signal as _scipy_signal
        from math import gcd
        from core.utils import opus_encoder_utils

        with tempfile.TemporaryDirectory() as tmpdir:
            mid_to_render = midi_path
            if target_tempo_bpm is not None:
                import mido
                src = mido.MidiFile(midi_path)
                new_tempo = mido.bpm2tempo(target_tempo_bpm)
                for track in src.tracks:
                    has_tempo = False
                    for msg in track:
                        if msg.type == "set_tempo":
                            msg.tempo = new_tempo
                            has_tempo = True
                    if not has_tempo and track is src.tracks[0]:
                        track.insert(0, mido.MetaMessage("set_tempo", tempo=new_tempo, time=0))
                mid_to_render = f"{tmpdir}/retempo.mid"
                src.save(mid_to_render)

            wav_path = f"{tmpdir}/render.wav"
            subprocess.run(
                [
                    "fluidsynth", "-ni",
                    "-r", str(target_rate),
                    "-g", "0.7",
                    "-F", wav_path,
                    FLUID_SOUNDFONT,
                    mid_to_render,
                ],
                check=True, capture_output=True, timeout=60,
            )

            with _wave.open(wav_path, "rb") as wf:
                src_rate = wf.getframerate()
                channels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                raw = wf.readframes(wf.getnframes())

            if sampwidth != 2:
                raise RuntimeError(f"fluidsynth produced sampwidth={sampwidth}, expected 2")
            pcm = np.frombuffer(raw, dtype=np.int16)
            if channels == 2:
                pcm = pcm.reshape(-1, 2).mean(axis=1).astype(np.int16)

            if src_rate != target_rate:
                g = gcd(src_rate, target_rate)
                up = target_rate // g
                down = src_rate // g
                pcm = _scipy_signal.resample_poly(pcm.astype(np.float32), up, down)
                pcm = np.clip(pcm, -32768, 32767).astype(np.int16)

            if max_duration_ms is not None:
                max_samples = int(max_duration_ms / 1000.0 * target_rate)
                if len(pcm) > max_samples:
                    pcm = pcm[:max_samples]
                elif len(pcm) < max_samples:
                    pcm = np.concatenate([pcm, np.zeros(max_samples - len(pcm), dtype=np.int16)])

            encoder = opus_encoder_utils.OpusEncoderUtils(
                sample_rate=target_rate, channels=1, frame_size_ms=60
            )
            frame_samples = int(target_rate * 60 / 1000)
            frame_bytes = frame_samples * 2
            pcm_bytes = pcm.tobytes()
            packets: list[bytes] = []

            def _collect(opus_bytes):
                if opus_bytes:
                    packets.append(opus_bytes)

            for i in range(0, len(pcm_bytes), frame_bytes):
                chunk = pcm_bytes[i : i + frame_bytes]
                if len(chunk) < frame_bytes:
                    chunk += b"\x00" * (frame_bytes - len(chunk))
                encoder.encode_pcm_to_opus_stream(
                    chunk, end_of_stream=(i + frame_bytes >= len(pcm_bytes)),
                    callback=_collect,
                )
            encoder.close()
            return packets

    packets = await asyncio.get_running_loop().run_in_executor(None, _render)
    _MIDI_RENDER_CACHE[cache_key] = packets
    return packets


async def _encode_song_to_opus(wav_path: str, target_rate: int) -> list[bytes]:
    """Read a WAV file and return Opus-encoded 60ms frames at target_rate.

    The upstream audio_to_data() hardcodes 16kHz, but the device negotiates
    a different output rate via the welcome handshake (24kHz on this StackChan).
    Decoding 16kHz Opus when the device expects 24kHz silently produces no
    audible output. This helper resamples to target_rate and encodes Opus at
    the same rate, matching what Piper's TTS provider does.
    """
    from core.utils import opus_encoder_utils
    import numpy as np
    from scipy import signal as scipy_signal
    from math import gcd
    from pydub import AudioSegment

    def _decode_and_encode():
        audio = AudioSegment.from_file(
            wav_path, format="wav", parameters=["-nostdin"]
        )
        audio = audio.set_channels(1).set_sample_width(2)
        src_rate = audio.frame_rate
        pcm = np.frombuffer(audio.raw_data, dtype=np.int16)

        if src_rate != target_rate:
            g = gcd(src_rate, target_rate)
            up = target_rate // g
            down = src_rate // g
            resampled = scipy_signal.resample_poly(pcm, up, down)
            pcm = np.clip(resampled, -32768, 32767).astype(np.int16)

        encoder = opus_encoder_utils.OpusEncoderUtils(
            sample_rate=target_rate, channels=1, frame_size_ms=60
        )
        frame_samples = int(target_rate * 60 / 1000)
        frame_bytes = frame_samples * 2

        pcm_bytes = pcm.tobytes()
        packets: list[bytes] = []

        def _collect(opus_bytes):
            if opus_bytes:
                packets.append(opus_bytes)

        for i in range(0, len(pcm_bytes), frame_bytes):
            chunk = pcm_bytes[i : i + frame_bytes]
            if len(chunk) < frame_bytes:
                chunk += b"\x00" * (frame_bytes - len(chunk))
            encoder.encode_pcm_to_opus_stream(
                chunk, end_of_stream=(i + frame_bytes >= len(pcm_bytes)),
                callback=_collect,
            )
        encoder.close()
        return packets

    return await asyncio.get_running_loop().run_in_executor(None, _decode_and_encode)


async def _stream_singing(conn: "ConnectionHandler", opus_packets: list) -> None:
    """Send a list of Opus packets to the device with 60 ms pacing.

    Bypasses tts_audio_queue because the consumer's future.result
    (tts_timeout=15s default) trips on long clips. Sends packets directly to
    the WebSocket, paced by asyncio.sleep. Respects client_abort for barge-in.
    """
    frame_s = 0.06
    sent = 0
    try:
        # Match what sendAudioMessage(FIRST, ...) does: emit a sentence_start
        # so the device firmware transitions into "playing" state. Without
        # this, Opus frames arrive but get dropped on the floor.
        await conn.websocket.send(json.dumps({
            "type": "tts",
            "state": "sentence_start",
            "text": "Macarena",
            "session_id": conn.session_id,
        }))
        for packet in opus_packets:
            if conn.client_abort or conn.is_exiting:
                conn.logger.bind(tag=TAG).info(
                    f"Singing aborted after {sent}/{len(opus_packets)} packets"
                )
                break
            await conn.websocket.send(packet)
            sent += 1
            await asyncio.sleep(frame_s)
    except Exception as exc:
        conn.logger.bind(tag=TAG).error(f"Singing stream failed: {exc}")
    finally:
        conn.client_is_speaking = False
        try:
            await conn.websocket.send(json.dumps({
                "type": "tts",
                "state": "stop",
                "session_id": conn.session_id,
            }))
        except Exception:
            pass
        conn.logger.bind(tag=TAG).info(f"Singing stream complete ({sent} packets sent)")


async def handleAudioMessage(conn: "ConnectionHandler", audio):
    if conn.is_exiting:
        return
    have_voice = conn.vad.is_vad(conn, audio)
    if hasattr(conn, "just_woken_up") and conn.just_woken_up:
        have_voice = False
        if not hasattr(conn, "vad_resume_task") or conn.vad_resume_task.done():
            conn.vad_resume_task = asyncio.create_task(resume_vad_detection(conn))
        return
    if have_voice:
        if conn.client_is_speaking and conn.client_listen_mode != "manual":
            await handleAbortMessage(conn)
    await no_voice_close_connect(conn, have_voice)
    await conn.asr.receive_audio(conn, audio, have_voice)


async def resume_vad_detection(conn: "ConnectionHandler"):
    await asyncio.sleep(2)
    conn.just_woken_up = False


async def startToChat(conn: "ConnectionHandler", text):
    speaker_name = None
    language_tag = None
    actual_text = text

    try:
        if text.strip().startswith("{") and text.strip().endswith("}"):
            data = json.loads(text)
            if "speaker" in data and "content" in data:
                speaker_name = data["speaker"]
                language_tag = data["language"]
                actual_text = data["content"]
                conn.logger.bind(tag=TAG).info(f"解析到说话人信息: {speaker_name}")
    except (json.JSONDecodeError, KeyError):
        pass

    if _is_noise(actual_text):
        conn.logger.bind(tag=TAG).info(f"ASR noise rejected: {actual_text!r}")
        return

    actual_text = _apply_asr_corrections(actual_text)
    actual_text = _apply_phrase_corrections(actual_text)

    # Smart-mode session ends when a new user turn arrives that ISN'T a
    # smart-mode re-trigger. We don't have a chat-completion callback in
    # this layer, but the next ASR turn from the same connection is a
    # reliable "the previous response is done" signal. Re-trigger paths
    # below set the flag back to True before sending the next colour.
    if getattr(conn, "smart_mode_active", False):
        # Clear unless the new turn is itself a smart-mode trigger or
        # the queued smart_mode_next flag (handled below).
        if not _is_smart_mode_request(actual_text) and not getattr(
            conn, "smart_mode_next", False
        ):
            conn.smart_mode_active = False

    if speaker_name:
        conn.current_speaker = speaker_name
    else:
        conn.current_speaker = None

    if conn.need_bind:
        await check_bind_device(conn)
        return

    if conn.max_output_size > 0:
        if check_device_output_limit(
            conn.headers.get("device-id"), conn.max_output_size
        ):
            await max_out_size(conn)
            return

    if conn.client_is_speaking and conn.client_listen_mode != "manual":
        dance_task = getattr(conn, "_dance_task", None)
        if dance_task and not dance_task.done():
            dance_task.cancel()
        await handleAbortMessage(conn)

    intent_handled = await handle_user_intent(conn, actual_text)

    if intent_handled:
        return

    await send_stt_message(conn, actual_text)

    thinking_frame = json.dumps({
        "type": "llm",
        "text": "\U0001f914",
        "emotion": "thinking",
        "session_id": conn.session_id,
    })
    conn.logger.bind(tag=TAG).info(f"Sending thinking emotion frame to device")
    await conn.websocket.send(thinking_frame)

    user_text = actual_text
    try:
        if actual_text.strip().startswith("{"):
            user_text = json.loads(actual_text).get("content", actual_text)
    except (json.JSONDecodeError, KeyError):
        pass

    if _is_smart_mode_request(user_text):
        remaining_q = _strip_smart_trigger(user_text)
        conn.logger.bind(tag=TAG).info(f"Smart mode: q={remaining_q!r}")
        # Mark BEFORE the colour change so _send_led_color's re-assert
        # paints the smart-mode pixel on top of the full-ring purple.
        conn.smart_mode_active = True
        await _send_led_color(conn, 168, 0, 168)
        await _send_led_multi(conn, SMART_MODE_LED_INDEX, 168, 0, 168)
        if remaining_q:
            _submit_chat(conn, f"[SMART_MODE]\n{remaining_q}")
        else:
            conn.smart_mode_next = True
            conn.executor.submit(
                conn.chat,
                "[SMART_MODE_ACK] The user activated Smart Mode. "
                "Say only: 'Smart mode! What would you like to know?'",
            )
        return

    if getattr(conn, 'smart_mode_next', False):
        conn.smart_mode_next = False
        conn.smart_mode_active = True
        await _send_led_color(conn, 168, 0, 168)
        await _send_led_multi(conn, SMART_MODE_LED_INDEX, 168, 0, 168)
        _submit_chat(conn, f"[SMART_MODE]\n{actual_text}")
        return

    # Face recognition (Layer 4) must run BEFORE the vision check —
    # "take a photo of me, this is brett" would otherwise match the
    # generic VISION_PHRASES and route to the VLM instead of enrollment.
    if await _handle_face_enroll(conn, user_text):
        return
    if _is_face_list_request(user_text):
        if await _handle_face_list(conn):
            return
    if await _handle_face_forget(conn, user_text):
        return

    if _is_vision_request(user_text):
        conn.logger.bind(tag=TAG).info(f"Vision intent detected: {user_text[:60]}")
        description = await _handle_vision(conn, user_text)
        if description:
            vision_prompt = (
                f"[You just used your camera and took a photo. "
                f"The photo shows: {description}]\n"
                f'The child said: "{user_text}"\n'
                f"Respond naturally about what you see, as if looking at it together."
            )
            _submit_chat(conn, vision_prompt)
            return

    if _is_dance_request(user_text):
        dance_name = _detect_dance_name(user_text)
        await _handle_dance(conn, dance_name)
        return

    _submit_chat(conn, actual_text)


async def no_voice_close_connect(conn: "ConnectionHandler", have_voice):
    if have_voice:
        conn.last_activity_time = time.time() * 1000
        return
    if conn.last_activity_time > 0.0:
        no_voice_time = time.time() * 1000 - conn.last_activity_time
        close_connection_no_voice_time = int(
            conn.config.get("close_connection_no_voice_time", 120)
        )
        if (
            not conn.close_after_chat
            and no_voice_time > 1000 * close_connection_no_voice_time
        ):
            conn.close_after_chat = True
            conn.client_abort = False
            end_prompt = conn.config.get("end_prompt", {})
            if end_prompt and end_prompt.get("enable", True) is False:
                conn.logger.bind(tag=TAG).info("结束对话，无需发送结束提示语")
                await conn.close()
                return
            prompt = end_prompt.get("prompt")
            if not prompt:
                prompt = "Time flies when we're having fun! Let's chat again next time!"
            await startToChat(conn, prompt)


async def max_out_size(conn: "ConnectionHandler"):
    conn.client_abort = False
    text = "Sorry, I need to take a break now. Let's talk again tomorrow — same time, same place! Bye bye!"
    await send_stt_message(conn, text)
    file_path = "config/assets/max_output_size.wav"
    opus_packets = await audio_to_data(file_path)
    conn.tts.tts_audio_queue.put((SentenceType.LAST, opus_packets, text))
    conn.close_after_chat = True


async def check_bind_device(conn: "ConnectionHandler"):
    if conn.bind_code:
        if len(conn.bind_code) != 6:
            conn.logger.bind(tag=TAG).error(f"Invalid bind code format: {conn.bind_code}")
            text = "Bind code format error, please check the configuration."
            await send_stt_message(conn, text)
            return

        text = f"Please open the control panel and enter {conn.bind_code} to bind this device."
        await send_stt_message(conn, text)

        music_path = "config/assets/bind_code.wav"
        opus_packets = await audio_to_data(music_path)
        conn.tts.tts_audio_queue.put((SentenceType.FIRST, opus_packets, text))

        for i in range(6):
            try:
                digit = conn.bind_code[i]
                num_path = f"config/assets/bind_code/{digit}.wav"
                num_packets = await audio_to_data(num_path)
                conn.tts.tts_audio_queue.put((SentenceType.MIDDLE, num_packets, None))
            except Exception as e:
                conn.logger.bind(tag=TAG).error(f"播放数字音频失败: {e}")
                continue
        conn.tts.tts_audio_queue.put((SentenceType.LAST, [], None))
    else:
        conn.client_abort = False
        text = "Could not find device version information. Please configure the OTA URL correctly and rebuild the firmware."
        await send_stt_message(conn, text)
        music_path = "config/assets/bind_not_found.wav"
        opus_packets = await audio_to_data(music_path)
        conn.tts.tts_audio_queue.put((SentenceType.LAST, opus_packets, text))
