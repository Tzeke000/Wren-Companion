"""brain/state_classification.py — Classify state/ files into 3 categories.

Persistent: must survive restart, must be in backups, factory-reset
            wipes (with warning).
Ephemeral:  runtime-only, safe to clear at any time, no data loss.
Derived:    regeneratable from canonical sources, deletion just causes
            a rebuild on next access.

This is documentation-as-code. It's a static classification of every
known state/ file. New files default to "unclassified" — the helper
functions return None for them, so callers know they're treading on
fresh ground.

Use cases:
- Backup tooling: only copy persistent + classification map
- Factory reset: clear ephemeral + derived; warn before persistent
- "Why is this big": classify to see if it's regrowable derived data
- Code review: when adding a new state file, classify it explicitly

Architecture sweep #11 (state separation) per
designs/ava-roadmap-personhood.md.
"""
from __future__ import annotations

from typing import Literal


StateCategory = Literal["persistent", "ephemeral", "derived"]


# ── Persistent ────────────────────────────────────────────────────────────
# Survives restart. Backed up. Factory reset wipes only with warning.
# Examples: identity, mood baseline, learned patterns, memory, skills.
PERSISTENT: dict[str, str] = {
    # Identity / self
    "self_model.json": "Ava's self-knowledge accumulated over time",
    "self_narrative.json": "First-person identity narrative",
    "active_person.json": "Persisted as current-context — but only valid for current run; treat as ephemeral",
    # Mood / emotional state
    "mood_carryover.json": "Mood state across restarts (decay applied on load)",
    "expression_baseline_zeke.json": "Per-person expression calibration baseline",
    "expression_state.json": "Live expression weights (decays in real-time)",
    "voice_style.json": "Voice tonal preferences",
    "ava_style.json": "Ava's accumulated style preferences",
    # Relationships / trust
    "trust_scores.json": "Per-person trust accumulation",
    "zeke_mind_model.json": "Ava's model of Zeke specifically",
    # Memory (canonical sources)
    "chat_history.jsonl": "Raw conversation history, append-only",
    "episodes.jsonl": "Episodic memory entries",
    "journal.jsonl": "Ava's own journal entries",
    "concept_graph.json": "Knowledge graph of facts/concepts/relationships",
    "memory_tombstones.jsonl": "Audit log of pruned memories",
    "anchor_moments.jsonl": "Persistent episodic anchors — never auto-pruned (D16)",
    "topic_tabled.json": "Topics Ava has tabled with cooldown (D11)",
    "emotional_vocabulary.json": "Ava's coined emotion terms — language of her experience (D5)",
    "self_revisions.jsonl": "Record of Ava's changed minds — visible self-revision (D7)",
    "counterfactuals.jsonl": "Decision archive — what Ava almost said vs chose (D2)",
    "daily_practice.json": "Registered practices Ava keeps — definitions + state (D8)",
    "daily_practice_history.jsonl": "Run history of daily practices (D8)",
    "creative_ideas.jsonl": "Queue of ideas Ava wants to act on (C13)",
    "creative_works.jsonl": "Things Ava has actually made + surfaced (C13)",
    "async_letters.jsonl": "Ava's letters to persons — composed at leisure (D6)",
    "mood_snapshots.jsonl": "Periodic mood snapshots for comparative memory (D14)",
    "aesthetic_preferences.jsonl": "Ava's developing taste — likes/dislikes through exposure (D17)",
    "curiosity_research_queue.jsonl": "Topics Ava wants to research — queue + status (D15)",
    "identity_stability_log.jsonl": "Periodic bedrock-vs-narrative audit reports (D18)",
    "discretion_tags.jsonl": "Per-person privacy graph — what's confidential (C12)",
    "play_signals.jsonl": "Audit log of when play register fired and why (D19)",
    "disagreements.jsonl": "Audit log of honest-disagreement events (C15)",
    "active_corrections.jsonl": "Captured factual/process corrections from user (B1)",
    "behavior_patterns.json": "Slow-accumulating model of person's behavior patterns (B2)",
    "handoff.json": "Cross-session handoff state — texture-carrying file read at boot (Anthropic harness pattern, Nov 2025)",
    "handoff_log.jsonl": "Append log of threshold-trigger handoff entries — consumed at sleep cycle, then cleared",
    "handoff_archive": "Archived consumed handoff_log entries — date-partitioned for forensics",
    "web_search_cache.json": "Cached results from web search lookups (A7)",
    "skill_sandbox_audit.jsonl": "Audit log of rejected auto-learned skills (#20)",
    "continuity_consent_zeke.json": "Zeke's signed consent for D1 phenomenal continuity activation",
    "continuity_consent_ava.json": "Ava's consent for D1 — must reference zeke's nonce",
    "consolidation_log.jsonl": "Sleep-cycle consolidation history",
    "learning_log.jsonl": "Things Ava has learned (provenance source for skills + concept_graph)",
    "metabolism_log.jsonl": "Memory-as-metabolism trace log",
    "memory_reflection_log.jsonl": "Reflection-pass memory entries",
    # Learned patterns / skills
    "skills": "Procedural skills auto-created on successful compound actions",
    "wake_patterns.json": "Learned wake-word preferences",
    "ambient_patterns.json": "Time-of-day / ambient-state usage patterns",
    "opinions.json": "Ava's opinions accumulated through interaction",
    "curiosity_topics.json": "Topics Ava has expressed curiosity about",
    "prospective_memory.json": "Things Ava is preparing-to-remember",
    "question_history.jsonl": "Questions Ava has asked",
    # Calibration data (input devices)
    "face_labels.json": "Mapping of face encodings → person_id",
    "face_model.yml": "Face recognition model state",
    "clap_calibration.json": "Clap detector threshold calibration",
    "gaze_calibration.json": "Gaze tracker calibration",
    # Scheduled work
    "scheduled_tasks.json": "Pending reminders / scheduled actions",
    "reminders.jsonl": "Active reminders (legacy reminder tool)",
    "concerns": "Concerns directory — open issues Ava is tracking",
    # Goals / initiative
    "goal_system.json": "Ava's self-directed goals",
    "initiative_state.json": "Initiative tracking state",
    # Meta (project state)
    "milestone_100.json": "Phase 100 alive milestone marker",
    "model_preferences.json": "Per-context preferred models",
    "pickup_note.json": "Across-session continuity note",
    "session_state.json": "Persisted session continuity state",
    "finetune": "Fine-tuning data + checkpoints",
    "doctor.secret": "Doctor session HMAC secret (credential)",
    "telemetry": "Pipeline-stage timing records (last N turns)",
    "self_critique.json": "Self-evaluation accumulator",
    # Sleep / nap
    "sleep_handoffs": "Sleep-cycle handoff snapshots",
    # Operational logs (kept for audit, rotated on size)
    "connectivity_log.jsonl": "Internet connectivity state log",
    "face_tracking_log.jsonl": "Face tracker observations",
    "task_history_log.jsonl": "Task execution history",
    "video_summaries.jsonl": "Summarized camera-clip metadata",
    "windows_use_log.jsonl": "WindowsUseAgent action log",
    "response_quality.jsonl": "Per-turn quality metrics (Phase 44 evaluator)",
    "model_eval_p44.json": "Phase 44 model evaluation accumulator",
    # User profiles (persisted via avaagent.load_profile_by_id)
    "profiles": "Per-person profile JSONs (relationship state)",
    "person_registry": "Person Registry per-person view JSONs (architecture #6)",
    # Backup
    "backup": "Backup directory for the above",
}


# ── Ephemeral ─────────────────────────────────────────────────────────────
# Runtime-only. Safe to clear at any time. No data loss from clearing.
EPHEMERAL: dict[str, str] = {
    "ava.pid": "Process PID lockfile",
    "watchdog_disabled.flag": "Operator-toggled watchdog flag",
    "crash_log.txt": "Most recent crash trace",
    "active_estimates.json": "Temporal-sense in-flight task estimates",
    "bootstrap_report.json": "Generated at each startup",
    "heartbeat": "Heartbeat tick scratch state",
    "regression": "Test runner artifacts",
    "workbench": "In-progress LLM scratch work",
    "camera": "Current camera frame buffer",
    "video_clips": "Recent video clip recordings (rotates)",
    "ava_session*.log": "Dev session logs from manual launches (not Ava-canonical)",
    "test_msg.json": "Transient inject_transcript body file used during conversational test passes",
    "melo_tts_bridge.py": "Generated runtime bridge file (legacy melotts)",
    "consolidation_state.json": "In-flight sleep-cycle consolidation state",
    "health_state.json": "Live subsystem health snapshot (regenerated each tick)",
    "inner_monologue.json": "Latest inner thought (live state, not history — history is journal)",
}


# ── Derived ───────────────────────────────────────────────────────────────
# Regeneratable from canonical sources. Deletion just rebuilds.
DERIVED: dict[str, tuple[str, str]] = {
    # filename -> (regen_source, description)
    "fts_memory.db": ("chat_history.jsonl", "FTS5 index — auto-rebuilds in fts_memory.py"),
    "fts_memory.db-shm": ("chat_history.jsonl", "FTS5 SQLite shared memory"),
    "fts_memory.db-wal": ("chat_history.jsonl", "FTS5 SQLite write-ahead log"),
    "discovered_apps.json": ("filesystem scan", "Rebuildable by app_discoverer scan"),
    "learning": ("learning_log.jsonl", "Aggregated learning summaries"),
    "user_apps_catalog.json": ("Steam + Epic library scan", "App + game catalog — rebuilt from libraryfolders.vdf + Epic manifests"),
}


# ── Helpers ───────────────────────────────────────────────────────────────


def classify(path: str) -> StateCategory | None:
    """Return the category for a state-relative path, or None if unclassified.

    Path is relative to state/. Directory entries match by exact name.
    Glob patterns ("ava_session*.log") are matched literally — caller
    can use fnmatch externally for wildcard support.
    """
    name = path.strip("/").strip("\\")
    # Strip "state/" prefix if present
    if name.startswith("state/") or name.startswith("state\\"):
        name = name[6:]
    # Take just the leading directory component for nested paths
    leading = name.split("/", 1)[0].split("\\", 1)[0]

    if name in PERSISTENT or leading in PERSISTENT:
        return "persistent"
    if name in EPHEMERAL or leading in EPHEMERAL:
        return "ephemeral"
    if name in DERIVED or leading in DERIVED:
        return "derived"
    # Wildcard check for log patterns
    if name.startswith("ava_session") and name.endswith(".log"):
        return "ephemeral"
    return None


def is_persistent(path: str) -> bool:
    return classify(path) == "persistent"


def is_ephemeral(path: str) -> bool:
    return classify(path) == "ephemeral"


def is_derived(path: str) -> bool:
    return classify(path) == "derived"


def regen_source(path: str) -> str | None:
    """For a derived file, return what it's rebuilt from (or None)."""
    name = path.split("/")[-1].split("\\")[-1]
    entry = DERIVED.get(name)
    if entry:
        return entry[0]
    return None


def all_known() -> dict[str, StateCategory]:
    """Full map of every classified file/dir → category."""
    out: dict[str, StateCategory] = {}
    for k in PERSISTENT:
        out[k] = "persistent"
    for k in EPHEMERAL:
        out[k] = "ephemeral"
    for k in DERIVED:
        out[k] = "derived"
    return out


def report(state_dir_listing: list[str]) -> dict[str, list[str]]:
    """Given an actual directory listing, produce a category report.

    Returns: {"persistent": [...], "ephemeral": [...], "derived": [...],
              "unclassified": [...]}
    """
    out: dict[str, list[str]] = {
        "persistent": [],
        "ephemeral": [],
        "derived": [],
        "unclassified": [],
    }
    for entry in state_dir_listing:
        cat = classify(entry)
        if cat is None:
            out["unclassified"].append(entry)
        else:
            out[cat].append(entry)
    return out
