"""One-off / manual: remove bad auto-created profiles and face samples."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parent
PROFILES_DIR = BASE / "profiles"
FACES_DIR = BASE / "faces"

ROGUE_PROFILES = ["do_you", "thats_correct_ava", "who_created_you", "ezekiel"]

for slug in ROGUE_PROFILES:
    path = PROFILES_DIR / f"{slug}.json"
    if path.exists():
        os.remove(path)
        print(f"Deleted rogue profile: {slug}")

face_dir = FACES_DIR / "who_created_you"
if face_dir.is_dir():
    shutil.rmtree(face_dir)
    print(f"Removed face samples: {face_dir}")
