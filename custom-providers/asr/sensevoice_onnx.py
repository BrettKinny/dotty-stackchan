import os
import time
import asyncio

import numpy as np
import sherpa_onnx

from config.logger import setup_logging
from typing import Optional, Tuple, List
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType

TAG = __name__
logger = setup_logging()

MAX_RETRIES = 2
RETRY_DELAY = 1  # seconds


class ASRProvider(ASRProviderBase):
    """sherpa-onnx SenseVoiceSmall (int8) local ASR provider.

    A lighter, no-PyTorch alternative to fun_local.py (#135). onnxruntime is
    statically bundled inside the sherpa-onnx wheel — no torch, no separate
    onnxruntime dep. Coexists with FunASR; selected via
    selected_module.ASR: SenseVoiceOnnx.

    Mirrors whisper_local.py's contract: speech_to_text returns
    ({"content": "<utt>"}, file_path) (callers access text["content"]).

    No VAD model is needed — xiaozhi hands us a SileroVAD-segmented utterance,
    which OfflineRecognizer decodes directly.
    """

    def __init__(self, config: dict, delete_audio_file: bool):
        super().__init__()

        self.interface_type = InterfaceType.LOCAL
        self.model_dir = config.get("model_dir")
        self.output_dir = config.get("output_dir")
        # SenseVoice mis-detects ko/ja on short/unclear English when on "auto";
        # default to the native English pin. Valid: auto, zh, en, ja, ko, yue.
        self.language = config.get("language", "en")
        self.num_threads = int(config.get("num_threads", 2))
        self.use_itn = bool(config.get("use_itn", True))
        self.delete_audio_file = delete_audio_file

        # Resolve model + tokens: explicit overrides win, else look in model_dir.
        self.model_path = config.get("model_path")
        self.tokens_path = config.get("tokens_path")
        if not self.model_path and self.model_dir:
            self.model_path = os.path.join(self.model_dir, "model.int8.onnx")
        if not self.tokens_path and self.model_dir:
            self.tokens_path = os.path.join(self.model_dir, "tokens.txt")

        if self.output_dir:
            os.makedirs(self.output_dir, exist_ok=True)

        if not self.model_path or not os.path.isfile(self.model_path):
            raise FileNotFoundError(
                f"sherpa-onnx SenseVoice model not found: {self.model_path!r} "
                f"(set ASR.SenseVoiceOnnx.model_dir or model_path; run `make fetch-models`)"
            )
        if not self.tokens_path or not os.path.isfile(self.tokens_path):
            raise FileNotFoundError(
                f"sherpa-onnx tokens.txt not found: {self.tokens_path!r}"
            )

        logger.bind(tag=TAG).info(
            f"Loading sherpa-onnx SenseVoice: model={self.model_path} "
            f"tokens={self.tokens_path} num_threads={self.num_threads} "
            f"use_itn={self.use_itn} language={self.language}"
        )

        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=self.model_path,
            tokens=self.tokens_path,
            num_threads=self.num_threads,
            use_itn=self.use_itn,
            language=self.language,
        )

        # Warm-up: decode 1 s silence so onnxruntime session init is paid here,
        # not on the first real utterance. Non-fatal.
        try:
            warm_start = time.time()
            self._decode_blocking(np.zeros(16000, dtype=np.float32))
            logger.bind(tag=TAG).info(
                f"sherpa-onnx warm-up complete in {time.time() - warm_start:.3f}s"
            )
        except Exception as e:
            logger.bind(tag=TAG).warning(f"sherpa-onnx warm-up failed (non-fatal): {e}")

    async def speech_to_text(
        self, opus_data: List[bytes], session_id: str, audio_format="opus", artifacts=None
    ) -> Tuple[Optional[dict], Optional[str]]:
        if artifacts is None:
            return "", None

        retry_count = 0
        while retry_count < MAX_RETRIES:
            try:
                start_time = time.time()

                # artifacts.pcm_bytes is 16-bit signed PCM @ 16 kHz mono.
                pcm_i16 = np.frombuffer(artifacts.pcm_bytes, dtype=np.int16)
                audio = pcm_i16.astype(np.float32) / 32768.0

                content = await asyncio.to_thread(self._decode_blocking, audio)
                content = (content or "").strip()
                text = {"content": content}

                dt = time.time() - start_time
                logger.bind(tag=TAG).info(f"语音识别耗时: {dt:.3f}s | 结果: {content}")

                # #135 RTF instrumentation — read the default-flip benchmark
                # straight from prod logs.
                try:
                    dur = audio.size / 16000.0
                    rtf = dt / dur if dur > 0 else 0.0
                    logger.bind(tag=TAG).info(
                        f"ASR-RTF provider=sensevoice_onnx dur={dur:.2f}s "
                        f"proc={dt:.3f}s rtf={rtf:.3f} | {content!r}"
                    )
                except Exception as _e:
                    logger.bind(tag=TAG).warning(f"ASR-RTF log failed (non-fatal): {_e}")

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

    def _decode_blocking(self, audio: np.ndarray) -> str:
        """Run inside asyncio.to_thread. create_stream is per-call; the
        recognizer is reused. accept_waveform takes (sample_rate, float32_pcm)."""
        stream = self.recognizer.create_stream()
        stream.accept_waveform(16000, audio)
        self.recognizer.decode_stream(stream)
        return stream.result.text
