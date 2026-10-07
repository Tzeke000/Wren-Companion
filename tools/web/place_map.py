"""place_map — see what a place looks like (Zeke 2026-10-07: "be able to get the map of a place if you
wanna know how it looks ... a 2D and a 3D one").

  2D street map  : OpenStreetMap standard tiles (© OpenStreetMap contributors, ODbL)
  satellite      : Esri World Imagery tiles (© Esri, Maxar, Earthstar Geographics)
  3D             : this tool returns the Google Earth 3D link; cognition opens it in the cloak
                   browser and screenshots it (recipe in the docstring of _earth_url).

Both 2D images are stitched from map tiles, cropped around the place, marked with a red dot,
attributed, and saved as small JPEGs (<= 900 px wide, safe to Read) under state/maps/.
Tiles are cached on disk and requests carry an honest User-Agent (OSM tile policy: light use only).

params: place (str) | lat + lon · zoom (3-19, default 16) · kind "both"|"2d"|"satellite" (default both)
"""
from __future__ import annotations

import io
import math
import re
import time
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw

from tools.tool_registry import register_tool

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "state" / "maps"
TILES = OUT / "tiles"
UA = {"User-Agent": "IrisCompanion/1.0 (personal assistant; light use; contact via github Tzeke000)"}
SOURCES = {
    "2d": ("https://tile.openstreetmap.org/{z}/{x}/{y}.png", "© OpenStreetMap contributors"),
    "satellite": ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
                  "© Esri, Maxar, Earthstar Geographics"),
}
W, H, TS = 900, 600, 256


def _tile(kind: str, z: int, x: int, y: int) -> Image.Image:
    n = 2 ** z
    x %= n
    if not 0 <= y < n:
        return Image.new("RGB", (TS, TS), (200, 200, 200))
    f = TILES / kind / str(z) / str(x) / f"{y}.img"
    if f.is_file() and time.time() - f.stat().st_mtime < 30 * 86400:
        return Image.open(f).convert("RGB")
    url = SOURCES[kind][0].format(z=z, x=x, y=y)
    r = requests.get(url, headers=UA, timeout=15)
    r.raise_for_status()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(r.content)
    time.sleep(0.05)
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def _render(kind: str, lat: float, lon: float, z: int) -> Image.Image:
    n = 2 ** z
    px = (lon + 180.0) / 360.0 * n * TS
    py = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n * TS
    left, top = px - W / 2, py - H / 2
    tx0, ty0 = int(left // TS), int(top // TS)
    tx1, ty1 = int((left + W) // TS), int((top + H) // TS)
    canvas = Image.new("RGB", ((tx1 - tx0 + 1) * TS, (ty1 - ty0 + 1) * TS))
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            canvas.paste(_tile(kind, z, tx, ty), ((tx - tx0) * TS, (ty - ty0) * TS))
    ox, oy = int(left - tx0 * TS), int(top - ty0 * TS)
    img = canvas.crop((ox, oy, ox + W, oy + H))
    d = ImageDraw.Draw(img)
    cx, cy = W // 2, H // 2
    d.ellipse((cx - 9, cy - 9, cx + 9, cy + 9), fill=(230, 30, 30), outline=(255, 255, 255), width=3)
    attrib = SOURCES[kind][1]
    tw = d.textlength(attrib)
    d.rectangle((W - tw - 10, H - 18, W, H), fill=(255, 255, 255))
    d.text((W - tw - 5, H - 16), attrib, fill=(40, 40, 40))
    return img


def _earth_url(lat: float, lon: float) -> str:
    """3D RECIPE (cognition, not this tool): cloak_launch → cloak_navigate(this url) → wait ~12 s for the
    3D tiles → cloak_screenshot → downscale to <=900 px before Read. Range 600 m, tilt 60°."""
    return f"https://earth.google.com/web/@{lat:.6f},{lon:.6f},0a,600d,35y,0h,60t,0r"


def _tool_place_map(params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    from tools.web.weather import geocode  # same geocoder as the weather tool
    place = str(params.get("place") or "").strip()
    if params.get("lat") is not None and params.get("lon") is not None:
        lat, lon, label = float(params["lat"]), float(params["lon"]), place or "pin"
    elif place:
        loc = geocode(place)
        if not loc:
            return {"ok": False, "error": f"couldn't find '{place}'"}
        lat, lon = float(loc["lat"]), float(loc["lon"])
        label = loc.get("display") or ", ".join(x for x in (loc.get("name"), loc.get("admin1"), loc.get("country")) if x)
    else:
        return {"ok": False, "error": "give place='...' or lat + lon"}
    z = max(3, min(19, int(params.get("zoom") or 16)))
    kinds = {"both": ["2d", "satellite"], "2d": ["2d"], "satellite": ["satellite"]}.get(
        str(params.get("kind") or "both").lower(), ["2d", "satellite"])
    slug = re.sub(r"[^a-z0-9]+", "_", (place or f"{lat:.4f}_{lon:.4f}").lower()).strip("_")[:40] or "place"
    OUT.mkdir(parents=True, exist_ok=True)
    files, errors = {}, {}
    for k in kinds:
        try:
            p = OUT / f"{slug}_{k}_z{z}.jpg"
            _render(k, lat, lon, z).save(p, "JPEG", quality=65, optimize=True)
            files[k] = str(p)
        except Exception as e:  # noqa: BLE001
            errors[k] = repr(e)[:160]
    return {"ok": bool(files), "place": label, "lat": lat, "lon": lon, "zoom": z, "files": files,
            "errors": errors or None,
            "earth_3d_url": _earth_url(lat, lon),
            "google_maps_url": f"https://www.google.com/maps/@{lat:.6f},{lon:.6f},{z}z",
            "note": "files are <=900 px JPEGs, safe to Read. 3D: open earth_3d_url in the cloak browser, "
                    "wait ~12 s, screenshot, downscale before Read."}


register_tool(
    "place_map",
    "See what a place looks like: params place='Eiffel Tower' (or lat+lon), zoom 3-19 (default 16), "
    "kind 'both'|'2d'|'satellite'. Saves a 2D street map (OpenStreetMap) and a satellite view (Esri) as "
    "small JPEGs under state/maps/ (safe to Read) and returns a Google Earth 3D link to open in the "
    "cloak browser for the 3D view.",
    1,
    _tool_place_map,
)
