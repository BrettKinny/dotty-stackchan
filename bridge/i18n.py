"""Minimal i18n helper — placeholder t() wrapper for UI strings.

Usage:
    from bridge.i18n import t

    msg = t("bridge.no_device")
    msg = t("perception.sensor_stale", threshold=30)

The active locale is read from the DOTTY_LANG env var (default "en").
Locale files live in locales/<lang>.json at the repo root.

Falls back: active locale -> "en" -> bare key (never raises).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_LOCALE_DIR = Path(__file__).parent.parent / "locales"
_cache: dict[str, dict[str, str]] = {}


def _load(lang: str) -> dict[str, str]:
    if lang not in _cache:
        path = _LOCALE_DIR / f"{lang}.json"
        try:
            _cache[lang] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _cache[lang] = {}
    return _cache[lang]


def t(key: str, lang: str = "", **kwargs: Any) -> str:
    """Look up a UI string by dotted key.

    Falls back to English, then to the bare key if not found.
    Supports keyword interpolation: t("foo.bar", name="World").
    """
    active = lang.strip() or os.environ.get("DOTTY_LANG", "en").strip() or "en"
    text = _load(active).get(key) or _load("en").get(key) or key
    return text.format(**kwargs) if kwargs else text


def reload_cache() -> None:
    """Drop all cached locale data (call after editing locale files)."""
    _cache.clear()
