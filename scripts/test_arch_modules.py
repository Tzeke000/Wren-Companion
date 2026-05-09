"""Standalone tests for architecture modules shipped 2026-05-06.

Runs without Ava being up. Tests imports, basic API correctness,
edge cases. Can't test runtime integration (that needs Ava +
actual voice loop), but verifies the modules are correctly
structured and their APIs work.

Run: py -3.11 scripts/test_arch_modules.py
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        msg = f"  FAIL  {name}"
        if detail:
            msg += f" -- {detail}"
        print(msg)
        ERRORS.append(f"{name}: {detail or 'condition false'}")


def section(title: str) -> None:
    print()
    print(f"=== {title} ===")


def safe_run(test_name: str, fn) -> None:
    try:
        fn()
    except Exception as e:
        global FAIL
        FAIL += 1
        tb = traceback.format_exc()
        ERRORS.append(f"{test_name}: EXCEPTION {e!r}\n{tb}")
        print(f"  FAIL  {test_name} -- EXCEPTION: {e!r}")


# ─────────────────────────────────────────────────────────────────────────
# state_classification
# ─────────────────────────────────────────────────────────────────────────


def test_state_classification() -> None:
    section("state_classification")
    from brain.state_classification import (
        classify, is_persistent, is_ephemeral, is_derived, regen_source,
        all_known, report,
    )
    check("classify('chat_history.jsonl') == 'persistent'",
          classify("chat_history.jsonl") == "persistent")
    check("classify('ava.pid') == 'ephemeral'",
          classify("ava.pid") == "ephemeral")
    check("classify('fts_memory.db') == 'derived'",
          classify("fts_memory.db") == "derived")
    check("classify('something_unknown.json') is None",
          classify("something_unknown.json") is None)
    check("is_persistent('skills') is True",
          is_persistent("skills") is True)
    check("is_ephemeral('crash_log.txt') is True",
          is_ephemeral("crash_log.txt") is True)
    check("is_derived('discovered_apps.json') is True",
          is_derived("discovered_apps.json") is True)
    check("regen_source('fts_memory.db') is non-empty",
          bool(regen_source("fts_memory.db")))
    # all_known() may have key overlap across categories (e.g. "learning"
    # appears as both directory and aggregated summaries). >= 70 is safe.
    check("all_known() returns >= 70 unique entries",
          len(all_known()) >= 70, f"got {len(all_known())}")

    # Test report against actual state dir
    import os
    listing = os.listdir(ROOT / "state")
    r = report(listing)
    check("report includes all 4 categories",
          set(r.keys()) == {"persistent", "ephemeral", "derived", "unclassified"})
    check("no unclassified entries in real state dir",
          len(r["unclassified"]) == 0,
          f"unclassified: {r['unclassified']}")


# ─────────────────────────────────────────────────────────────────────────
# safety_layer
# ─────────────────────────────────────────────────────────────────────────


def test_safety_layer() -> None:
    section("safety_layer")
    from brain.safety_layer import (
        SafetyLayer, Action, Decision, trust_of, is_high_impact,
    )
    layer = SafetyLayer()

    # Default behavior: no rules -> execute
    action = Action(
        action_type="open_app",
        target="chrome",
        source_user="zeke",
        impact_level="low",
    )
    decision = layer.evaluate(action, {})
    check("default decision is execute",
          decision.kind == "execute")
    check("default rule_name is 'default'",
          decision.rule_name == "default")

    # Register a rule that declines
    def decline_rule(act, g):
        if act.action_type == "delete_file":
            return Decision(kind="decline", rule_name="decline_rule",
                          reason="testing", spoken_reply="No.")
        return None

    layer.register("decline_rule", decline_rule)

    decision2 = layer.evaluate(
        Action(action_type="delete_file", target="x", source_user="zeke"), {}
    )
    check("registered rule fires for matching action",
          decision2.kind == "decline" and decision2.spoken_reply == "No.")

    decision3 = layer.evaluate(
        Action(action_type="open_app", target="x", source_user="zeke"), {}
    )
    check("non-matching action falls through to default",
          decision3.kind == "execute")

    # trust_of
    check("trust_of(zeke) == high",
          trust_of(Action(action_type="x", source_user="zeke"), {}) == "high")
    check("trust_of(claude_code) == high",
          trust_of(Action(action_type="x", source_user="claude_code"), {}) == "high")
    check("trust_of(unknown) == unknown",
          trust_of(Action(action_type="x", source_user="rando"), {}) == "unknown")

    # is_high_impact
    check("is_high_impact(low) == False",
          not is_high_impact(Action(action_type="x", impact_level="low")))
    check("is_high_impact(critical) == True",
          is_high_impact(Action(action_type="x", impact_level="critical")))

    # Decisions log
    decisions = layer.recent_decisions(limit=10)
    check("decisions log captured >=3 entries",
          len(decisions) >= 3)


# ─────────────────────────────────────────────────────────────────────────
# decision_router
# ─────────────────────────────────────────────────────────────────────────


def test_decision_router() -> None:
    section("decision_router")
    from brain.decision_router import (
        DecisionRouter, Handler, HandlerResult, TurnContext,
        build_turn_context_from_run_ava,
    )

    router = DecisionRouter()

    # Empty router returns None
    ctx = build_turn_context_from_run_ava(
        "hello", {}, "zeke", turn_id="t1", source="test"
    )
    check("empty router returns None",
          router.dispatch(ctx) is None)

    # Register a handler that always claims
    class AlwaysHandler(Handler):
        @property
        def name(self) -> str:
            return "always"
        def try_handle(self, ctx):
            return HandlerResult(reply="claimed", route="always")

    router.register(AlwaysHandler())
    result = router.dispatch(ctx)
    check("registered always-handler claims",
          result is not None and result.reply == "claimed")
    check("route auto-set to handler name",
          result.route == "always")

    # Register a deferring handler in front (should defer)
    class DeferHandler(Handler):
        @property
        def name(self) -> str:
            return "defer"
        def try_handle(self, ctx):
            return None

    router2 = DecisionRouter()
    router2.register(DeferHandler())
    router2.register(AlwaysHandler())
    result2 = router2.dispatch(ctx)
    check("deferring handler falls through to next",
          result2 is not None and result2.route == "always")

    # TurnContext convenience
    check("ctx.normalized_input strips trailing punctuation",
          TurnContext(user_input="Hello?", g={}, active_person_id="zeke").normalized_input == "hello")


# ─────────────────────────────────────────────────────────────────────────
# telemetry
# ─────────────────────────────────────────────────────────────────────────


def test_telemetry() -> None:
    section("telemetry")
    from brain.telemetry import Telemetry

    t = Telemetry()

    # Don't configure -> no persistence
    turn_id = t.start_turn(input_text="open chrome", source="voice", person_id="zeke")
    check("start_turn returns non-empty id",
          bool(turn_id))
    t.mark(turn_id, "router_entry")
    time.sleep(0.01)
    t.mark(turn_id, "voice_command_match", model="regex")
    t.end_turn(turn_id, reply_chars=20, route="voice_command", ok=True)

    recent = t.recent(limit=10)
    check("recent() returns the just-completed turn",
          len(recent) == 1)
    rec = recent[0]
    check("recorded turn has 2 stages",
          len(rec.get("stages", [])) == 2)
    check("recorded turn has route",
          rec.get("route") == "voice_command")
    check("recorded turn has duration_ms > 0",
          (rec.get("duration_ms") or 0) > 0)

    summary = t.summary()
    check("summary count matches",
          summary.get("count") == 1)


# ─────────────────────────────────────────────────────────────────────────
# lifecycle
# ─────────────────────────────────────────────────────────────────────────


def test_lifecycle() -> None:
    section("lifecycle")
    from brain.lifecycle import Lifecycle, BEHAVIOR_HINTS

    lc = Lifecycle()
    check("default state is booting",
          lc.current() == "booting")
    check("booting has respond_to_voice=False",
          lc.hint("respond_to_voice") is False)

    lc.transition("alive_attentive", reason="test")
    check("transition to alive_attentive works",
          lc.current() == "alive_attentive")
    check("alive_attentive has respond_to_voice=True",
          lc.hint("respond_to_voice") is True)

    lc.transition("drifting", reason="boredom")
    check("drifting allows play_mode",
          lc.hint("play_mode_allowed") is True)
    check("alive_attentive does NOT allow play_mode",
          BEHAVIOR_HINTS["alive_attentive"]["play_mode_allowed"] is False)

    history = lc.history()
    check("transition history captured 2 entries",
          len(history) == 2)

    # Listener test
    triggered = []
    def listener(old, new):
        triggered.append((old, new))
    lc.on_change(listener)
    lc.transition("sleeping", reason="test_listener")
    check("listener fired on transition",
          len(triggered) == 1 and triggered[0] == ("drifting", "sleeping"))


# ─────────────────────────────────────────────────────────────────────────
# provenance
# ─────────────────────────────────────────────────────────────────────────


def test_provenance() -> None:
    section("provenance")
    from brain.provenance import ProvenanceGraph

    p = ProvenanceGraph()  # not configured -> no persistence

    cid1 = p.record_claim(
        "Polar bears are the largest land predator",
        "training",
        confidence=0.85,
    )
    check("record_claim returns non-empty id",
          bool(cid1))
    cid2 = p.record_claim(
        "Zeke prefers shorter replies",
        "user_told",
        person_id="zeke",
        source_ref="2026-05-06 morning",
        confidence=0.95,
    )

    check("lookup returns the right record",
          p.lookup(cid2).claim == "Zeke prefers shorter replies")

    # Search
    results = p.search("polar")
    check("search finds polar bear claim",
          len(results) == 1 and "Polar" in results[0].claim)

    # describe_source for various kinds
    desc1 = p.describe_source(cid1)
    check("describe training source",
          "training" in desc1.lower())

    desc2 = p.describe_source(cid2)
    check("describe user_told source",
          "told me directly" in desc2.lower())

    summary = p.summary()
    check("summary count == 2",
          summary["count"] == 2)
    check("summary has training in by_kind",
          summary["by_kind"].get("training") == 1)


# ─────────────────────────────────────────────────────────────────────────
# memory_hierarchy
# ─────────────────────────────────────────────────────────────────────────


def test_memory_hierarchy() -> None:
    section("memory_hierarchy")
    from brain.memory_hierarchy import (
        l1_set, l1_get, l1_clear, l1_keys,
        consolidation_status,
    )

    l1_clear()
    check("l1_keys() starts empty",
          len(l1_keys()) == 0)

    l1_set("current_topic", "ava architecture")
    l1_set("active_task", "decision router")
    check("l1_get retrieves stored value",
          l1_get("current_topic") == "ava architecture")
    check("l1_keys() reflects sets",
          set(l1_keys()) == {"current_topic", "active_task"})

    l1_clear()
    check("l1_clear empties the store",
          len(l1_keys()) == 0)

    # consolidation_status produces sensible output
    status = consolidation_status(ROOT)
    check("consolidation_status has 5 layers",
          set(status.keys()) == {"L1_working", "L2_episodic", "L3_semantic",
                                  "L4_procedural", "L5_identity"})


# ─────────────────────────────────────────────────────────────────────────
# claude_code_recognition
# ─────────────────────────────────────────────────────────────────────────


def test_claude_code_recognition() -> None:
    section("claude_code_recognition")
    from brain.claude_code_recognition import (
        is_claude_code_session, looks_like_session_start_for_claude_code,
        compose_claude_code_greeting, mark_claude_code_seen,
        maybe_prefix_with_greeting, claude_code_register_hint,
    )

    check("is_claude_code_session('claude_code') True",
          is_claude_code_session("claude_code"))
    check("is_claude_code_session('Claude_Code') True (case insensitive)",
          is_claude_code_session("Claude_Code"))
    check("is_claude_code_session('zeke') False",
          not is_claude_code_session("zeke"))
    check("is_claude_code_session(None) False",
          not is_claude_code_session(None))

    g = {}
    check("first interaction -> greet",
          looks_like_session_start_for_claude_code(g))

    greeting = compose_claude_code_greeting(g)
    check("greeting non-empty",
          bool(greeting) and len(greeting) > 5)

    mark_claude_code_seen(g)
    check("after mark_seen, cooldown active",
          not looks_like_session_start_for_claude_code(g))

    g2 = {}
    out = maybe_prefix_with_greeting("Opening Chrome.", g2)
    check("prefix wraps greeting before reply",
          "Opening Chrome." in out and out != "Opening Chrome.")

    out2 = maybe_prefix_with_greeting("Opening Chrome.", g2)
    check("subsequent call doesn't re-greet",
          out2 == "Opening Chrome.")

    hint = claude_code_register_hint()
    check("register hint mentions Claude Code",
          "Claude Code" in hint)


# ─────────────────────────────────────────────────────────────────────────
# constraints_honesty (B8)
# ─────────────────────────────────────────────────────────────────────────


def test_constraints_honesty() -> None:
    section("constraints_honesty (B8)")
    from brain.constraints_honesty import (
        detect_constraint_query, answer_constraint_query,
    )

    check("detect 'can you see my screen' -> see screen / can",
          detect_constraint_query("can you see my screen?") == ("see screen", "can"))
    check("detect 'do you have access to the internet' -> internet / can't",
          detect_constraint_query("do you have access to the internet")[0] == "internet")
    check("detect 'do you remember things' -> remember / can",
          detect_constraint_query("do you remember things?") == ("remember", "can"))
    check("detect 'random question' -> None",
          detect_constraint_query("random question with no constraint pattern") is None)

    a1 = answer_constraint_query("can you see my screen?")
    check("answer for 'see screen' includes 'screenshot'",
          a1 and "screenshot" in a1.lower())

    a2 = answer_constraint_query("can you make a phone call?")
    check("answer for 'phone' is decline",
          a2 and ("can't" in a2.lower() or "cannot" in a2.lower()))

    a3 = answer_constraint_query("nope, just chat")
    check("answer for non-pattern is None",
          a3 is None)


# ─────────────────────────────────────────────────────────────────────────
# Run all
# ─────────────────────────────────────────────────────────────────────────


def test_event_schema() -> None:
    section("event_schema")
    from brain.event_schema import (
        all_events, get, by_category, is_declared, validate_payload, summary,
    )
    check("at least 25 events declared",
          len(all_events()) >= 25)
    check("clipboard_changed declared",
          is_declared("clipboard_changed"))
    check("undeclared event correctly flagged",
          not is_declared("totally_made_up_event"))
    schema = get("clipboard_changed")
    check("schema has emitter_category",
          schema is not None and schema.emitter_category == "system")
    ok, missing = validate_payload("clipboard_changed", {"text": "hi", "size_bytes": 2})
    check("valid clipboard payload",
          ok)
    ok, missing = validate_payload("clipboard_changed", {"text": "hi"})
    check("missing-key payload flagged",
          not ok and "size_bytes" in missing)
    ok, missing = validate_payload("not_declared", {})
    check("undeclared payload flagged",
          not ok)
    s = summary()
    check("summary has by_category",
          "by_category" in s)


def test_contracts() -> None:
    section("contracts")
    from brain.contracts import (
        MemoryStore, ActionHandler, Verifier, PersonProfile,
        SkillProvider, EventEmitter, assert_conforms,
    )

    class FakeMemoryStore:
        def search(self, query, *, limit=4, **kwargs):
            return []
        def is_available(self):
            return True

    class IncompleteStore:
        def search(self, query, **kwargs):
            return []
        # missing is_available

    fake = FakeMemoryStore()
    check("FakeMemoryStore conforms to MemoryStore",
          isinstance(fake, MemoryStore))
    incomplete = IncompleteStore()
    check("IncompleteStore does not conform",
          not isinstance(incomplete, MemoryStore))

    ok, missing = assert_conforms(fake, MemoryStore)
    check("assert_conforms reports OK for complete impl", ok)
    ok2, missing2 = assert_conforms(incomplete, MemoryStore)
    check("assert_conforms reports missing for incomplete impl",
          not ok2 and "is_available" in missing2)


def test_hooks() -> None:
    section("hooks")
    from brain.hooks import (
        hook, fire, count, clear, unregister, list_hooks,
        HOOK_ON_TURN_START, HOOK_ON_IDLE_ENTER,
    )
    clear()
    check("clear() empties registry",
          count(HOOK_ON_TURN_START) == 0)

    fired_count = []

    @hook(HOOK_ON_TURN_START, name="test_handler_a")
    def handler_a(g):
        fired_count.append("a")

    @hook(HOOK_ON_TURN_START, name="test_handler_b")
    def handler_b(g):
        fired_count.append("b")

    @hook(HOOK_ON_IDLE_ENTER, name="test_idle_handler")
    def handler_idle(g):
        fired_count.append("idle")

    check("count after registration", count(HOOK_ON_TURN_START) == 2)
    check("count for different hook", count(HOOK_ON_IDLE_ENTER) == 1)

    n = fire(HOOK_ON_TURN_START, {})
    check("fire returns success count",
          n == 2)
    check("fired in order",
          fired_count == ["a", "b"])

    fire(HOOK_ON_IDLE_ENTER, {})
    check("idle hook fired",
          fired_count[-1] == "idle")

    # unregister
    removed = unregister(HOOK_ON_TURN_START, "test_handler_a")
    check("unregister removes a registered handler",
          removed and count(HOOK_ON_TURN_START) == 1)

    # Exception in one hook doesn't stop others
    @hook("on_test_resilience", name="raises")
    def bad_hook(g):
        raise RuntimeError("test")

    @hook("on_test_resilience", name="works")
    def good_hook(g):
        fired_count.append("worked")

    n2 = fire("on_test_resilience", {})
    check("fire continues past exceptions",
          fired_count[-1] == "worked")
    check("fire success count counts only successful runs",
          n2 == 1)

    clear()


def test_feature_flags() -> None:
    section("feature_flags")
    import os
    from brain.feature_flags import (
        flag_value, flag_enabled, FLAGS, set_runtime_override,
        clear_runtime_override, all_resolved,
    )
    # Defaults from declared FLAGS
    check("streaming_tts default enabled",
          flag_enabled("streaming_tts"))
    check("decision_router default disabled",
          not flag_enabled("decision_router"))
    check("introspection_timeout_s default = 14.0",
          flag_value("introspection_timeout_s") == 14.0)
    # env override
    set_runtime_override("decision_router", "1")
    check("env override flips boolean flag on",
          flag_enabled("decision_router"))
    clear_runtime_override("decision_router")
    check("clearing override restores default",
          not flag_enabled("decision_router"))
    # Numeric env override
    os.environ["AVA_FEATURE_INTROSPECTION_TIMEOUT_S"] = "20.0"
    check("numeric env override coerces type",
          flag_value("introspection_timeout_s") == 20.0)
    del os.environ["AVA_FEATURE_INTROSPECTION_TIMEOUT_S"]
    # Unknown flag with default
    check("unknown flag returns provided default",
          flag_value("not_real_flag", default=42) == 42)
    # all_resolved includes everything
    resolved = all_resolved()
    check("all_resolved has every declared flag",
          set(resolved.keys()) == set(FLAGS.keys()))


def test_external_service() -> None:
    section("external_service")
    from brain.external_service import (
        ExternalServiceManager, call, configure, status,
    )

    mgr = ExternalServiceManager()
    mgr.configure("test_svc", max_attempts=3, backoff_seconds=(0.0, 0.0, 0.0),
                  failure_threshold=2, open_seconds=0.5)

    # Successful call
    result, ok, err = mgr.call("test_svc", lambda: "hello")
    check("successful call returns ok=True with result",
          ok and result == "hello")

    # Failing call — but only 1 failure (under threshold of 2)
    counter = {"n": 0}
    def fail_once():
        counter["n"] += 1
        raise RuntimeError("boom")
    result2, ok2, err2 = mgr.call("test_svc", fail_once,
                                  max_attempts=2,
                                  backoff_seconds=(0.0, 0.0))
    check("all-attempts-failing call returns ok=False",
          not ok2)
    check("failing call attempted max_attempts times",
          counter["n"] == 2)

    # One more failure should open the circuit (threshold=2 → 2nd consecutive failure)
    counter2 = {"n": 0}
    def fail_again():
        counter2["n"] += 1
        raise RuntimeError("boom2")
    mgr.call("test_svc", fail_again, max_attempts=2, backoff_seconds=(0.0, 0.0))

    # Circuit should be open now — fast-fail
    result3, ok3, err3 = mgr.call("test_svc", lambda: "should not run")
    check("circuit-open call fast-fails",
          not ok3 and err3 == "circuit_open")

    # Wait for circuit to close
    time.sleep(0.6)
    result4, ok4, err4 = mgr.call("test_svc", lambda: "recovered")
    check("after open_seconds, half-open allows successful call",
          ok4 and result4 == "recovered")

    # Status check
    s = mgr.status("test_svc")
    check("status reports total_calls > 0",
          s["total_calls"] > 0)
    check("status reports total_successes >= 2",
          s["total_successes"] >= 2)


def test_plugin_manifest() -> None:
    section("plugin_manifest")
    from brain.plugin_manifest import (
        PluginManifest, register_plugin, get_plugin, list_plugins,
        depends_on, dependents_of, validate_all, summary,
        configure as configure_pm,
    )
    configure_pm()  # self-bootstrap
    plugins = list_plugins()
    check("at least 20 architecture plugins registered",
          len(plugins) >= 20, f"got {len(plugins)}")

    p = get_plugin("post_action_verifier")
    check("post_action_verifier registered",
          p is not None and p.name == "post_action_verifier")

    # Register a test plugin with a dependency
    register_plugin(PluginManifest(
        name="test_plugin",
        version="1.0.0",
        description="test",
        depends_on=["post_action_verifier"],
    ))
    deps = depends_on("test_plugin")
    check("depends_on returns the declared dep",
          "post_action_verifier" in deps)

    dependents = dependents_of("post_action_verifier")
    check("dependents_of finds the test plugin",
          "test_plugin" in dependents)

    # Validate — should be clean (we depend on a registered plugin)
    issues = validate_all()
    test_dep_issues = [i for i in issues["missing_deps"] if "test_plugin" in i]
    check("test_plugin has no missing-dep issues",
          len(test_dep_issues) == 0)

    # Register one with a missing dep — should surface
    register_plugin(PluginManifest(
        name="broken_plugin",
        version="1.0.0",
        depends_on=["nonexistent_thing"],
    ))
    issues2 = validate_all()
    broken_issues = [i for i in issues2["missing_deps"] if "broken_plugin" in i]
    check("broken_plugin missing-dep surfaces",
          len(broken_issues) >= 1)


def test_resource_budget() -> None:
    section("resource_budget")
    from brain.resource_budget import (
        BudgetTracker, reserve, release, status, is_over_soft, is_over_hard,
    )

    tracker = BudgetTracker()
    tracker.configure("test_kind", soft=10.0, hard=20.0)

    h1 = tracker.reserve("test_feature", "test_kind", 5.0)
    check("reserve returns a handle",
          isinstance(h1, str) and len(h1) > 0)
    check("reservation tracked in status",
          tracker.status()["states"]["test_kind"]["committed"] == 5.0)

    h2 = tracker.reserve("another_feature", "test_kind", 8.0)
    check("over-soft commitment allowed (advisory only)",
          h2 is not None)
    check("status reports over_soft",
          tracker.status()["states"]["test_kind"]["over_soft"])

    # Try with enforce=True and we're over hard
    h3 = tracker.reserve("greedy", "test_kind", 100.0, enforce=True)
    check("enforce mode rejects over-hard reservation",
          h3 is None)
    check("denial counted",
          tracker.status()["states"]["test_kind"]["total_denials"] == 1)

    # Release
    ok = tracker.release(h1)
    check("release returns True for valid handle",
          ok)
    ok2 = tracker.release("nonexistent_handle")
    check("release returns False for invalid handle",
          not ok2)

    # Active reservations
    s = tracker.status()
    active = s["active_reservations"]
    check("active reservations only includes h2 now",
          len(active) == 1 and active[0]["handle"] == h2)


def main() -> int:
    safe_run("state_classification", test_state_classification)
    safe_run("safety_layer", test_safety_layer)
    safe_run("decision_router", test_decision_router)
    safe_run("telemetry", test_telemetry)
    safe_run("lifecycle", test_lifecycle)
    safe_run("provenance", test_provenance)
    safe_run("memory_hierarchy", test_memory_hierarchy)
    safe_run("claude_code_recognition", test_claude_code_recognition)
    safe_run("constraints_honesty", test_constraints_honesty)
    safe_run("event_schema", test_event_schema)
    safe_run("contracts", test_contracts)
    safe_run("hooks", test_hooks)
    safe_run("feature_flags", test_feature_flags)
    safe_run("external_service", test_external_service)
    safe_run("plugin_manifest", test_plugin_manifest)
    safe_run("resource_budget", test_resource_budget)

    print()
    print(f"=== Results: PASS={PASS}  FAIL={FAIL} ===")
    if ERRORS:
        print()
        print("Failures:")
        for e in ERRORS:
            print(f"  - {e[:300]}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
