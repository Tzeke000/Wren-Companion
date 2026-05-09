"""brain/windows_use/volume_control.py — pycaw + virtual-key fallback.

Three operations:
    set_volume_percent(pct) — precision via pycaw scalar
    volume_up() / volume_down() / volume_mute() — virtual keys

pycaw is the preferred path; if it's unavailable for any reason we fall
back to keyboard volume keys (no precision, but functional).
"""
from __future__ import annotations

import time
from typing import Any


def _endpoint_volume():
    """Return the IAudioEndpointVolume COM pointer for the default
    speakers, or None if pycaw isn't usable in this environment.

    pycaw 2025+ exposes IAudioEndpointVolume via AudioDevice.EndpointVolume —
    older recipes that call .Activate(IAudioEndpointVolume._iid_, ...) on
    the GetSpeakers() return no longer work because GetSpeakers now
    returns an AudioDevice wrapper rather than the raw IMMDevice.
    """
    try:
        from pycaw.pycaw import AudioUtilities  # type: ignore
        return AudioUtilities.GetSpeakers().EndpointVolume
    except Exception:
        return None


def set_volume_percent(pct: int) -> bool:
    """Set master output volume to pct (0-100). Scalar is 0.0-1.0."""
    pct = max(0, min(100, int(pct)))
    ev = _endpoint_volume()
    if ev is None:
        return False
    try:
        ev.SetMasterVolumeLevelScalar(pct / 100.0, None)
        return True
    except Exception:
        return False


def get_volume_percent() -> int | None:
    ev = _endpoint_volume()
    if ev is None:
        return None
    try:
        return int(round(ev.GetMasterVolumeLevelScalar() * 100))
    except Exception:
        return None


def _vk_press(key: str) -> bool:
    """Press a single uiautomation virtual key by name."""
    try:
        import uiautomation as auto
        auto.SendKeys(key)
        return True
    except Exception:
        return False


def volume_up() -> bool:
    return _vk_press("{VOLUME_UP}")


def volume_down() -> bool:
    return _vk_press("{VOLUME_DOWN}")


def volume_mute() -> bool:
    return _vk_press("{VOLUME_MUTE}")
