"""Download poly.pizza models as .glb into assets/models3d, wireframe-vetted.

    python scripts/fetch_models.py <name>:<polyPizzaId> [...]
    python scripts/fetch_models.py --from-catalog        # every ID in the doc

Why a script and not hand-curl: the short ID in a poly.pizza URL is NOT the
asset name. `static.poly.pizza/<shortid>.glb` 403s. The page embeds the real
`static.poly.pizza/<uuid>.glb`, so you must scrape the page first (learned
2026-08-28).

Vetting matters as much as downloading: these render as WIREFRAMES, every mesh
edge drawn as a line. Verified by eye — low-poly reads as the object, dense
meshes collapse into an unreadable ball of lines. So anything outside
MIN_EDGES..MAX_EDGES is rejected and deleted rather than left to disappoint
someone later.
"""
import argparse
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "assets" / "models3d"
CATALOG = ROOT / "docs" / "3d_model_catalog_2026-08-27.md"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
MIN_EDGES, MAX_EDGES = 40, 4000
_UUID = re.compile(r"static\.poly\.pizza/([0-9a-f-]{36})\.glb")


def resolve(short_id: str) -> "str | None":
    return (resolve_full(short_id) or (None, None, None))[0]


def resolve_full(short_id: str):
    """-> (uuid, title, license) from the model page, or None."""
    try:
        r = requests.get(f"https://poly.pizza/m/{short_id}", headers=UA, timeout=25)
        m = _UUID.search(r.text)
        if not m:
            return None
        t = re.search(r"<title>([^<]{1,120})</title>", r.text)
        title = t.group(1).split("|")[0].split(" by ")[0].strip() if t else short_id
        lic = "CC0" if "CC0" in r.text else (
            "CC-BY" if re.search(r"CC[- ]BY|Creative Commons", r.text) else "?")
        return m.group(1), title, lic
    except Exception as e:
        print(f"    resolve failed: {e!r}")
        return None


def search_ids(term: str) -> "list[str]":
    try:
        r = requests.get(f"https://poly.pizza/search/{term}", headers=UA, timeout=30)
        return sorted(set(re.findall(r"/m/([A-Za-z0-9_-]{8,14})", r.text)))
    except Exception as e:
        print(f"  search '{term}' failed: {e!r}")
        return []


def edge_count(path: Path) -> "tuple[int, int] | None":
    try:
        import numpy as np
        import trimesh
        m = trimesh.load(str(path), force="mesh")
        f = np.asarray(m.faces)
        e = np.unique(np.sort(np.concatenate(
            [f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1), axis=0)
        return len(m.vertices), len(e)
    except Exception as e:
        print(f"    mesh check failed: {e!r}")
        return None


def fetch(name: str, short_id: str) -> bool:
    out = DEST / f"{name}.glb"
    if out.exists():
        print(f"  {name}: already present, skipping")
        return False
    uuid = resolve(short_id)
    if not uuid:
        print(f"  {name} ({short_id}): NO uuid on page — skipped")
        return False
    try:
        r = requests.get(f"https://static.poly.pizza/{uuid}.glb",
                         headers=UA, timeout=60)
    except Exception as e:
        print(f"  {name}: download failed {e!r}")
        return False
    if r.status_code != 200 or r.content[:4] != b"glTF":
        # an S3 error page starts with '<?xm', not 'glTF'
        print(f"  {name}: HTTP {r.status_code}, magic={r.content[:4]!r} — rejected")
        return False
    out.write_bytes(r.content)
    vc = edge_count(out)
    if vc is None:
        out.unlink(missing_ok=True)
        print(f"  {name}: unloadable — deleted")
        return False
    verts, edges = vc
    if not (MIN_EDGES <= edges <= MAX_EDGES):
        out.unlink(missing_ok=True)
        print(f"  {name}: {edges} edges outside {MIN_EDGES}-{MAX_EDGES} "
              f"(would render as mush/nothing) — deleted")
        return False
    # ASCII only: this repo's stdout is cp1252 and a tick mark raises
    # UnicodeEncodeError mid-run (the documented cp1252 trap — hit it here).
    print(f"  OK {name}: {verts} verts, {edges} edges, "
          f"{len(r.content) // 1024}KB")
    return True


def from_catalog() -> "list[tuple[str, str, bool]]":
    """Parse the catalog table -> (name, id, is_cc0). CC0 first: CC-BY legally
    requires crediting the author in the video description, which is a burden
    on Zeke every time he posts."""
    rows = []
    seen = set()
    for line in CATALOG.read_text(encoding="utf-8").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 6 or parts[1] in ("Model", "") or "---" in parts[2]:
            continue
        label, _author, lic, sid = parts[1], parts[2], parts[3], parts[4]
        sid = sid.replace("`", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-]{8,14}", sid):
            continue
        name = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
        base = name
        k = 2
        while name in seen:
            name = f"{base}_{k}"
            k += 1
        seen.add(name)
        rows.append((name, sid, "CC0" in lic))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pairs", nargs="*", help="name:polyPizzaId")
    ap.add_argument("--from-catalog", action="store_true")
    ap.add_argument("--cc0-only", action="store_true")
    ap.add_argument("--search", default="", help="comma-list of search terms")
    ap.add_argument("--limit", type=int, default=6, help="max kept per term")
    args = ap.parse_args()
    DEST.mkdir(parents=True, exist_ok=True)

    if args.search:
        existing = {p.stem for p in DEST.glob("*.glb")}
        kept_total, manifest = 0, []
        for term in [t.strip() for t in args.search.split(",") if t.strip()]:
            ids = search_ids(term)
            print(f"\n[{term}] {len(ids)} results")
            kept = 0
            for sid in ids:
                if kept >= args.limit:
                    break
                info = resolve_full(sid)
                if not info:
                    continue
                uuid, title, lic = info
                if args.cc0_only and lic != "CC0":
                    continue
                name = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:28]
                if not name:
                    name = f"{term}_{sid[:5]}"
                base, k = name, 2
                while name in existing:
                    name = f"{base}_{k}"
                    k += 1
                if fetch(name, sid):
                    existing.add(name)
                    kept += 1
                    kept_total += 1
                    manifest.append((name, sid, lic))
        print(f"\nkept {kept_total} new")
        for n, s, l in manifest:
            print(f"  {n:28s} {s:14s} {l}")
        print(f"{len(list(DEST.glob('*.glb')))} .glb total in {DEST}")
        return 0

    if args.from_catalog:
        rows = from_catalog()
        if args.cc0_only:
            rows = [r for r in rows if r[2]]
        rows.sort(key=lambda r: (not r[2], r[0]))     # CC0 first
        items = [(n, s) for n, s, _ in rows]
        print(f"catalog: {len(items)} ids "
              f"({sum(1 for r in rows if r[2])} CC0)")
    else:
        items = [tuple(p.split(":", 1)) for p in args.pairs]

    got = sum(fetch(n, s) for n, s in items)
    have = len(list(DEST.glob("*.glb")))
    print(f"\ndownloaded {got} new; {have} .glb total in {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
