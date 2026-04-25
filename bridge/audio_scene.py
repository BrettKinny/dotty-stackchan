"""
Audio scene classifier — YAMNet (TFLite) wrapper for the bridge.

Phase 2 of the perception roadmap. Taps the same 16 kHz audio frames
that flow into ASR, runs them through a quantised YAMNet model, and
emits ``sound_event`` perception events whenever a curated home-assistant
class crosses a confidence threshold. Sits alongside the firmware-side
``sound_event(direction)`` from the on-device sound localiser; the two
are complementary — direction tells you *where*, this tells you *what*.

Design notes
------------
* **Async, single-worker.** Inference runs on a
  ``concurrent.futures.ThreadPoolExecutor(max_workers=1)`` so we never
  block the ASR ingestion path. If a frame arrives while a previous
  frame is still being classified, the new frame is dropped — a
  half-second YAMNet result is fine to skip; what matters is that
  audio keeps flowing into ASR.

* **Sliding buffer.** YAMNet expects 0.96 s windows of 16 kHz mono
  audio (15 360 samples). We accumulate raw PCM int16 in a ``bytearray``
  and only schedule inference when at least one full frame is buffered.
  Excess samples are kept for the next call so we never lose audio
  across frame boundaries.

* **Defensive imports.** ``tflite-runtime`` is an *optional* dependency.
  If it's missing, the module still imports cleanly — the classifier
  logs a warning at start time and ``feed()`` becomes a no-op. Same
  for a missing model file. The bridge must remain bootable on the
  RPi even if the operator hasn't fetched YAMNet yet.

* **Whitelist + threshold + cooldown.** YAMNet exposes 521 classes;
  most are noise for our use case. The whitelist filters down to the
  classes that should actually trigger Dotty behaviour. Each class
  has its own cooldown so a sustained doorbell ring or continuous
  music doesn't spam the perception bus.

* **Bus integration.** The classifier receives an injected ``bus``
  callable at construction. We call ``bus(event)`` with an event in
  the same shape ``bridge.py`` uses for the existing ``sound_event``
  ingest path — ``{"device_id", "ts", "name": "sound_event",
  "data": {"kind", "confidence", "raw_class"}}``. The wiring on the
  bridge side (turning ``_perception_broadcast`` into a callable, or
  scheduling it onto the asyncio loop) is intentionally deferred to
  a follow-up commit; this module just hands the bus a dict.

Environment variables
---------------------
* ``YAMNET_MODEL_PATH`` — path to the .tflite model. Default:
  ``models/yamnet/yamnet.tflite`` relative to repo root.
* ``YAMNET_THRESHOLD`` — minimum confidence (0.0–1.0) for a class to
  fire. Default: ``0.4``.
* ``YAMNET_COOLDOWN_SEC`` — per-class cooldown after a fire. Default:
  ``5.0``.

Typical wiring (deferred)
-------------------------
Inside ``bridge.py`` once the audio frame hook lands:

    from bridge.audio_scene import AudioSceneClassifier

    _audio_scene = AudioSceneClassifier(bus=_emit_perception_from_thread)
    _audio_scene.start()

    # in the pre-ASR audio frame callback:
    _audio_scene.feed(pcm_bytes, device_id=device_id)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from .yamnet_classmap import YAMNET_CLASS_MAP, label_for

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defensive TFLite import. ``tflite-runtime`` is an optional dependency for
# the bridge — the module degrades to a warning + no-op if it isn't
# available. Two import locations are tried (the slim runtime and the full
# tensorflow lite namespace), since deployments may have either.
# ---------------------------------------------------------------------------
_TFLITE_AVAILABLE = False
_tflite_interpreter_cls = None
try:
    from tflite_runtime.interpreter import Interpreter as _tflite_interpreter_cls  # type: ignore[no-redef]
    _TFLITE_AVAILABLE = True
except Exception:
    try:
        from tensorflow.lite.python.interpreter import Interpreter as _tflite_interpreter_cls  # type: ignore[no-redef]
        _TFLITE_AVAILABLE = True
    except Exception:
        _TFLITE_AVAILABLE = False
        _tflite_interpreter_cls = None


# numpy is widely already pulled in by the audio stack; import defensively
# anyway — same degradation policy as tflite.
_NUMPY_AVAILABLE = False
try:
    import numpy as _np  # type: ignore
    _NUMPY_AVAILABLE = True
except Exception:
    _np = None  # type: ignore[assignment]
    _NUMPY_AVAILABLE = False


# ---------------------------------------------------------------------------
# Whitelist — YAMNet display_names (must match the upstream CSV verbatim)
# we actually want to surface to the perception bus, mapped to a friendly
# kind string used in the emitted event. Anything not in this dict is
# discarded after inference.
# ---------------------------------------------------------------------------
_USEFUL_CLASSES: dict[str, str] = {
    "Doorbell": "doorbell",
    "Ding-dong": "doorbell",
    "Knock": "knock",
    "Tap": "knock",
    "Baby cry, infant cry": "baby_cry",
    "Crying, sobbing": "crying",
    "Dog": "dog",
    "Bark": "dog",
    "Cat": "cat",
    "Music": "music",
    "Speech": "speech",
    "Child speech, kid speaking": "child_speech",
    "Kettle whistle": "kettle",
    "Footsteps": "footsteps",        # not a YAMNet display_name, kept
                                     # as a friendly synonym; see below
    "Walk, footsteps": "footsteps",
    "Alarm": "alarm",
    "Alarm clock": "alarm",
    "Smoke detector, smoke alarm": "smoke_alarm",
    "Fire alarm": "fire_alarm",
    "Telephone bell ringing": "phone",
    "Ringtone": "phone",
    "Laughter": "laughter",
    "Silence": "silence",
}


# Default config — env-tunable. Read at construction so test cases can
# override via env without monkey-patching the module.
_DEFAULT_MODEL_PATH = os.environ.get(
    "YAMNET_MODEL_PATH", "models/yamnet/yamnet.tflite",
)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        log.warning(
            "audio_scene: invalid %s=%r — falling back to %s",
            name, raw, default,
        )
        return default


def _iso_now() -> str:
    """ISO-8601 UTC timestamp matching the rest of the perception
    pipeline."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------
class AudioSceneClassifier:
    """Buffers 16 kHz int16 PCM, runs YAMNet asynchronously, and emits
    sound_event perception events for whitelisted classes.

    Parameters
    ----------
    bus
        Callable invoked with the emitted event dict. Typically wired
        to ``bridge._perception_broadcast`` (or a loop-safe wrapper
        thereof). May be ``None`` for tests or detached use; the
        classifier logs and discards events in that case.
    model_path
        Filesystem path to the YAMNet .tflite model. Default is taken
        from ``YAMNET_MODEL_PATH`` env or ``models/yamnet/yamnet.tflite``.
    sample_rate
        Expected input sample rate. YAMNet was trained at 16 kHz; we
        do not resample, so callers must feed 16 kHz audio.
    frame_size
        Number of int16 samples per inference window. Default 15 360
        = 0.96 s @ 16 kHz, YAMNet's native input length.
    threshold
        Minimum softmax probability required to emit a class. Default
        from ``YAMNET_THRESHOLD`` env, else 0.4.
    cooldown_sec
        Per-class cooldown (seconds) between emits. Default from
        ``YAMNET_COOLDOWN_SEC`` env, else 5.0.
    device_id
        Optional default device_id stamped onto emitted events when
        ``feed()`` is called without one. Defaults to ``"bridge"``.
    """

    def __init__(
        self,
        bus: Optional[Callable[[dict], None]] = None,
        model_path: Optional[str] = None,
        sample_rate: int = 16000,
        frame_size: int = 15360,
        threshold: Optional[float] = None,
        cooldown_sec: Optional[float] = None,
        device_id: str = "bridge",
    ) -> None:
        self._bus = bus
        self._model_path = model_path or _DEFAULT_MODEL_PATH
        self._sample_rate = sample_rate
        self._frame_size = frame_size
        self._frame_bytes = frame_size * 2  # int16 → 2 bytes/sample
        self._threshold = (
            threshold if threshold is not None
            else _env_float("YAMNET_THRESHOLD", 0.4)
        )
        self._cooldown_sec = (
            cooldown_sec if cooldown_sec is not None
            else _env_float("YAMNET_COOLDOWN_SEC", 5.0)
        )
        self._device_id = device_id

        self._buf = bytearray()
        self._buf_lock = threading.Lock()
        self._last_emit: dict[str, float] = {}
        self._last_emit_lock = threading.Lock()

        self._executor: Optional[ThreadPoolExecutor] = None
        self._inflight: Optional[Future] = None

        self._interpreter = None  # lazy
        self._input_index: Optional[int] = None
        self._output_index: Optional[int] = None
        self._model_loaded = False
        self._model_load_attempted = False

        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Spin up the inference executor. Idempotent. Logs a clear
        warning and stays in no-op mode if tflite-runtime is missing
        or the model file can't be loaded — the bridge still boots."""
        if self._started:
            return
        self._started = True

        if not _TFLITE_AVAILABLE:
            log.warning(
                "audio_scene: tflite-runtime not installed — "
                "AudioSceneClassifier is a no-op. Install "
                "tflite-runtime>=2.13 on the inference host to enable.",
            )
            return
        if not _NUMPY_AVAILABLE:
            log.warning(
                "audio_scene: numpy not available — "
                "AudioSceneClassifier is a no-op.",
            )
            return

        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="audio-scene",
        )
        # Lazy model load happens on the worker thread so a slow first
        # load doesn't stall start(). It's also retried on subsequent
        # frames if it fails the first time.
        log.info(
            "audio_scene: started (model=%s threshold=%.2f cooldown=%.1fs)",
            self._model_path, self._threshold, self._cooldown_sec,
        )

    def stop(self) -> None:
        """Shut down the inference executor. Drains in-flight work
        with a short timeout so an unresponsive interpreter can't
        block bridge shutdown forever."""
        if not self._started:
            return
        self._started = False
        ex = self._executor
        self._executor = None
        if ex is not None:
            ex.shutdown(wait=False, cancel_futures=True)
        log.info("audio_scene: stopped")

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------
    def feed(
        self,
        pcm_bytes: bytes,
        device_id: Optional[str] = None,
    ) -> None:
        """Append raw 16 kHz int16 PCM bytes. When the buffer reaches
        a full frame, schedule inference. If a previous inference is
        still in flight, the new frame is dropped — backpressure is
        intentional here, ASR must never wait on us."""
        if not self._started:
            return
        if not pcm_bytes:
            return
        if not _TFLITE_AVAILABLE or not _NUMPY_AVAILABLE:
            return

        with self._buf_lock:
            self._buf.extend(pcm_bytes)
            if len(self._buf) < self._frame_bytes:
                return
            # Slice off exactly one frame; keep any tail for next time.
            frame = bytes(self._buf[: self._frame_bytes])
            del self._buf[: self._frame_bytes]

        # Drop if a prior inference is still running (max_workers=1
        # would queue them otherwise → unbounded backlog).
        if self._inflight is not None and not self._inflight.done():
            return
        ex = self._executor
        if ex is None:
            return
        dev = device_id or self._device_id
        try:
            self._inflight = ex.submit(self._run_inference, frame, dev)
        except RuntimeError:
            # executor shut down between checks
            return

    # ------------------------------------------------------------------
    # Inference (worker-thread side)
    # ------------------------------------------------------------------
    def _ensure_model(self) -> bool:
        """Load the TFLite model on first use. Returns True if loaded.
        Failures are logged once; we don't retry every frame."""
        if self._model_loaded:
            return True
        if self._model_load_attempted:
            return False
        self._model_load_attempted = True

        if _tflite_interpreter_cls is None:
            return False
        if not os.path.isfile(self._model_path):
            log.warning(
                "audio_scene: model file missing at %s — "
                "run `make fetch-yamnet` to download. Classifier no-op.",
                self._model_path,
            )
            return False

        try:
            interp = _tflite_interpreter_cls(model_path=self._model_path)
            interp.allocate_tensors()
            inputs = interp.get_input_details()
            outputs = interp.get_output_details()
            if not inputs or not outputs:
                log.error("audio_scene: model has no inputs/outputs")
                return False
            self._interpreter = interp
            self._input_index = inputs[0]["index"]
            # YAMNet's TFLite export typically returns three outputs
            # (scores, embeddings, log_mel_spectrogram). The first is
            # the per-class score tensor we care about.
            self._output_index = outputs[0]["index"]
            self._model_loaded = True
            log.info(
                "audio_scene: model loaded (%d inputs, %d outputs)",
                len(inputs), len(outputs),
            )
            return True
        except Exception:
            log.exception(
                "audio_scene: failed to load model at %s",
                self._model_path,
            )
            return False

    def _run_inference(self, frame_bytes: bytes, device_id: str) -> None:
        """Worker-thread entry point. Decodes PCM → float32 [-1,1],
        runs the interpreter, walks the score tensor, emits whitelist
        hits that pass threshold + cooldown."""
        if not self._ensure_model():
            return
        if _np is None or self._interpreter is None:
            return
        try:
            pcm_i16 = _np.frombuffer(frame_bytes, dtype=_np.int16)
            if pcm_i16.size != self._frame_size:
                # Defensive — feed() should have sliced exactly one frame.
                return
            pcm_f32 = (pcm_i16.astype(_np.float32) / 32768.0)

            interp = self._interpreter
            # YAMNet's TFLite signature accepts a 1D waveform; some
            # exports want shape [N], others want [1, N]. Try the
            # cached shape first, fall back if the interpreter rejects.
            try:
                interp.set_tensor(self._input_index, pcm_f32)
            except (ValueError, TypeError):
                interp.set_tensor(
                    self._input_index, pcm_f32.reshape(1, -1),
                )
            interp.invoke()
            scores = interp.get_tensor(self._output_index)
            self._consume_scores(scores, device_id)
        except Exception:
            log.exception("audio_scene: inference failed")

    def _consume_scores(self, scores, device_id: str) -> None:
        """Reduce the YAMNet score tensor to a per-class vector and
        emit whitelist hits over threshold + outside cooldown.

        YAMNet's TFLite output is shape (frames, 521) for windowed
        inputs or (1, 521) / (521,) for single-frame. We mean-pool
        over any time axis, then walk the 521 entries."""
        if _np is None:
            return
        try:
            arr = _np.asarray(scores)
        except Exception:
            return
        if arr.ndim == 2:
            scores_1d = arr.mean(axis=0)
        elif arr.ndim == 1:
            scores_1d = arr
        else:
            scores_1d = arr.reshape(-1)

        threshold = self._threshold
        now = time.time()
        # Walk only the indices we know about — saves us from
        # iterating 521 floats every 0.96 s.
        for idx, label in YAMNET_CLASS_MAP.items():
            if idx >= scores_1d.shape[0]:
                continue
            try:
                conf = float(scores_1d[idx])
            except Exception:
                continue
            if conf < threshold:
                continue
            kind = _USEFUL_CLASSES.get(label)
            if kind is None:
                continue
            if not self._cooldown_clear(kind, now):
                continue
            self._emit(kind, conf, label, device_id, now)

    # ------------------------------------------------------------------
    # Cooldown + emit
    # ------------------------------------------------------------------
    def _cooldown_clear(self, kind: str, now: float) -> bool:
        """Return True if ``kind`` is past cooldown. Updates the
        last-emit timestamp atomically so two simultaneous high
        scores in the same frame don't both fire."""
        with self._last_emit_lock:
            last = self._last_emit.get(kind, 0.0)
            if now - last < self._cooldown_sec:
                return False
            self._last_emit[kind] = now
        return True

    def _emit(
        self,
        kind: str,
        confidence: float,
        raw_class: str,
        device_id: str,
        ts: float,
    ) -> None:
        """Hand a sound_event to the injected bus. Shape mirrors the
        existing perception event pipeline:

            {"device_id", "ts" (epoch float), "name": "sound_event",
             "data": {"kind", "confidence", "raw_class", "iso_ts"}}

        Includes both an epoch float ``ts`` (matches what
        ``_perception_broadcast`` already gets from the firmware path)
        and an ISO-8601 ``iso_ts`` inside ``data`` for human-readable
        logs / debug surfaces."""
        event = {
            "device_id": device_id,
            "ts": ts,
            "name": "sound_event",
            "data": {
                "kind": kind,
                "confidence": round(confidence, 4),
                "raw_class": raw_class,
                "iso_ts": _iso_now(),
                "source": "yamnet",
            },
        }
        log.info(
            "audio_scene: %s (raw=%r conf=%.3f device=%s)",
            kind, raw_class, confidence, device_id,
        )
        if self._bus is None:
            return
        try:
            self._bus(event)
        except Exception:
            log.exception("audio_scene: bus emit failed")

    # ------------------------------------------------------------------
    # Test / introspection helpers
    # ------------------------------------------------------------------
    @property
    def buffered_bytes(self) -> int:
        with self._buf_lock:
            return len(self._buf)

    @property
    def model_loaded(self) -> bool:
        return self._model_loaded

    @property
    def tflite_available(self) -> bool:
        return _TFLITE_AVAILABLE


__all__ = [
    "AudioSceneClassifier",
    "_USEFUL_CLASSES",
]
