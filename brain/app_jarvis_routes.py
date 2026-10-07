"""App routes for the 2026-10-07 "Jarvis" tools (Zeke: "harden them with a front and back end and everything
else in between"). Installed by brain.app_extra_routes.install() — so they land at boot AND live via the
`app_routes` tool — never by reloading brain.orb_http.

  GET    /api/v1/app/weather?place=&days=&units=     weather tool (any place; empty = tower's location)
  GET    /api/v1/app/map?place=&zoom=&kind=          place_map tool -> image URLs + Google Earth 3D link
  GET    /api/v1/app/map/image/{name}                a rendered map JPEG from state/maps (name-checked)
  GET    /api/v1/app/reminders                       all reminders (pending first)
  POST   /api/v1/app/reminders      {text, when}     add one (durable scheduled task)
  DELETE /api/v1/app/reminders/{rid}                 cancel one
  GET    /api/v1/app/pc/audio                        master volume + per-app sessions + Spotify now-playing
  POST   /api/v1/app/pc/volume      {percent} | {app, percent?, mute?} | {mute: bool}
  POST   /api/v1/app/pc/media       {key: play_pause|next|prev|stop}
  GET    /api/v1/app/bridge/status                   server->PC reach: SSH key, desktop-bridge task, WOL, server
  POST   /api/v1/app/open           {url}            open a Google Earth / Maps / OSM link in his browser (allow-list)

SECURITY: orb_http's CORS is allow_origins=["*"], so ANY web page open in his browser could call
localhost:5876. Every route that CHANGES something (POST/DELETE) therefore refuses a request whose Origin
header is present and not the app's own (Tauri v2 / Vite dev). curl and my own tools send no Origin and
pass. Inputs are validated and bounded; handlers never raise (structured {ok:false,error}).
"""
from __future__ import annotations

import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

try:  # module-level so FastAPI can resolve the (string) annotations under `from __future__ import annotations`
    from fastapi import Request
except Exception:  # noqa: BLE001 — tests import this module without fastapi present
    Request = Any  # type: ignore[assignment,misc]

import json

MAP3D_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>loading</title>
<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet">
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<style>html,body,#m{margin:0;height:100%;background:#0d0f13}</style></head><body><div id="m"></div><script>
const P = __P__;
const dem = {type: "raster-dem", url: "https://tiles.mapterhorn.com/tilejson.json", tileSize: 512, encoding: "terrarium"};
const style = P.mode === "satellite" ? {version: 8,
  sources: {sat: {type: "raster", tileSize: 256, maxzoom: 19,
    tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"],
    attribution: "Imagery \\u00a9 Esri, Vantor, Earthstar Geographics"}, dem: dem},
  layers: [{id: "sat", type: "raster", source: "sat"}]} : "https://tiles.openfreemap.org/styles/liberty";
const map = new maplibregl.Map({container: "m", style, center: [P.lon, P.lat], zoom: P.zoom, pitch: P.pitch,
  bearing: P.bearing, maxPitch: 85, preserveDrawingBuffer: true, attributionControl: {compact: false}});
map.addControl(new maplibregl.NavigationControl({visualizePitch: true}));
map.on("load", () => {
  if (!map.getSource("dem")) map.addSource("dem", dem);
  map.setTerrain({source: "dem", exaggeration: P.mode === "satellite" ? 1.3 : 1.0});
  try { map.setSky({"sky-color": "#88b6e8", "horizon-color": "#d8e6f2", "sky-horizon-blend": 0.5}); } catch (e) {}
  new maplibregl.Marker({color: "#e61e1e"}).setLngLat([P.lon, P.lat]).addTo(map);
  map.once("idle", () => { document.title = "ready"; });
});
</script></body></html>"""

APP_ORIGINS = {"http://tauri.localhost", "https://tauri.localhost", "tauri://localhost",
               "http://localhost:5173", "http://127.0.0.1:5173"}
MAP_NAME = re.compile(r"^[a-z0-9_]{1,80}\.jpg$")
NOWIN = 0x08000000
_RATE: dict[str, list[float]] = {}


def origin_ok(origin: str | None) -> bool:
    return not origin or origin.rstrip("/") in APP_ORIGINS


def rate_ok(key: str, n: int, per_s: float) -> bool:
    now = time.time()
    hist = [t for t in _RATE.get(key, []) if now - t < per_s]
    ok = len(hist) < n
    if ok:
        hist.append(now)
    _RATE[key] = hist
    return ok


def _tool(name: str):
    from tools.tool_registry import _REGISTRY
    td = _REGISTRY.get(name)
    if td is None:
        raise RuntimeError(f"tool {name} not loaded")
    return td.handler


def _call(name: str, params: dict[str, Any], g: dict[str, Any]) -> dict[str, Any]:
    try:
        return _tool(name)(params, g)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)[:240]}


def bridge_status(root: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": True}
    try:
        cp = subprocess.run(["schtasks", "/query", "/tn", "Iris-Desktop-Bridge", "/fo", "list"],
                            capture_output=True, text=True, timeout=10, creationflags=NOWIN)
        m = re.search(r"Status:\s*(\S+)", cp.stdout)
        out["desktop_bridge_task"] = m.group(1) if cp.returncode == 0 and m else "missing"
    except Exception as e:  # noqa: BLE001
        out["desktop_bridge_task"] = f"error {e!r}"[:80]
    try:
        keys = Path(r"C:\ProgramData\ssh\administrators_authorized_keys").read_text(encoding="ascii", errors="ignore")
        out["server_key_authorized"] = "iris-home-to-tower" in keys
    except Exception:
        out["server_key_authorized"] = None
    try:
        from brain.private_config import get as priv
        host = priv("iris_home_tailnet") or priv("iris_home_host")
        with socket.create_connection((host, 22), timeout=1.5):
            out["server_reachable"] = True
    except Exception:
        out["server_reachable"] = False
    try:
        ps = ("$a=Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | Select-Object -First 1;"
              "(Get-NetAdapterPowerManagement -Name $a.Name).WakeOnMagicPacket")
        cp = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                            text=True, timeout=15, creationflags=NOWIN)
        out["wol_nic"] = cp.stdout.strip() or "unknown"
        out["wol_tested_from_off"] = False  # honest: needs a real off-state test with Zeke home
    except Exception:
        out["wol_nic"] = "unknown"
    reqs = list((root / "state" / "desktop_bridge").glob("req_*.json"))
    out["bridge_queue"] = len(reqs)
    return out


def install(app: Any, g: dict[str, Any], root: Path, existing: set) -> list[str]:
    from fastapi.responses import FileResponse, JSONResponse

    maps_dir = root / "state" / "maps"

    def deny() -> JSONResponse:
        return JSONResponse({"ok": False, "error": "origin not allowed"}, status_code=403)

    def weather(place: str = "", days: int = 3, units: str = "f") -> dict[str, Any]:
        if len(place) > 120:
            return {"ok": False, "error": "place too long"}
        return _call("weather", {"place": place, "days": max(1, min(7, days)), "units": units}, g)

    def map_(place: str = "", zoom: int = 16, kind: str = "both", lat: float | None = None,
             lon: float | None = None) -> dict[str, Any]:
        if len(place) > 120 or kind not in ("both", "2d", "satellite"):
            return {"ok": False, "error": "bad place/kind"}
        if not rate_ok("map", 12, 60):
            return {"ok": False, "error": "slow down — map tiles are a shared free service"}
        p: dict[str, Any] = {"place": place, "zoom": max(3, min(19, zoom)), "kind": kind}
        if lat is not None and lon is not None:
            p.update(lat=lat, lon=lon)
        r = _call("place_map", p, g)
        if r.get("files"):
            r["image_urls"] = {k: f"/api/v1/app/map/image/{Path(v).name}" for k, v in r["files"].items()}
        return r

    def map_image(name: str):
        if not MAP_NAME.match(name) or not (maps_dir / name).is_file():
            return JSONResponse({"ok": False, "error": "no such map image"}, status_code=404)
        return FileResponse(str(maps_dir / name), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    def reminders_list() -> dict[str, Any]:
        return _call("reminder", {"action": "list", "all": True}, g)

    async def reminders_add(request: Request):
        if not origin_ok(request.headers.get("origin")):
            return deny()
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "JSON body required"}, status_code=400)
        text, when = str(body.get("text") or "").strip(), str(body.get("when") or "").strip()
        if not text or not when or len(text) > 300 or len(when) > 40:
            return JSONResponse({"ok": False, "error": "text (<=300) and when (<=40) required"}, status_code=400)
        if not rate_ok("reminder_add", 20, 3600):
            return JSONResponse({"ok": False, "error": "too many reminders this hour"}, status_code=429)
        return _call("reminder", {"action": "add", "text": text, "when": when}, g)

    def reminders_cancel(rid: str, request: Request):
        if not origin_ok(request.headers.get("origin")):
            return deny()
        if not re.fullmatch(r"[0-9a-f]{8}", rid):
            return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
        return _call("reminder", {"action": "cancel", "id": rid}, g)

    def pc_audio() -> dict[str, Any]:
        h = _tool("pc")
        try:
            return {"ok": True, "master": h({"action": "volume_get"}, g),
                    "sessions": h({"action": "sessions"}, g).get("sessions", []),
                    "now_playing": h({"action": "now_playing"}, g).get("now_playing")}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": repr(e)[:200]}

    async def pc_volume(request: Request):
        if not origin_ok(request.headers.get("origin")):
            return deny()
        try:
            b = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "JSON body required"}, status_code=400)
        if b.get("app"):
            p = {"action": "app_volume", "app": str(b["app"])[:60]}
            if b.get("percent") is not None:
                p["percent"] = max(0, min(100, float(b["percent"])))
            if b.get("mute") is not None:
                p["mute"] = bool(b["mute"])
            return _call("pc", p, g)
        if b.get("percent") is not None:
            return _call("pc", {"action": "volume_set", "percent": max(0, min(100, float(b["percent"])))}, g)
        if b.get("mute") is not None:
            return _call("pc", {"action": "mute" if b["mute"] else "unmute"}, g)
        return JSONResponse({"ok": False, "error": "give percent, mute, or app"}, status_code=400)

    async def pc_media(request: Request):
        if not origin_ok(request.headers.get("origin")):
            return deny()
        try:
            key = str((await request.json()).get("key") or "")
        except Exception:
            key = ""
        if key not in ("play_pause", "next", "prev", "stop"):
            return JSONResponse({"ok": False, "error": "key must be play_pause|next|prev|stop"}, status_code=400)
        return _call("pc", {"action": "media", "key": key}, g)

    def bridge() -> dict[str, Any]:
        return bridge_status(root)

    def map3d(lat: float, lon: float, zoom: float = 16, pitch: float = 60, bearing: float = -20,
              mode: str = "buildings"):
        """A real 3D view rendered locally with MapLibre (free, no key): OpenFreeMap 'liberty' style with 3D
        building extrusions, or Esri imagery draped over Mapterhorn terrain. Research 2026-10-07: replaces
        screen-scraping Google Earth for MY views (its terms forbid copying); his browser can still open
        Earth normally."""
        from fastapi.responses import HTMLResponse
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or mode not in ("buildings", "satellite"):
            return JSONResponse({"ok": False, "error": "bad lat/lon/mode"}, status_code=400)
        p = {"lat": lat, "lon": lon, "zoom": max(3.0, min(19.0, zoom)), "pitch": max(0.0, min(85.0, pitch)),
             "bearing": max(-180.0, min(180.0, bearing)), "mode": mode}
        return HTMLResponse(MAP3D_HTML.replace("__P__", json.dumps(p)), headers={"Cache-Control": "no-store"})

    async def open_link(request: Request):
        """Open a MAP link in his default browser (the 3D view). Allow-listed hosts only — this is not a
        general 'open any URL' endpoint."""
        if not origin_ok(request.headers.get("origin")):
            return deny()
        try:
            url = str((await request.json()).get("url") or "")
        except Exception:
            url = ""
        from urllib.parse import urlparse
        u = urlparse(url)
        ok_host = u.scheme == "https" and (u.netloc in ("earth.google.com", "www.openstreetmap.org")
                                           or (u.netloc == "www.google.com" and u.path.startswith("/maps")))
        if not ok_host or len(url) > 400:
            return JSONResponse({"ok": False, "error": "only Google Earth / Google Maps / OpenStreetMap links"},
                                status_code=400)
        import os
        os.startfile(url)
        return {"ok": True, "opened": url}

    routes = [
        ("/api/v1/app/weather", weather, ["GET"]),
        ("/api/v1/app/map", map_, ["GET"]),
        ("/api/v1/app/map/image/{name}", map_image, ["GET"]),
        ("/api/v1/app/reminders", reminders_list, ["GET"]),
        ("/api/v1/app/reminders/add", reminders_add, ["POST"]),
        ("/api/v1/app/reminders/{rid}", reminders_cancel, ["DELETE"]),
        ("/api/v1/app/pc/audio", pc_audio, ["GET"]),
        ("/api/v1/app/pc/volume", pc_volume, ["POST"]),
        ("/api/v1/app/pc/media", pc_media, ["POST"]),
        ("/api/v1/app/bridge/status", bridge, ["GET"]),
        ("/api/v1/app/open", open_link, ["POST"]),
        ("/api/v1/app/map3d", map3d, ["GET"]),
    ]
    # Re-install = REPLACE this module's own routes (named 'jarvis:*'), so a live fix lands without a restart.
    mine = {pth for pth, _, _ in routes}
    app.router.routes[:] = [r for r in app.router.routes
                            if not (str(getattr(r, "name", "")).startswith("jarvis:")
                                    or (getattr(r, "path", None) in mine
                                        and getattr(getattr(r, "endpoint", None), "__module__", "") == __name__))]
    added = []
    for path, fn, methods in routes:
        app.add_api_route(path, fn, methods=methods, name=f"jarvis:{methods[0]}:{path}")
        added.append(f"{methods[0]} {path}")
    return added
