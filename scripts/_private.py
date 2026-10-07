"""scripts/_private.py — standalone-script twin of brain/private_config.py (2026-10-07).

Usage from any file under scripts/:  from _private import priv
(scripts/ is sys.path[0] when a script runs directly). Same file, same env override, same rules:
config/private.local.json (git-ignored), IRIS_PRIV_<KEY_UPPER> wins, missing => default."""
from __future__ import annotations

import json
import os
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "config" / "private.local.json"


def priv(key: str, default: str = "") -> str:
    env = os.environ.get("IRIS_PRIV_" + key.upper())
    if env:
        return env
    try:
        v = json.loads(_PATH.read_text(encoding="utf-8")).get(key)
    except Exception:
        v = None
    return str(v) if v not in (None, "") else default
