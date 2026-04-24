import os
import re
import time
import json
import asyncio
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
}
_ASR_CORRECTION_RE = re.compile(
    r'\b(' + '|'.join(re.escape(k) for k in _ASR_CORRECTIONS) + r')\b',
    re.IGNORECASE,
)


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
            "id": int(time.time() * 1000),
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
                actual_text = text
    except (json.JSONDecodeError, KeyError):
        pass

    if _is_noise(actual_text):
        conn.logger.bind(tag=TAG).info(f"ASR noise rejected: {actual_text!r}")
        return

    actual_text = _apply_asr_corrections(actual_text)

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
                prompt = "请你以```时间过得真快```未来头，用富有感情、依依不舍的话来结束这场对话吧。！"
            await startToChat(conn, prompt)


async def max_out_size(conn: "ConnectionHandler"):
    conn.client_abort = False
    text = "不好意思，我现在有点事情要忙，明天这个时候我们再聊，约好了哦！明天不见不散，拜拜！"
    await send_stt_message(conn, text)
    file_path = "config/assets/max_output_size.wav"
    opus_packets = await audio_to_data(file_path)
    conn.tts.tts_audio_queue.put((SentenceType.LAST, opus_packets, text))
    conn.close_after_chat = True


async def check_bind_device(conn: "ConnectionHandler"):
    if conn.bind_code:
        if len(conn.bind_code) != 6:
            conn.logger.bind(tag=TAG).error(f"无效的绑定码格式: {conn.bind_code}")
            text = "绑定码格式错误，请检查配置。"
            await send_stt_message(conn, text)
            return

        text = f"请登录控制面板，输入{conn.bind_code}，绑定设备。"
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
        text = f"没有找到该设备的版本信息，请正确配置 OTA地址，然后重新编译固件。"
        await send_stt_message(conn, text)
        music_path = "config/assets/bind_not_found.wav"
        opus_packets = await audio_to_data(music_path)
        conn.tts.tts_audio_queue.put((SentenceType.LAST, opus_packets, text))
