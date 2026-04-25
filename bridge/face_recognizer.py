"""
Face recognizer — Layer 4 server-side embedding + matching service.

Wraps the ``face_recognition`` library (dlib-backed, 128-d embeddings)
behind a small async surface so the FastAPI bridge can call it from
request handlers without blocking the event loop.

Why ``face_recognition`` vs ``insightface``
-------------------------------------------
* Pre-built ARM wheels via piwheels — ``pip install`` finishes in
  seconds on DietPi rather than spending an hour compiling dlib.
* 128-d embeddings keep ``faces.sqlite`` tiny (≤ 50 rows × 512 B).
* Clean Python API; the encoder is a single function call.

Trade-off: dlib's HOG detector is weaker than ArcFace on kid faces,
hats, and side-profile shots. If false-negative rate exceeds 10 %
in bench testing, swap this module for an insightface-based one —
the public surface (``enroll``, ``recognize``, ``forget``,
``list_names``) is small enough that the swap is localised.

Threading model
---------------
``face_recognition.face_locations`` and ``face_recognition.face_encodings``
are heavy CPU calls (200–800 ms each on RPi 4). We marshal them onto
a single-worker ``ThreadPoolExecutor`` so:

1. The FastAPI event loop stays responsive.
2. Concurrent /api/face/recognize calls serialise rather than thrash
   the CPU. (Throttling on the caller side caps the rate at ≤1 call
   per device per ~10 s anyway.)

If the optional ``face_recognition`` import fails — likely on a fresh
checkout where the user hasn't run ``pip install`` yet — the service
boots in a degraded mode where every call returns
``{"ok": False, "error": "module_unavailable"}``. The bridge stays
bootable; this is consistent with how ``audio_scene.py`` handles the
optional ``tflite-runtime`` dependency.

Environment variables
---------------------
* ``FACE_DB_PATH`` — SQLite location. Default
  ``~/.zeroclaw/faces.sqlite``.
* ``FACE_MATCH_THRESHOLD`` — cosine similarity required for a positive
  match. Default ``0.5`` (roughly equivalent to face_recognition's
  built-in ``tolerance=0.6`` distance threshold, expressed as
  similarity).
* ``FACE_DETECTOR_MODEL`` — ``"hog"`` (CPU, default) or ``"cnn"`` (CUDA;
  not applicable on the Pi). Pass through to ``face_locations``.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import numpy as np

from .face_db import FaceDB

log = logging.getLogger("zeroclaw-bridge.face_recognizer")

# Optional heavy deps — keep the import survivable so the bridge
# boots without them (operator can install later, then restart).
try:
    import face_recognition  # type: ignore[import-not-found]
    from PIL import Image     # type: ignore[import-not-found]
    _FACE_RECOGNITION_OK = True
except Exception as exc:  # pragma: no cover — environment-dependent
    log.warning(
        "face_recognizer: face_recognition import failed (%s); "
        "service will run in degraded mode (all calls return "
        "module_unavailable). Install with: pip install "
        "face-recognition --extra-index-url "
        "https://www.piwheels.org/simple",
        exc,
    )
    face_recognition = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]
    _FACE_RECOGNITION_OK = False


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("face_recognizer: invalid %s=%r; using default %s",
                    key, raw, default)
        return default


class FaceRecognizerService:
    """Async wrapper around ``face_recognition`` + ``FaceDB``.

    All public methods return a dict in the shape ``{"ok": bool, ...}``
    so the FastAPI handlers can pass-through to JSON without further
    massaging.
    """

    def __init__(
        self,
        db: FaceDB,
        *,
        threshold: Optional[float] = None,
        detector_model: Optional[str] = None,
    ) -> None:
        self._db = db
        self._threshold = (
            threshold if threshold is not None
            else _env_float("FACE_MATCH_THRESHOLD", 0.5)
        )
        self._detector_model = (
            detector_model
            or os.environ.get("FACE_DETECTOR_MODEL", "hog")
        ).strip().lower()
        if self._detector_model not in ("hog", "cnn"):
            log.warning("face_recognizer: unknown detector %r; using hog",
                        self._detector_model)
            self._detector_model = "hog"
        # Single-worker executor so heavy face_recognition calls
        # serialise rather than thrash the Pi's 4 cores.
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="face-rec"
        )
        log.info(
            "face_recognizer: ready (threshold=%.2f detector=%s available=%s)",
            self._threshold, self._detector_model, _FACE_RECOGNITION_OK,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------
    # Public async surface
    # ------------------------------------------------------------------
    async def enroll(self, name: str, jpeg_bytes: bytes) -> dict:
        if not _FACE_RECOGNITION_OK:
            return {"ok": False, "error": "module_unavailable"}
        embedding = await self._detect_and_embed(jpeg_bytes)
        if embedding is None:
            return {"ok": False, "error": "no_face_detected"}
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            self._executor, self._db.enroll, name, embedding,
        )
        return result

    async def recognize(self, jpeg_bytes: bytes) -> dict:
        if not _FACE_RECOGNITION_OK:
            return {"ok": False, "error": "module_unavailable",
                    "name": "unknown", "confidence": 0.0}
        embedding = await self._detect_and_embed(jpeg_bytes)
        if embedding is None:
            return {"ok": True, "name": "unknown", "confidence": 0.0,
                    "reason": "no_face_detected"}
        loop = asyncio.get_running_loop()
        name, sim = await loop.run_in_executor(
            self._executor, self._db.match, embedding, self._threshold,
        )
        return {"ok": True, "name": name, "confidence": float(sim)}

    async def forget(self, name: str) -> dict:
        loop = asyncio.get_running_loop()
        if name == "*":
            n = await loop.run_in_executor(
                self._executor, self._db.forget_all,
            )
            return {"ok": True, "deleted": n}
        deleted = await loop.run_in_executor(
            self._executor, self._db.forget, name,
        )
        return {"ok": deleted, "name": name}

    async def list_names(self) -> dict:
        loop = asyncio.get_running_loop()
        names, count = await loop.run_in_executor(
            self._executor, self._list_sync,
        )
        return {
            "ok": True,
            "names": names,
            "count": count,
            "capacity": FaceDB.CAPACITY,
        }

    def _list_sync(self) -> tuple[list[str], int]:
        return (self._db.list_names(), self._db.count())

    # ------------------------------------------------------------------
    # Detection + embedding (CPU-bound, run in executor)
    # ------------------------------------------------------------------
    async def _detect_and_embed(
        self, jpeg_bytes: bytes,
    ) -> Optional[np.ndarray]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, self._detect_and_embed_sync, jpeg_bytes,
        )

    def _detect_and_embed_sync(
        self, jpeg_bytes: bytes,
    ) -> Optional[np.ndarray]:
        """Decode → detect → embed. Returns the largest face's
        encoding, or ``None`` if no face was detected or decoding
        failed."""
        if not jpeg_bytes:
            return None
        # Caller is gated on _FACE_RECOGNITION_OK; assert for the
        # type-checker's flow analysis so the None-fallbacks above
        # don't bleed into here.
        assert _FACE_RECOGNITION_OK and Image is not None \
            and face_recognition is not None
        try:
            img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        except Exception:
            log.warning("face_recognizer: JPEG decode failed",
                        exc_info=True)
            return None
        arr = np.asarray(img)
        try:
            locations = face_recognition.face_locations(
                arr, model=self._detector_model,
            )
        except Exception:
            log.exception("face_recognizer: face_locations failed")
            return None
        if not locations:
            return None
        # Pick the largest bounding box — for enrollment the user is
        # the closest subject; for recognition the closest face is
        # the most likely speaker.
        largest = max(
            locations,
            key=lambda loc: (loc[2] - loc[0]) * (loc[1] - loc[3]),
        )
        try:
            encodings = face_recognition.face_encodings(
                arr, known_face_locations=[largest], num_jitters=1,
            )
        except Exception:
            log.exception("face_recognizer: face_encodings failed")
            return None
        if not encodings:
            return None
        return encodings[0]


def default_db_path() -> Path:
    """Resolve the default SQLite location, honouring ``FACE_DB_PATH``."""
    raw = os.environ.get("FACE_DB_PATH", "~/.zeroclaw/faces.sqlite")
    return Path(raw).expanduser()


__all__ = ["FaceRecognizerService", "default_db_path"]
