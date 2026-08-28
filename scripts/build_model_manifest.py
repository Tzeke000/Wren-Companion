"""Rebuild assets/models3d/manifest.json: filename -> poly.pizza id, author, license.

Why this exists: the first download pass kept no manifest, so after a rename/prune
pass the per-file LICENSE mapping was lost — and CC-BY 3.0 legally requires
crediting the author in the video description. Zeke posts these publicly, so
"I think it's CC0" is not good enough.

The name->id mapping below is reconstructed from git history (the original
catalog table) plus the search-run log plus the explicit rename map. Every entry
is then VERIFIED by fetching its poly.pizza page — reconstruction is a guess
until the page confirms it.
"""
import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "assets" / "models3d"
MANIFEST = DEST / "manifest.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# reconstructed name -> poly.pizza short id
KNOWN = {
    "skull_kay": "7clFQGz5jH", "skull_quaternius": "YgmRBtFlcF",
    "skull_q3": "EAsVEJwsv7", "skull_q4": "DJT7N0HuVB",
    "skull_2": "oxtpAT0TaB", "skull_3": "52a7EEFzFWi", "skull_4": "738EKrsYz96",
    "heart": "8RA5hHU5gHK", "heart_2": "1yCRUwFnwX",
    "fire": "1QpMTUO7P-G", "campfire": "0vzzmM-t8CP", "red_car": "dVLJ5CjB0h",
    "diamond": "5SvQ6iU_CHg", "gem_green": "kbgiCMzdxg", "jewel": "velVo80s1D",
    "lightning_bolt": "7I1IhiE7O8s", "lightning_bolt_2": "8rX_fFhz6XH",
    "planets_set": "3_tN7i962hZ", "sun": "77wHkzwlpOq",
    "crystal_1": "2c3eNMf_J4y", "crystal_2": "3saqXqoOti", "crystal_3": "5wGpIRD2AKD",
    "dome": "0vAOG_PcdNb", "top_hat": "2Givq4Q3YTH", "harp": "102E7hcxEPT",
    "phone": "1L9oJAw6nY2", "headphones_3": "2nWENCU3DsE",
    "rocket_1": "1Xid2Qhqn2s", "crown_1": "1PP65IsuNwv", "crown_2": "3uTiBmJOhB0", "hat_1": "5D_ieYZ0HUp",
    "wolf_1": "10u8FYPC5Br", "wolf_3": "2LZMldgUhTj",
    "dragonfly": "0myA_BOcZrD", "figure": "7oO8liwQv6",
    "moon_1": "10VNaCg1jYM", "moon_2": "63c8LKpoXTO", "moon_3": "exMi1Wb_iEU",
    "star_1": "0ddNZ3EsIhw", }


def page_info(sid: str):
    """-> (author, license) scraped from the model page, or (None, None)."""
    try:
        r = requests.get(f"https://poly.pizza/m/{sid}", headers=UA, timeout=25)
        if r.status_code != 200:
            return None, None
        txt = r.text
        lic = "CC0" if "CC0" in txt else (
            "CC-BY 3.0" if re.search(r"CC[- ]?BY", txt) else "UNKNOWN")
        # the author link is the reliable anchor. NOT <title> — poly.pizza
        # renders it as `<title data-react-helmet="true">`, so a plain
        # `<title>` regex silently matches nothing and every author came back
        # as "?" (which would have made every CC-BY model unusable, since
        # attribution needs a NAME).
        a = re.search(r'href="/u/([^"]{1,60})"', txt)
        if not a:
            a = re.search(r"Model By ([^<]{1,50}?) - Poly Pizza", txt)
        return (a.group(1).strip() if a else None), lic
    except Exception as e:
        print(f"    {sid}: {e!r}")
        return None, None


def main() -> int:
    on_disk = sorted(p.stem for p in DEST.glob("*.glb"))
    old = {}
    if MANIFEST.exists():
        old = json.loads(MANIFEST.read_text(encoding="utf-8"))

    if "--prune" in sys.argv:
        # Drop entries whose .glb is gone. fetch_models.record() appends but
        # never removes, so deleting a model leaves a ghost entry — and a ghost
        # with license UNKNOWN raises a false "DO NOT PUBLISH" on a model that
        # is not even in the render. Cheap, no network.
        keep = {k: v for k, v in old.items() if k in set(on_disk)}
        dropped = sorted(set(old) - set(keep))
        MANIFEST.write_text(json.dumps(keep, indent=2, sort_keys=True),
                            encoding="utf-8")
        print(f"pruned {len(dropped)} stale entries: {', '.join(dropped) or '(none)'}")
        print(f"{len(keep)} entries remain")
        return 0

    out = {}
    for name in on_disk:
        sid = KNOWN.get(name) or old.get(name, {}).get("id")
        if not sid:
            out[name] = {"id": None, "author": None, "license": "UNKNOWN",
                         "note": "no id on record - re-check before public use"}
            print(f"  {name:22s} NO ID ON RECORD -> UNKNOWN")
            continue
        author, lic = page_info(sid)
        out[name] = {"id": sid, "author": author, "license": lic or "UNKNOWN"}
        print(f"  {name:22s} {sid:14s} {lic or '?':10s} {author or '?'}")
        time.sleep(0.3)                 # be polite to poly.pizza
    MANIFEST.write_text(json.dumps(out, indent=2, sort_keys=True), encoding="utf-8")
    cc0 = sum(1 for v in out.values() if v["license"] == "CC0")
    ccby = sum(1 for v in out.values() if v["license"].startswith("CC-BY"))
    unk = len(out) - cc0 - ccby
    print(f"\n{len(out)} models: {cc0} CC0, {ccby} CC-BY, {unk} unknown")
    print(f"wrote {MANIFEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
