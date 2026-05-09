"""brain/app_catalog.py — Bootstrap-via-environment-scan (A9).

Builds and maintains a unified catalog of every app + game Zeke has
installed: Steam library, Epic Games library, Start Menu apps, Desktop
shortcuts. Stored at state/user_apps_catalog.json with fuzzy aliases
so "Cyberpunk" / "cp2077" / "cyberpunk 2077" all resolve to the same
entry.

Why this matters: today's "Open Chrome" works via APP_MAP (built-in)
or app_discoverer (Start Menu / Desktop). Games installed via Steam
or Epic don't easily plug in without each being added by name. The
catalog merges all install sources into one queryable index.

Per Zeke's CLAUDE.md rule 11: most Steam apps + ML tools live at
C:\\Users\\Tzeke\\OneDrive\\Desktop. The Desktop scan added in
2026-05-05 phase B picks up .lnk shortcuts there. This catalog
extends that with Steam and Epic native libraries.

Storage: state/user_apps_catalog.json — flat list of:
  {
    "id": "...",
    "name": "Cyberpunk 2077",
    "kind": "steam_game" | "epic_game" | "desktop_shortcut" | "start_menu" | "system",
    "launch": {
      "kind": "steam_url" | "epic_url" | "exe" | "shortcut",
      "ref": "steam://rungameid/1091500" | "C:/Path/to/Game.exe" | ...
    },
    "aliases": ["cyberpunk", "cp2077", "cyberpunk 2077"],
    "discovered_at": <ts>,
    "source_path": "...",
    "metadata": {...}
  }

The catalog is rebuildable — derived data per state_classification.
Initial scan happens at startup or on-demand. Periodic refresh
(daily) catches new installs.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any


# ── Steam scanning ────────────────────────────────────────────────────────


_STEAMAPPS_VDF_PATHS = [
    Path(r"C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf"),
    Path(r"C:\Program Files\Steam\steamapps\libraryfolders.vdf"),
]


def _parse_libraryfolders_vdf(vdf_path: Path) -> list[Path]:
    """Read libraryfolders.vdf and return all steamapps directories.

    Steam libraries can live on multiple drives. The VDF format is
    Valve's own (non-JSON); we extract `path` entries via regex.
    """
    if not vdf_path.exists():
        return []
    try:
        text = vdf_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    paths: list[Path] = []
    # Regex: lines like  "path"  "D:\\SteamLibrary"
    for m in re.finditer(r'"path"\s+"([^"]+)"', text):
        raw = m.group(1).replace("\\\\", "\\").replace("\\\\", "\\")
        try:
            p = Path(raw) / "steamapps"
            if p.is_dir():
                paths.append(p)
        except Exception:
            continue
    return paths


def _parse_appmanifest(acf_path: Path) -> dict[str, str] | None:
    """Steam appmanifest_NNN.acf is Valve's KeyValues format. We only
    need name + appid + installdir."""
    try:
        text = acf_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    out: dict[str, str] = {}
    for key in ("appid", "name", "installdir"):
        m = re.search(rf'"{key}"\s+"([^"]+)"', text)
        if m:
            out[key] = m.group(1)
    if "appid" in out and "name" in out:
        return out
    return None


def scan_steam_library() -> list[dict[str, Any]]:
    """Scan all known Steam libraries. Returns list of game records."""
    games: list[dict[str, Any]] = []
    libraries: set[Path] = set()
    for vdf in _STEAMAPPS_VDF_PATHS:
        if vdf.exists():
            # Default library is the directory containing the VDF
            libraries.add(vdf.parent)
            for extra in _parse_libraryfolders_vdf(vdf):
                libraries.add(extra)
    for steamapps in libraries:
        if not steamapps.is_dir():
            continue
        for acf in steamapps.glob("appmanifest_*.acf"):
            meta = _parse_appmanifest(acf)
            if not meta:
                continue
            appid = meta.get("appid", "")
            name = meta.get("name", "")
            install_dir = meta.get("installdir", "")
            if not appid or not name:
                continue
            games.append({
                "id": f"steam_{appid}",
                "name": name,
                "kind": "steam_game",
                "launch": {
                    "kind": "steam_url",
                    "ref": f"steam://rungameid/{appid}",
                },
                "aliases": _generate_aliases(name),
                "discovered_at": time.time(),
                "source_path": str(acf),
                "metadata": {
                    "appid": appid,
                    "install_dir": install_dir,
                    "library_root": str(steamapps),
                },
            })
    return games


# ── Epic Games scanning ───────────────────────────────────────────────────


_EPIC_MANIFEST_DIRS = [
    Path(r"C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests"),
]


def scan_epic_library() -> list[dict[str, Any]]:
    """Scan Epic Games manifests directory. Each .item file is JSON."""
    games: list[dict[str, Any]] = []
    for d in _EPIC_MANIFEST_DIRS:
        if not d.is_dir():
            continue
        for item_path in d.glob("*.item"):
            try:
                data = json.loads(item_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            display_name = str(data.get("DisplayName") or "").strip()
            install_location = str(data.get("InstallLocation") or "").strip()
            launch_exe = str(data.get("LaunchExecutable") or "").strip()
            catalog_namespace = str(data.get("CatalogNamespace") or "")
            catalog_item_id = str(data.get("CatalogItemId") or "")
            app_name = str(data.get("AppName") or "")
            if not display_name:
                continue
            # Epic launch URL: com.epicgames.launcher://apps/<namespace>%3A<itemid>%3A<appname>?action=launch&silent=true
            launch_url = ""
            if catalog_namespace and catalog_item_id and app_name:
                launch_url = (
                    f"com.epicgames.launcher://apps/"
                    f"{catalog_namespace}%3A{catalog_item_id}%3A{app_name}"
                    f"?action=launch&silent=true"
                )
            launch_kind = "epic_url" if launch_url else "exe"
            launch_ref = launch_url or (
                str(Path(install_location) / launch_exe) if install_location and launch_exe else ""
            )
            if not launch_ref:
                continue
            games.append({
                "id": f"epic_{app_name or catalog_item_id}",
                "name": display_name,
                "kind": "epic_game",
                "launch": {"kind": launch_kind, "ref": launch_ref},
                "aliases": _generate_aliases(display_name),
                "discovered_at": time.time(),
                "source_path": str(item_path),
                "metadata": {
                    "app_name": app_name,
                    "catalog_namespace": catalog_namespace,
                    "catalog_item_id": catalog_item_id,
                    "install_location": install_location,
                },
            })
    return games


# ── Alias generation ──────────────────────────────────────────────────────


_ALIAS_STRIP_TOKENS = {"the", "a", "an", "of", "and"}


def _generate_aliases(name: str) -> list[str]:
    """Produce searchable aliases from a display name.

    Examples:
      "Cyberpunk 2077"     -> ["cyberpunk 2077", "cyberpunk", "cp2077"]
      "The Witcher 3: Wild Hunt" -> ["the witcher 3 wild hunt",
                                      "witcher 3 wild hunt",
                                      "witcher 3", "witcher", "tw3"]
      "Counter-Strike 2"   -> ["counter-strike 2", "counter strike 2",
                                "cs2", "counter strike"]
    """
    if not name:
        return []
    aliases: set[str] = set()
    nm = name.strip().lower()
    aliases.add(nm)
    # Without punctuation
    no_punct = re.sub(r"[^\w\s]+", " ", nm).strip()
    no_punct = re.sub(r"\s+", " ", no_punct)
    if no_punct and no_punct != nm:
        aliases.add(no_punct)
    # Drop leading "the" / "a" / "an"
    words = no_punct.split() if no_punct else nm.split()
    if words and words[0] in _ALIAS_STRIP_TOKENS:
        aliases.add(" ".join(words[1:]))
        words = words[1:]
    # Drop trailing version year if 4-digit
    if words and re.fullmatch(r"\d{4}", words[-1]):
        aliases.add(" ".join(words[:-1]))
    # Drop trailing single-digit numbers but only as a casual alias
    # (so "Witcher 3" stays a real name, but search also includes "Witcher")
    if len(words) > 1 and words[-1].isdigit():
        aliases.add(" ".join(words[:-1]))
    # Acronym: uppercase letters from each word
    acronym = "".join(w[0] for w in words if w and w[0].isalpha())
    if 2 <= len(acronym) <= 5:
        aliases.add(acronym.lower())
    # Common abbreviations
    if "counter-strike" in nm:
        aliases.add("cs")
        # CS2, CSGO etc — append last digit if exists
        m = re.search(r"counter-strike\s+(\d+|go)", nm)
        if m:
            aliases.add(f"cs{m.group(1)}")
    if "cyberpunk" in nm and "2077" in nm:
        aliases.update(["cp2077", "cyberpunk", "cp"])
    if "witcher" in nm:
        aliases.add("witcher")
        m = re.search(r"witcher\s+(\d+)", nm)
        if m:
            aliases.add(f"tw{m.group(1)}")
    return sorted(aliases)


# ── Catalog management ────────────────────────────────────────────────────


def _catalog_path(base_dir: Path) -> Path:
    p = base_dir / "state" / "user_apps_catalog.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def build_catalog(base_dir: Path, *, include_steam: bool = True, include_epic: bool = True) -> dict[str, Any]:
    """Scan installed app sources and write user_apps_catalog.json.

    Returns the catalog dict.
    """
    entries: list[dict[str, Any]] = []
    sources_used: list[str] = []
    if include_steam:
        try:
            steam_games = scan_steam_library()
            entries.extend(steam_games)
            if steam_games:
                sources_used.append("steam")
        except Exception as e:
            print(f"[app_catalog] steam scan error: {e!r}")
    if include_epic:
        try:
            epic_games = scan_epic_library()
            entries.extend(epic_games)
            if epic_games:
                sources_used.append("epic")
        except Exception as e:
            print(f"[app_catalog] epic scan error: {e!r}")

    catalog = {
        "version": 1,
        "built_at": time.time(),
        "sources": sources_used,
        "entries": entries,
    }
    try:
        _catalog_path(base_dir).write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[app_catalog] write error: {e!r}")
    return catalog


def load_catalog(base_dir: Path) -> dict[str, Any]:
    p = _catalog_path(base_dir)
    if not p.exists():
        return {"version": 1, "built_at": 0.0, "sources": [], "entries": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "built_at": 0.0, "sources": [], "entries": []}


def needs_rebuild(base_dir: Path, *, max_age_seconds: float = 86400.0) -> bool:
    """True if catalog is missing or older than max_age_seconds (default 1 day)."""
    cat = load_catalog(base_dir)
    built_at = float(cat.get("built_at") or 0.0)
    return (time.time() - built_at) > max_age_seconds


# ── Lookup ────────────────────────────────────────────────────────────────


def find_app(base_dir: Path, query: str) -> dict[str, Any] | None:
    """Find a catalog entry matching `query`. Returns best match or None.

    Match priority:
    1. Exact alias match
    2. Substring on name
    3. Substring on any alias
    """
    q = (query or "").strip().lower()
    if not q:
        return None
    cat = load_catalog(base_dir)
    entries = cat.get("entries") or []
    if not entries:
        return None

    # 1. Exact alias match
    for e in entries:
        for alias in e.get("aliases", []):
            if alias == q:
                return e

    # 2. Substring on name
    for e in entries:
        if q in str(e.get("name") or "").lower():
            return e

    # 3. Substring on alias
    for e in entries:
        for alias in e.get("aliases", []):
            if q in alias:
                return e

    return None


def list_all(base_dir: Path) -> list[dict[str, Any]]:
    cat = load_catalog(base_dir)
    return list(cat.get("entries") or [])


def summary(base_dir: Path) -> dict[str, Any]:
    cat = load_catalog(base_dir)
    entries = cat.get("entries") or []
    by_kind: dict[str, int] = {}
    for e in entries:
        k = str(e.get("kind") or "unknown")
        by_kind[k] = by_kind.get(k, 0) + 1
    return {
        "version": cat.get("version", 1),
        "built_at": cat.get("built_at", 0.0),
        "sources": cat.get("sources", []),
        "total_entries": len(entries),
        "by_kind": by_kind,
    }


# ── Launch helper ─────────────────────────────────────────────────────────


def launch_app(entry: dict[str, Any]) -> tuple[bool, str]:
    """Launch an app via its catalog entry. Returns (ok, msg)."""
    launch = entry.get("launch") or {}
    kind = str(launch.get("kind") or "")
    ref = str(launch.get("ref") or "")
    name = str(entry.get("name") or "")
    if not ref:
        return False, f"I have {name} in the catalog but no launch reference."
    try:
        if kind in ("steam_url", "epic_url"):
            os.startfile(ref)  # type: ignore[attr-defined]
            return True, f"Opening {name}."
        if kind == "exe" or kind == "shortcut":
            os.startfile(ref)  # type: ignore[attr-defined]
            return True, f"Opening {name}."
        # Fallback
        os.startfile(ref)  # type: ignore[attr-defined]
        return True, f"Opening {name}."
    except Exception as e:
        return False, f"I couldn't launch {name}: {e!r}"
