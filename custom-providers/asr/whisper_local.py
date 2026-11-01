import os
import time
import asyncio
import inspect

import numpy as np
from faster_whisper import WhisperModel

from config.logger import setup_logging
from typing import Optional, Tuple, List
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType

TAG = __name__
logger = setup_logging()

MAX_RETRIES = 2
RETRY_DELAY = 1  # seconds
DEFAULT_NO_SPEECH_THRESHOLD = 0.6  # Whisper's own default
# The hotword bias turns servo whirr and other non-speech into a lone "Dotty"
# (no_speech 0.43 on the robot, 2026-10-20), so a transcript that is nothing
# but the hotword has to be clearer speech than that before it becomes a turn.
HOTWORD_ONLY_NO_SPEECH_THRESHOLD = 0.3


def _no_speech_threshold(config: dict) -> float:
    """Mean no_speech_prob above which a transcript is dropped. 1.0 disables."""
    try:
        value = float(config.get("no_speech_threshold", DEFAULT_NO_SPEECH_THRESHOLD))
    except (TypeError, ValueError):
        return DEFAULT_NO_SPEECH_THRESHOLD
    return value if 0.0 <= value <= 1.0 else DEFAULT_NO_SPEECH_THRESHOLD


# #105: room noise (TV, podcast, a busy kitchen) keeps a session open and feeds
# Whisper fake turns. The maintainer's direction is to close the session after
# a run of noise-like turns and let touch wake it. Level alone cannot tell this
# noise from a quiet child (rms 0.002-0.014 vs 0.007-0.017), so the signals are
# Whisper's own confidence. Real close-talk on the robot scored no_speech <=
# 0.32 and avg_logprob >= -0.9; the room-noise run scored no_speech up to 0.59.
DEFAULT_NOISE_TURN_LIMIT = 3  # 0 disables; Brett to confirm
DEFAULT_NOISE_NO_SPEECH = 0.45
DEFAULT_NOISE_LOGPROB = -1.0  # Whisper's own "failed decode" line
# Noise turns further apart than this are separate episodes, not a run.
NOISE_RUN_WINDOW_S = 90.0


def _float_option(config: dict, key: str, default: float) -> float:
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _int_option(config: dict, key: str, default: int) -> int:
    try:
        return max(0, int(config.get(key, default)))
    except (TypeError, ValueError):
        return default


def _is_noise_like(content: str, no_speech: float, avg_logprob: float,
                   no_speech_cutoff: float, logprob_cutoff: float) -> bool:
    """A turn that is probably not someone talking to Dotty.

    An empty transcript counts: VAD opened on sound that Whisper (or the
    no-speech gate) would not turn into words. Otherwise the transcript needs
    to be a weak decode or likely non-speech."""
    if not content.strip():
        return True
    return no_speech >= no_speech_cutoff or avg_logprob < logprob_cutoff


class NoiseRunTracker:
    """Counts consecutive noise-like turns per session."""

    def __init__(self, limit: int, window_s: float = NOISE_RUN_WINDOW_S):
        self.limit = limit
        self.window_s = window_s
        self._runs: dict = {}  # session_id -> (count, last_time)

    def record(self, session_id, noise_like: bool, now: float) -> bool:
        """Note one turn. True when the run has reached the limit (the run is
        then reset so the caller closes the session once)."""
        if self.limit <= 0:
            return False
        if not noise_like:
            self._runs.pop(session_id, None)
            return False
        count, last = self._runs.get(session_id, (0, now))
        if now - last > self.window_s:
            count = 0
        count += 1
        if count >= self.limit:
            self._runs.pop(session_id, None)
            return True
        self._runs[session_id] = (count, now)
        # Forget long-dead sessions so the map cannot grow without bound.
        if len(self._runs) > 64:
            self._runs = {k: v for k, v in self._runs.items()
                          if now - v[1] <= self.window_s}
        return False


async def _close_session_for_noise(session_id, count: int) -> None:
    """End the device session (touch/wake word reopens it). Never raises."""
    try:
        from core.portal_bridge import active_connections
        from core.utils import device_command

        conn = next((c for c in list(active_connections.values())
                     if getattr(c, "session_id", None) == session_id), None)
        if conn is None:
            logger.bind(tag=TAG).warning(
                f"ASR-NOISE-CLOSE no live connection for session {session_id!r}")
            return
        await device_command.end_session(
            conn, reason=f"{count} noise-like turns in a row (#105)")
    except Exception as e:
        logger.bind(tag=TAG).warning(f"ASR-NOISE-CLOSE failed (non-fatal): {e}")


def _hotwords(config: dict) -> Optional[str]:
    """Decoder bias for rare words such as the wake-name (#164). None = off."""
    value = config.get("hotwords")
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value if v)
    if not isinstance(value, str):
        return None
    value = value.strip()
    # An unrendered template placeholder ("<ROBOT_NAME>") is not a hotword.
    if not value or (value.startswith("<") and value.endswith(">")):
        return None
    return value


def _is_hotword_only(content: str, hotwords: Optional[str]) -> bool:
    if not hotwords:
        return False
    vocab = {w.strip(".,!?;:'\"").lower() for w in hotwords.split()}
    words = [w.strip(".,!?;:'\"").lower() for w in content.split()]
    words = [w for w in words if w]
    return bool(words) and all(w in vocab for w in words)


def _supports_kwarg(func, name: str) -> bool:
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


class ASRProvider(ASRProviderBase):
    """faster-whisper local ASR provider — drop-in replacement for FunASR.

    Mirrors the contract of fun_local.py: speech_to_text returns
    (text_or_dict, file_path) where the dict is shaped {"content": "<utt>"}
    to match the downstream expectation set by FunASR's lang_tag_filter
    output (callers access text["content"]).

    Phase 1: CPU-only. The GPU swap (Phase 2) is a config-only flip —
    set device: cuda, compute_type: float16 in .config.yaml when the
    GPUs land. No code change needed.
    """

    def __init__(self, config: dict, delete_audio_file: bool):
        super().__init__()

        self.interface_type = InterfaceType.LOCAL
        self.model_dir = config.get("model_dir")
        self.output_dir = config.get("output_dir")
        self.language = config.get("language", "en")
        self.model_size = config.get("model_size", "small.en")
        self.device = config.get("device", "cpu")
        self.compute_type = config.get("compute_type", "int8")
        self.beam_size = int(config.get("beam_size", 1))
        self.cpu_threads = int(config.get("cpu_threads", 0))
        self.initial_prompt = config.get("initial_prompt", None)
        self.hotwords = _hotwords(config)
        self.no_speech_threshold = _no_speech_threshold(config)
        self.noise_no_speech = _float_option(
            config, "noise_no_speech", DEFAULT_NOISE_NO_SPEECH)
        self.noise_logprob = _float_option(
            config, "noise_avg_logprob", DEFAULT_NOISE_LOGPROB)
        self.noise_runs = NoiseRunTracker(
            _int_option(config, "noise_turn_limit", DEFAULT_NOISE_TURN_LIMIT))
        self.delete_audio_file = delete_audio_file

        if self.output_dir:
            os.makedirs(self.output_dir, exist_ok=True)

        # Prefer an on-disk CTranslate2 model directory if provided; otherwise
        # fall back to the named model_size (faster-whisper auto-fetches).
        model_id = self.model_dir if self.model_dir else self.model_size

        logger.bind(tag=TAG).info(
            f"Loading faster-whisper model: id={model_id} device={self.device} "
            f"compute_type={self.compute_type} cpu_threads={self.cpu_threads} "
            f"beam_size={self.beam_size} language={self.language}"
        )

        self.model = WhisperModel(
            model_id,
            device=self.device,
            compute_type=self.compute_type,
            cpu_threads=self.cpu_threads,
        )

        # `hotwords` arrived in faster-whisper 1.0. Passing it to an older
        # build would raise on every utterance, so drop it there instead.
        self._decode_options = {}
        if self.hotwords:
            if _supports_kwarg(self.model.transcribe, "hotwords"):
                self._decode_options["hotwords"] = self.hotwords
                logger.bind(tag=TAG).info(f"faster-whisper hotwords: {self.hotwords!r}")
            else:
                logger.bind(tag=TAG).warning(
                    "faster-whisper build has no hotwords support; ignoring "
                    f"hotwords={self.hotwords!r}"
                )

        # Warm-up: transcribe 1 s of silence so the lazy model load + first
        # CTranslate2 init cost is paid here, not on the first real utterance.
        # Wrapped in try/except — a warm-up failure shouldn't kill init; the
        # real call will surface the error with proper retry logic.
        try:
            warm_start = time.time()
            warm_audio = np.zeros(16000, dtype=np.float32)
            warm_segments, _ = self.model.transcribe(
                warm_audio,
                language=self.language,
                beam_size=self.beam_size,
                condition_on_previous_text=False,
                vad_filter=False,
                initial_prompt=self.initial_prompt,
                **self._decode_options,
            )
            for _ in warm_segments:
                pass
            logger.bind(tag=TAG).info(
                f"faster-whisper warm-up complete in {time.time() - warm_start:.3f}s"
            )
        except Exception as e:
            logger.bind(tag=TAG).warning(f"faster-whisper warm-up failed (non-fatal): {e}")

    async def speech_to_text(
        self, opus_data: List[bytes], session_id: str, audio_format="opus", artifacts=None
    ) -> Tuple[Optional[dict], Optional[str]]:
        if artifacts is None:
            return "", None

        retry_count = 0
        while retry_count < MAX_RETRIES:
            try:
                start_time = time.time()

                # artifacts.pcm_bytes is 16-bit signed PCM @ 16 kHz mono
                # (the format SileroVAD + the xiaozhi pipeline produce).
                pcm_i16 = np.frombuffer(artifacts.pcm_bytes, dtype=np.int16)
                audio = pcm_i16.astype(np.float32) / 32768.0

                segments, _info = await asyncio.to_thread(
                    self._transcribe_blocking, audio
                )
                segments = list(segments)

                content = "".join(seg.text for seg in segments).strip()
                # Near-silence after Dotty stops speaking comes back as a
                # confident stock phrase ("Thank you."). vad_filter is off, so
                # Whisper's own no-speech score is the only signal here.
                if segments and content:
                    mean_no_speech = sum(s.no_speech_prob for s in segments) / len(segments)
                    threshold = self.no_speech_threshold
                    if _is_hotword_only(content, getattr(self, "hotwords", None)):
                        threshold = min(threshold, HOTWORD_ONLY_NO_SPEECH_THRESHOLD)
                    if mean_no_speech > threshold:
                        logger.bind(tag=TAG).info(
                            f"ASR-REJECT no_speech={mean_no_speech:.3f} > "
                            f"{threshold:.2f} | dropped {content!r}"
                        )
                        content = ""
                text = {"content": content}

                logger.bind(tag=TAG).info(
                    f"语音识别耗时: {time.time() - start_time:.3f}s | 结果: {content}"
                )

                # #105 instrumentation — log signals that may separate real
                # close-talk from ambient TV/podcast audio, so a reject gate can
                # be tuned on measured distributions. Pure logging; no behaviour
                # change. Strip once the gate thresholds are chosen.
                try:
                    dur = audio.size / 16000.0
                    rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
                    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
                    if segments:
                        nsp = sum(s.no_speech_prob for s in segments) / len(segments)
                        alp = sum(s.avg_logprob for s in segments) / len(segments)
                    else:
                        nsp, alp = 1.0, 0.0
                    lang_prob = getattr(_info, "language_probability", 0.0)
                    logger.bind(tag=TAG).info(
                        f"ASR-METRICS dur={dur:.2f}s rms={rms:.4f} peak={peak:.3f} "
                        f"no_speech={nsp:.3f} avg_logprob={alp:.3f} "
                        f"lang_prob={lang_prob:.3f} segs={len(segments)} | {content!r}"
                    )
                except Exception as _e:
                    logger.bind(tag=TAG).warning(f"ASR-METRICS log failed (non-fatal): {_e}")

                if self._note_turn_for_noise(session_id, content, segments):
                    # The run's last noise turn is not answered either.
                    text = {"content": ""}

                return text, artifacts.file_path

            except OSError as e:
                retry_count += 1
                if retry_count >= MAX_RETRIES:
                    logger.bind(tag=TAG).error(
                        f"语音识别失败（已重试{retry_count}次）: {e}", exc_info=True
                    )
                    return "", None
                logger.bind(tag=TAG).warning(
                    f"语音识别失败，正在重试（{retry_count}/{MAX_RETRIES}）: {e}"
                )
                await asyncio.sleep(RETRY_DELAY)

            except Exception as e:
                logger.bind(tag=TAG).error(f"语音识别失败: {e}", exc_info=True)
                return "", None

        return "", None

    def _note_turn_for_noise(self, session_id, content: str, segments) -> bool:
        """#105: close the session after a run of noise-like turns. Never
        raises: this must not cost a real turn. True when the session is
        being closed."""
        try:
            if segments:
                nsp = sum(s.no_speech_prob for s in segments) / len(segments)
                alp = sum(s.avg_logprob for s in segments) / len(segments)
            else:
                nsp, alp = 1.0, 0.0
            noise = _is_noise_like(
                content, nsp, alp, self.noise_no_speech, self.noise_logprob)
            if self.noise_runs.record(session_id, noise, time.monotonic()):
                logger.bind(tag=TAG).info(
                    f"ASR-NOISE-CLOSE {self.noise_runs.limit} noise-like turns "
                    f"in a row; ending session {session_id!r}")
                asyncio.get_running_loop().create_task(
                    _close_session_for_noise(session_id, self.noise_runs.limit))
                return True
        except Exception as e:
            logger.bind(tag=TAG).warning(f"noise tracking failed (non-fatal): {e}")
        return False

    def _transcribe_blocking(self, audio: np.ndarray):
        """Run inside asyncio.to_thread. Eagerly consume the segment iterator
        here so the async caller doesn't iterate a generator backed by C++
        state on the event-loop thread."""
        segments_iter, info = self.model.transcribe(
            audio,
            language=self.language,
            beam_size=self.beam_size,
            condition_on_previous_text=False,
            vad_filter=False,
            initial_prompt=self.initial_prompt,
            **self._decode_options,
        )
        return list(segments_iter), info
