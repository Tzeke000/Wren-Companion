"""Print the attribution line required for the 3D models used in a render.

    python scripts/model_credits.py --shape "model:skull_kay,model:heart,orb"
    python scripts/model_credits.py skull_kay heart_2
    python scripts/model_credits.py --all

CC-BY 3.0 models legally require crediting the author wherever the video is
published. CC0 models require nothing. This reads assets/models3d/manifest.json
and emits a paste-ready line naming only the models that actually need it — so
the caption stays short and is never wrong.

Exit code 2 if any requested model has an UNKNOWN licence: that is a stop sign,
not a warning. Do not publish a model whose licence could not be verified.
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "assets" / "models3d" / "manifest.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*")
    ap.add_argument("--shape", default="", help="a lyric_viz --shape string")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    if not MANIFEST.exists():
        print("no manifest.json — run scripts/build_model_manifest.py", file=sys.stderr)
        return 2
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))

    names = list(args.names)
    if args.shape:
        names += [m.split(":", 1)[1] for m in
                  re.split(r"[,\s]+", args.shape.strip())
                  if m.startswith("model:")]
    if args.all:
        names = sorted(man)
    names = [n for n in dict.fromkeys(names) if n]
    if not names:
        ap.error("give model names, --shape, or --all")

    missing = [n for n in names if n not in man]
    unknown = [n for n in names if man.get(n, {}).get("license") == "UNKNOWN"]
    need = {}
    free = []
    for n in names:
        e = man.get(n)
        if not e:
            continue
        if e["license"] == "CC0":
            free.append(n)
        elif e["license"].startswith("CC-BY"):
            need.setdefault(e["author"] or "unknown author", []).append(n)

    if free:
        print(f"CC0 (no credit needed): {', '.join(sorted(free))}")
    if missing:
        print(f"NOT IN MANIFEST: {', '.join(missing)}", file=sys.stderr)
    if unknown:
        print(f"UNKNOWN LICENCE — DO NOT PUBLISH: {', '.join(unknown)}",
              file=sys.stderr)

    if need:
        authors = sorted(need)
        print("\n--- paste into the video description ---")
        print("3D models by " + ", ".join(authors)
              + " (CC-BY 3.0) via poly.pizza")
        print("---")
        for a in authors:
            print(f"  {a}: {', '.join(sorted(need[a]))}")
    elif not unknown and not missing:
        print("\nNothing to credit — every model used is CC0.")

    return 2 if (unknown or missing) else 0


if __name__ == "__main__":
    raise SystemExit(main())
