"""brain/windows_use/agent.py — Task B2 orchestrator (Tier 2).

The wrapper Ava calls. Composes primitives into multi-strategy
operations, integrates deny-list, emits events, hooks temporal_sense,
narrates slow paths.

See docs/WINDOWS_USE_INTEGRATION.md §5.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from brain.windows_use import (
    deny_list,
    event_subscriber,
    navigation_guards,
    primitives,
    retry_cascade,
    slow_app_detector,
    temporal_integration,
    tts_narration,
    volume_control,
)


@dataclass
class WindowsUseResult:
    ok: bool
    operation: str
    target: str
    duration_seconds: float = 0.0
    strategy_used: str | None = None
    attempts: int = 0
    error: str | None = None
    reason: str | None = None
    estimate_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    # Disambiguation: when a tool finds multiple matches it returns ok=False
    # reason="ambiguous" and populates `candidates` with the structured options.
    # The agent's caller (Ava's reply pipeline) reads this and asks the user
    # which one. See docs/AVA_FEATURE_ADDITIONS_2026-05.md §5.
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class WindowsUseAgent:
    """Orchestrator. One per session; lazy-singleton via brain.windows_use.get_agent."""

    def __init__(self, g: dict[str, Any]) -> None:
        self.g = g

    # ── Public API ───────────────────────────────────────────────

    def open_app(self, name: str, *, context: str = "") -> WindowsUseResult:
        op = "open_app"
        if not name or not isinstance(name, str):
            return WindowsUseResult(
                ok=False, operation=op, target=str(name or ""),
                error="name required", reason="bad_input",
            )
        # Deny-list does not apply to app names (those don't resolve to
        # paths). It applies to the navigate() / read / write surface.
        # ── Already-open dedup ────────────────────────────────────
        # Before launching, check if a window for this app is already
        # visible. If yes, return ok=True with reason="already_open" so the
        # voice command router speaks "already open" instead of opening a
        # second instance. Mirrors the cu_close_app disambiguation pattern.
        # Vault: 2026-05 work order Phase A — Session A step 4.
        try:
            from tools.system.app_launcher import _resolve_app
            from pathlib import Path as _P
            exe_path, canonical = _resolve_app(name)
            target_exe = ""
            if exe_path and isinstance(exe_path, str):
                target_exe = _P(exe_path).name.lower()
            elif canonical:
                target_exe = f"{canonical}.exe"
            existing = primitives.find_window_candidates(name)
            if target_exe:
                existing = [
                    c for c in (existing or [])
                    if str(c.get("process_name") or "").lower() == target_exe
                ]
            else:
                existing = []
        except Exception:
            existing = []
        if existing:
            tid_dedup, started_dedup, _est_dedup = temporal_integration.begin(self.g, kind=op, context=name)
            event_subscriber.emit(self.g, "TOOL_CALL", op, {
                "name": name, "context": context, "estimate_id": tid_dedup,
                "estimate_seconds": 0.0, "dedup": True,
            })
            temporal_integration.end(self.g, tid_dedup, started_dedup)
            result = WindowsUseResult(
                ok=True, operation=op, target=name,
                duration_seconds=0.0, strategy_used="already_open",
                attempts=0, reason="already_open",
                estimate_id=tid_dedup,
                candidates=[
                    {
                        "kind": c.get("kind"),
                        "title": c.get("title"),
                        "process": c.get("process"),
                    }
                    for c in existing[:5]
                ],
            )
            event_subscriber.emit(self.g, "TOOL_RESULT", op, result.as_dict())
            return result
        tid, started, est = temporal_integration.begin(self.g, kind=op, context=name)
        event_subscriber.emit(self.g, "TOOL_CALL", op, {
            "name": name, "context": context, "estimate_id": tid, "estimate_seconds": est,
        })

        def _on_transition(from_s: str, to_s: str) -> None:
            event_subscriber.emit(self.g, "THOUGHT", op, {
                "thought": f"strategy={from_s} exhausted for {name}; trying {to_s}",
                "from_strategy": from_s, "to_strategy": to_s,
            })
            tts_narration.narrate_strategy_transition(
                self.g, app_name=name, from_strategy=from_s, to_strategy=to_s,
            )

        try:
            cascade = retry_cascade.run_open_app_cascade(
                name=name, g=self.g, estimate_seconds=est,
                on_strategy_transition=_on_transition,
            )
        except Exception as e:
            cascade = {
                "ok": False, "strategy_used": None, "attempts": 0,
                "elapsed": time.time() - started, "window_found": False,
                "last_classification": None, "error": repr(e),
            }

        # Slow-app narration: if last classification was VERY_SLOW_STILL_WORKING,
        # speak the slow-app line.
        if cascade.get("last_classification") == slow_app_detector.VERY_SLOW_STILL_WORKING:
            tts_narration.narrate_slow_app(self.g, app_name=name)

        temporal_integration.end(self.g, tid, started)
        result = WindowsUseResult(
            ok=bool(cascade.get("ok")),
            operation=op,
            target=name,
            duration_seconds=float(cascade.get("elapsed") or 0.0),
            strategy_used=cascade.get("strategy_used"),
            attempts=int(cascade.get("attempts") or 0),
            error=cascade.get("error"),
            reason=None if cascade.get("ok") else "no_app_found",
            estimate_id=tid,
            extra={"last_classification": cascade.get("last_classification")},
        )
        event_subscriber.emit(
            self.g,
            "TOOL_RESULT" if result.ok else "ERROR",
            op,
            result.as_dict(),
        )
        return result

    def click(self, window_title: str, control: dict[str, Any], *, context: str = "") -> WindowsUseResult:
        op = "click"
        tid, started, est = temporal_integration.begin(self.g, kind="ui_click", context=window_title)
        event_subscriber.emit(self.g, "TOOL_CALL", op, {
            "window_title": window_title, "control": control, "context": context, "estimate_id": tid,
        })
        ok = False
        err = None
        try:
            ok = primitives.click_in_window(window_title, control or {})
        except Exception as e:
            err = repr(e)
        temporal_integration.end(self.g, tid, started)
        result = WindowsUseResult(
            ok=ok, operation=op, target=window_title,
            duration_seconds=time.time() - started, attempts=1,
            error=err, reason=None if ok else "click_failed", estimate_id=tid,
        )
        event_subscriber.emit(
            self.g, "TOOL_RESULT" if ok else "ERROR", op, result.as_dict(),
        )
        return result

    def type_text(self, window_title: str, text: str, *, context: str = "") -> WindowsUseResult:
        op = "type_text"
        tid, started, est = temporal_integration.begin(self.g, kind="type_text", context=window_title)
        event_subscriber.emit(self.g, "TOOL_CALL", op, {
            "window_title": window_title, "text_len": len(text or ""), "context": context, "estimate_id": tid,
        })
        ok = False
        err = None
        try:
            ok = primitives.type_text_in_window(window_title, text or "")
        except Exception as e:
            err = repr(e)
        temporal_integration.end(self.g, tid, started)
        result = WindowsUseResult(
            ok=ok, operation=op, target=window_title,
            duration_seconds=time.time() - started, attempts=1,
            error=err, reason=None if ok else "type_failed", estimate_id=tid,
        )
        event_subscriber.emit(
            self.g, "TOOL_RESULT" if ok else "ERROR", op, result.as_dict(),
        )
        return result

    def navigate(self, path: str, *, context: str = "") -> WindowsUseResult:
        op = "navigate"
        # Deny-list / sensitivity check FIRST.
        guard = navigation_guards.check_navigation(path or "", self.g)
        target_for_log = (
            deny_list.mask_target(path) if guard["tier"] == "tier2" else (path or "")
        )

        if guard["tier"] == "tier2":
            event_subscriber.emit(self.g, "ERROR", op, {
                "target": target_for_log, "reason": guard["reason"], "context": context,
                "thought": f"refusing navigate to protected target — reason={guard['reason']}",
            })
            if guard.get("back_out_required"):
                navigation_guards.execute_back_out(path or "")
            tts_narration.narrate_deny_list_refusal(self.g, target_basename=Path(path or "").name)
            return WindowsUseResult(
                ok=False, operation=op, target=target_for_log,
                error="denied", reason=guard["reason"] or "denied:explorer_in_protected_area",
            )

        if guard["tier"] == "tier1" and guard.get("alert_required"):
            tts_narration.narrate_tier1_alert(self.g, prefix=guard.get("matched_prefix") or "")
            navigation_guards.mark_alerted(guard.get("matched_prefix"), self.g)
            event_subscriber.emit(self.g, "THOUGHT", op, {
                "thought": f"tier-1 alert raised for sensitive prefix {guard.get('matched_prefix')}",
                "prefix": guard.get("matched_prefix"),
            })
            # Don't actually open. The orchestrator's contract is: alert
            # AND wait for confirmation. The agent's caller (Ava's reply
            # pipeline) decides whether to call again to confirm. For now
            # the second call (after this prefix is in the alerted map)
            # falls through to the navigate.
            return WindowsUseResult(
                ok=False, operation=op, target=path,
                error="awaiting_confirmation", reason="declined:tier1",
            )

        # Allow path.
        tid, started, est = temporal_integration.begin(self.g, kind="explorer_nav", context=path or "")
        event_subscriber.emit(self.g, "TOOL_CALL", op, {
            "target": path, "context": context, "estimate_id": tid,
        })
        ok = False
        err = None
        try:
            ok = primitives.navigate_explorer(path)
        except Exception as e:
            err = repr(e)
        temporal_integration.end(self.g, tid, started)
        result = WindowsUseResult(
            ok=ok, operation=op, target=path or "",
            duration_seconds=time.time() - started, attempts=1,
            error=err, reason=None if ok else "navigate_failed", estimate_id=tid,
        )
        event_subscriber.emit(
            self.g, "TOOL_RESULT" if ok else "ERROR", op, result.as_dict(),
        )
        return result

    def set_volume(self, percent: int, *, context: str = "") -> WindowsUseResult:
        op = "set_volume"
        tid, started, est = temporal_integration.begin(self.g, kind="volume", context=str(percent))
        event_subscriber.emit(self.g, "TOOL_CALL", op, {"percent": percent, "estimate_id": tid})
        ok = volume_control.set_volume_percent(int(percent))
        temporal_integration.end(self.g, tid, started)
        result = WindowsUseResult(
            ok=ok, operation=op, target=f"{percent}%",
            duration_seconds=time.time() - started, attempts=1,
            reason=None if ok else "pycaw_unavailable", estimate_id=tid,
        )
        event_subscriber.emit(
            self.g, "TOOL_RESULT" if ok else "ERROR", op, result.as_dict(),
        )
        return result

    def volume_up(self, *, context: str = "") -> WindowsUseResult:
        op = "volume_up"
        ok = volume_control.volume_up()
        result = WindowsUseResult(ok=ok, operation=op, target="up")
        event_subscriber.emit(self.g, "TOOL_RESULT" if ok else "ERROR", op, result.as_dict())
        return result

    def volume_down(self, *, context: str = "") -> WindowsUseResult:
        op = "volume_down"
        ok = volume_control.volume_down()
        result = WindowsUseResult(ok=ok, operation=op, target="down")
        event_subscriber.emit(self.g, "TOOL_RESULT" if ok else "ERROR", op, result.as_dict())
        return result

    def volume_mute(self, *, context: str = "") -> WindowsUseResult:
        op = "volume_mute"
        ok = volume_control.volume_mute()
        result = WindowsUseResult(ok=ok, operation=op, target="mute")
        event_subscriber.emit(self.g, "TOOL_RESULT" if ok else "ERROR", op, result.as_dict())
        return result

    def read_window(self, window_title: str) -> WindowsUseResult:
        op = "read_window"
        tid, started, est = temporal_integration.begin(self.g, kind="read_window", context=window_title)
        event_subscriber.emit(self.g, "TOOL_CALL", op, {"window_title": window_title, "estimate_id": tid})
        text = ""
        err = None
        try:
            text = primitives.read_window_text(window_title)
        except Exception as e:
            err = repr(e)
        temporal_integration.end(self.g, tid, started)
        ok = bool(text)
        result = WindowsUseResult(
            ok=ok, operation=op, target=window_title,
            duration_seconds=time.time() - started, attempts=1,
            error=err, reason=None if ok else "no_text_or_window_missing",
            estimate_id=tid, extra={"text": text[:1500]},
        )
        event_subscriber.emit(
            self.g, "TOOL_RESULT" if ok else "ERROR", op, result.as_dict(),
        )
        return result

    def list_running_apps(self) -> list[dict[str, Any]]:
        return primitives.list_visible_windows()

    # ── Clipboard operations (atomic alternative to per-char keystroke typing) ──

    def clipboard_write(self, text: str, *, context: str = "") -> WindowsUseResult:
        op = "clipboard_write"
        started = time.time()
        event_subscriber.emit(self.g, "TOOL_CALL", op, {
            "text_len": len(text or ""), "context": context,
        })
        ok = primitives.set_clipboard(text)
        result = WindowsUseResult(
            ok=ok, operation=op, target="<clipboard>",
            duration_seconds=time.time() - started, attempts=1,
            reason=None if ok else "set_clipboard_failed",
        )
        event_subscriber.emit(self.g, "TOOL_RESULT" if ok else "ERROR", op, result.as_dict())
        return result

    def clipboard_paste(self, window_title: str, *, context: str = "") -> WindowsUseResult:
        op = "clipboard_paste"
        started = time.time()
        event_subscriber.emit(self.g, "TOOL_CALL", op, {
            "window_title": window_title, "context": context,
        })
        ok = primitives.paste_into_window(window_title)
        result = WindowsUseResult(
            ok=ok, operation=op, target=window_title,
            duration_seconds=time.time() - started, attempts=1,
            reason=None if ok else "paste_failed",
        )
        event_subscriber.emit(self.g, "TOOL_RESULT" if ok else "ERROR", op, result.as_dict())
        return result

    def type_via_clipboard(self, window_title: str, text: str, *, context: str = "") -> WindowsUseResult:
        """Atomic alternative to `type_text` for text >10 chars. Sets clipboard,
        focuses window, sends Ctrl+V, restores prior clipboard. ~50 ms regardless
        of text length, vs ~50 ms / char for keystroke typing."""
        op = "type_via_clipboard"
        tid, started, est = temporal_integration.begin(self.g, kind="type_text", context=window_title)
        event_subscriber.emit(self.g, "TOOL_CALL", op, {
            "window_title": window_title, "text_len": len(text or ""), "context": context, "estimate_id": tid,
        })
        ok = primitives.type_text_via_clipboard(window_title, text or "")
        temporal_integration.end(self.g, tid, started)
        result = WindowsUseResult(
            ok=ok, operation=op, target=window_title,
            duration_seconds=time.time() - started, attempts=1,
            reason=None if ok else "type_clipboard_failed", estimate_id=tid,
        )
        event_subscriber.emit(self.g, "TOOL_RESULT" if ok else "ERROR", op, result.as_dict())
        return result

    # ── Close operations (with disambiguation) ──

    def close_app(self, name: str, *, target: str | None = None,
                  force: bool = False, last_n: int | None = None,
                  context: str = "") -> WindowsUseResult:
        """Close app(s) by name.

        target: None (auto-disambiguate), "desktop", "browser_tab", "all", or
                a specific window-handle int (passed as string).
        force:  if True, TerminateProcess via psutil instead of WM_CLOSE.
        last_n: for browser tabs, close only the last N matching (MRU).

        Returns ok=False reason="not_found" if zero candidates,
        ok=False reason="ambiguous" with candidates= populated if multiple
        and target is None.
        """
        op = "close_app"
        started = time.time()
        event_subscriber.emit(self.g, "TOOL_CALL", op, {
            "name": name, "target": target, "force": force, "last_n": last_n, "context": context,
        })
        candidates = primitives.find_window_candidates(name)
        if not candidates:
            result = WindowsUseResult(
                ok=False, operation=op, target=name,
                duration_seconds=time.time() - started, attempts=0,
                reason="not_found",
            )
            event_subscriber.emit(self.g, "ERROR", op, result.as_dict())
            return result

        # Auto-disambiguation logic
        if target is None and len(candidates) > 1:
            # Check if all candidates are same kind/process — if so, no ambiguity needed
            kinds = {c["kind"] for c in candidates}
            pids = {c["pid"] for c in candidates}
            if len(kinds) > 1 or (len(kinds) == 1 and "browser_tab" in kinds and len(candidates) > 1 and last_n is None):
                # Ambiguous: mixed desktop+browser, or multiple browser tabs without last_n
                result = WindowsUseResult(
                    ok=False, operation=op, target=name,
                    duration_seconds=time.time() - started,
                    reason="ambiguous", candidates=candidates,
                )
                event_subscriber.emit(self.g, "ERROR", op, result.as_dict())
                return result

        # Filter candidates by target selector
        selected = candidates
        if target == "desktop":
            selected = [c for c in candidates if c["kind"] == "desktop"]
        elif target == "browser_tab":
            selected = [c for c in candidates if c["kind"] == "browser_tab"]
        elif target == "all":
            selected = candidates
        elif target and target.isdigit():
            handle_filter = int(target)
            selected = [c for c in candidates if int(c.get("handle") or 0) == handle_filter]

        # Execute close
        closed_count = 0
        if any(c["kind"] == "browser_tab" for c in selected) and last_n is not None:
            # Close as tabs (Ctrl+W)
            closed_count += primitives.close_browser_tab_by_title(name, last_n=last_n)
        else:
            # WM_CLOSE per window, or force-terminate per pid
            seen_pids: set[int] = set()
            for c in selected:
                if c["kind"] == "browser_tab":
                    primitives.close_browser_tab_by_title(name, last_n=1)
                    closed_count += 1
                    continue
                pid = int(c.get("pid") or 0)
                if pid and pid in seen_pids:
                    continue
                if force:
                    if primitives.close_app_by_pid(pid, force=True):
                        closed_count += 1
                        seen_pids.add(pid)
                else:
                    if primitives.close_window_by_handle(int(c.get("handle") or 0)):
                        closed_count += 1
                seen_pids.add(pid)

        ok = closed_count > 0
        result = WindowsUseResult(
            ok=ok, operation=op, target=name,
            duration_seconds=time.time() - started, attempts=1,
            reason=None if ok else "close_failed",
            extra={"closed_count": closed_count, "candidate_count": len(candidates)},
        )
        event_subscriber.emit(self.g, "TOOL_RESULT" if ok else "ERROR", op, result.as_dict())
        return result
