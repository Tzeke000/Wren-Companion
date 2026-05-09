def print_startup_selftest(g: dict):
    try:
        from brain.face_recognizer import get_recognizer
        from pathlib import Path
        _fr = get_recognizer(Path(g.get("BASE_DIR") or "."))
        face_recognizer_ok = _fr is not None and _fr.available
    except Exception:
        face_recognizer_ok = False
    checks = {
        "vector_memory": "vectorstore" in g and g["vectorstore"] is not None,
        "mood_path": g.get("MOOD_PATH") is not None,
        "personality_path": g.get("PERSONALITY_PATH") is not None,
        "face_recognizer": face_recognizer_ok,
    }
    status = "OK" if all(checks.values()) else "DEGRADED"
    details = " | ".join(f"{k}={'ok' if v else 'missing'}" for k, v in checks.items())
    print(f"[startup-selftest-stage6] {status} :: {details}")
