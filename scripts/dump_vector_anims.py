"""One-off: dump the authoritative animation TRIGGER + animation names off THIS
Vector, so the reaction/reflex layer can map recipes onto real on-device triggers.
Run with the venv python:  .venv/Scripts/python scripts/dump_vector_anims.py
"""
import sys, json, contextlib

SERIAL = "0dd1cdaf"

def main():
    import anki_vector
    out = {"triggers": [], "anims": [], "error": None}
    try:
        with anki_vector.Robot(SERIAL, cache_animation_lists=True,
                               default_logging=False) as r:
            with contextlib.suppress(Exception):
                out["triggers"] = sorted(list(r.anim.anim_trigger_list))
            with contextlib.suppress(Exception):
                out["anims"] = sorted(list(r.anim.anim_list))
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    print(json.dumps(out, indent=1))

if __name__ == "__main__":
    main()
