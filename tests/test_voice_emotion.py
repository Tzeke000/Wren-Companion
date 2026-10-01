"""Round-2 fix 2.1 — emotion → prosody math (pure). The live A/B is scripts/voice_emotion_ab.py.

    .venv\\Scripts\\python.exe tests/test_voice_emotion.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain import voice_emotion as ve  # noqa: E402

BASE = dict(base_speed=1.28, base_beta=0.5, base_scale=1.0, enabled=True)


def test_no_emotion_is_byte_identical_to_base() -> None:
    for label in (None, "", "neutral", "NEUTRAL", "garbage-label", "   "):
        p = ve.emotion_params(label, 1.0, **BASE)
        assert (p["speed"], p["beta"], p["embedding_scale"]) == (1.28, 0.5, 1.0), (label, p)
        assert p["applied"] is False and p["label"] == "neutral"


def test_disabled_flag_and_zero_intensity_are_base_too() -> None:
    p = ve.emotion_params("joy", 1.0, base_speed=1.28, base_beta=0.5, base_scale=1.0, enabled=False)
    assert p["applied"] is False and p["speed"] == 1.28
    p0 = ve.emotion_params("joy", 0.0, **BASE)
    assert p0["applied"] is False and p0["speed"] == 1.28


def test_aliases_normalise() -> None:
    assert ve.normalize_emotion("calmness") == "calm"
    assert ve.normalize_emotion("Frustration") == "anger"
    assert ve.normalize_emotion("curiosity") == "interest"
    assert ve.normalize_emotion("warmth") == "tenderness"
    assert ve.normalize_emotion("melancholy") == "sadness"
    assert ve.normalize_emotion("boredom") == "tired"
    assert ve.normalize_emotion("nonsense") == "neutral"


def test_intensity_scales_linearly_and_direction_is_right() -> None:
    full = ve.emotion_params("joy", 1.0, **BASE)
    half = ve.emotion_params("joy", 0.5, **BASE)
    assert full["applied"] and half["applied"]
    assert full["speed"] > half["speed"] > 1.28, (full, half)
    assert abs((half["speed"] - 1.28) - (full["speed"] - 1.28) / 2) < 1e-6
    assert abs((half["beta"] - 0.5) - (full["beta"] - 0.5) / 2) < 1e-6
    sad = ve.emotion_params("sadness", 1.0, **BASE)
    assert sad["speed"] < 1.28 and sad["beta"] < 0.5, sad
    assert full["speed"] > 1.28 > sad["speed"], "joy must be faster than sadness"


def test_clamps_hold_the_locked_pace_near_base() -> None:
    for label in ve.PRESETS:
        p = ve.emotion_params(label, 1.0, **BASE)
        assert 1.28 * 0.92 - 1e-9 <= p["speed"] <= 1.28 * 1.08 + 1e-9, (label, p)
        assert 0.30 <= p["beta"] <= 0.80, (label, p)
        assert 1.0 <= p["embedding_scale"] <= 1.8, (label, p)
    # an absurd intensity is clamped to 1.0, not extrapolated
    p = ve.emotion_params("anger", 7.0, **BASE)
    assert p == ve.emotion_params("anger", 1.0, **BASE)
    assert ve.emotion_params("anger", "not-a-number", **BASE) == ve.emotion_params("anger", 0.5, **BASE)


def test_mood_to_emotion_reads_mood_core_shape() -> None:
    mood = {"current_mood": "interest", "primary_emotions": [{"name": "interest", "percent": 77},
                                                             {"name": "calmness", "percent": 23}]}
    assert ve.mood_to_emotion(mood) == ("interest", 0.77)
    assert ve.mood_to_emotion({"primary_emotions": [{"name": "joy", "percent": 60}]}) == ("joy", 0.6)
    assert ve.mood_to_emotion({"current_mood": "calm"}) == ("calm", 0.5)
    assert ve.mood_to_emotion({}) == (None, 0.5)
    assert ve.mood_to_emotion(None) == (None, 0.5)
    label, inten = ve.mood_to_emotion({"current_mood": "sadness", "primary_emotions": [{"name": "sadness", "percent": 140}]})
    assert inten == 1.0


def test_queue_items_round_trip_and_legacy_strings_survive() -> None:
    assert ve.make_item("hello") == "hello"
    item = ve.make_item("hello", "joy", 0.8)
    assert item == {"text": "hello", "emotion": "joy", "intensity": 0.8}
    assert ve.item_fields(item) == ("hello", "joy", 0.8)
    assert ve.item_fields("plain") == ("plain", None, None)
    assert ve.item_text(item) == "hello" and ve.item_text("x") == "x"
    assert ve.item_fields({"text": "t", "emotion": "", "intensity": "bad"}) == ("t", None, None)


def test_describe() -> None:
    assert ve.describe(None) == "base"
    assert ve.describe(ve.emotion_params("neutral", 1.0, **BASE)) == "base"
    d = ve.describe(ve.emotion_params("sadness", 1.0, **BASE))
    assert d.startswith("sadness: speed=") and "beta=" in d


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e!r}")
    sys.exit(1 if fails else 0)
