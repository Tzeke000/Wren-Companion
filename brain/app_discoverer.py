"""
App + game discoverer.

Scans the user's machine ONCE at startup and incrementally afterwards to find
installed apps and games. Builds a small registry that the voice command
router uses for "open <something>" requests, and that Ava's curiosity engine
uses to wonder about things she finds on the machine.

Bootstrap-friendly: no preferences are baked in — Ava just finds what's there.
What she chooses to ask about reveals her interests over time.

Storage:
  state/discovered_apps.json — full registry, refreshed daily
  state/learned_apps.json    — confirmed mappings from corrections / fuzzy matches
"""
from __future__ import annotations

import json
import os
import re
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any, Iterable, Optional


_DISCOVERED_PATH = "state/discovered_apps.json"
_LEARNED_PATH = "state/learned_apps.json"
_REFRESH_INTERVAL_SEC = 24 * 3600  # 24 hours


def _trace(label: str) -> None:  # TRACE-PHASE1
    """Timestamped diagnostic trace for app discovery. Removed/gated in Phase 3."""  # TRACE-PHASE1
    ts = time.strftime("%H:%M:%S") + f".{int(time.time()*1000)%1000:03d}"  # TRACE-PHASE1
    print(f"[trace] {ts} {label}")  # TRACE-PHASE1


# ── Category heuristics ───────────────────────────────────────────────────────

_BROWSER_EXES = {"chrome.exe", "firefox.exe", "msedge.exe", "brave.exe", "opera.exe"}
_MEDIA_EXES = {"spotify.exe", "vlc.exe", "winamp.exe", "foobar2000.exe", "iTunes.exe", "musicbee.exe"}
_PRODUCTIVITY_EXES = {
    "notepad.exe", "code.exe", "winword.exe", "excel.exe", "powerpnt.exe",
    "outlook.exe", "obsidian.exe", "notion.exe", "slack.exe",
}
_KNOWN_GAME_EXES = {
    "minecraft.exe", "minecraftlauncher.exe", "steam.exe", "epicgameslauncher.exe",
    "battle.net.exe", "rsi launcher.exe", "league of legends.exe",
    "leagueoflegends.exe", "valorant.exe", "fortnite.exe",
}
_GAME_HINTS = ("game", "rust", "minecraft", "steam", "epic", "valheim", "skyrim", "fallout")


# ── Path helpers ──────────────────────────────────────────────────────────────

def _user_home() -> Path:
    return Path(os.path.expanduser("~"))


def _expand_search_paths() -> dict[str, list[Path]]:
    home = _user_home()
    return {
        "desktop": [
            home / "Desktop",
            home / "OneDrive" / "Desktop",  # OneDrive-redirected Desktop on Windows 10/11
            Path(r"C:\Users\Public\Desktop"),
        ],
        "start_menu": [
            Path(r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs"),
            home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        ],
        "program_files": [
            Path(r"C:\Program Files"),
            Path(r"C:\Program Files (x86)"),
        ],
        "local_app_data": [
            home / "AppData" / "Local",
        ],
        "steam_common": [
            Path(r"C:\Program Files (x86)\Steam\steamapps\common"),
            Path(r"C:\Program Files\Steam\steamapps\common"),
            home / "Steam" / "steamapps" / "common",
        ],
        "steam_libraryfolders": [
            Path(r"C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf"),
            Path(r"C:\Program Files\Steam\steamapps\libraryfolders.vdf"),
        ],
        "epic": [
            Path(r"C:\Program Files\Epic Games"),
            Path(r"C:\Program Files (x86)\Epic Games"),
        ],
    }


# ── .lnk parsing (lightweight, no extra deps) ────────────────────────────────

def _parse_lnk(lnk_path: Path) -> Optional[str]:
    """Best-effort .lnk → exe path. Uses a tiny Windows shell COM call when
    pywin32 is available; falls back to a binary scan otherwise. Returns the
    target exe path string or None."""
    try:
        # Preferred: pywin32 shell shortcut
        import pythoncom  # type: ignore
        from win32com.shell import shell, shellcon  # type: ignore  # noqa: F401
        link = pythoncom.CoCreateInstance(
            shell.CLSID_ShellLink, None,
            pythoncom.CLSCTX_INPROC_SERVER,
            shell.IID_IShellLink,
        )
        persist = link.QueryInterface(pythoncom.IID_IPersistFile)
        persist.Load(str(lnk_path))
        target, _ = link.GetPath(shell.SLGP_RAWPATH)
        if target:
            return str(target)
    except Exception:
        pass

    # Fallback: parse the LNK header manually for the LinkTargetIDList /
    # LinkInfo path. Just look for the first ".exe" string in the file.
    try:
        data = lnk_path.read_bytes()
    except Exception:
        return None
    # Search both ASCII and UTF-16 LE for ".exe"
    for needle in (b".exe", ".exe".encode("utf-16-le")):
        idx = 0
        while True:
            i = data.find(needle, idx)
            if i < 0:
                break
            # walk backwards to find a path-ish start
            start = i
            sentinel = max(0, i - 260)
            while start > sentinel and data[start - 1:start] not in (b"\x00", b"\x01"):
                start -= 1
            chunk = data[start:i + len(needle)]
            try:
                if needle == b".exe":
                    s = chunk.decode("latin-1", errors="ignore")
                else:
                    s = chunk.decode("utf-16-le", errors="ignore")
                # Pick last path-looking token before .exe
                m = re.search(r"([A-Za-z]:\\[^<>|\?\*\"\x00-\x1f]+\.exe)", s)
                if m:
                    return m.group(1)
            except Exception:
                pass
            idx = i + len(needle)
    return None


# ── Steam appmanifest parsing ─────────────────────────────────────────────────

def _parse_steam_appmanifest(acf_path: Path) -> Optional[dict[str, str]]:
    try:
        text = acf_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    name_m = re.search(r'"name"\s+"([^"]+)"', text)
    appid_m = re.search(r'"appid"\s+"([^"]+)"', text)
    install_m = re.search(r'"installdir"\s+"([^"]+)"', text)
    if not (name_m and appid_m):
        return None
    return {
        "name": name_m.group(1),
        "appid": appid_m.group(1),
        "installdir": install_m.group(1) if install_m else "",
    }


def _steam_library_paths() -> list[Path]:
    """Read steamapps/libraryfolders.vdf for additional steam library paths."""
    paths: list[Path] = []
    for vdf in _expand_search_paths()["steam_libraryfolders"]:
        if not vdf.is_file():
            continue
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in re.finditer(r'"path"\s+"([^"]+)"', text):
            p = Path(m.group(1).replace("\\\\", "\\"))
            common = p / "steamapps" / "common"
            if common.is_dir() and common not in paths:
                paths.append(common)
    return paths


# ── Categorisation ────────────────────────────────────────────────────────────

def _categorise(name: str, exe_name: str, source: str) -> str:
    n = (name or "").lower()
    e = (exe_name or "").lower()
    if source in ("steam", "epic"):
        return "game"
    if e in _BROWSER_EXES:
        return "browser"
    if e in _MEDIA_EXES:
        return "media"
    if e in _PRODUCTIVITY_EXES:
        return "productivity"
    if e in _KNOWN_GAME_EXES or any(h in n for h in _GAME_HINTS):
        return "game"
    return "app"


def _aliases_for(name: str, exe_name: str) -> list[str]:
    aliases: set[str] = set()
    n = (name or "").lower().strip()
    if n:
        aliases.add(n)
        # Word-by-word: "Visual Studio Code" → ["visual","studio","code","vs code","vscode"]
        words = n.split()
        if len(words) > 1:
            initials = "".join(w[0] for w in words if w)
            if 2 <= len(initials) <= 5:
                aliases.add(initials)
            # Drop "the", "for", etc.
            stop = {"the", "for", "with", "and", "of"}
            kept = [w for w in words if w not in stop]
            if kept:
                aliases.add(" ".join(kept))
    if exe_name:
        stem = Path(exe_name).stem.lower()
        if stem and stem != n:
            aliases.add(stem)
    return sorted(aliases)


# ── Discoverer ────────────────────────────────────────────────────────────────

class AppDiscoverer:
    def __init__(self, base_dir: Path):
        self._base = Path(base_dir)
        self._lock = threading.Lock()
        self._registry: dict[str, dict[str, Any]] = {}  # exe_path -> entry
        self._last_scan_ts: float = 0.0

    # ── public ────────────────────────────────────────────────────────────────

    # ── parallel scan helpers ──────────────────────────────────────────────
    # The scan roots are I/O bound and independent. Running them in parallel
    # threads cuts wall time substantially when Program Files dominates.
    # The Python GIL doesn't block I/O, so threading is the right primitive.
    #
    # Six independent scan tasks fan out via ThreadPoolExecutor:
    #   1. C:\Program Files
    #   2. C:\Program Files (x86)
    #   3. .lnk Desktop directories  (Desktop + Public Desktop)
    #   4. .lnk Start Menu directories (ProgramData + User Start Menu)
    #   5. Steam appmanifests
    #   6. Epic Games launcher
    #
    # Earlier versions ran (1)+(2) sequentially in one thread and (3)+(4)
    # sequentially in another, which capped speedup at ~2x even though four
    # cores were idle. True per-root fan-out matches the I/O parallelism
    # the disk subsystem can actually serve.
    #
    # Each scan writes to its OWN local dict to avoid TOCTOU races on
    # _add_entry's dedup check. Results are merged under self._lock.

    def _run_scans_parallel(self) -> dict[str, dict[str, Any]]:
        """Run all scan roots in parallel threads; return merged registry."""
        paths = _expand_search_paths()
        pf_roots = paths["program_files"]
        pf_root = pf_roots[0] if len(pf_roots) > 0 else None
        pfx_root = pf_roots[1] if len(pf_roots) > 1 else None
        desktop_dirs = [p for p in paths["desktop"] if p.is_dir()]
        start_menu_dirs = [p for p in paths["start_menu"] if p.is_dir()]

        tasks: list[tuple[str, Any]] = []
        if pf_root is not None and pf_root.is_dir():
            tasks.append(("program_files", partial(self._scan_program_files, root=pf_root)))
        if pfx_root is not None and pfx_root.is_dir():
            tasks.append(("program_files_x86", partial(self._scan_program_files, root=pfx_root)))
        if desktop_dirs:
            tasks.append(("desktop_lnk", partial(self._scan_lnk_dirs, desktop_dirs)))
        if start_menu_dirs:
            tasks.append(("start_menu_lnk", partial(self._scan_lnk_dirs, start_menu_dirs)))
        tasks.append(("steam", self._scan_steam))
        tasks.append(("epic", self._scan_epic))

        results: list[dict[str, dict[str, Any]]] = []
        results_lock = threading.Lock()

        def _run(name: str, fn) -> None:
            local: dict[str, dict[str, Any]] = {}
            try:
                fn(target=local)
            except Exception as e:
                print(f"[app_discovery] {name} scan error: {e!r}")
            with results_lock:
                results.append(local)

        max_workers = max(1, len(tasks))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ava-app-scan") as pool:
            futures = [pool.submit(_run, name, fn) for name, fn in tasks]
            for f in futures:
                f.result()

        merged: dict[str, dict[str, Any]] = {}
        for r in results:
            for k, v in r.items():
                # First scan to find a key wins (matches the original
                # _add_entry dedup behavior).
                if k not in merged:
                    merged[k] = v
        return merged

    def discover_all(self, g: Optional[dict[str, Any]] = None) -> int:
        """Full scan. Replaces existing registry. Returns number of entries.

        Scans run in parallel (one thread per root). Final merge happens
        under self._lock so concurrent readers see a consistent snapshot.
        """
        _t0 = time.time()  # TRACE-PHASE1
        _trace("app_disc.discover_all_start")  # TRACE-PHASE1
        # Run scans WITHOUT holding the lock — they only touch their own
        # local dicts. Lock is acquired only for the final assignment + save.
        merged = self._run_scans_parallel()
        with self._lock:
            self._registry = merged
            self._last_scan_ts = time.time()
            self._save()
            entries = list(self._registry.values())
            apps = sum(1 for e in entries if e.get("category") != "game")
            games = sum(1 for e in entries if e.get("category") == "game")
            print(f"[app_discovery] found {apps} apps, {games} games (total {len(entries)})")
            _trace(f"app_disc.discover_all_done ms={int((time.time()-_t0)*1000)} apps={apps} games={games}")  # TRACE-PHASE1
            return len(entries)

    def discover_new_since_last(self, g: Optional[dict[str, Any]] = None) -> int:
        """Re-scan and add only new entries (preserving launch_count etc)."""
        _t0 = time.time()  # TRACE-PHASE1
        _trace("app_disc.discover_incremental_start")  # TRACE-PHASE1
        with self._lock:
            prior = {k: dict(v) for k, v in self._registry.items()}
        merged = self._run_scans_parallel()
        with self._lock:
            self._registry = merged
            # Merge: keep prior launch_count / last_launched.
            for path, entry in self._registry.items():
                if path in prior:
                    entry["launch_count"] = int(prior[path].get("launch_count") or 0)
                    entry["last_launched"] = prior[path].get("last_launched")
            new_count = sum(1 for k in self._registry if k not in prior)
            removed = sum(1 for k in prior if k not in self._registry)
            self._last_scan_ts = time.time()
            self._save()
            if new_count or removed:
                print(f"[app_discovery] {new_count} new, {removed} removed apps/games")
            _trace(f"app_disc.discover_incremental_done ms={int((time.time()-_t0)*1000)} new={new_count} removed={removed}")  # TRACE-PHASE1
            return new_count

    def fuzzy_match(self, query: str) -> Optional[dict[str, Any]]:
        """Return best entry matching `query` against name + aliases."""
        q = (query or "").lower().strip()
        if not q:
            return None
        with self._lock:
            entries = list(self._registry.values())
        # Exact match on name or alias first
        for e in entries:
            if e.get("name", "").lower() == q:
                return e
            if q in [a.lower() for a in (e.get("aliases") or [])]:
                return e
        # Exact substring match on name
        sub = [e for e in entries if q in e.get("name", "").lower()]
        if sub:
            sub.sort(key=lambda e: int(e.get("launch_count") or 0), reverse=True)
            return sub[0]
        # Substring on aliases
        sub = [e for e in entries if any(q in a.lower() for a in (e.get("aliases") or []))]
        if sub:
            return sub[0]
        # Token overlap fallback
        q_tokens = set(re.split(r"\W+", q))
        best = None
        best_score = 0.0
        for e in entries:
            name_tokens = set(re.split(r"\W+", e.get("name", "").lower()))
            overlap = q_tokens & name_tokens
            if not overlap:
                continue
            score = len(overlap) / max(1, len(q_tokens))
            if score > best_score:
                best_score = score
                best = e
        return best if best_score >= 0.5 else None

    def top_matches(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Return up to `limit` best fuzzy matches for `query`, ranked.

        Used to give Ava a useful error response when an app isn't found
        ("I don't know an app called X. Did you mean A, B, or C?"). Looser
        than fuzzy_match — returns ALL substring/token candidates ordered
        by score, not just the single best.
        """
        q = (query or "").lower().strip()
        if not q:
            return []
        with self._lock:
            entries = list(self._registry.values())
        scored: list[tuple[float, int, dict[str, Any]]] = []
        q_tokens = set(t for t in re.split(r"\W+", q) if t)
        for e in entries:
            name = str(e.get("name") or "").lower()
            aliases = [str(a or "").lower() for a in (e.get("aliases") or [])]
            score = 0.0
            if q == name or q in aliases:
                score = 100.0
            elif q in name:
                # Earlier substring position = better score.
                pos = name.find(q)
                score = 50.0 - (pos * 0.1)
            elif any(q in a for a in aliases):
                score = 35.0
            else:
                # Token overlap.
                name_tokens = set(t for t in re.split(r"\W+", name) if t)
                overlap = q_tokens & name_tokens
                if overlap:
                    score = (len(overlap) / max(1, len(q_tokens))) * 25.0
            if score > 0:
                # Tiebreak by launch_count so frequently-used apps rank first.
                lc = int(e.get("launch_count") or 0)
                scored.append((score, lc, e))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [dict(e) for (_s, _lc, e) in scored[:max(1, int(limit))]]

    def get_by_category(self, category: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._registry.values() if e.get("category") == category]

    def all_entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._registry.values()]

    def top_by_launch(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            entries = list(self._registry.values())
        entries.sort(key=lambda e: int(e.get("launch_count") or 0), reverse=True)
        return [dict(e) for e in entries[:limit]]

    def record_launch(self, exe_path: str) -> None:
        with self._lock:
            entry = self._registry.get(exe_path)
            if entry is None:
                return
            entry["launch_count"] = int(entry.get("launch_count") or 0) + 1
            entry["last_launched"] = time.time()
            self._save()

    # ── scan helpers ──────────────────────────────────────────────────────────

    def _scan_lnk_dirs(self, dirs: Iterable[Path], target: Optional[dict[str, dict[str, Any]]] = None) -> None:
        tgt = target if target is not None else self._registry
        for d in dirs:
            _t0 = time.time()  # TRACE-PHASE1
            _before = len(tgt)  # TRACE-PHASE1
            _trace(f"app_disc.scan_start root={d}")  # TRACE-PHASE1
            try:
                for lnk in d.rglob("*.lnk"):
                    if not lnk.is_file():
                        continue
                    target_path = _parse_lnk(lnk)
                    if not target_path:
                        continue
                    if not target_path.lower().endswith(".exe"):
                        continue
                    if not Path(target_path).is_file():
                        continue
                    name = lnk.stem
                    self._add_entry(target_path, name, source="shortcut", target=tgt)
            except Exception as e:
                print(f"[app_discovery] lnk scan {d} error: {e}")
            _trace(f"app_disc.scan_done root={d} ms={int((time.time()-_t0)*1000)} found={len(tgt)-_before}")  # TRACE-PHASE1

    def _scan_program_files(
        self,
        target: Optional[dict[str, dict[str, Any]]] = None,
        root: Optional[Path] = None,
    ) -> None:
        """Scan one or both Program Files roots for top-level .exe files.

        If `root` is provided, only that root is scanned (used by the
        parallel runner so PF and PF(x86) get their own threads). If
        `root` is None, scans both — kept for backward compat.
        """
        tgt = target if target is not None else self._registry
        if root is not None:
            roots: list[Path] = [root]
        else:
            roots = list(_expand_search_paths()["program_files"])
        # Top-level .exe scan in Program Files / Program Files (x86) only.
        # LOCALAPPDATA is huge and dominated by per-app caches/installers/
        # helpers; the .lnk shortcut scan already picks up the user-facing
        # entry points there. Skipping it cuts the initial scan from ~45s
        # to ~5-10s.
        for r in roots:
            if not r.is_dir():
                continue
            _t0 = time.time()  # TRACE-PHASE1
            _before = len(tgt)  # TRACE-PHASE1
            _trace(f"app_disc.scan_start root={r}")  # TRACE-PHASE1
            try:
                for exe in self._iter_exes(r, max_depth=3):
                    name = exe.stem
                    # Skip obvious uninstallers / setup binaries.
                    low = name.lower()
                    if any(x in low for x in ("uninstall", "setup", "installer", "update", "crashpad", "helper")):
                        continue
                    self._add_entry(str(exe), name, source="program_files", target=tgt)
            except Exception as e:
                print(f"[app_discovery] program_files {r} error: {e}")
            _trace(f"app_disc.scan_done root={r} ms={int((time.time()-_t0)*1000)} found={len(tgt)-_before}")  # TRACE-PHASE1

    def _iter_exes(self, root: Path, max_depth: int) -> Iterable[Path]:
        # Depth-limited BFS instead of rglob+post-filter. rglob walks the
        # whole tree before yielding anything; this version stops descending
        # past max_depth so the scan is genuinely bounded.
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            d, depth = stack.pop()
            try:
                entries = list(d.iterdir())
            except (OSError, PermissionError):
                continue
            for entry in entries:
                try:
                    if entry.is_file():
                        if entry.suffix.lower() == ".exe":
                            yield entry
                    elif entry.is_dir() and depth < max_depth:
                        stack.append((entry, depth + 1))
                except OSError:
                    continue

    def _scan_steam(self, target: Optional[dict[str, dict[str, Any]]] = None) -> None:
        tgt = target if target is not None else self._registry
        paths = _expand_search_paths()
        common_dirs: list[Path] = []
        for d in paths["steam_common"]:
            if d.is_dir():
                common_dirs.append(d)
        common_dirs.extend(_steam_library_paths())
        seen: set[str] = set()
        for common in common_dirs:
            steamapps = common.parent
            if not steamapps.name == "steamapps":
                continue
            for acf in steamapps.glob("appmanifest_*.acf"):
                meta = _parse_steam_appmanifest(acf)
                if not meta:
                    continue
                key = f"steam:{meta['appid']}"
                if key in seen:
                    continue
                seen.add(key)
                # Use steam:// URL as the launch target — works without picking an exe.
                launch_uri = f"steam://rungameid/{meta['appid']}"
                self._add_entry(
                    launch_uri,
                    meta["name"],
                    source="steam",
                    extra={"appid": meta["appid"], "installdir": meta.get("installdir") or ""},
                    target=tgt,
                )

    def _scan_epic(self, target: Optional[dict[str, dict[str, Any]]] = None) -> None:
        tgt = target if target is not None else self._registry
        paths = _expand_search_paths()
        for root in paths["epic"]:
            if not root.is_dir():
                continue
            try:
                for sub in root.iterdir():
                    if not sub.is_dir():
                        continue
                    # Find a likely .exe inside
                    candidates = list(sub.rglob("*.exe"))
                    if not candidates:
                        continue
                    # Prefer one matching the dir name
                    candidates.sort(key=lambda e: 0 if e.stem.lower() == sub.name.lower() else 1)
                    self._add_entry(str(candidates[0]), sub.name, source="epic", target=tgt)
            except Exception as e:
                print(f"[app_discovery] epic scan {root} error: {e}")

    def _add_entry(
        self,
        exe_path: str,
        name: str,
        *,
        source: str,
        extra: Optional[dict[str, Any]] = None,
        target: Optional[dict[str, dict[str, Any]]] = None,
    ) -> None:
        if not name or not exe_path:
            return
        tgt = target if target is not None else self._registry
        # Dedup: prefer keeping the entry we already have so prior aliases /
        # launch counts survive.
        if exe_path in tgt:
            return
        exe_name = Path(exe_path).name if not exe_path.startswith("steam://") else ""
        category = _categorise(name, exe_name, source)
        entry: dict[str, Any] = {
            "name": name.strip(),
            "exe_path": exe_path,
            "category": category,
            "source": source,
            "aliases": _aliases_for(name, exe_name),
            "icon_location": None,
            "last_launched": None,
            "launch_count": 0,
        }
        if extra:
            entry.update(extra)
        tgt[exe_path] = entry

    # ── persistence ────────────────────────────────────────────────────────────

    def _path(self) -> Path:
        return self._base / _DISCOVERED_PATH

    def _save(self) -> None:
        p = self._path()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.write_text(
                json.dumps(
                    {
                        "last_scan_ts": self._last_scan_ts,
                        "entries": list(self._registry.values()),
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[app_discovery] save error: {e}")

    def load(self) -> None:
        p = self._path()
        if not p.is_file():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            entries = data.get("entries") if isinstance(data, dict) else []
            with self._lock:
                self._registry = {}
                for e in entries:
                    if isinstance(e, dict) and e.get("exe_path"):
                        self._registry[str(e["exe_path"])] = e
                self._last_scan_ts = float(data.get("last_scan_ts") or 0.0)
        except Exception as e:
            print(f"[app_discovery] load error: {e}")

    @property
    def last_scan_ts(self) -> float:
        return self._last_scan_ts

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._registry)


# ── singleton + bootstrap ─────────────────────────────────────────────────────

_SINGLETON: Optional[AppDiscoverer] = None
_LOCK = threading.Lock()


def get_app_discoverer(base_dir: Optional[Path] = None) -> Optional[AppDiscoverer]:
    global _SINGLETON
    if _SINGLETON is not None:
        return _SINGLETON
    if base_dir is None:
        return None
    with _LOCK:
        if _SINGLETON is None:
            _SINGLETON = AppDiscoverer(Path(base_dir))
            _SINGLETON.load()
    return _SINGLETON


def bootstrap_app_discoverer(g: dict[str, Any]) -> Optional[AppDiscoverer]:
    """Load registry from disk fast, then kick a full scan in a background
    thread. Re-scans every 24h while the process is alive."""
    base = Path(g.get("BASE_DIR") or ".")
    disc = get_app_discoverer(base)
    g["_app_discoverer"] = disc
    if disc is None:
        return None

    def _wait_for_idle() -> None:
        """Block while a voice turn is in progress so we don't compete for
        disk + CPU during the user's response."""
        while bool(g.get("_turn_in_progress")):
            time.sleep(0.5)

    def _set_low_priority() -> None:
        """Ask Windows to schedule this thread below normal so it can't starve
        the LLM / TTS threads even when it does run."""
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            THREAD_PRIORITY_BELOW_NORMAL = -1
            kernel32.SetThreadPriority(kernel32.GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL)
        except Exception:
            pass

    def _bg_scan():
        _set_low_priority()
        # Initial scan — wait for any in-flight turn before starting (rare on
        # first start, but defensive).
        _wait_for_idle()
        try:
            disc.discover_all(g)
        except Exception as e:
            print(f"[app_discovery] initial scan error: {e}")
        # Periodic refresh — gated on idle.
        while True:
            try:
                time.sleep(_REFRESH_INTERVAL_SEC)
                _wait_for_idle()
                disc.discover_new_since_last(g)
            except Exception as e:
                print(f"[app_discovery] periodic scan error: {e}")
                time.sleep(60)

    threading.Thread(target=_bg_scan, daemon=True, name="ava-app-discovery").start()
    return disc
