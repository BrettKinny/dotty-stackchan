import asyncio
import io
import os
import queue
import traceback

import edge_tts
from pydub import AudioSegment

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
            "voice", "en-AU-WilliamNeural"
        )
        self.audio_format = "pcm"
        self.before_stop_play_files = []

        self.opus_encoder = opus_encoder_utils.OpusEncoderUtils(
            sample_rate=24000, channels=1, frame_size_ms=60
        )
        self.pcm_buffer = bytearray()

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
                    f"Edge stream TTS text thread error: {e}\n{traceback.format_exc()}"
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
            asyncio.run(self.text_to_speak(text, is_last))
        except Exception as e:
            logger.bind(tag=TAG).error(
                f"Edge stream synth failed for {text!r}: {e}"
            )
        return None

    async def text_to_speak(self, text, is_last):
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

            mp3_buffer = bytearray()
            communicate = edge_tts.Communicate(text, voice=self.voice)
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    mp3_buffer.extend(chunk["data"])

            if not mp3_buffer:
                logger.bind(tag=TAG).warning(
                    f"Edge stream returned no audio for {text!r}"
                )
                if is_last:
                    self._process_before_stop_play_files()
                return

            seg = AudioSegment.from_file(io.BytesIO(bytes(mp3_buffer)), format="mp3")
            seg = seg.set_channels(1).set_frame_rate(24000).set_sample_width(2)
            self.pcm_buffer.extend(seg.raw_data)

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
                f"Edge stream synth exception for {text!r}: {e}"
            )
            self.tts_audio_queue.put((SentenceType.LAST, [], None))

    async def close(self):
        await super().close()
        if hasattr(self, "opus_encoder"):
            self.opus_encoder.close()
