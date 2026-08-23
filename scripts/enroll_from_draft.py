"""Enroll a NEW person from an unknown-capture draft, cropping them out.

Why this exists (2026-08-22): unknown_capture saves FULL FRAMES that contain
the known person (Zeke) AND the stranger. Feeding those straight into
faces/<person>/ poisons the new person's embedding with Zeke's face. This
script detects every face per frame, works out which one is NOT the known
person, and saves a padded crop of only that face.

Usage:
  python scripts/enroll_from_draft.py <draft_dir> <new_person_id> [known_id]
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
from insightface.app import FaceAnalysis

ROOT = Path(__file__).resolve().parents[1]
FACES = ROOT / "faces"


def _norm(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n else v


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    draft_dir = Path(sys.argv[1])
    new_id = sys.argv[2]
    known_id = sys.argv[3] if len(sys.argv) > 3 else "zeke"

    app = FaceAnalysis(name="buffalo_l")
    app.prepare(ctx_id=0, det_size=(640, 640))
    print(f"[provider] {app.models['recognition'].session.get_providers()}")

    # Reference embeddings for the KNOWN person, so we can tell the two apart.
    ref: list[np.ndarray] = []
    for p in sorted((FACES / known_id).glob("*.jpg")):
        img = cv2.imread(str(p))
        if img is None:
            continue
        fs = app.get(img)
        if len(fs) == 1:              # only unambiguous reference shots
            ref.append(_norm(fs[0].embedding))
    print(f"[ref] {known_id}: {len(ref)} reference embeddings")
    if not ref:
        print("!! no usable reference embeddings — cannot disambiguate")
        return 1

    out_dir = FACES / new_id
    out_dir.mkdir(parents=True, exist_ok=True)

    saved = 0
    for fp in sorted(draft_dir.glob("*.jpg")):
        img = cv2.imread(str(fp))
        if img is None:
            continue
        faces = app.get(img)
        if len(faces) < 2:
            print(f"  {fp.name}: only {len(faces)} face(s) — skipped")
            continue

        # max-similarity against ANY reference shot (per-photo, not averaged)
        scored = []
        for f in faces:
            e = _norm(f.embedding)
            sim = max(float(np.dot(e, r)) for r in ref)
            scored.append((sim, f))
        scored.sort(key=lambda t: t[0])
        sim, target = scored[0]           # least like the known person
        best_sim = scored[-1][0]
        print(f"  {fp.name}: stranger sim={sim:.3f} vs {known_id} sim={best_sim:.3f}")
        if sim > 0.45:
            print("    !! both faces look like the known person — skipping")
            continue

        x1, y1, x2, y2 = [int(v) for v in target.bbox]
        h, w = img.shape[:2]
        mx, my = int((x2 - x1) * 0.35), int((y2 - y1) * 0.35)
        crop = img[max(0, y1 - my):min(h, y2 + my), max(0, x1 - mx):min(w, x2 + mx)]
        if crop.size == 0:
            continue
        dest = out_dir / f"{new_id}_{fp.stem}.jpg"
        cv2.imwrite(str(dest), crop)
        saved += 1
        print(f"    -> {dest.name}  ({crop.shape[1]}x{crop.shape[0]})")

    print(f"\n[done] saved {saved} crop(s) to {out_dir}")
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
