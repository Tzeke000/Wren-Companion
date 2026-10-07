# "Jarvis-level" tools (2026-10-07)

Zeke asked for weather anywhere, maps of a place in 2D and 3D, direct PC control, reminders, and a way for
the server copy of Iris to reach this PC. Then: research existing MCPs/skills first and harden everything
"with a front and back end and everything in between". This page is the map of what exists.

## Research outcome (don't reinvent the wheel)

A survey of existing MCP servers found nothing that beat the home-grown versions for this setup, so they
stay, with ideas borrowed:

| Capability | Decision | Borrowed |
|---|---|---|
| Weather | keep | — (Open-Meteo MCPs do the same job) |
| 2D/satellite maps | keep | per-service rate limiting + caching (NERVsystems/osmmcp) |
| 3D maps | **replaced** Google-Earth screen-scraping (its terms forbid copying) with a local MapLibre page | MapLibre GL + OpenFreeMap 3D buildings + Mapterhorn terrain (all free, no key) |
| PC control | keep | Windows media sessions (SMTC) for now-playing + per-app control (SecretiveShell/mcp-windows) |
| Reminders | keep (one-shot scheduled tasks survive restarts; scheduler MCPs die with their process) | — |
| Server → PC | keep (no always-on listener) | — |

## Tools (registry: `iris_tool_call name=…`)

| Tool | File | What |
|---|---|---|
| `weather` | `tools/web/weather.py` | any place (Nominatim → Open-Meteo geocoding, Open-Meteo forecast, wttr.in fallback) |
| `place_map` | `tools/web/place_map.py` | 2D (OSM) + satellite (Esri) JPEGs, local 3D URLs, Google Earth link for a person's browser |
| `pc` | `tools/system/pc_control_tool.py` | master/per-app volume, media (SMTC, media-key fallback), now playing, windows, launch |
| `reminder` | `tools/system/reminder_task_tool.py` + `scripts/fire_reminder.py` | add/list/cancel/sweep; Discord DM at the time |
| desktop bridge | `scripts/desktop_bridge.py` | SSH → on-demand Interactive task → actions inside the logged-in desktop |

## App (tabs: World, Reminders, PC; Server tab shows bridge status)

Routes live in `brain/app_jarvis_routes.py`, installed by `brain/app_extra_routes.install()` at boot and live
via the `app_routes` tool (never by reloading `brain.orb_http`).

`GET /api/v1/app/{weather,map,map/image/{name},map3d,reminders,pc/audio,bridge/status}` ·
`POST /api/v1/app/{reminders/add,pc/volume,pc/media,open}` · `DELETE /api/v1/app/reminders/{id}`

## Hardening

- **Origin check on every state-changing route.** orb_http allows any CORS origin, so a web page in the
  browser could otherwise drive these endpoints. Requests with a non-app `Origin` get 403.
- **Input bounds + rate limits** (maps 12/min, reminders 20/h); map image names are regex-checked (no path
  traversal); `open` only accepts Google Earth / Google Maps / OpenStreetMap links.
- **Service policies:** Nominatim ≤1 req/s with a process-wide lock and an on-disk geocode cache; honest
  User-Agent; tile TTLs (OSM 7 d, Esri 1 d), 2–4 parallel fetches, cache capped at 200 MB; attribution on
  every image and map.
- **Reminders:** cross-process lock + atomic writes, DM retries after wake-from-sleep, `sweep` self-repair
  (also run by the 3-hourly self-check), StartWhenAvailable so a reminder missed while the PC was off fires
  late (and says so).
- **Desktop bridge:** fixed action allow-list, request folder ACL'd to the owner account, every request
  logged, locked-screen detection, clipboard paste for non-ASCII typing. SSH into the PC is Tailscale-only
  and the server's key is restricted to the server's address.
- **Tests:** `tests/test_jarvis_tools.py` (offline; 39 tests).
- **Privacy:** no addresses, IDs or names in this code — they come from git-ignored local config
  (`brain/private_config.py`); a pre-commit/pre-push guard (`scripts/privacy_guard.py`) blocks leaks.

## Not done / known limits

- Wake-on-LAN is armed on the network card but untested from a powered-off state (needs someone at the PC).
- Voice delivery when away is on hold by request.
