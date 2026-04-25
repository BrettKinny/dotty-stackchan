import os
import queue
import traceback
from math import gcd

import numpy as np
from piper.voice import PiperVoice, SynthesisConfig
from scipy import signal

from config.logger import setup_logging
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import ContentType, InterfaceType, SentenceType
from core.utils import opus_encoder_utils, textUtils
from core.utils.tts import MarkdownCleaner

TAG = __name__
logger = setup_logging()


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.interface_type = InterfaceType.SINGLE_STREAM
        self.voice = config.get("private_voice") or config.get(
            "voice", "en_GB-cori-medium"
        )
        self.audio_format = "pcm"
        self.before_stop_play_files = []

        # pitch_scale > 1.0 raises pitch (and shortens duration) via a resample trick.
        # length_scale is Piper's native speed knob: <1.0 faster, >1.0 slower, pitch preserved.
        self.pitch_scale = float(config.get("pitch_scale", 1.0))
        if self.pitch_scale <= 0:
            raise ValueError(
                f"piper_local: pitch_scale must be positive, got {self.pitch_scale}"
            )
        length_scale = config.get("length_scale")
        self.syn_config = (
            SynthesisConfig(length_scale=float(length_scale))
            if length_scale is not None
            else None
        )

        model_path = config.get("model_path")
        config_path = config.get("config_path")
        if not model_path or not os.path.exists(model_path):
            raise ValueError(
                f"piper_local: model_path missing or not found: {model_path!r}"
            )
        if not config_path or not os.path.exists(config_path):
            raise ValueError(
                f"piper_local: config_path missing or not found: {config_path!r}"
            )

        self.voice_obj = PiperVoice.load(model_path, config_path)

        self.opus_encoder = opus_encoder_utils.OpusEncoderUtils(
            sample_rate=24000, channels=1, frame_size_ms=60
        )
        self.pcm_buffer = bytearray()

        src_rate = int(self.voice_obj.config.sample_rate)
        self._src_rate = src_rate
        # Tell scipy the source is faster than it really is — the polyphase resample
        # to 24000 Hz then produces fewer output samples, so playback ends up
        # shorter + higher-pitched ("chipmunk" trick).
        effective_rate = max(1, int(round(src_rate * self.pitch_scale)))
        if effective_rate == 24000:
            self._up = 1
            self._down = 1
        else:
            g = gcd(effective_rate, 24000)
            self._up = 24000 // g
            self._down = effective_rate // g

        logger.bind(tag=TAG).info(
            f"piper_local loaded voice={self.voice!r} src_rate={src_rate} "
            f"pitch_scale={self.pitch_scale} length_scale={length_scale} "
            f"resample={self._up}/{self._down}"
        )

    def tts_text_priority_thread(self):
        while not self.conn.stop_event.is_set():
            try:
                message = self.tts_text_queue.get(timeout=1)
                if message.sentence_type == SentenceType.FIRST:
                    self.tts_stop_request = False
                    self.processed_chars = 0
                    self.tts_text_buff = []
                    self.before_stop_play_files.clear()
                elif ContentType.TEXT == message.content_type:
                    self.tts_text_buff.append(message.content_detail)
                    segment_text = self._get_segment_text()
                    if segment_text:
                        self.to_tts_single_stream(segment_text)
                elif ContentType.FILE == message.content_type:
                    if message.content_file and os.path.exists(message.content_file):
                        self._process_audio_file_stream(
                            message.content_file,
                            callback=lambda audio_data: self.handle_audio_file(
                                audio_data, message.content_detail
                            ),
                        )

                if message.sentence_type == SentenceType.LAST:
                    self._process_remaining_text_stream(True)
            except queue.Empty:
                continue
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"Piper TTS text thread error: {e}\n{traceback.format_exc()}"
                )

    def _process_remaining_text_stream(self, is_last=False):
        full_text = "".join(self.tts_text_buff)
        remaining_text = full_text[self.processed_chars :]
        if remaining_text:
            segment_text = textUtils.get_string_no_punctuation_or_emoji(remaining_text)
            if segment_text:
                self.to_tts_single_stream(segment_text, is_last)
                self.processed_chars += len(full_text)
            else:
                self._process_before_stop_play_files()
        else:
            self._process_before_stop_play_files()

    def to_tts_single_stream(self, text, is_last=False):
        text = MarkdownCleaner.clean_markdown(text)
        try:
            self.text_to_speak(text, is_last)
        except Exception as e:
            logger.bind(tag=TAG).error(f"Piper synth failed for {text!r}: {e}")
        return None

    def text_to_speak(self, text, is_last):
        frame_bytes = int(
            self.opus_encoder.sample_rate
            * self.opus_encoder.channels
            * self.opus_encoder.frame_size_ms
            / 1000
            * 2
        )

        try:
            self.pcm_buffer.clear()
            self.tts_audio_queue.put((SentenceType.FIRST, [], text))

            raw_pcm = bytearray()
            for chunk in self.voice_obj.synthesize(text, syn_config=self.syn_config):
                if chunk and chunk.audio_int16_bytes:
                    raw_pcm.extend(chunk.audio_int16_bytes)

            if not raw_pcm:
                logger.bind(tag=TAG).warning(
                    f"Piper returned no audio for {text!r}"
                )
                if is_last:
                    self._process_before_stop_play_files()
                return

            if self._up == 1 and self._down == 1:
                self.pcm_buffer.extend(bytes(raw_pcm))
            else:
                samples = np.frombuffer(bytes(raw_pcm), dtype=np.int16)
                resampled = signal.resample_poly(samples, self._up, self._down)
                resampled = np.clip(resampled, -32768, 32767).astype(np.int16)
                self.pcm_buffer.extend(resampled.tobytes())

            while len(self.pcm_buffer) >= frame_bytes:
                frame = bytes(self.pcm_buffer[:frame_bytes])
                del self.pcm_buffer[:frame_bytes]
                self.opus_encoder.encode_pcm_to_opus_stream(
                    frame, end_of_stream=False, callback=self.handle_opus
                )

            if self.pcm_buffer:
                self.opus_encoder.encode_pcm_to_opus_stream(
                    bytes(self.pcm_buffer),
                    end_of_stream=True,
                    callback=self.handle_opus,
                )
                self.pcm_buffer.clear()

            if is_last:
                self._process_before_stop_play_files()

        except Exception as e:
            logger.bind(tag=TAG).error(
                f"Piper synth exception for {text!r}: {e}"
            )
            self.tts_audio_queue.put((SentenceType.LAST, [], None))

    async def close(self):
        await super().close()
        if hasattr(self, "opus_encoder"):
            self.opus_encoder.close()
