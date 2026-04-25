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


# ---------- Dance / singing mode ----------
# Singing and dancing are unified — both route to _handle_dance(), which plays
# the choreography and (if a matching audio_file exists in DANCE_REGISTRY)
# injects pre-rendered singing audio into the TTS queue.

_DANCE_PHRASES = (
    "dance", "do a dance", "let's dance", "can you dance",
    "dance for me", "do the macarena", "macarena",
    "dance time", "dance mode",
    # Singing triggers — same handler, optional audio file makes it sing.
    "sing a song", "sing the macarena", "sing macarena",
    "can you sing", "sing for me", "sing something",
    "let's sing",
)

# Short "sing" needs word-boundary matching to avoid false positives on
# words like "single" or "singapore".
_SING_WORD_RE = re.compile(r"\bsing\b", re.IGNORECASE)


def _is_dance_request(text: str) -> bool:
    lower = text.lower().strip()
    if any(phrase in lower for phrase in _DANCE_PHRASES):
        return True
    return bool(_SING_WORD_RE.search(lower))


def _detect_dance_name(text: str) -> str:
    from core.handle.dances import DANCE_REGISTRY, DEFAULT_DANCE
    lower = text.lower()
    for name in DANCE_REGISTRY:
        if name in lower:
            return name
    return DEFAULT_DANCE


async def _handle_dance(conn: "ConnectionHandler", dance_name: str) -> None:
    from core.handle.dances import DANCE_REGISTRY, execute_choreography

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
            opus_packets = await audio_to_data(audio_file)
        except Exception as exc:
            conn.logger.bind(tag=TAG).error(f"Dance mode: audio decode failed: {exc}")
            has_audio = False

    if has_audio and opus_packets is not None:
        conn.tts.tts_audio_queue.put((SentenceType.FIRST, opus_packets, None))
        conn.tts.tts_audio_queue.put((SentenceType.LAST, [], None))
        conn.logger.bind(tag=TAG).info(
            f"Dance mode: queued singing audio {audio_file} ({len(opus_packets)} packets)"
        )

    # Only delay choreography for audio sync when we actually queued audio.
    from core.handle.dances import AUDIO_LATENCY_OFFSET_MS
    audio_offset = AUDIO_LATENCY_OFFSET_MS if has_audio else 0

    dance_task = asyncio.create_task(
        execute_choreography(
            conn, dance["timeline"], _send_head_angles, _send_led_color,
            audio_latency_offset_ms=audio_offset,
        )
    )
    conn._dance_task = dance_task

    if not has_audio:
        conn.executor.submit(
            conn.chat,
            f"[DANCE:{dance_name}] You're about to dance the {dance_name.title()}! "
            f"Say a SHORT excited one-liner intro (under 15 words). "
            f"Example: '\U0001f606 {dance['intro']}'",
        )

    def _on_dance_done(task):
        async def _cleanup():
            if task.cancelled():
                await _send_head_angles(conn, 0, 0, 200)
            await _send_led_color(conn, 0, 0, 0)
        asyncio.ensure_future(_cleanup())

    dance_task.add_done_callback(_on_dance_done)


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
        await _send_led_color(conn, 168, 0, 168)
        if remaining_q:
            conn.executor.submit(conn.chat, f"[SMART_MODE]\n{remaining_q}")
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
        await _send_led_color(conn, 168, 0, 168)
        conn.executor.submit(conn.chat, f"[SMART_MODE]\n{actual_text}")
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
            conn.executor.submit(conn.chat, vision_prompt)
            return

    if _is_dance_request(user_text):
        dance_name = _detect_dance_name(user_text)
        await _handle_dance(conn, dance_name)
        return

    conn.executor.submit(conn.chat, actual_text)


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
