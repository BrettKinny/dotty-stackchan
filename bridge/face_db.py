"""
Face embedding store — Layer 4 (server-side variant).

Persists per-person face embeddings + names in a SQLite file on the RPi
bridge. Pure SQLite + numpy; no async surface here — the embedding
service (`face_recognizer.py`) wraps these calls in `run_in_executor`
since SQLite calls are non-trivial on slower SD-cards.

Privacy contract (see `stackchan-fw/firmware/main/stackchan/face/PRIVACY.md`):
biometric data stays on the LAN. The DB file is mode 0600 and lives
alongside the existing per-day conversation logs in `~/.zeroclaw/`.
There is no soft-delete — `forget()` is destructive and immediate.

Schema
------
``faces`` table keys names case-insensitively (``COLLATE NOCASE``) so
``Brett`` and ``brett`` collide on enrollment. Embeddings are stored
as raw float32 bytes (`numpy.tobytes()`) — fixed length per row
(``FaceDB.EMBEDDING_DIM`` floats), so corruption checks reduce to
``len(blob) == EMBEDDING_DIM * 4``.

Capacity
--------
Soft-capped at ``FaceDB.CAPACITY`` enrolled identities. Past the cap
``enroll()`` returns ``{"ok": False, "error": "capacity_reached"}``
without touching the DB. The default is generous (50) — enough for
extended family + frequent visitors — but the embedding library's
nearest-neighbour search is O(N) over enrolled rows, which is fine
at this scale.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger("zeroclaw-bridge.face_db")


class FaceDB:
    # face_recognition's `face_encodings()` returns 128-d float64. We
    # downcast to float32 on insert — half the storage, no measurable
    # accuracy loss for cosine-similarity matching at this dimensionality.
    EMBEDDING_DIM = 128
    EMBEDDING_DTYPE = np.float32

    # Soft cap. Enforced in `enroll()`; not a hard schema constraint.
    CAPACITY = 50

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS faces (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
        embedding   BLOB    NOT NULL,
        created_at  REAL    NOT NULL,
        updated_at  REAL    NOT NULL,
        n_samples   INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX IF NOT EXISTS idx_faces_name ON faces(name);
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Touch the file so we can chmod before sqlite writes anything
        # sensitive into it; mode is preserved on subsequent opens.
        if not self._path.exists():
            self._path.touch(mode=0o600)
        else:
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                log.warning("face_db: chmod 0600 failed on %s", self._path,
                            exc_info=True)
        with self._connect() as conn:
            conn.executescript(self.SCHEMA)

    # ------------------------------------------------------------------
    # Connection helper
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        # `isolation_level=None` keeps us in autocommit; we use explicit
        # BEGIN/COMMIT for the multi-step write paths. `check_same_thread`
        # is False because the recognizer service hops connections
        # across executor threads.
        conn = sqlite3.connect(
            self._path,
            isolation_level=None,
            check_same_thread=False,
            timeout=5.0,
        )
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def enroll(self, name: str, embedding: np.ndarray) -> dict:
        """Insert or update a single identity.

        Returns a dict ``{"ok": bool, "name": str, "error": str?}``. The
        ``name`` field reflects the canonical (Title-cased) spelling
        we persisted. On capacity-reached the DB is unchanged.
        """
        canonical = self._canonical_name(name)
        if canonical is None:
            return {"ok": False, "error": "invalid_name"}
        blob = self._embedding_to_blob(embedding)
        if blob is None:
            return {"ok": False, "error": "invalid_embedding"}

        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                row = conn.execute(
                    "SELECT id FROM faces WHERE name = ? COLLATE NOCASE",
                    (canonical,),
                ).fetchone()
                if row is None:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM faces"
                    ).fetchone()[0]
                    if count >= self.CAPACITY:
                        conn.execute("ROLLBACK")
                        return {"ok": False, "error": "capacity_reached"}
                    conn.execute(
                        "INSERT INTO faces (name, embedding, created_at, "
                        "updated_at, n_samples) VALUES (?, ?, ?, ?, 1)",
                        (canonical, blob, now, now),
                    )
                    action = "created"
                else:
                    conn.execute(
                        "UPDATE faces SET embedding = ?, updated_at = ?, "
                        "n_samples = n_samples + 1 WHERE id = ?",
                        (blob, now, row["id"]),
                    )
                    action = "updated"
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                log.exception("face_db: enroll failed for %s", canonical)
                return {"ok": False, "error": "db_error"}
        log.info("face_db: enroll %s name=%s", action, canonical)
        return {"ok": True, "name": canonical, "action": action}

    def forget(self, name: str) -> bool:
        """Remove a single identity. Returns True if a row was deleted."""
        canonical = self._canonical_name(name)
        if canonical is None:
            return False
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM faces WHERE name = ? COLLATE NOCASE",
                (canonical,),
            )
            deleted = cur.rowcount > 0
        if deleted:
            log.info("face_db: forget name=%s", canonical)
        return deleted

    def forget_all(self) -> int:
        """Wipe the whole table. Returns the number of rows deleted."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM faces")
            n = cur.rowcount
        log.info("face_db: forget_all deleted=%d", n)
        return n

    def list_names(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name FROM faces ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [r["name"] for r in rows]

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0]

    def match(
        self,
        embedding: np.ndarray,
        threshold: float,
    ) -> tuple[str, float]:
        """Return the closest enrolled identity by cosine similarity.

        Returns ``(name, similarity)`` where ``name`` is the matched
        identity or ``"unknown"`` if no row's similarity meets
        ``threshold`` (or the DB is empty). ``similarity`` is the best
        score seen — useful for logging even on a miss.
        """
        query = self._normalize(embedding)
        if query is None:
            return ("unknown", 0.0)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, embedding FROM faces"
            ).fetchall()
        if not rows:
            return ("unknown", 0.0)
        best_name = "unknown"
        best_sim = -1.0
        for row in rows:
            stored = self._blob_to_embedding(row["embedding"])
            if stored is None:
                continue
            sim = float(np.dot(query, stored))  # both unit vectors
            if sim > best_sim:
                best_sim = sim
                best_name = row["name"]
        if best_sim >= threshold:
            return (best_name, best_sim)
        return ("unknown", best_sim)

    # ------------------------------------------------------------------
    # Validation / encoding helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _canonical_name(name: str) -> Optional[str]:
        if not isinstance(name, str):
            return None
        cleaned = name.strip()
        if len(cleaned) < 2 or len(cleaned) > 32:
            return None
        # Lowercase denylist of pronouns / fillers that the ASR will
        # frequently hand us if regex extraction goes wrong.
        if cleaned.lower() in {"me", "myself", "i", "the", "you", "him",
                               "her", "them", "us", "we"}:
            return None
        # Title-case for display consistency. Names with apostrophes
        # ("O'Brien") or hyphens ("Mary-Jane") survive .title() well
        # enough for our purposes.
        return cleaned.title()

    @classmethod
    def _embedding_to_blob(cls, embedding: np.ndarray) -> Optional[bytes]:
        normed = cls._normalize(embedding)
        if normed is None:
            return None
        return normed.astype(cls.EMBEDDING_DTYPE).tobytes()

    @classmethod
    def _blob_to_embedding(cls, blob: bytes) -> Optional[np.ndarray]:
        expected = cls.EMBEDDING_DIM * np.dtype(cls.EMBEDDING_DTYPE).itemsize
        if len(blob) != expected:
            log.warning(
                "face_db: skipping malformed embedding (len=%d expected=%d)",
                len(blob), expected,
            )
            return None
        arr = np.frombuffer(blob, dtype=cls.EMBEDDING_DTYPE)
        # Stored vectors are pre-normalised; cosine = dot product.
        return arr

    @classmethod
    def _normalize(cls, embedding: np.ndarray) -> Optional[np.ndarray]:
        if embedding is None:
            return None
        try:
            arr = np.asarray(embedding, dtype=np.float64).ravel()
        except (TypeError, ValueError):
            return None
        if arr.size != cls.EMBEDDING_DIM:
            log.warning(
                "face_db: embedding size %d != expected %d",
                arr.size, cls.EMBEDDING_DIM,
            )
            return None
        norm = float(np.linalg.norm(arr))
        if norm < 1e-9:
            return None
        return (arr / norm).astype(cls.EMBEDDING_DTYPE)


__all__ = ["FaceDB"]
