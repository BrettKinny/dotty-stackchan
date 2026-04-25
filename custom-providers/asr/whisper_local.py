import os
import time
import asyncio

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

                content = "".join(seg.text for seg in segments).strip()
                text = {"content": content}

                logger.bind(tag=TAG).info(
                    f"语音识别耗时: {time.time() - start_time:.3f}s | 结果: {content}"
                )

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
        )
        return list(segments_iter), info
