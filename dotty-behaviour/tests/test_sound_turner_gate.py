"""SoundTurner is off unless SOUND_TURN_ENABLED=1 — the firmware localizer
is stuck-left (#27), so the turner only ever snapped the head left."""
from __future__ import annotations

import importlib

import config


def test_sound_turner_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("SOUND_TURN_ENABLED", raising=False)
    try:
        assert importlib.reload(config).SOUND_TURN_ENABLED is False
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_sound_turner_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("SOUND_TURN_ENABLED", "1")
    try:
        assert importlib.reload(config).SOUND_TURN_ENABLED is True
    finally:
        monkeypatch.undo()
        importlib.reload(config)
