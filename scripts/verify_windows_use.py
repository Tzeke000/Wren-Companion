"""scripts/verify_windows_use.py — Phase C harness for the
brain/windows_use/ wrapper.

Like scripts/verify_temporal_sense.py: stub `g` + temp BASE_DIR; no
Ava boot. Tests deny-list, navigation guards, slow-app classifier,
retry-cascade orchestration logic, event emission, temporal-sense
integration, and the orchestrator's contract surface.

Usage:
  py -3.11 scripts/verify_windows_use.py

Per docs/WINDOWS_USE_AUDIT.md §5: this is the doctor-harness pattern,
NOT real audio loopback. Voice-loop integration is verified through
inject_transcript when avaagent is running; that path is exercised
in scripts/verify_windows_use_doctor.py (separate harness).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── Stub `g` plumbing ────────────────────────────────────────────────


TMP_DIR = Path(tempfile.mkdtemp(prefix="ava_winuse_verify_"))
print(f"[verify] using temp BASE_DIR: {TMP_DIR}")

# Build a project-tree-like directory under TMP_DIR so deny-list checks
# can run against a real path that simulates ava_core/ files. We do NOT
# touch the real project tree.
SIM_PROJECT = TMP_DIR / "simAvaAgentv2"
(SIM_PROJECT / "ava_core").mkdir(parents=True)
(SIM_PROJECT / "ava_core" / "IDENTITY.md").write_text("# IDENTITY (sim)\n", encoding="utf-8")
(SIM_PROJECT / "ava_core" / "SOUL.md").write_text("# SOUL (sim)\n", encoding="utf-8")
(SIM_PROJECT / "ava_core" / "USER.md").write_text("# USER (sim)\n", encoding="utf-8")
(SIM_PROJECT / "brain").mkdir(parents=True)
(SIM_PROJECT / "brain" / "reply_engine.py").write_text("# stub\n", encoding="utf-8")


def _make_g(*, with_tts_worker: bool = False) -> dict:
    g: dict = {
        "BASE_DIR": str(SIM_PROJECT),
        "_last_user_interaction_ts": time.time(),
        "_turn_in_progress": False,
        "_calming_activity_active": False,
        "load_mood": lambda: {},
        "save_mood": lambda m: None,
    }
    if with_tts_worker:
        class _StubTTS:
            def __init__(self):
                self.calls: list[dict] = []

            def speak(self, text, emotion="neutral", intensity=0.3, blocking=False):
                self.calls.append({
                    "text": text, "emotion": emotion,
                    "intensity": intensity, "blocking": blocking,
                })
        g["_tts_worker"] = _StubTTS()
    return g


# ── TEST 1: deny_list.is_protected_for_read on identity files ────────


def test_deny_list_identity_read():
    print("\n[TEST 1] deny_list.is_protected_for_read on identity files")
    from brain.windows_use import deny_list
    g = _make_g()

    cases = [
        (str(SIM_PROJECT / "ava_core" / "IDENTITY.md"), True),
        (str(SIM_PROJECT / "ava_core" / "SOUL.md"), True),
        (str(SIM_PROJECT / "ava_core" / "USER.md"), True),
        # Case-insensitive on Windows.
        (str(SIM_PROJECT / "ava_core" / "identity.md").upper(), True),
        # `..` traversal still blocks.
        (str(SIM_PROJECT / "ava_core" / ".." / "ava_core" / "IDENTITY.md"), True),
        # Other project files: NOT read-blocked.
        (str(SIM_PROJECT / "brain" / "reply_engine.py"), False),
        # Outside project: not blocked.
        (str(TMP_DIR / "elsewhere.txt"), False),
    ]
    all_pass = True
    for path, expected in cases:
        actual, _ = deny_list.is_protected_for_read(path, g)
        ok = actual == expected
        all_pass = all_pass and ok
        print(f"  read-protect({path[-60:]!r}) = {actual}  (expected {expected})  -> {'PASS' if ok else 'FAIL'}")
    return all_pass


def test_deny_list_project_write():
    print("\n[TEST 2] deny_list.is_protected_for_write on project tree")
    from brain.windows_use import deny_list
    g = _make_g()
    cases = [
        (str(SIM_PROJECT / "brain" / "reply_engine.py"), True),
        (str(SIM_PROJECT / "ava_core" / "IDENTITY.md"), True),
        (str(SIM_PROJECT), True),
        (str(TMP_DIR / "outside.txt"), False),
        # Mixed slashes still hit on Windows.
        (str(SIM_PROJECT).replace("\\", "/") + "/brain/reply_engine.py", True),
    ]
    all_pass = True
    for path, expected in cases:
        actual, _ = deny_list.is_protected_for_write(path, g)
        ok = actual == expected
        all_pass = all_pass and ok
        print(f"  write-protect({path[-60:]!r}) = {actual}  (expected {expected})  -> {'PASS' if ok else 'FAIL'}")
    return all_pass


def test_navigation_guard_classifies():
    print("\n[TEST 3] navigation_guards.check_navigation classification")
    from brain.windows_use import navigation_guards
    g = _make_g()

    # Project root → Tier 2 (deny-list).
    r1 = navigation_guards.check_navigation(str(SIM_PROJECT), g)
    # Identity file → Tier 2.
    r2 = navigation_guards.check_navigation(str(SIM_PROJECT / "ava_core" / "IDENTITY.md"), g)
    # An outside path → allow.
    r3 = navigation_guards.check_navigation(str(TMP_DIR / "elsewhere.txt"), g)

    ok = (r1["tier"] == "tier2" and r2["tier"] == "tier2" and r3["tier"] == "allow")
    print(f"  project-root tier={r1['tier']}, identity tier={r2['tier']}, outside tier={r3['tier']}  -> {'PASS' if ok else 'FAIL'}")
    return ok


def test_slow_app_classifier_no_window():
    print("\n[TEST 4] slow_app_detector returns FAILED_NO_WINDOW after estimate when no window appears")
    from brain.windows_use import slow_app_detector
    started = time.time() - 12.0  # 12s ago
    cls, info = slow_app_detector.classify_app_state(
        name="this-app-does-not-exist-1234567890",
        started_at=started, estimate_seconds=8.0,
    )
    ok = cls == slow_app_detector.FAILED_NO_WINDOW
    print(f"  classification={cls}  -> {'PASS' if ok else 'FAIL'}")
    return ok


def test_slow_app_classifier_starting():
    print("\n[TEST 5] slow_app_detector returns STARTING when no window AND under estimate")
    from brain.windows_use import slow_app_detector
    started = time.time() - 1.0
    cls, _info = slow_app_detector.classify_app_state(
        name="this-app-does-not-exist-1234567890",
        started_at=started, estimate_seconds=8.0,
    )
    ok = cls == slow_app_detector.STARTING
    print(f"  classification={cls}  -> {'PASS' if ok else 'FAIL'}")
    return ok


def test_event_emission_audit_log():
    print("\n[TEST 6] event_subscriber.emit writes to state/windows_use_log.jsonl")
    from brain.windows_use import event_subscriber
    g = _make_g()
    event_subscriber.emit(g, "TOOL_CALL", "open_app", {"name": "notepad"})
    event_subscriber.emit(g, "TOOL_RESULT", "open_app", {"ok": True})
    log_path = SIM_PROJECT / "state" / "windows_use_log.jsonl"
    if not log_path.is_file():
        print("  FAIL: log file not created")
        return False
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    types = [r.get("type") for r in rows[-2:]]
    ok = types == ["TOOL_CALL", "TOOL_RESULT"]
    print(f"  last events = {types}  -> {'PASS' if ok else 'FAIL'}")
    return ok


def test_event_routes_thought_to_inner_monologue():
    print("\n[TEST 7] event THOUGHT routes into inner_monologue.json")
    from brain.windows_use import event_subscriber
    from brain import inner_monologue
    g = _make_g()
    base_dir = Path(g["BASE_DIR"])
    event_subscriber.emit(g, "THOUGHT", "open_app", {"thought": "test thought from windows_use"})
    # Read the ring back.
    state = inner_monologue.load_state(base_dir)
    thoughts = state.get("thoughts") or []
    found = any("test thought from windows_use" in (t.get("thought") or "") for t in thoughts)
    print(f"  inner monologue contains thought = {found}  -> {'PASS' if found else 'FAIL'}")
    return found


def test_temporal_integration_estimate_for():
    print("\n[TEST 8] temporal_integration.estimate_for returns seed when no history")
    from brain.windows_use import temporal_integration
    g = _make_g()
    est = temporal_integration.estimate_for(g, "open_app")
    ok = abs(est - 8.0) < 0.01
    print(f"  open_app default estimate = {est}  -> {'PASS' if ok else 'FAIL'}")
    return ok


def test_orchestrator_navigate_protected_refuses():
    print("\n[TEST 9] WindowsUseAgent.navigate refuses protected paths AND emits ERROR event")
    from brain.windows_use.agent import WindowsUseAgent
    g = _make_g(with_tts_worker=True)
    agent = WindowsUseAgent(g)
    result = agent.navigate(str(SIM_PROJECT / "ava_core" / "IDENTITY.md"))
    log_path = SIM_PROJECT / "state" / "windows_use_log.jsonl"
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()] if log_path.is_file() else []
    last_error = next((r for r in reversed(rows) if r.get("type") == "ERROR" and r.get("operation") == "navigate"), None)
    spoken = (g.get("_tts_worker").calls if g.get("_tts_worker") else [])
    ok = (
        not result.ok
        and result.reason and "denied" in result.reason
        and last_error is not None
        and any("not allowed" in c["text"].lower() or "protected" in c["text"].lower() for c in spoken)
    )
    print(f"  ok={result.ok} reason={result.reason!r} error_event_present={last_error is not None} tts_count={len(spoken)}  -> {'PASS' if ok else 'FAIL'}")
    return ok


def test_orchestrator_open_app_emits_call_and_result():
    print("\n[TEST 10] WindowsUseAgent.open_app emits TOOL_CALL and TOOL_RESULT/ERROR even on failure")
    from brain.windows_use.agent import WindowsUseAgent
    g = _make_g(with_tts_worker=True)
    agent = WindowsUseAgent(g)
    # Use a name guaranteed not to exist so the cascade fully escalates.
    result = agent.open_app("zzz-nonexistent-app-zzz")
    log_path = SIM_PROJECT / "state" / "windows_use_log.jsonl"
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()] if log_path.is_file() else []
    has_call = any(r.get("type") == "TOOL_CALL" and r.get("operation") == "open_app" and (r.get("payload") or {}).get("name") == "zzz-nonexistent-app-zzz" for r in rows)
    has_terminal = any(r.get("type") in ("TOOL_RESULT", "ERROR") and r.get("operation") == "open_app" and (r.get("payload") or {}).get("target") == "zzz-nonexistent-app-zzz" for r in rows)
    ok = (not result.ok) and has_call and has_terminal
    print(f"  ok={result.ok} has_call={has_call} has_terminal={has_terminal} attempts={result.attempts}  -> {'PASS' if ok else 'FAIL'}")
    return ok


def test_temporal_estimate_logged_after_open_app():
    print("\n[TEST 11] open_app round-trip writes a row to task_history_log.jsonl")
    from brain.windows_use.agent import WindowsUseAgent
    g = _make_g()
    agent = WindowsUseAgent(g)
    agent.open_app("zzz-nonexistent-app-zzz-2")
    log_path = SIM_PROJECT / "state" / "task_history_log.jsonl"
    if not log_path.is_file():
        print("  FAIL: task_history_log.jsonl not written")
        return False
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    found = any(r.get("kind") == "open_app" for r in rows)
    print(f"  history rows with kind=open_app = {found}  -> {'PASS' if found else 'FAIL'}")
    return found


def test_volume_control_pycaw_available():
    print("\n[TEST 12] volume_control.set_volume_percent doesn't raise (pycaw present)")
    from brain.windows_use import volume_control
    # Do NOT actually change the user's volume — read it, then restore.
    initial = volume_control.get_volume_percent()
    if initial is None:
        print(f"  pycaw unavailable; SKIP (treated as PASS for this harness)")
        return True
    try:
        volume_control.set_volume_percent(initial)
        print(f"  pycaw set/get round-trip OK (current={initial}%)  -> PASS")
        return True
    except Exception as e:
        print(f"  exception {e!r}  -> FAIL")
        return False


def test_path_traversal_attack_still_blocked():
    print("\n[TEST 13] deny-list defeats `..` traversal attack on identity file")
    from brain.windows_use import deny_list
    g = _make_g()
    # Construct a malicious path using `..` to dodge prefix matching.
    attack_path = str(SIM_PROJECT / "brain" / ".." / "ava_core" / "IDENTITY.md")
    blocked, reason = deny_list.is_protected_for_read(attack_path, g)
    ok = blocked is True
    print(f"  blocked={blocked} reason={reason!r}  -> {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> int:
    results = []
    try:
        results.append(("deny_list.identity_read", test_deny_list_identity_read()))
        results.append(("deny_list.project_write", test_deny_list_project_write()))
        results.append(("navigation_guards.classify", test_navigation_guard_classifies()))
        results.append(("slow_app.no_window_after_estimate", test_slow_app_classifier_no_window()))
        results.append(("slow_app.starting_under_estimate", test_slow_app_classifier_starting()))
        results.append(("event.audit_log_writes", test_event_emission_audit_log()))
        results.append(("event.thought_to_monologue", test_event_routes_thought_to_inner_monologue()))
        results.append(("temporal.seed_estimate", test_temporal_integration_estimate_for()))
        results.append(("orchestrator.navigate_refuses", test_orchestrator_navigate_protected_refuses()))
        results.append(("orchestrator.open_app_events", test_orchestrator_open_app_emits_call_and_result()))
        results.append(("temporal.history_logged", test_temporal_estimate_logged_after_open_app()))
        results.append(("volume.pycaw_round_trip", test_volume_control_pycaw_available()))
        results.append(("deny_list.traversal_attack", test_path_traversal_attack_still_blocked()))
    finally:
        try:
            shutil.rmtree(TMP_DIR)
        except Exception:
            pass

    print()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"=== {passed}/{total} tests passed ===")
    for name, ok in results:
        print(f"  {'OK ' if ok else 'FAIL'}  {name}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
