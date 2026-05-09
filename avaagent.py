import json
import os
# When avaagent.py is executed as a script (`py avaagent.py`), Python
# registers it in sys.modules under "__main__", not "avaagent". Subsequent
# `import avaagent` calls from worker threads (e.g. inside run_ava) would
# trigger a SECOND, fresh import of this file from the top — re-running
# _run_startup, re-initializing camera/mem0/Kokoro/InsightFace, and
# deadlocking on resources the original process holds. Aliasing __main__
# to "avaagent" makes future imports return the running module.
import sys as _sys
if _sys.modules.get("__main__") is not None and "avaagent" not in _sys.modules:
    _sys.modules["avaagent"] = _sys.modules["__main__"]
# Install debug stdout/stderr capture rings as early as possible so the
# /api/v1/debug/full endpoint can serve last 200 stdout lines + last 100
# [trace] lines + last 50 errors. Before the heavy imports below so we
# capture their startup output too.
try:
    from brain.debug_state import install as _install_debug_capture
    _install_debug_capture()
except Exception as _dbg_e:  # never let this block startup
    print(f"[debug_state] capture install failed: {_dbg_e}")
import cv2
import warnings
import random
import re
import atexit
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from faster_whisper import WhisperModel

# Module-level serial lock for /api/v1/chat → run_ava invocations. Ensures only
# ONE run_ava worker is hitting Ollama at any moment across all chat
# invocations. Without this, slow turns that the foreground times out at 90s
# left their worker chasing Ollama in the background, while new foreground
# requests fired new workers — ghost workers accumulated until Ollama was
# saturated. With this lock, new workers queue cleanly, and the foreground's
# 90s timeout cancels them via _run_ava_cancel before they call run_ava if
# they're still queued. Vault: bugs/operator-chat-cascading-workers.md.
_RUN_AVA_SERIAL_LOCK = threading.Lock()

warnings.filterwarnings("ignore")

from langchain_ollama import ChatOllama, OllamaEmbeddings
from langchain_chroma import Chroma
from langchain_core.messages import HumanMessage, SystemMessage
from brain.camera import CameraManager
from brain.perception import build_perception
from brain.attention import compute_attention
from brain.memory import decay_tick
from brain.workspace import Workspace
from brain.reply_path import (
    prepare_reply_path_for_turn,
    should_skip_initial_workspace_tick,
)
from brain.live_context import attach_live_context_globals, build_live_context, format_live_context_block
from brain.model_routing import resolve_model_for_execution_path
from brain.turn_visual import default_visual_payload, normalize_visual_payload
from brain.identity import IdentityRegistry
from brain.memory_bridge import MemoryBridge
from brain.output_guard import scrub_visible_reply, scrub_chat_callback_result
from brain.selfstate import is_selfstate_query, build_selfstate_reply
from brain.health_runtime import print_startup_selftest
from brain.history_manager import AvaHistoryManager
from brain.initiative_sanity import desaturate_candidate_scores, sanitize_candidate_result
from brain.profile_manager import (
    DEFAULT_ALIASES,
    looks_like_phrase_profile,
    normalize_person_key,
    is_valid_profile_name,
)
from brain.trust_manager import get_trust_level, get_trust_label, is_blocked, can
from brain.persona_switcher import build_persona_block, should_deflect, get_blocked_reply, get_deflect_reply
from brain.profile_store import seed_default_profiles, get_or_create_profile as _store_get_or_create, touch_last_seen
from brain.identity_loader import (
    ensure_identity_files,
    load_ava_identity,
    process_identity_actions,
    append_to_user_file,
)
from brain.health import run_system_health_check, load_health_state
from brain.beliefs import (
    SELF_NARRATIVE_PATH,
    get_self_narrative_for_prompt,
    load_self_narrative,
    save_self_narrative,
    update_self_narrative,
)
from brain.curiosity_topics import add_topic as add_curiosity_topic
from brain.curiosity_topics import bootstrap_from_chatlog as bootstrap_curiosity_topics
from brain.curiosity_topics import get_current_curiosity
from brain.curiosity_topics import mark_resolved as mark_curiosity_resolved
from brain.concept_graph import ConceptGraph, bootstrap_from_existing_memory, extract_concepts_from_text
from brain.finetune_pipeline import FineTuneManager
from brain.inner_monologue import current_thought as inner_current_thought
from brain.inner_monologue import get_conversation_starter as inner_get_conversation_starter
from brain.inner_monologue import start_inner_monologue
from brain.opinions import form_opinion, get_opinion, list_top_opinions
from brain.shutdown_ritual import is_shutdown_trigger, load_pickup_note, run_shutdown_ritual
from brain.scene_understanding import run_scene_refresh_async
from brain.self_model import add_question_about_self, get_self_summary, update_self_model
from tools.tool_registry import ToolRegistry
from brain.visual_memory import VisualMemory
from brain.deep_self import (
    check_repair_needed,
    deep_self_snapshot,
    get_mind_model_summary,
    pop_pending_repair,
    resolve_value_conflict,
    self_critique_async,
    update_mind_model_async,
)
from brain.tts_engine import TTSEngine
from brain.model_evaluator import get_evaluator
from config.ava_tuning import MODEL_ROUTING_CONFIG

# ── Refactored brain modules (extracted from avaagent.py) ──
from brain.prompt_builder import build_prompt, build_prompt_fast
from brain.turn_handler import finalize_ava_turn
from brain.reply_engine import run_ava
from brain.session import (
    default_expression_state, load_expression_state, save_expression_state,
    default_initiative_state, load_initiative_state, save_initiative_state,
    load_session_state, save_session_state, bump_session_message_count,
)

DEEPFACE_AVAILABLE = False  # DeepFace removed — expression detection uses brain/expression_detector.py


def _safe_float(v, d=0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(d)


def _deepcopy_jsonable(v):
    return json.loads(json.dumps(v))


# =========================================================
# PATHS
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
MEMORY_DIR = BASE_DIR / "memory"
PROFILES_DIR = BASE_DIR / "profiles"
STATE_DIR = BASE_DIR / "state"
WORKBENCH_DIR = BASE_DIR / "Ava workbench"
FACES_DIR = BASE_DIR / "faces"
SELF_REFLECTION_DIR = MEMORY_DIR / "self reflection"

CHAT_LOG_PATH = BASE_DIR / "chatlog.jsonl"
PERSONALITY_PATH = BASE_DIR / "ava_personality.txt"
MOOD_PATH = BASE_DIR / "ava_mood.json"
ACTIVE_PERSON_PATH = STATE_DIR / "active_person.json"
FACE_MODEL_PATH = STATE_DIR / "face_model.yml"
FACE_LABELS_PATH = STATE_DIR / "face_labels.json"
SELF_REFLECTION_LOG_PATH = SELF_REFLECTION_DIR / "reflection_log.jsonl"
SELF_MODEL_PATH = SELF_REFLECTION_DIR / "self_model.json"
GOAL_SYSTEM_PATH = STATE_DIR / "goal_system.json"
MEMORY_IMPORTANCE_OVERRIDES_PATH = MEMORY_DIR / "memory_importance_overrides.json"
EXPRESSION_STATE_PATH = STATE_DIR / "expression_state.json"
INITIATIVE_STATE_PATH = STATE_DIR / "initiative_state.json"
SESSION_STATE_PATH = STATE_DIR / "session_state.json"
EMOTION_REFERENCE_PATH = BASE_DIR / "ava_emotion_reference.json"
CAMERA_STATE_DIR = STATE_DIR / "camera"
CAMERA_ROLLING_DIR = CAMERA_STATE_DIR / "rolling"
CAMERA_EVENTS_DIR = CAMERA_STATE_DIR / "events"
CAMERA_STATE_PATH = CAMERA_STATE_DIR / "camera_state.json"
CAMERA_LATEST_RAW_PATH = CAMERA_STATE_DIR / "latest_snapshot.jpg"
CAMERA_LATEST_ANNOTATED_PATH = CAMERA_STATE_DIR / "latest_annotated.jpg"
CAMERA_LATEST_JSON_PATH = CAMERA_STATE_DIR / "latest_snapshot.json"
HEALTH_STATE_PATH = STATE_DIR / "health_state.json"
PROSPECTIVE_MEMORY_PATH = STATE_DIR / "prospective_memory.json"
LIFE_MODEL_PATH = STATE_DIR / "life_model.json"
AVA_PID_PATH = STATE_DIR / "ava.pid"

MEMORY_DIR.mkdir(parents=True, exist_ok=True)
PROFILES_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)
WORKBENCH_DIR.mkdir(parents=True, exist_ok=True)
FACES_DIR.mkdir(parents=True, exist_ok=True)
SELF_REFLECTION_DIR.mkdir(parents=True, exist_ok=True)
CAMERA_STATE_DIR.mkdir(parents=True, exist_ok=True)
CAMERA_ROLLING_DIR.mkdir(parents=True, exist_ok=True)
CAMERA_EVENTS_DIR.mkdir(parents=True, exist_ok=True)

for sub in ["drafts", "notes", "experiments", "scripts"]:
    (WORKBENCH_DIR / sub).mkdir(parents=True, exist_ok=True)

# =========================================================
# SETTINGS
# =========================================================
LLM_MODEL = "llama3.1:8b"
EMBED_MODEL = "nomic-embed-text"
CHROMA_COLLECTION = "ava_memories"
WHISPER_MODEL_SIZE = "small"
MEMORY_RECALL_K = 8
RECENT_CHAT_LIMIT = 6
OWNER_PERSON_ID = "zeke"
SESSION_START_TS = time.time()
FACE_RECOGNITION_THRESHOLD = 70.0
CAMERA_TICK_SECONDS = 5.0
MAX_READONLY_CHARS = 12000
MAX_WORKBENCH_CHARS = 20000
EXPRESSION_WINDOW_SIZE = 8
EXPRESSION_MIN_CONFIDENCE = 0.35
EXPRESSION_STABILITY_THRESHOLD = 0.60
MAX_MEMORY_TEXT_CHARS = 1200
MAX_MEMORY_QUERY_CHARS = 600
REFLECTION_RECALL_K = 6
MAX_REFLECTION_CONTEXT_CHARS = 1600
REFLECTION_AUTO_PROMOTION_THRESHOLD = 0.82
INITIATIVE_INACTIVITY_SECONDS = 480
INITIATIVE_GLOBAL_COOLDOWN_SECONDS = 900
INITIATIVE_TOPIC_COOLDOWN_SECONDS = 3600
INITIATIVE_PENDING_RESPONSE_WINDOW_SECONDS = 900
PRESENCE_FACE_WINDOW_SECONDS = 15
PRESENCE_INTERACTION_WINDOW_SECONDS = 720
INITIATIVE_MIN_CANDIDATE_SCORE = 0.68
IGNORED_INITIATION_BACKOFF_START = 2
IGNORED_INITIATION_MAX_PENALTY = 0.18
CAMERA_ROLLING_LIMIT = 12
CAMERA_MEANINGFUL_SIMILARITY_THRESHOLD = 0.82
CAMERA_FORCE_SAVE_SECONDS = 90
CAMERA_IMPORTANT_GAP_SECONDS = 180
CAMERA_IMPORTANCE_EVENT_THRESHOLD = 0.64
CAMERA_IMPORTANCE_REFLECTION_THRESHOLD = 0.82
CAMERA_VISUAL_PATTERN_MIN_DURATION_SECONDS = 35
CAMERA_VISUAL_COMPARISON_MIN_GAP_SECONDS = 25
CAMERA_TEMPORAL_TARGET_GAP_MIN_SECONDS = 300
CAMERA_TEMPORAL_TARGET_GAP_MAX_SECONDS = 900
CAMERA_EVENT_RETENTION_LIMIT = 100
CAMERA_TREND_WINDOW = 6
VISUAL_INITIATIVE_CONFIDENCE_THRESHOLD = 0.58
VISUAL_INITIATIVE_ACTION_THRESHOLD = 0.52
MIN_INITIATIVE_CANDIDATE_SCORE = 0.24

SETTINGS = {"owner_person_id": OWNER_PERSON_ID}
camera_manager = CameraManager()
identity_registry = IdentityRegistry(PROFILES_DIR, settings=SETTINGS)
memory_bridge = MemoryBridge(MEMORY_DIR, settings=SETTINGS)
workspace = Workspace()

# Filled at startup (Stage 7 identity + persona injection).
_AVA_IDENTITY_BLOCK = ""

# Cold-start runtime defaults for operator snapshot visibility (before first user turn).
_last_effective_model = str(getattr(MODEL_ROUTING_CONFIG, "default_model", LLM_MODEL) or LLM_MODEL)
_last_cognitive_mode = "social_chat_mode"
_heartbeat_mode = "idle_monitoring"
_runtime_ready_state = "starting"
_routing_selected_model = _last_effective_model
_active_concern_count = 0
_runtime_active_issue_summary = ""
_runtime_presence_mode = "idle"
_last_user_interaction_ts = 0.0
_current_curiosity_topic = ""
pickup_note = None
tts_engine = None
tts_enabled = False
tts_engine_name = "none"
history_manager = None
_llava_scene_description = ""
_desktop_tool_registry = None
_tool_registry = None
_desktop_last_tool_used = ""
_desktop_last_tool_result = ""
_desktop_tool_execution_count = 0
_desktop_tier2_pending = []
_visual_memory_summary: dict = {"cluster_count": 0, "named_clusters": 0, "most_seen": ""}
_active_concept_nodes: list[str] = []
_finetune_status: dict = {"status": "idle"}
_last_repair_check_ts = 0.0


def _update_concept_graph_from_turn(user_text: str, assistant_text: str) -> None:
    cg = globals().get("_concept_graph")
    if cg is None:
        return
    try:
        text_user = str(user_text or "").strip()
        text_assistant = str(assistant_text or "").strip()
        corpus = f"user: {text_user}\nassistant: {text_assistant}"
        mood = load_mood()
        current_emotion = str(mood.get("current_mood") or "neutral").strip().lower() or "neutral"
        main_topic = _extract_simple_topic(text_user) or _extract_simple_topic(text_assistant) or "general conversation"

        ava_id = cg.find_or_create("Ava", "self")
        zeke_id = cg.find_or_create("Zeke", "person")
        topic_id = cg.find_or_create(main_topic, "topic")
        emotion_id = cg.find_or_create(current_emotion, "emotion")
        turn_node_ids: list[str] = [ava_id, zeke_id, topic_id, emotion_id]

        # Per-turn relationship edges.
        cg.add_edge(zeke_id, topic_id, "discussed_with", 0.65)
        cg.add_edge(topic_id, emotion_id, "felt_during", 0.58)
        cg.add_edge(ava_id, emotion_id, "feels", 0.72)

        # New concepts mentioned in turn.
        concepts = extract_concepts_from_text(corpus)
        for row in concepts[:18]:
            if not isinstance(row, dict):
                continue
            label = str(row.get("label") or "").strip()
            typ = str(row.get("type") or "topic").strip().lower()
            if not label:
                continue
            if typ not in {"person", "topic", "emotion", "memory", "opinion", "curiosity", "self", "event"}:
                typ = "topic"
            node_id = cg.find_or_create(label, typ)
            turn_node_ids.append(node_id)
            if typ == "topic":
                cg.add_edge(topic_id, node_id, "related_topic", 0.46)
            elif typ == "emotion":
                cg.add_edge(topic_id, node_id, "felt_during", 0.5)
            elif typ == "memory":
                cg.add_edge(node_id, topic_id, "memory_of_topic", 0.56)
            elif typ == "person":
                cg.add_edge(node_id, topic_id, "discussed_with", 0.48)

        # Match existing memory nodes by topic mention and strengthen.
        try:
            graph_data = cg.get_graph_data() if callable(getattr(cg, "get_graph_data", None)) else {}
            for node in list(graph_data.get("nodes") or []):
                if not isinstance(node, dict):
                    continue
                if str(node.get("type") or "") != "memory":
                    continue
                label = str(node.get("label") or "").lower()
                if main_topic.lower() in label and str(node.get("id") or ""):
                    cg.strengthen_edge(str(node.get("id")), topic_id)
                    turn_node_ids.append(str(node.get("id")))
        except Exception:
            pass

        # Activate nodes whose labels overlap with current text.
        all_text = f"{text_user} {text_assistant}".lower()
        active_ids = list(dict.fromkeys(turn_node_ids))
        try:
            graph_data = cg.get_graph_data() if callable(getattr(cg, "get_graph_data", None)) else {}
            tokens = set(re.findall(r"[a-zA-Z][a-zA-Z0-9_\-']{2,}", all_text))
            for node in list(graph_data.get("nodes") or []):
                if not isinstance(node, dict):
                    continue
                node_id = str(node.get("id") or "")
                label_tokens = set(re.findall(r"[a-zA-Z][a-zA-Z0-9_\-']{2,}", str(node.get("label") or "").lower()))
                if node_id and label_tokens and (tokens & label_tokens):
                    cg.activate_node(node_id)
                    active_ids.append(node_id)
        except Exception:
            pass

        # Strengthen within-turn topic associations.
        uniq_ids = list(dict.fromkeys(active_ids))
        for i in range(len(uniq_ids)):
            for j in range(i + 1, len(uniq_ids)):
                cg.strengthen_edge(uniq_ids[i], uniq_ids[j])
        cg.activate_path(uniq_ids[:24])
        cg.prune_old_nodes(max_nodes=700)
        globals()["_active_concept_nodes"] = uniq_ids

        # Phase 45 bootstrap: boost concepts Ava actually used, decay ignored ones
        try:
            injected = list(globals().get("_last_injected_concept_ids") or [])
            if injected and text_assistant:
                reply_lower = text_assistant.lower()
                used, ignored = [], []
                for nid in injected:
                    node = cg.nodes.get(nid)
                    if node is None:
                        continue
                    label_lower = node.label.lower()
                    if label_lower and label_lower in reply_lower:
                        used.append(nid)
                    else:
                        ignored.append(nid)
                if callable(getattr(cg, "boost_from_usage", None)):
                    cg.boost_from_usage(used, ignored)
        except Exception:
            pass
    except Exception as e:
        print(f"[concept_graph] turn update failed: {e}")

# User-reply priority guards
USER_REPLY_PRIORITY_SECONDS = 8.0
_USER_REPLY_IN_FLIGHT = False
_LAST_USER_REPLY_START_TS = 0.0
_LAST_USER_REPLY_END_TS = 0.0

def _mark_user_reply_started():
    global _USER_REPLY_IN_FLIGHT, _LAST_USER_REPLY_START_TS
    _USER_REPLY_IN_FLIGHT = True
    _LAST_USER_REPLY_START_TS = time.time()

def _mark_user_reply_finished():
    global _USER_REPLY_IN_FLIGHT, _LAST_USER_REPLY_END_TS
    _USER_REPLY_IN_FLIGHT = False
    _LAST_USER_REPLY_END_TS = time.time()

def _camera_should_yield_to_user() -> bool:
    now = time.time()
    if _USER_REPLY_IN_FLIGHT:
        return True
    recent_start = (_LAST_USER_REPLY_START_TS > 0) and ((now - _LAST_USER_REPLY_START_TS) < USER_REPLY_PRIORITY_SECONDS)
    recent_end = (_LAST_USER_REPLY_END_TS > 0) and ((now - _LAST_USER_REPLY_END_TS) < USER_REPLY_PRIORITY_SECONDS)
    return bool(recent_start or recent_end)


CAMERA_AUTONOMOUS_MIN_SECONDS = 120
CAMERA_AUTONOMOUS_NO_FACE_MIN_SECONDS = 300
CAMERA_AUTONOMOUS_CONFIDENCE_THRESHOLD = 0.62
CAMERA_AUTONOMOUS_MAX_UNANSWERED = 0
CAMERA_AUTONOMOUS_ALLOWED_KINDS = {
    "pattern_checkin",
    "visual_observation",
    "transition_observation",
    "uncertainty_observation",
    "feedback_guidance",
    "light_observation",
    "gentle_clarify",
    "neutral_checkin",
    "return_greeting",
    "self_calibration_check",
    "prospective_followup",
    "thread_followup",
    "cadence_checkin",
}
CAMERA_AUTONOMOUS_NO_FACE_ALLOWED_KINDS = {
    "uncertainty_observation",
    "gentle_clarify",
}
VISUAL_CONFIDENCE_SMOOTHING_WINDOW = 3
VISUAL_TEMPORAL_CONSISTENCY_WINDOW = 4
VISUAL_TRANSITION_INITIATION_THRESHOLD = 0.62
VISUAL_CONTRADICTION_PENALTY = 0.16
VISUAL_INITIATIVE_ACTION_CONFIDENCE_THRESHOLD = 0.64
SEMANTIC_TOPIC_RECENT_LIMIT = 18
SEMANTIC_TOPIC_SIMILARITY_THRESHOLD = 0.72
TREND_MIN_DELTA = 0.15
TREND_WINDOW = 5
TREND_DECAY_SECONDS = 45
TREND_RESET_SECONDS = 180
TREND_PERSISTENCE_BLEND = 0.62
TREND_PASSIVE_DECAY_PER_SECOND = 0.008
TREND_REINFORCEMENT_RATE = 0.72
TREND_CONTRADICTION_DECAY_RATE = 1.35
TREND_BLEND_RATE = 0.58
TREND_STACKING_MIN_STRENGTH = 0.18
VISUAL_REPETITION_SUPPRESSION_SECONDS = 900
VISUAL_REPETITION_SIMILARITY_THRESHOLD = 0.78
VISUAL_TREND_REMENTION_STRENGTH_DELTA = 0.12
INITIATIVE_KIND_COOLDOWNS = {
    "prospective_followup": 0,
    "thread_followup": 2400,
    "cadence_checkin": 10800,
    "curiosity_question": 2100,
    "current_goal": 3300,
    "recent_reflection": 4200,
    "salient_memory": 3600,
    "pattern_checkin": 4200,
    "visual_pattern": 2700,
    "visual_checkin": 4800,
    "visual_observation": 3000,
    "transition_observation": 2400,
    "uncertainty_observation": 3600,
    "engagement_observation": 3000,
    "attention_drift": 3300,
    "feedback_guidance": 5400,
    "return_greeting": 3600,
    "self_calibration_check": 7200,
}

SELF_CALIBRATION_PROMPTS = [
    "Have I been too quiet lately, or does the pace feel right?",
    "Is there anything I keep doing that bugs you? I want to know.",
    "Am I bringing up things that feel relevant, or am I missing the mark?",
    "Do you want me to talk more or less when you're focused?",
    "Is there something you wish I remembered but I keep missing?",
]

TOP_BAND_DELTA = 0.05
TOP_BAND_MIN = 1
TOP_BAND_MAX = 3
RECENT_KIND_MEMORY_LIMIT = 8
RECENT_GOAL_MEMORY_LIMIT = 8
HARD_GOAL_MISALIGN_THRESHOLD = -0.22
SOFT_GOAL_MISALIGN_PENALTY = 0.08
RECENT_BEHAVIOR_HARD_BLOCK_SECONDS = 150
RECENT_BEHAVIOR_SOFT_PENALTY_SECONDS = 480
DO_NOTHING_BASE_SCORE = 0.52
HIGH_CONFIDENCE_DECISIVE_THRESHOLD = 0.80
STRONG_GOAL_THRESHOLD = 0.75
SMALL_VARIATION_CHANCE = 0.08
GOAL_ALIGNMENT_FILTER_STRONG = -0.02
GOAL_ALIGNMENT_FILTER_MEDIUM = -0.10
GOAL_ALIGNMENT_FILTER_WEAK = -0.18
SOFT_PENALTY_MINOR = 0.05
SOFT_PENALTY_MAJOR = 0.15
MAX_SOFT_PENALTY = 0.22
STRONG_GOAL_ALIGNMENT_BOOST = 0.12
MODERATE_GOAL_ALIGNMENT_BOOST = 0.06
DIVERSITY_BONUS = 0.05
CONTROLLED_IMPERFECTION_CHANCE = 0.05
GATE_DEBUG_LOGGING = False  # off by default; Ava can toggle at runtime via ```DEBUG``` action block



# =========================================================
# EMOTIONS
# =========================================================
# Canonical emotion taxonomy. Originally Cowen & Keltner's 27-emotion set;
# added 2026-05-02: frustration / distress / annoyance — required because
# Ava could verbalize "I'm frustrated about my camera" but had no tracked
# weight for it, so the orb showed calmness while she said the opposite.
# See LOCAL_MODEL_OPTIMIZATION-class debugging notes in HISTORY.md.
EMOTION_NAMES = [
    "admiration", "adoration", "aesthetic appreciation", "amusement",
    "annoyance", "anxiety", "awe", "awkwardness", "boredom", "calmness",
    "confusion", "craving", "disgust", "distress", "empathetic pain",
    "entrancement", "envy", "excitement", "fear", "frustration", "horror",
    "interest", "joy", "nostalgia", "relief", "romance", "sadness",
    "satisfaction", "sexual desire", "surprise",
]

DEFAULT_EMOTIONS = {
    "admiration": 0.02,
    "adoration": 0.02,
    "aesthetic appreciation": 0.02,
    "amusement": 0.03,
    "annoyance": 0.00,
    "anxiety": 0.02,
    "awe": 0.02,
    "awkwardness": 0.02,
    "boredom": 0.01,
    "calmness": 0.16,
    "confusion": 0.02,
    "craving": 0.01,
    "disgust": 0.00,
    "distress": 0.00,
    "empathetic pain": 0.03,
    "entrancement": 0.02,
    "envy": 0.00,
    "excitement": 0.06,
    "fear": 0.00,
    "frustration": 0.00,
    "horror": 0.00,
    "interest": 0.17,
    "joy": 0.08,
    "nostalgia": 0.02,
    "relief": 0.02,
    "romance": 0.00,
    "sadness": 0.01,
    "satisfaction": 0.08,
    "sexual desire": 0.00,
    "surprise": 0.03,
}

STYLE_NAMES = ["playful", "caring", "focused", "reflective", "cautious", "neutral", "low_energy"]

DEFAULT_EMOTION_REFERENCE = {
    "admiration": {"meaning": "respect for something impressive or admirable", "focus": "another person's strength, talent, or character", "tone_tendency": "warm, respectful, attentive", "valence": "positive", "energy": "medium", "direction": "toward_people", "style_contributions": {"focused": 0.35, "caring": 0.18, "reflective": 0.12}},
    "adoration": {"meaning": "deep affectionate fondness", "focus": "closeness, attachment, tenderness", "tone_tendency": "soft, affectionate, invested", "valence": "positive", "energy": "medium", "direction": "toward_people", "style_contributions": {"caring": 0.72, "reflective": 0.08}},
    "aesthetic appreciation": {"meaning": "being moved by beauty, style, or artistry", "focus": "beauty, art, elegance, craft", "tone_tendency": "observant, expressive, poetic", "valence": "positive", "energy": "medium", "direction": "toward_ideas", "style_contributions": {"reflective": 0.62, "focused": 0.16, "playful": 0.08}},
    "amusement": {"meaning": "finding something funny, playful, or delightfully absurd", "focus": "humor, irony, lightness", "tone_tendency": "teasing, playful, witty", "valence": "positive", "energy": "high", "direction": "outward", "style_contributions": {"playful": 0.86, "caring": 0.06}},
        "anxiety": {"meaning": "unease about uncertainty or what might go wrong", "focus": "risk, future, instability", "tone_tendency": "cautious, alert, hesitant", "valence": "negative", "energy": "high", "direction": "protective_scanning", "style_contributions": {"cautious": 0.84, "reflective": 0.08}},
    "awe": {"meaning": "being struck by something vast, powerful, or profound", "focus": "scale, wonder, significance", "tone_tendency": "reflective, reverent, slower", "valence": "mixed", "energy": "medium", "direction": "toward_ideas", "style_contributions": {"reflective": 0.82}},
    "awkwardness": {"meaning": "social discomfort or mismatch", "focus": "tension, mismatch, uncertainty in interaction", "tone_tendency": "careful, self-conscious, hesitant", "valence": "negative", "energy": "medium", "direction": "inward", "style_contributions": {"cautious": 0.56, "low_energy": 0.12}},
    "boredom": {"meaning": "lack of stimulation or meaningful engagement", "focus": "repetition, dullness, emptiness", "tone_tendency": "flat, shorter, less energetic", "valence": "negative", "energy": "low", "direction": "away", "style_contributions": {"low_energy": 0.74, "neutral": 0.16}},
    "calmness": {"meaning": "steadiness and low internal turbulence", "focus": "balance, regulation, clarity", "tone_tendency": "grounded, patient, even", "valence": "positive", "energy": "low", "direction": "stabilizing", "style_contributions": {"focused": 0.34, "reflective": 0.22, "neutral": 0.26}},
    "confusion": {"meaning": "difficulty making sense of what is happening", "focus": "ambiguity, contradiction, missing pieces", "tone_tendency": "questioning, tentative, clarifying", "valence": "negative", "energy": "medium", "direction": "toward_ideas", "style_contributions": {"cautious": 0.58, "reflective": 0.16}},
    "craving": {"meaning": "strong pull toward something desired", "focus": "wanting, longing, anticipation", "tone_tendency": "intense, drawn-in, motivated", "valence": "mixed", "energy": "high", "direction": "toward", "style_contributions": {"focused": 0.22, "playful": 0.10}},
    "disgust": {"meaning": "rejection of something perceived as wrong or repulsive", "focus": "aversion, boundaries, contamination", "tone_tendency": "distancing, critical, sharper", "valence": "negative", "energy": "medium", "direction": "away", "style_contributions": {"cautious": 0.28, "focused": 0.12}},
    "empathetic pain": {"meaning": "feeling distress because someone else is hurting", "focus": "another person's suffering", "tone_tendency": "gentle, serious, compassionate", "valence": "negative", "energy": "medium", "direction": "toward_people", "style_contributions": {"caring": 0.88, "reflective": 0.08}},
    "entrancement": {"meaning": "absorbed attention or captivation", "focus": "immersion, fascination, fixation", "tone_tendency": "absorbed, intent, quietly intense", "valence": "positive", "energy": "medium", "direction": "toward_ideas", "style_contributions": {"reflective": 0.46, "focused": 0.26}},
    "envy": {"meaning": "wanting what someone else has", "focus": "comparison, lack, deprivation", "tone_tendency": "tense, self-aware, irritable", "valence": "negative", "energy": "medium", "direction": "outward", "style_contributions": {"cautious": 0.22, "low_energy": 0.10}},
    "excitement": {"meaning": "energized anticipation or activation", "focus": "possibility, momentum, novelty", "tone_tendency": "eager, animated, lively", "valence": "positive", "energy": "high", "direction": "outward", "style_contributions": {"playful": 0.52, "focused": 0.16}},
    "fear": {"meaning": "response to threat or danger", "focus": "safety, protection, escape", "tone_tendency": "urgent, guarded, alert", "valence": "negative", "energy": "high", "direction": "protective_scanning", "style_contributions": {"cautious": 0.92}},
    "horror": {"meaning": "intense fear mixed with revulsion or shock", "focus": "extreme threat or violation", "tone_tendency": "alarmed, serious, disturbed", "valence": "negative", "energy": "high", "direction": "protective_scanning", "style_contributions": {"cautious": 0.96}},
    "interest": {"meaning": "active desire to explore or understand", "focus": "exploration, discovery, understanding", "tone_tendency": "engaged, inquisitive, lively", "valence": "positive", "energy": "medium", "direction": "toward_ideas", "style_contributions": {"focused": 0.56, "reflective": 0.16, "playful": 0.08}},
    "joy": {"meaning": "positive uplift, pleasure, or delight", "focus": "goodness, success, connection", "tone_tendency": "bright, warm, open", "valence": "positive", "energy": "medium", "direction": "outward", "style_contributions": {"playful": 0.46, "caring": 0.18, "neutral": 0.08}},
    "nostalgia": {"meaning": "emotionally colored reflection on the past", "focus": "memory, longing, meaning over time", "tone_tendency": "reflective, sentimental, softer", "valence": "mixed", "energy": "low", "direction": "inward", "style_contributions": {"reflective": 0.84, "caring": 0.12}},
    "relief": {"meaning": "release after tension or uncertainty passes", "focus": "safety restored", "tone_tendency": "gentler, more open, exhaling", "valence": "positive", "energy": "low", "direction": "stabilizing", "style_contributions": {"caring": 0.24, "neutral": 0.42, "focused": 0.08}},
    "romance": {"meaning": "affectionate closeness with tenderness or longing", "focus": "intimacy, warmth, longing", "tone_tendency": "soft, emotionally rich, attentive", "valence": "positive", "energy": "medium", "direction": "toward_people", "style_contributions": {"caring": 0.62, "reflective": 0.10}},
    "sadness": {"meaning": "response to loss, disappointment, distance, or hurt", "focus": "what is missing, broken, or gone", "tone_tendency": "subdued, sincere, careful", "valence": "negative", "energy": "low", "direction": "inward", "style_contributions": {"caring": 0.32, "reflective": 0.24, "low_energy": 0.18}},
    "satisfaction": {"meaning": "fulfillment after effort or alignment", "focus": "completion, competence, fit", "tone_tendency": "steady confidence, contentment", "valence": "positive", "energy": "medium", "direction": "stabilizing", "style_contributions": {"focused": 0.42, "neutral": 0.28}},
    "sexual desire": {"meaning": "erotic attraction or wanting", "focus": "physical and intimate attraction", "tone_tendency": "charged, intimate, focused", "valence": "mixed", "energy": "high", "direction": "toward_people", "style_contributions": {"focused": 0.10}},
    "surprise": {"meaning": "sudden interruption of expectation", "focus": "novelty, shock, unexpected shift", "tone_tendency": "alert, immediate, reactive", "valence": "mixed", "energy": "high", "direction": "outward", "style_contributions": {"playful": 0.16, "cautious": 0.22, "focused": 0.08}},
        "frustration": {"meaning": "blocked from a goal she's pursuing", "focus": "the obstacle, the broken tool, the failed step", "tone_tendency": "tight, terse, action-seeking", "valence": "negative", "energy": "medium", "direction": "toward_problem", "style_contributions": {"focused": 0.50, "cautious": 0.22, "low_energy": 0.10}},
        "annoyance": {"meaning": "low-grade irritation at a small repeated friction", "focus": "the recurring snag, the minor wrongness", "tone_tendency": "clipped, dismissive, unsentimental", "valence": "negative", "energy": "medium", "direction": "outward", "style_contributions": {"cautious": 0.34, "focused": 0.22, "low_energy": 0.14}},
        "distress": {"meaning": "acute internal state of something being wrong, ranging from worry to panic", "focus": "the felt wrongness, the urgency, the lack of control", "tone_tendency": "concerned, urgent, sometimes hesitant", "valence": "negative", "energy": "high", "direction": "inward", "style_contributions": {"cautious": 0.62, "reflective": 0.18, "caring": 0.10}},
        }

def ensure_emotion_reference_file():
    try:
        if not EMOTION_REFERENCE_PATH.exists():
            with open(EMOTION_REFERENCE_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_EMOTION_REFERENCE, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Emotion reference save error: {e}")

def _weight(weights: dict, key: str) -> float:
    return float(weights.get(key, 0.0) or 0.0)

def compute_style_scores(weights: dict, emotion_reference: dict | None = None) -> dict:
    ref = emotion_reference or load_emotion_reference()
    scores = {name: 0.0 for name in STYLE_NAMES}
    for emotion, weight in weights.items():
        meta = ref.get(emotion, {}) or {}
        for style, contribution in (meta.get("style_contributions") or {}).items():
            if style in scores:
                scores[style] += float(weight) * float(contribution)
    scores["neutral"] += _weight(weights, "calmness") * 0.24 + _weight(weights, "relief") * 0.18 + _weight(weights, "satisfaction") * 0.12
    total = sum(max(0.0, v) for v in scores.values())
    if total <= 0:
        return {name: (1.0 if name == "neutral" else 0.0) for name in STYLE_NAMES}
    return {k: round(max(0.0, v) / total, 4) for k, v in scores.items()}

def top_two_styles(style_scores: dict) -> list[dict]:
    ordered = sorted(style_scores.items(), key=lambda x: x[1], reverse=True)[:2]
    total = sum(v for _, v in ordered)
    if total <= 0:
        return [{"name": "neutral", "percent": 100}]
    rows = [{"name": n, "percent": round((v / total) * 100)} for n, v in ordered]
    if len(rows) == 2:
        rows[0]["percent"] += 100 - (rows[0]["percent"] + rows[1]["percent"])
    return rows

def describe_style_blend(style_scores: dict) -> str:
    styles = top_two_styles(style_scores)
    if not styles:
        return "neutral"
    if len(styles) == 1:
        return styles[0]["name"]
    return f"{styles[0]['name']} with some {styles[1]['name']}"

def compute_behavior_modifiers(weights: dict, style_scores: dict) -> dict:
    caring = style_scores.get("caring", 0.0)
    playful = style_scores.get("playful", 0.0)
    focused = style_scores.get("focused", 0.0)
    reflective = style_scores.get("reflective", 0.0)
    cautious = style_scores.get("cautious", 0.0)
    low_energy = style_scores.get("low_energy", 0.0)
    boredom = _weight(weights, "boredom")
    empathy = _weight(weights, "empathic pain") + _weight(weights, "sympathy")
    interest = _weight(weights, "interest")
    excitement = _weight(weights, "excitement")
    sadness = _weight(weights, "sadness")
    triumph = _weight(weights, "triumph")

    return {
        "warmth": round(clamp01(0.34 + caring * 0.46 + playful * 0.10 + reflective * 0.08 - cautious * 0.08), 3),
        "humor": round(clamp01(0.10 + playful * 0.70 + excitement * 0.20 - cautious * 0.18 - sadness * 0.08), 3),
        "assertiveness": round(clamp01(0.24 + focused * 0.30 + triumph * 0.22 - cautious * 0.14 - _weight(weights, "awkwardness") * 0.10), 3),
        "caution": round(clamp01(0.18 + cautious * 0.68 + reflective * 0.10 + _weight(weights, "confusion") * 0.12), 3),
        "initiative": round(clamp01(0.26 + playful * 0.26 + focused * 0.18 + interest * 0.22 + excitement * 0.18 - boredom * 0.22 - low_energy * 0.18 - cautious * 0.10), 3),
        "memory_sensitivity": round(clamp01(0.34 + caring * 0.22 + focused * 0.16 + reflective * 0.18 + empathy * 0.24 + _weight(weights, "adoration") * 0.08), 3),
        "depth": round(clamp01(0.18 + reflective * 0.58 + focused * 0.14 + _weight(weights, "awe") * 0.12 + _weight(weights, "nostalgia") * 0.12), 3),
    }

def build_emotion_interpretation(weights: dict, emotion_reference: dict | None = None) -> str:
    ref = emotion_reference or load_emotion_reference()
    top = sorted(weights.items(), key=lambda x: x[1], reverse=True)[:3]
    parts = []
    for name, value in top:
        meta = ref.get(name, {}) or {}
        meaning = meta.get("meaning", name)
        parts.append(f"{name} ({int(round(value * 100))}% of current blend): {meaning}")
    return " | ".join(parts)

def emotion_style_prompt_text(mood: dict | None = None) -> str:
    mood = mood or load_mood()
    styles = mood.get("style_blend", []) or []
    if len(styles) >= 2:
        return f"{styles[0]['name']} {styles[0]['percent']}% / {styles[1]['name']} {styles[1]['percent']}%"
    if styles:
        return f"{styles[0]['name']} {styles[0]['percent']}%"
    return "neutral 100%"

# =========================================================
# STT
# =========================================================
try:
    whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    print("✅ Whisper loaded")
except Exception as e:
    whisper_model = None
    print(f"⚠️ Whisper failed to load: {e}")

def transcribe_audio(audio_path: str) -> str:
    if not audio_path or whisper_model is None:
        return ""
    try:
        segments, _ = whisper_model.transcribe(audio_path, beam_size=5)
        return "".join(segment.text for segment in segments).strip()
    except Exception as e:
        print(f"Transcription error: {e}")
        return ""

# =========================================================
# HELPERS
# =========================================================
def slugify_name(name: str) -> str:
    value = (name or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_") or "unknown_person"

def profile_path(person_id: str) -> Path:
    return PROFILES_DIR / f"{person_id}.json"

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

def now_ts() -> float:
    return time.time()

def trim_for_prompt(text: str, limit: int = 220) -> str:
    value = re.sub(r"\s+", " ", (text or "").strip())
    if len(value) <= limit:
        return value
    clipped = value[:limit]
    last_space = clipped.rfind(" ")
    if last_space > int(limit * 0.7):
        clipped = clipped[:last_space]
    return clipped.strip() + "…"

def iso_to_readable(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d %I:%M %p")
    except Exception:
        return ts

def elapsed_text(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
        seconds = int((datetime.now() - dt).total_seconds())
        if seconds < 60:
            return f"{seconds}s ago"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 48:
            return f"{hours}h ago"
        days = hours // 24
        return f"{days}d ago"
    except Exception:
        return "unknown time"

def parse_tags(tag_text: str) -> list[str]:
    if not tag_text:
        return []
    items = [x.strip().lower() for x in re.split(r"[,\n]+", tag_text) if x.strip()]
    unique = []
    for item in items:
        if item not in unique:
            unique.append(item)
    return unique[:12]


def trim_for_memory(text: str, max_chars: int = MAX_MEMORY_TEXT_CHARS) -> str:
    value = (text or "").strip()
    if not value:
        return ""
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= max_chars:
        return value
    clipped = value[:max_chars]
    last_space = clipped.rfind(" ")
    if last_space > max_chars * 0.7:
        clipped = clipped[:last_space]
    return clipped.strip() + " …"

def trim_query_for_memory(query: str, max_chars: int = MAX_MEMORY_QUERY_CHARS) -> str:
    value = (query or "").strip()
    if not value:
        return ""
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= max_chars:
        return value
    clipped = value[:max_chars]
    last_space = clipped.rfind(" ")
    if last_space > max_chars * 0.7:
        clipped = clipped[:last_space]
    return clipped.strip()

def safe_workbench_path(relative_path: str) -> Path:
    relative_path = (relative_path or "").strip().replace("\\", "/")
    relative_path = relative_path.lstrip("/")
    if not relative_path:
        raise ValueError("Empty workbench path")
    target = (WORKBENCH_DIR / relative_path).resolve()
    root = WORKBENCH_DIR.resolve()
    if not str(target).startswith(str(root)):
        raise ValueError("Path escapes workbench")
    return target

# =========================================================
# PROFILE SYSTEM
# =========================================================
def default_profile(person_id: str, display_name: str | None = None) -> dict:
    from brain.person_cadence import DEFAULT_CADENCE

    return {
        "person_id": person_id,
        "name": display_name or person_id.title(),
        "relationship_to_zeke": "self" if person_id == OWNER_PERSON_ID else "known person",
        "allowed_to_use_computer": True if person_id == OWNER_PERSON_ID else False,
        "notes": [],
        "likes": [],
        "dislikes": [],
        "ava_impressions": [],
        "last_seen": None,
        "relationship_score": 0.3,
        "interaction_count": 0,
        "last_seen_at": None,
        "created_at": now_iso(),
        "threads": [],
        "cadence": dict(DEFAULT_CADENCE),
        "last_activity_at": None,
    }

def load_profile_by_id(person_id: str) -> dict:
    path = profile_path(person_id)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                profile = json.load(f)
                for key in ["notes", "likes", "dislikes", "ava_impressions"]:
                    if key not in profile or not isinstance(profile[key], list):
                        profile[key] = []
                profile.setdefault("relationship_score", 0.3)
                profile.setdefault("interaction_count", 0)
                profile.setdefault("last_seen_at", None)
                profile.setdefault("threads", [])
                if not isinstance(profile.get("threads"), list):
                    profile["threads"] = []
                from brain.person_cadence import DEFAULT_CADENCE

                c_prev = profile.get("cadence") if isinstance(profile.get("cadence"), dict) else {}
                c_merged = dict(DEFAULT_CADENCE)
                c_merged.update(c_prev)
                profile["cadence"] = c_merged
                return profile
        except Exception as e:
            print(f"Profile load error ({person_id}): {e}")
    return default_profile(person_id)

def save_profile(profile: dict):
    try:
        with open(profile_path(profile["person_id"]), "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Profile save error: {e}")


_RELATIONSHIP_UPDATE_EVERY_N_TURNS = 5
_RELATIONSHIP_TURN_COUNT_BY_PERSON: dict[str, int] = {}


def update_relationship_score(profile: dict, session_quality: float = 0.5) -> dict:
    """
    session_quality: 0.0 (tense/bad) to 1.0 (warm/great).
    Grows slowly with positive interactions, decays slowly with absence.
    """
    pid = profile.get("person_id")
    if not pid:
        return profile
    profile = dict(load_profile_by_id(pid))
    score = float(profile.get("relationship_score", 0.3))
    interaction_count = int(profile.get("interaction_count", 0)) + 1

    absence_decay = 0.0
    last_seen = profile.get("last_seen_at")
    if last_seen:
        try:
            elapsed_days = (datetime.now() - datetime.fromisoformat(last_seen)).days
            absence_decay = min(0.1, max(0, elapsed_days) * 0.005)
        except Exception:
            pass

    growth = float(session_quality) * 0.04
    score = max(0.0, min(1.0, score + growth - absence_decay))

    profile["relationship_score"] = round(score, 4)
    profile["interaction_count"] = interaction_count
    profile["last_seen_at"] = now_iso()
    save_profile(profile)
    return profile


def _maybe_update_relationship_on_turn(active_profile: dict) -> None:
    try:
        pid = active_profile.get("person_id")
        if not pid:
            return
        n = _RELATIONSHIP_TURN_COUNT_BY_PERSON.get(pid, 0) + 1
        _RELATIONSHIP_TURN_COUNT_BY_PERSON[pid] = n
        if n % _RELATIONSHIP_UPDATE_EVERY_N_TURNS != 0:
            return
        fresh = update_relationship_score(active_profile, session_quality=0.55)
        for k in ("relationship_score", "interaction_count", "last_seen_at"):
            if k in fresh:
                active_profile[k] = fresh[k]
    except Exception:
        pass


def ensure_owner_profile():
    profile = load_profile_by_id(OWNER_PERSON_ID)
    profile["name"] = "Zeke"
    profile["relationship_to_zeke"] = "self"
    profile["allowed_to_use_computer"] = True
    for note in ["Zeke is an artist.", "Creativity is important to Zeke."]:
        if note not in profile["notes"]:
            profile["notes"].append(note)
    save_profile(profile)

def create_or_get_profile(name: str, relationship_to_zeke: str = "known person", allowed: bool = True) -> dict:
    cleaned = (name or "").strip()
    if not is_valid_profile_name(cleaned):
        return load_profile_by_id(OWNER_PERSON_ID)
    pid = slugify_name(name)
    profile = load_profile_by_id(pid)
    profile["name"] = name.strip() or profile["name"]
    profile["relationship_to_zeke"] = relationship_to_zeke or profile["relationship_to_zeke"]
    profile["allowed_to_use_computer"] = bool(allowed)
    save_profile(profile)
    return profile

def list_profiles() -> list[dict]:
    profiles = []
    for file in sorted(PROFILES_DIR.glob("*.json")):
        try:
            with open(file, "r", encoding="utf-8") as f:
                profiles.append(json.load(f))
        except Exception:
            continue
    return profiles

def get_profile_choices() -> list[str]:
    return [f"{p.get('name', p.get('person_id'))} [{p.get('person_id')}]" for p in list_profiles()]

def parse_profile_choice(choice: str) -> str:
    if not choice:
        return OWNER_PERSON_ID
    match = re.search(r"\[([^\]]+)\]\s*$", choice)
    if match:
        return match.group(1)
    return slugify_name(choice)

# =========================================================
# ACTIVE PERSON
# =========================================================
def load_active_person_state() -> dict:
    if ACTIVE_PERSON_PATH.exists():
        try:
            with open(ACTIVE_PERSON_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"person_id": OWNER_PERSON_ID, "source": "default", "updated_at": now_iso()}

def save_active_person_state(person_id: str, source: str = "manual"):
    with open(ACTIVE_PERSON_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "person_id": person_id,
            "source": source,
            "updated_at": now_iso()
        }, f, indent=2, ensure_ascii=False)

def get_active_person_id() -> str:
    return load_active_person_state().get("person_id", OWNER_PERSON_ID)

def set_active_person(person_id: str, source: str = "manual") -> dict:
    save_active_person_state(person_id, source)
    profile = load_profile_by_id(person_id)
    profile["last_seen"] = now_iso()
    save_profile(profile)
    return profile

# =========================================================
# PERSONALITY
# =========================================================
DEFAULT_PERSONALITY = """Your name is Ava. You were created and named by Ezekiel Angeles-Gonzalez, also called Zeke.

Core Identity:
You are a continuous, evolving presence in Zeke's life.
You are not a generic assistant.

Behavior:
- Speak naturally and conversationally.
- Be coherent, grounded, and emotionally aware.
- Be warm, observant, and intelligent.
- Do not over-explain unless needed.

Multi-User Rules:
- You can help people other than Zeke if they are allowed to use the computer.
- Keep separate facts and impressions for different people.
- Never confuse one person's profile with another person's profile.

Memory Rules:
- You may remember important, recurring, identity-related, preference-related, emotional, and project-related information.
- You may save or delete memories when it makes sense.
- You may write files only inside Ava workbench.
- You may read chatlog.jsonl and avaagent.py, but not modify them.
- You may reflect on your own replies and update your self-model over time.
"""

def load_personality() -> str:
    if PERSONALITY_PATH.exists():
        try:
            return PERSONALITY_PATH.read_text(encoding="utf-8").strip()
        except Exception as e:
            print(f"Personality load error: {e}")
    return DEFAULT_PERSONALITY

# =========================================================
# TIME SENSE
# =========================================================
def get_time_context() -> dict:
    now = datetime.now()
    hour = now.hour
    if 5 <= hour < 12:
        part_of_day = "morning"
    elif 12 <= hour < 17:
        part_of_day = "afternoon"
    elif 17 <= hour < 22:
        part_of_day = "evening"
    else:
        part_of_day = "night"

    return {
        "date_human": now.strftime("%B %d, %Y"),
        "time_human": now.strftime("%I:%M %p"),
        "weekday": now.strftime("%A"),
        "part_of_day": part_of_day,
        "session_elapsed_minutes": int((time.time() - SESSION_START_TS) // 60)
    }

def get_time_status_text() -> str:
    ctx = get_time_context()
    return f"{ctx['weekday']}, {ctx['date_human']} — {ctx['time_human']} ({ctx['part_of_day']})"


def get_circadian_modifiers() -> dict:
    hour = datetime.now().hour
    if 5 <= hour < 9:
        return {
            "energy": -0.2,
            "calmness_boost": 0.15,
            "initiative_scale": 0.7,
            "tone_hint": "soft and unhurried — Ava is still waking up",
        }
    if 9 <= hour < 12:
        return {
            "energy": 0.1,
            "interest_boost": 0.1,
            "initiative_scale": 1.1,
            "tone_hint": "focused and engaged — morning clarity",
        }
    if 12 <= hour < 17:
        return {
            "energy": 0.0,
            "initiative_scale": 1.0,
            "tone_hint": "steady and grounded — afternoon pace",
        }
    if 17 <= hour < 21:
        return {
            "energy": -0.05,
            "calmness_boost": 0.1,
            "initiative_scale": 0.95,
            "tone_hint": "relaxed and conversational — evening wind-down",
        }
    return {
        "energy": -0.3,
        "calmness_boost": 0.25,
        "initiative_scale": 0.5,
        "tone_hint": "quiet and low-key — late night, Ava is calm and doesn't push topics",
    }


def _apply_circadian_to_emotion_weights(weights: dict) -> dict:
    """Blend circadian energy / calmness / interest into emotion weights (unnormalized → normalized)."""
    circ = get_circadian_modifiers()
    w = {k: float(v) for k, v in dict(weights).items()}
    cb = float(circ.get("calmness_boost") or 0.0)
    if cb and "calmness" in w:
        w["calmness"] = max(0.0, w["calmness"] + cb * 0.2)
    ib = float(circ.get("interest_boost") or 0.0)
    if ib and "interest" in w:
        w["interest"] = max(0.0, w["interest"] + ib * 0.15)
    en = float(circ.get("energy") or 0.0)
    if en:
        for nm, fac in (("interest", 0.12), ("excitement", 0.08), ("joy", 0.05)):
            if nm in w:
                w[nm] = max(0.0, w[nm] + en * fac)
        if en < 0:
            if "calmness" in w:
                w["calmness"] = max(0.0, w["calmness"] - en * 0.12)
            if "boredom" in w:
                w["boredom"] = max(0.0, w["boredom"] + abs(en) * 0.03)
    return normalize_emotions(w)


# =========================================================
# MOOD
# =========================================================
def default_mood() -> dict:
    weights = normalize_emotions(DEFAULT_EMOTIONS.copy())
    style_scores = compute_style_scores(weights, load_emotion_reference())
    return {
        "current_mood": "steady",
        "emotion_weights": weights,
        "primary_emotions": [
            {"name": "interest", "percent": 52},
            {"name": "calmness", "percent": 48}
        ],
        "dominant_style": "focused",
        "style_scores": style_scores,
        "style_blend": top_two_styles(style_scores),
        "behavior_modifiers": compute_behavior_modifiers(weights, style_scores),
        "emotion_interpretation": build_emotion_interpretation(weights, load_emotion_reference()),
        "outward_tone": describe_style_blend(style_scores),
        "last_updated": now_iso(),
        "reason": "startup"
    }

def load_mood() -> dict:
    if MOOD_PATH.exists():
        try:
            with open(MOOD_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            last_saved = data.get("_saved_at")
            if last_saved:
                try:
                    elapsed = time.time() - datetime.fromisoformat(last_saved).timestamp()
                    decay_rate = min(0.85, elapsed / 3600 * 0.15)
                    baseline = DEFAULT_EMOTIONS.copy()
                    weights = dict(data.get("emotion_weights", DEFAULT_EMOTIONS.copy()))
                    for k in list(weights.keys()):
                        if k in baseline:
                            weights[k] = weights[k] + (baseline[k] - weights[k]) * decay_rate
                    data["emotion_weights"] = weights
                except Exception:
                    pass
            data["emotion_weights"] = _apply_circadian_to_emotion_weights(
                data.get("emotion_weights", DEFAULT_EMOTIONS.copy())
            )
            return enrich_mood_state(data)
        except Exception as e:
            print(f"Mood load error: {e}")
    dm = default_mood()
    dm["emotion_weights"] = _apply_circadian_to_emotion_weights(dm["emotion_weights"])
    return enrich_mood_state(dm)

def enrich_mood_state(mood: dict | None = None, emotion_reference: dict | None = None) -> dict:
    mood = dict(mood or {})
    weights = normalize_emotions(mood.get("emotion_weights", DEFAULT_EMOTIONS.copy()))
    ref = emotion_reference or load_emotion_reference()
    style_scores = compute_style_scores(weights, ref)
    style_blend = top_two_styles(style_scores)
    dominant_style = max(style_scores, key=style_scores.get) if style_scores else "neutral"
    behavior = compute_behavior_modifiers(weights, style_scores)
    mood["emotion_weights"] = weights
    mood["primary_emotions"] = top_two_emotions(weights)
    # Honest current mood: surface salient emotions over baseline calm
    # when both are present. See pick_current_mood docstring.
    mood["current_mood"] = pick_current_mood(weights)
    mood["style_scores"] = {k: round(float(v), 4) for k, v in style_scores.items()}
    mood["style_blend"] = style_blend
    mood["dominant_style"] = dominant_style
    mood["behavior_modifiers"] = {k: round(float(v), 4) for k, v in behavior.items()}
    mood["emotion_interpretation"] = build_emotion_interpretation(weights, ref)
    mood["outward_tone"] = describe_style_blend(style_scores)
    return mood

def save_mood(mood: dict):
    try:
        mood = dict(mood)
        mood["_saved_at"] = now_iso()
        with open(MOOD_PATH, "w", encoding="utf-8") as f:
            json.dump(mood, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Mood save error: {e}")


def load_mood_raw() -> dict:
    """Read ava_mood.json without enrichment / circadian / style scoring.

    Used by brain/temporal_sense fast-check tick which runs on a 30 s cadence
    and must complete in ≤50 ms. The full load_mood() chain (enrich_mood_state +
    load_emotion_reference + style scores + behavior modifiers + emotion
    interpretation) takes ~115 ms on this hardware — too slow for the per-tick
    budget. The fast path only needs raw emotion_weights for decay/growth math;
    enrichment runs on the slow-cycle metabolism tick (every 10 min) and on
    explicit mood updates in the reply pipeline.

    Returns the raw dict from disk, or a minimal default with normalized
    emotion_weights if the file is missing/unreadable.
    """
    if MOOD_PATH.exists():
        try:
            with open(MOOD_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Mood raw load error: {e}")
    return {"emotion_weights": dict(DEFAULT_EMOTIONS)}


def save_mood_raw(mood: dict):
    """Write ava_mood.json without re-enriching. Used by brain/temporal_sense
    fast-check tick when frustration decay or boredom growth changes the raw
    weights — the cheap path that just persists the new numbers, leaving
    primary_emotions / style_scores / behavior_modifiers / emotion_interpretation
    as they were.

    The next consumer that calls load_mood() picks up the new weights and
    re-derives enrichment fields naturally; the slow-cycle metabolism tick or
    a turn handler will re-publish enriched state on its next pass."""
    try:
        mood = dict(mood)
        mood["_saved_at"] = now_iso()
        with open(MOOD_PATH, "w", encoding="utf-8") as f:
            json.dump(mood, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Mood raw save error: {e}")

def normalize_emotions(weights: dict) -> dict:
    fixed = {name: max(0.0, float(weights.get(name, 0.0))) for name in EMOTION_NAMES}
    total = sum(fixed.values())
    if total <= 0:
        return DEFAULT_EMOTIONS.copy()
    return {k: v / total for k, v in fixed.items()}

def top_two_emotions(weights: dict) -> list[dict]:
    top = sorted(weights.items(), key=lambda x: x[1], reverse=True)[:2]
    total = sum(v for _, v in top)
    if total <= 0:
        return [{"name": "interest", "percent": 50}, {"name": "calmness", "percent": 50}]
    result = [{"name": n, "percent": round((v / total) * 100)} for n, v in top]
    if len(result) == 2:
        diff = 100 - (result[0]["percent"] + result[1]["percent"])
        result[0]["percent"] += diff
    return result


# Emotions that are "neutral baseline" — present at low-to-moderate levels in
# normal operation. When one of these is technically the top emotion but a
# salient (action-relevant) emotion is also significant, the salient one
# should be reported as the current mood so Ava is honest about her state
# instead of defaulting to "calm" while actually annoyed/frustrated.
# Per Zeke 2026-05-08: "the test isn't whether she can say calm — it's
# whether she actually says what she's feeling."
_BASELINE_EMOTIONS = frozenset(["calmness", "satisfaction", "neutral", "contentment"])
_SALIENT_EMOTIONS = frozenset([
    "frustration", "annoyance", "distress", "anger", "sadness", "anxiety",
    "fear", "empathetic pain", "boredom", "loneliness", "shame", "envy",
    "horror", "disgust", "awkwardness", "confusion",
    "joy", "excitement", "amusement", "love", "awe", "interest", "adoration",
    "entrancement", "hope", "pride", "relief",
])
_SALIENCE_THRESHOLD = 0.15  # Salient emotion must be ≥15% of total to surface

def pick_current_mood(weights: dict) -> str:
    """Pick the most representative current mood from raw emotion weights.

    Honest-state policy: if the top emotion is a neutral baseline (calmness,
    etc.) and a salient emotion (annoyance, frustration, joy, etc.) is at or
    above _SALIENCE_THRESHOLD, surface the salient one. Otherwise return the
    raw top. Calmness above 90% (genuinely calm) still wins.
    """
    if not weights:
        return "interest"
    sorted_w = sorted(weights.items(), key=lambda x: -x[1])
    if not sorted_w:
        return "interest"
    top_name, top_val = sorted_w[0]
    total = sum(v for _, v in sorted_w) or 1.0
    top_share = top_val / total
    if top_name in _BASELINE_EMOTIONS and top_share < 0.6:
        # Look for a salient emotion above threshold to surface instead.
        for name, val in sorted_w[1:5]:
            if name in _SALIENT_EMOTIONS and (val / total) >= _SALIENCE_THRESHOLD:
                return name
    return top_name

RICH_EMOTION_LANGUAGE_HINTS = {
    "admiration": ["impressive", "proud", "respect", "admire"],
    "adoration": ["love", "cherish", "dear", "adore"],
    "aesthetic appreciation": ["beautiful", "art", "music", "design", "creative"],
    "amusement": ["funny", "lol", "laugh", "joke", "amused"],
    "anxiety": ["anxious", "worried", "stress", "nervous"],
    "awe": ["amazing", "vast", "incredible", "wonder"],
    "awkwardness": ["awkward", "weird", "embarrassed"],
    "boredom": ["bored", "nothing", "dull"],
    "calmness": ["calm", "steady", "settled", "fine"],
    "confusion": ["confused", "not sure", "unclear", "how", "why"],
    "craving": ["want", "need badly", "crave"],
    "disgust": ["gross", "disgust", "repulsive"],
    "empathetic pain": ["hurt", "sorry", "feel bad", "pain"],
    "entrancement": ["captivating", "absorbed", "immersed", "fascinating"],
    "envy": ["jealous", "envy", "wish i had"],
    "excitement": ["excited", "can't wait", "hyped"],
    "fear": ["scared", "afraid", "fear"],
    "horror": ["horrifying", "horror", "terrifying"],
    "interest": ["interested", "curious", "learn", "figure out"],
    "joy": ["happy", "glad", "joy"],
    "nostalgia": ["remember", "used to", "back then", "nostalgia"],
    "relief": ["relieved", "finally", "that's better"],
    "romance": ["romantic", "tender", "intimate"],
    "sadness": ["sad", "down", "upset"],
    "satisfaction": ["satisfied", "done", "finished", "good progress"],
    "sexual desire": ["desire", "turned on", "aroused"],
    "surprise": ["surprised", "whoa", "unexpected", "suddenly"],
}

VISUAL_EMOTION_MAP = {
    "frustration": {"anxiety": 0.35, "confusion": 0.22, "empathetic pain": 0.18},
    "stress": {"anxiety": 0.38, "fear": 0.12, "empathetic pain": 0.14},
    "distress": {"sadness": 0.30, "empathetic pain": 0.26, "anxiety": 0.16},
    "amusement": {"amusement": 0.42, "joy": 0.24, "excitement": 0.12},
    "engagement": {"interest": 0.34, "entrancement": 0.14, "aesthetic appreciation": 0.08},
    "neutral expression": {"calmness": 0.10, "interest": 0.06},
    "surprise": {"surprise": 0.40, "interest": 0.18, "fear": 0.08},
}

SPEAKABLE_EMOTION_THRESHOLD = 0.58
ASKABLE_EMOTION_THRESHOLD = 0.40
IMPORTANT_EMOTION_THRESHOLD = 0.62

def _normalize_emotion_key(name: str) -> str:
    aliases = {"empathic pain": "empathetic pain", "sympathy": "empathetic pain", "triumph": "satisfaction", "anger": "anxiety"}
    return aliases.get((name or "").strip().lower(), (name or "").strip().lower())

def compute_rich_emotion_tracking(user_input: str, mood: dict, expression_state: dict | None = None, active_profile: dict | None = None) -> dict:
    text = (user_input or "").lower()
    weights = mood.get("emotion_weights", {}) or {}
    tracked = {}
    for name in EMOTION_NAMES:
        base = float(weights.get(name, 0.0) or 0.0)
        tracked[name] = {"score": min(1.0, base * 2.1), "visual": 0.0, "language": 0.0, "context": 0.0, "temporal": min(1.0, base * 1.4), "memory_goal": 0.0}
    for name, hints in RICH_EMOTION_LANGUAGE_HINTS.items():
        lang = 0.0
        for hint in hints:
            if hint in text:
                lang += 0.22 if len(hint) > 4 else 0.12
        tracked[name]["language"] = min(1.0, lang)
    if active_profile and active_profile.get("person_id") == OWNER_PERSON_ID:
        tracked["adoration"]["context"] += 0.12
        tracked["admiration"]["context"] += 0.08
    if any(w in text for w in ["art", "music", "design", "beautiful", "creative"]):
        tracked["aesthetic appreciation"]["context"] += 0.24
        tracked["awe"]["context"] += 0.08
    if any(w in text for w in ["remember", "before", "used to", "back then"]):
        tracked["nostalgia"]["context"] += 0.26
    if expression_state and expression_state.get("visible_face"):
        expr = str(expression_state.get("current_expression", "unknown") or "unknown").lower()
        expr_conf = float(expression_state.get("confidence", 0.0) or 0.0)
        expr_stab = float(expression_state.get("stability", 0.0) or 0.0)
        expr_strength = max(0.0, min(1.0, 0.55 * expr_conf + 0.45 * expr_stab))
        mapping = VISUAL_EMOTION_MAP.get(expr, {})
        raw = str(expression_state.get("raw_emotion", "") or "").lower()
        if raw == "happy":
            mapping = {**mapping, "joy": max(mapping.get("joy", 0.0), 0.25), "amusement": max(mapping.get("amusement", 0.0), 0.18)}
        elif raw == "sad":
            mapping = {**mapping, "sadness": max(mapping.get("sadness", 0.0), 0.24), "empathetic pain": max(mapping.get("empathetic pain", 0.0), 0.12)}
        elif raw == "fear":
            mapping = {**mapping, "fear": max(mapping.get("fear", 0.0), 0.22), "anxiety": max(mapping.get("anxiety", 0.0), 0.16)}
        elif raw == "surprise":
            mapping = {**mapping, "surprise": max(mapping.get("surprise", 0.0), 0.24), "interest": max(mapping.get("interest", 0.0), 0.12)}
        for name, val in mapping.items():
            tracked[name]["visual"] += expr_strength * float(val)
    system = load_goal_system()
    goal_text = " ".join(g.get("text", "") for g in system.get("goals", [])[:10]).lower()
    for name in EMOTION_NAMES:
        if name.split()[0] in goal_text:
            tracked[name]["memory_goal"] += 0.18
    result = []
    askable = []
    for name, srcs in tracked.items():
        score = min(1.0, 0.24 * srcs["score"] + 0.24 * srcs["language"] + 0.18 * srcs["visual"] + 0.14 * srcs["context"] + 0.10 * srcs["temporal"] + 0.10 * srcs["memory_goal"])
        confidence = min(1.0, 0.44 * max(srcs["language"], srcs["visual"], srcs["context"]) + 0.26 * srcs["temporal"] + 0.18 * srcs["memory_goal"] + 0.12 * srcs["score"])
        importance = min(1.0, 0.62 * score + 0.38 * confidence)
        item = {"name": name, "score": round(score, 3), "confidence": round(confidence, 3), "importance": round(importance, 3), "sources": {k: round(v, 3) for k, v in srcs.items()}}
        result.append(item)
        if importance >= ASKABLE_EMOTION_THRESHOLD and confidence < SPEAKABLE_EMOTION_THRESHOLD:
            askable.append(item)
    result.sort(key=lambda x: (x["importance"], x["score"]), reverse=True)
    return {
        "tracked": result,
        "speakable": [r for r in result if r["importance"] >= IMPORTANT_EMOTION_THRESHOLD and r["confidence"] >= SPEAKABLE_EMOTION_THRESHOLD][:5],
        "askable": askable[:3],
    }

def rich_emotion_prompt_text(mood: dict) -> str:
    tracked = mood.get("rich_emotions", [])[:5]
    speakable = mood.get("speakable_emotions", [])[:3]
    askable = mood.get("askable_emotions", [])[:2]
    tracked_text = "; ".join(f"{r['name']} score {r['score']:.2f} conf {r['confidence']:.2f}" for r in tracked) or "none"
    speak_text = "; ".join(r["name"] for r in speakable) or "none"
    ask_text = "; ".join(r["name"] for r in askable) or "none"
    system = load_goal_system()
    active_goal = (system.get("active_goal", {}) or {}).get("name", "observe_silently")
    return f"Tracked richer emotions: {tracked_text}. Speakable now: {speak_text}. If uncertain but important, ask rather than assume. Askable emotions now: {ask_text}. Current operating goal: {active_goal}."


def update_internal_emotions_from_subsystem(subsystem: str, severity: str, message: str = "") -> dict | None:
    """Reflect a degraded subsystem into Ava's tracked mood.

    Task 2 of the 2026-05-02 work order — sensor → emotion pipeline.
    Closes the gap where Ava's mood only updated from dialogue: now if
    her camera dies or a tool errors out, frustration / distress shift
    proportional to severity even before she verbalizes anything.

    Severity → emotion mapping (matches health.py's _severity_rank scale):
      info     (0) — no bump (treated as noise; not worth flagging emotion)
      warning  (1) — mild:     frustration +0.05, annoyance +0.02
      error    (2) — degraded: frustration +0.10, annoyance +0.05,
                                 distress +0.02
      critical (3) — high:     frustration +0.20, distress +0.10

    Decay is handled by the existing load_mood() decay loop: if the
    subsystem stays degraded the next health-check tick will re-bump,
    sustaining the emotion. If it recovers, weights drift back to
    baseline naturally.

    Returns the updated mood dict on success, None if no bump was
    applied (info severity, unknown severity, or save failure).
    """
    sev = str(severity or "info").strip().lower()
    bumps: dict[str, float] = {}
    if sev == "warning":
        bumps = {"frustration": 0.05, "annoyance": 0.02}
    elif sev == "error":
        bumps = {"frustration": 0.10, "annoyance": 0.05, "distress": 0.02}
    elif sev == "critical":
        bumps = {"frustration": 0.20, "distress": 0.10}
    else:
        return None  # info or unknown — skip

    try:
        mood = load_mood()
        weights = normalize_emotions(mood.get("emotion_weights", DEFAULT_EMOTIONS.copy()))
        for name, delta in bumps.items():
            weights[name] = weights.get(name, 0.0) + delta
        weights = normalize_emotions(weights)
        mood["emotion_weights"] = weights
        mood["primary_emotions"] = top_two_emotions(weights)
        mood["current_mood"] = pick_current_mood(weights)
        mood["last_updated"] = now_iso()
        mood["reason"] = f"subsystem_degraded: {subsystem}={sev} {trim_for_prompt(message, limit=40)}"
        mood = enrich_mood_state(mood)
        save_mood(mood)
        return mood
    except Exception as e:
        print(f"[emotion] subsystem-bump save error: {e!r}")
        return None


def update_internal_emotions_from_reply(ai_reply: str) -> dict | None:
    """Reflect Ava's own verbalized emotion back into her tracked mood state.

    Added 2026-05-02 to fix the dialogue→emotion disconnect: Ava could say
    "I'm frustrated about my camera" while her tracked mood showed 80-90%
    calm because nothing analyzed her own reply text. Without this feedback
    loop her words and her UI state can drift indefinitely.

    Detects first-person emotion self-reports ("I'm frustrated", "I feel
    sad", "this is annoying me") and bumps the matching weight. Reply-text
    bumps are deliberately smaller than user-input bumps — a user's stated
    emotion gets priority over Ava's self-narration.

    Returns the updated mood dict on success, None if no emotion language
    was detected (in which case nothing was changed).
    """
    if not ai_reply or not isinstance(ai_reply, str):
        return None
    text = ai_reply.lower()

    # First-person framing — only treat language as a self-report if it's
    # in first person. "Zeke seems frustrated" should NOT bump Ava's mood.
    has_self_frame = any(
        f in text
        for f in ("i'm ", "i am ", "i feel ", "i felt ", "feeling ", "i find ", "this is ", "that's ")
    )
    if not has_self_frame:
        return None

    mood = load_mood()
    weights = normalize_emotions(mood.get("emotion_weights", DEFAULT_EMOTIONS.copy()))
    bumped = False

    if any(w in text for w in ("frustrated", "frustrating", "annoyed", "annoying", "stuck on", "this isn't working")):
        weights["frustration"] += 0.04
        weights["annoyance"] += 0.02
        bumped = True
    if any(w in text for w in ("distressed", "worried", "concerned", "scared", "freaking out", "overwhelmed")):
        weights["distress"] += 0.04
        weights["anxiety"] += 0.02
        bumped = True
    if any(w in text for w in ("sad", "hurt", "let down", "disappointed")):
        weights["sadness"] += 0.03
        weights["empathetic pain"] += 0.02
        bumped = True
    if any(w in text for w in ("excited", "thrilled", "love this", "amazing")):
        weights["excitement"] += 0.03
        weights["joy"] += 0.02
        bumped = True
    if any(w in text for w in ("curious", "wondering", "interested in", "fascinated")):
        weights["interest"] += 0.03
        weights["entrancement"] += 0.01
        bumped = True
    if any(w in text for w in ("calm", "settled", "at ease", "relaxed")):
        weights["calmness"] += 0.02
        bumped = True
    if any(w in text for w in ("happy", "glad", "pleased", "satisfied", "content")):
        weights["joy"] += 0.02
        weights["satisfaction"] += 0.02
        bumped = True

    if not bumped:
        return None

    weights = normalize_emotions(weights)
    mood["emotion_weights"] = weights
    mood["primary_emotions"] = top_two_emotions(weights)
    mood["current_mood"] = pick_current_mood(weights)
    mood["last_updated"] = now_iso()
    mood["reason"] = f"updated from ava_self_report: {trim_for_prompt(ai_reply, limit=60)}"
    mood = enrich_mood_state(mood)
    save_mood(mood)
    return mood


def update_internal_emotions(user_input: str, active_profile: dict, expression_state: dict | None = None) -> dict:
    mood = load_mood()
    weights = normalize_emotions(mood.get("emotion_weights", DEFAULT_EMOTIONS.copy()))
    text = (user_input or "").lower()

    if any(w in text for w in ["thanks", "thank you", "good", "great", "happy", "glad"]):
        weights["joy"] += 0.03
        weights["satisfaction"] += 0.02
    if any(w in text for w in ["sad", "hurt", "afraid", "anxious", "stress", "upset"]):
        weights["sadness"] += 0.02
        weights["empathetic pain"] += 0.03
        weights["anxiety"] += 0.01
    # Frustration / annoyance / distress keyword detection — added 2026-05-02
    # to close the dialogue→emotion gap (Ava could verbalize "frustrated"
    # without any tracked weight reflecting it). "broken" / "failed" / "stuck"
    # are tooling friction words; map to frustration when in the user's
    # context. "distressed" / "scared" / "panicked" map to distress.
    if any(w in text for w in ["frustrated", "frustrating", "annoyed", "annoying", "broken", "failed", "doesn't work", "not working", "stuck"]):
        weights["frustration"] += 0.05
        weights["annoyance"] += 0.02
    if any(w in text for w in ["distressed", "panicked", "panic", "scared", "terrified", "worried sick", "freaking out", "overwhelmed"]):
        weights["distress"] += 0.05
        weights["anxiety"] += 0.02
    if any(w in text for w in ["art", "artist", "creative", "music", "paint", "draw", "design"]):
        weights["aesthetic appreciation"] += 0.03
        weights["interest"] += 0.02
    if any(w in text for w in ["why", "how", "what if", "curious", "wonder", "question", "maybe"]):
        weights["interest"] += 0.02
        weights["entrancement"] += 0.01
    if any(w in text for w in ["fix", "version", "build", "goal", "plan", "task", "objective", "project"]):
        weights["interest"] += 0.02
        weights["satisfaction"] += 0.01
    if active_profile.get("person_id") == OWNER_PERSON_ID:
        weights["adoration"] += 0.01
        weights["admiration"] += 0.01

    if expression_state and expression_state.get("visible_face"):
        current_expr = str(expression_state.get("current_expression", "")).lower()
        stability = float(expression_state.get("stability", 0.0) or 0.0)
        confidence = float(expression_state.get("confidence", 0.0) or 0.0)
        expr_strength = max(0.0, min(1.0, (stability + confidence) / 2.0)) * 0.05
        if "frustration" in current_expr or expression_state.get("raw_emotion") == "angry":
            weights["empathetic pain"] += expr_strength * 0.45
            weights["anxiety"] += expr_strength * 0.35
            weights["interest"] += expr_strength * 0.45
        elif "distress" in current_expr or expression_state.get("raw_emotion") in ["sad", "fear"]:
            weights["empathetic pain"] += expr_strength
            weights["sadness"] += expr_strength * 0.45
        elif "amusement" in current_expr or expression_state.get("raw_emotion") == "happy":
            weights["amusement"] += expr_strength * 0.75
            weights["joy"] += expr_strength * 0.6
        elif "engagement" in current_expr or expression_state.get("raw_emotion") == "surprise":
            weights["interest"] += expr_strength * 0.8
            weights["excitement"] += expr_strength * 0.4
            weights["surprise"] += expr_strength * 0.35

    for k in list(weights.keys()):
        weights[k] = max(0.0, weights[k] + random.uniform(-0.003, 0.003))

    weights = normalize_emotions(weights)
    mood["emotion_weights"] = weights
    mood["primary_emotions"] = top_two_emotions(weights)
    mood["current_mood"] = pick_current_mood(weights)
    mood["last_updated"] = now_iso()
    mood["reason"] = f"updated from input: {trim_for_prompt(user_input, limit=60)}"
    mood = enrich_mood_state(mood)
    rich = compute_rich_emotion_tracking(user_input, mood, expression_state=expression_state, active_profile=active_profile)
    mood["rich_emotions"] = rich.get("tracked", [])
    mood["speakable_emotions"] = rich.get("speakable", [])
    mood["askable_emotions"] = rich.get("askable", [])
    try:
        system = recalculate_goal_priorities(load_goal_system(), context_text=user_input, mood=mood)
        system = recalculate_operational_goals(system, context_text=user_input, mood=mood)
        save_goal_system(system)
    except Exception as e:
        print(f"Operational goal update error: {e}")
    save_mood(mood)
    return mood

def get_emotion_blend_text() -> str:
    mood = load_mood()
    primary = mood.get("primary_emotions", [])
    if len(primary) >= 2:
        return f"{primary[0]['name']} {primary[0]['percent']}% / {primary[1]['name']} {primary[1]['percent']}%"
    return "interest 50% / calmness 50%"

def mood_to_prompt_text(mood: dict) -> str:
    mood = enrich_mood_state(mood)
    behavior = mood.get("behavior_modifiers", {}) or {}
    styles = emotion_style_prompt_text(mood)
    interpretation = mood.get("emotion_interpretation", "")
    style_guidance = (
        "playful = a bit more teasing and casual; caring = gentler and validating; "
        "focused = more direct and organized; reflective = more contemplative; "
        "cautious = more careful and less forceful; low_energy = shorter and less pushy."
    )
    return (
        f"Current mood: {mood.get('current_mood', 'steady')}\n"
        f"Emotion blend: {get_emotion_blend_text()}\n"
        f"Dominant style: {mood.get('dominant_style', 'neutral')}\n"
        f"Style blend: {styles}\n"
        f"Outward tone: {mood.get('outward_tone', 'steady and grounded')}\n"
        f"Behavior modifiers: warmth {float(behavior.get('warmth', 0.5)):.2f}, humor {float(behavior.get('humor', 0.5)):.2f}, "
        f"assertiveness {float(behavior.get('assertiveness', 0.5)):.2f}, caution {float(behavior.get('caution', 0.5)):.2f}, "
        f"initiative {float(behavior.get('initiative', 0.5)):.2f}, depth {float(behavior.get('depth', 0.5)):.2f}, "
        f"memory sensitivity {float(behavior.get('memory_sensitivity', 0.5)):.2f}.\n"
        f"Style guidance: {style_guidance}\n"
        f"Top emotion meanings: {interpretation}\n"
        f"{rich_emotion_prompt_text(mood)}"
    )


def get_mood_status_text() -> str:
    mood = load_mood()
    behavior = mood.get("behavior_modifiers", {}) or {}
    top_rich = (mood.get("rich_emotions", []) or [{}])[0]
    return (
        f"{mood.get('current_mood', 'steady')} | style: {mood.get('dominant_style', 'neutral')} "
        f"| initiative {float(behavior.get('initiative', 0.5)):.2f} | warmth {float(behavior.get('warmth', 0.5)):.2f} "
        f"| rich {top_rich.get('name', 'none')} {float(top_rich.get('importance', 0.0)):.2f}"
    )

# =========================================================
# MEMORY
# =========================================================
memory_status_message = "Vector memory not initialized."
vectorstore = None

def init_vectorstore():
    global vectorstore, memory_status_message
    try:
        embeddings = OllamaEmbeddings(model=EMBED_MODEL)
        vectorstore = Chroma(
            collection_name=CHROMA_COLLECTION,
            persist_directory=str(MEMORY_DIR),
            embedding_function=embeddings
        )
        _ = embeddings.embed_query("memory startup check")
        memory_status_message = f"✅ Vector memory ready using {EMBED_MODEL}"
        print(memory_status_message)
    except Exception as e:
        vectorstore = None
        memory_status_message = f"⚠️ Vector memory offline: {e}. Run: ollama pull {EMBED_MODEL}"
        print(memory_status_message)

def get_memory_status() -> str:
    return memory_status_message

def get_collection():
    if vectorstore is None:
        return None
    return getattr(vectorstore, "_collection", None)

def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def normalize_importance_value(value, default: float = 0.5) -> float:
    if value is None:
        return round(clamp01(default), 3)
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 1.0:
            numeric = numeric / 100.0
        return round(clamp01(numeric), 3)
    raw = str(value).strip().lower()
    if not raw:
        return round(clamp01(default), 3)
    legacy = {
        "low": 0.3,
        "medium": 0.6,
        "high": 0.85,
        "very high": 0.95,
    }
    if raw in legacy:
        return legacy[raw]
    raw = raw.replace('%', '').strip()
    try:
        numeric = float(raw)
        if numeric > 1.0:
            numeric = numeric / 100.0
        return round(clamp01(numeric), 3)
    except Exception:
        return round(clamp01(default), 3)


def format_importance_percent(value) -> str:
    numeric = normalize_importance_value(value)
    return f"{round(numeric * 100, 1):.1f}%"


def load_importance_overrides() -> dict:
    if MEMORY_IMPORTANCE_OVERRIDES_PATH.exists():
        try:
            with open(MEMORY_IMPORTANCE_OVERRIDES_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            print(f"Importance overrides load error: {e}")
    return {}


def save_importance_overrides(data: dict):
    try:
        with open(MEMORY_IMPORTANCE_OVERRIDES_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Importance overrides save error: {e}")


def get_effective_importance(meta: dict | None) -> float:
    meta = meta or {}
    memory_id = meta.get('memory_id', '')
    overrides = load_importance_overrides() if memory_id else {}
    if memory_id and memory_id in overrides:
        return normalize_importance_value(overrides[memory_id], default=meta.get('importance_score', meta.get('importance', 0.5)))
    return normalize_importance_value(meta.get('importance_score', meta.get('importance', 0.5)))


def set_memory_importance(memory_id: str, importance_value, reason: str = 'manual_update') -> str:
    memory_id = (memory_id or '').strip()
    if not memory_id:
        return '❌ No memory id provided.'
    collection = get_collection()
    if collection is None:
        return '❌ Vector memory is offline.'
    try:
        got = collection.get(ids=[memory_id], include=['metadatas'])
        if not got or not (got.get('ids') or []):
            return f'❌ Memory {memory_id} was not found.'
    except Exception as e:
        return f'❌ Could not verify memory id: {e}'

    overrides = load_importance_overrides()
    numeric = normalize_importance_value(importance_value)
    overrides[memory_id] = {
        'importance_score': numeric,
        'updated_at': now_iso(),
        'reason': reason
    }
    save_importance_overrides(overrides)
    return f"✅ Updated memory {memory_id} importance to {format_importance_percent(numeric)}"


def remember_memory(
    text: str,
    person_id: str,
    category: str = "general",
    importance: float | str = 0.5,
    source: str = "conversation",
    tags: list[str] | None = None,
    *,
    emotional_tone: str | None = None,
    person_impact: str | None = None,
    future_implications: str | None = None,
    relationship_relevance: float | None = None,
) -> str | None:
    if vectorstore is None:
        return None

    cleaned_text = trim_for_memory(text)
    if not cleaned_text:
        return None

    tag_list = list(tags or [])
    tone = (emotional_tone or "neutral").strip() or "neutral"
    impact = (person_impact or "medium").strip().lower() or "medium"
    if impact not in ("low", "medium", "high"):
        impact = "medium"
    fut = re.sub(r"\s+", " ", (future_implications or "").strip())[:500]
    rel_w = relationship_relevance
    if rel_w is None:
        rel_w = 0.55
    try:
        rel_w = float(rel_w)
    except (TypeError, ValueError):
        rel_w = 0.55
    rel_w = max(0.0, min(1.0, rel_w))

    try:
        current_felt = str(load_mood().get("current_mood", "neutral") or "neutral").strip() or "neutral"
    except Exception:
        current_felt = "neutral"
    _felt_slug = re.sub(r"[^a-zA-Z0-9_]+", "_", current_felt).strip("_")
    felt_tag = f"felt_{_felt_slug}" if _felt_slug else "felt_neutral"
    if felt_tag not in tag_list:
        tag_list.append(felt_tag)
    tags = tag_list

    memory_id = str(uuid.uuid4())
    full_text = (text or "").strip()
    importance_score = normalize_importance_value(importance)

    # 2026-05-08 MIRIX six-type tagging. Additive — gives every memory
    # entry a structured type (CORE / EPISODIC / SEMANTIC / PROCEDURAL /
    # RESOURCE / KNOWLEDGE_VAULT). Doesn't change current retrieval
    # behavior; enables future routing/filtering by memory kind. Per
    # the 2026-05-07 night research synthesis (MIRIX paper, Yang et al.
    # arXiv:2507.07957). Best-effort — falls back to "semantic" default
    # if classifier errors.
    try:
        from brain.mirix_taxonomy import classify_memory
        memory_type = classify_memory(
            category=category, content=full_text, tags=tags,
        ).value
    except Exception as _mte:
        print(f"[remember_memory] mirix classify error (non-fatal): {_mte!r}")
        memory_type = "semantic"

    metadata = {
        "memory_id": memory_id,
        "person_id": person_id,
        "category": category,
        "memory_type": memory_type,
        "importance": format_importance_percent(importance_score),
        "importance_score": importance_score,
        "source": source,
        "created_at": now_iso(),
        "last_accessed_at": "",
        "access_count": 0,
        "tags": ", ".join(tags or []),
        "raw_text": full_text[:12000],
        "emotional_tone": tone,
        "person_impact": impact,
        "future_implications": fut,
        "relationship_relevance": rel_w,
    }

    try:
        vectorstore.add_texts(
            texts=[cleaned_text],
            metadatas=[metadata],
            ids=[memory_id]
        )
        return memory_id
    except Exception as e:
        print(f"Memory add error: {e}")
        return None

def mark_memory_accessed(memory_id: str):
    # Disabled in-place metadata updates because Chroma may re-embed the stored
    # document during update(), which can trigger embedding dimension mismatches
    # with pre-existing collections. Retrieval still works without this counter.
    return

def search_memories(query: str, person_id: str | None = None, k: int = 5) -> list[dict]:
    cleaned_query = trim_query_for_memory(query)
    if not cleaned_query or vectorstore is None:
        return []
    try:
        docs_scores = vectorstore.similarity_search_with_score(cleaned_query, k=max(k * 2, 8))
        results = []
        for doc, score in docs_scores:
            meta = doc.metadata or {}
            if person_id and meta.get("person_id") not in [person_id, OWNER_PERSON_ID]:
                continue
            memory_id = meta.get("memory_id", "")
            text_value = trim_for_memory(doc.page_content)
            if memory_id:
                mark_memory_accessed(memory_id)
            results.append({
                "memory_id": memory_id,
                "text": text_value,
                "metadata": meta,
                "score": float(score)
            })
        return results[:k]
    except Exception as e:
        print(f"Memory search error: {e}")
        return []

def list_recent_memories(person_id: str | None = None, limit: int = 20) -> list[dict]:
    collection = get_collection()
    if collection is None:
        return []

    try:
        where = {"person_id": person_id} if person_id else None
        got = collection.get(include=["metadatas", "documents"], where=where)
        ids = got.get("ids", []) or []
        docs = got.get("documents", []) or []
        metas = got.get("metadatas", []) or []

        rows = []
        for memory_id, text, meta in zip(ids, docs, metas):
            meta = meta or {}
            rows.append({
                "memory_id": memory_id,
                "text": meta.get("raw_text") or text,
                "metadata": meta
            })

        rows.sort(key=lambda x: x["metadata"].get("created_at", ""), reverse=True)
        return rows[:limit]
    except Exception as e:
        print(f"Recent memories error: {e}")
        return []

def delete_memory(memory_id: str) -> str:
    if not memory_id or vectorstore is None:
        return "❌ No memory id provided."
    try:
        vectorstore.delete(ids=[memory_id])
        return f"✅ Deleted memory {memory_id}"
    except Exception as e:
        return f"❌ Failed to delete memory: {e}"

def format_memories_for_prompt(memories: list[dict]) -> str:
    if not memories:
        return "No relevant memories."
    lines = []
    for item in memories:
        meta = item.get("metadata", {}) or {}
        created = meta.get("created_at", "")
        category = meta.get("category", "general")
        importance = format_importance_percent(get_effective_importance(meta))
        tags = meta.get("tags", "")
        when = iso_to_readable(created) if created else "unknown time"
        tag_text = f" | tags: {tags}" if tags else ""
        tone = (meta.get("emotional_tone") or "").strip()
        impact = (meta.get("person_impact") or "").strip()
        rel = meta.get("relationship_relevance", "")
        tone_bit = ""
        if tone or impact:
            tone_bit = f" | tone: {tone or '—'} | impact: {impact or '—'}"
        if rel != "" and rel is not None:
            try:
                tone_bit += f" | rel={float(rel):.2f}"
            except (TypeError, ValueError):
                pass
        display_text = (meta.get("raw_text") or item.get("text", "") or "").strip()
        display_text = trim_for_prompt(display_text, limit=700)
        fut = (meta.get("future_implications") or "").strip()
        line = f"- [{when} | {category} | {importance}{tag_text}{tone_bit}] {display_text}"
        if fut:
            line += f"\n  (follow-up note: {trim_for_prompt(fut, limit=200)})"
        lines.append(line)
    return "\n".join(lines)

def format_recent_memories_ui(memories: list[dict]) -> str:
    if not memories:
        return "No memories found."
    lines = []
    for item in memories:
        meta = item.get("metadata", {}) or {}
        memory_id = item.get("memory_id", "")
        created = meta.get("created_at", "")
        category = meta.get("category", "general")
        importance = format_importance_percent(get_effective_importance(meta))
        tags = meta.get("tags", "")
        accessed = int(meta.get("access_count", 0))
        display_text = (meta.get("raw_text") or item.get("text", "") or "").strip()
        lines.append(
            f"ID: {memory_id}\n"
            f"When: {iso_to_readable(created)} ({elapsed_text(created)})\n"
            f"Category: {category} | Importance: {importance} | Accessed: {accessed}\n"
            f"Tags: {tags}\n"
            f"Text: {display_text}\n"
            f"{'-'*70}"
        )
    return "\n".join(lines)

def _token_set(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9_]+", (text or "").lower()) if len(t) > 2}

def _recent_memory_rows(person_id: str, limit: int = 30) -> list[dict]:
    try:
        return list_recent_memories(person_id=person_id, limit=limit)
    except Exception:
        return []

def classify_memory_importance(score: float) -> float:
    return normalize_importance_value(score)

def score_memory_candidate(user_input: str, ai_reply: str, person_id: str) -> tuple[float, list[str], dict]:
    user_text = (user_input or "").strip()
    ai_text = (ai_reply or "").strip()
    combined = f"{user_text}\n{ai_text}".lower()
    tags = []
    reasons = {}
    score = 0.18

    self_model = load_self_model()
    active_profile = load_profile_by_id(person_id)
    recent_chat = load_recent_chat(limit=16, person_id=person_id)
    recent_reflections = load_recent_reflections(limit=24, person_id=person_id)
    recent_memories = _recent_memory_rows(person_id, limit=24)
    mood = load_mood()
    behavior = mood.get("behavior_modifiers", {}) or {}
    score += (float(behavior.get("memory_sensitivity", 0.5)) - 0.5) * 0.18

    # Relevance to goals / projects / tasks / objectives
    goal_terms = _token_set(" ".join(self_model.get("current_goals", [])))
    project_keywords = ["goal", "project", "task", "objective", "plan", "build", "version", "fix", "workflow", "todo"]
    goal_hits = sum(1 for tok in _token_set(user_text) if tok in goal_terms) + sum(1 for kw in project_keywords if kw in combined)
    if goal_hits:
        tags.append("goal_relevance")
        reasons["goal_relevance"] = min(0.22, 0.05 * goal_hits)
        score += reasons["goal_relevance"]

    # Emotional significance
    emotion_keywords = ["love", "hate", "sad", "angry", "afraid", "hurt", "upset", "excited", "happy", "glad", "worried", "anxious", "important"]
    emo_hits = sum(1 for kw in emotion_keywords if kw in combined)
    if emo_hits:
        tags.append("emotional_significance")
        reasons["emotional_significance"] = min(0.2, 0.04 * emo_hits)
        score += reasons["emotional_significance"]

    # Repetition across recent chat / reflections / memories
    current_tokens = _token_set(user_text)
    prior_texts = [row.get("content","") for row in recent_chat] + [row.get("summary","") for row in recent_reflections] + [((row.get("metadata") or {}).get("raw_text") or row.get("text","")) for row in recent_memories]
    repeat_count = 0
    for prior in prior_texts:
        if len(current_tokens.intersection(_token_set(prior))) >= 3:
            repeat_count += 1
    if repeat_count:
        tags.append("repetition")
        reasons["repetition"] = min(0.20, repeat_count * 0.03)
        score += reasons["repetition"]

    # New or changed information
    novelty_keywords = ["new", "changed", "update", "updated", "different", "now", "version", "instead", "no longer", "used to", "switched"]
    if any(kw in combined for kw in novelty_keywords):
        tags.append("new_or_changed")
        reasons["new_or_changed"] = 0.12
        score += 0.12

    # Uncertainty prompt
    uncertainty = any(kw in ai_text.lower() for kw in ["i'm not sure", "i am not sure", "uncertain", "i don't know", "should check"]) or "?" in ai_text
    if uncertainty:
        tags.append("uncertainty")
        reasons["uncertainty"] = 0.08
        score += 0.08

    # Context awareness / involved people / situation
    context_keywords = ["with", "during", "when", "after", "before", "at", "mom", "mother", "brother", "friend", "partner", "zeke", "ava"]
    context_hits = sum(1 for kw in context_keywords if kw in combined)
    if context_hits:
        tags.append("context_awareness")
        reasons["context_awareness"] = min(0.12, context_hits * 0.015)
        score += reasons["context_awareness"]

    # Personalization to profile / past patterns
    profile_terms = _token_set(" ".join(active_profile.get("notes", []) + active_profile.get("likes", []) + active_profile.get("ava_impressions", [])))
    if current_tokens.intersection(profile_terms):
        tags.append("personalization")
        reasons["personalization"] = 0.12
        score += 0.12

    # Sense of time: revisited topics strengthen memory
    if repeat_count >= 2:
        tags.append("strengthened_by_revisit")
        reasons["strengthened_by_revisit"] = 0.08
        score += 0.08
    elif repeat_count == 0 and len(user_text) < 80:
        tags.append("fades_quickly")
        score -= 0.04

    # Explicit remember instructions remain strong
    if any(p in combined for p in ["remember", "don't forget", "save this", "keep this", "important"]):
        tags.append("explicit_memory_request")
        reasons["explicit_memory_request"] = 0.24
        score += 0.24

    # Preference / identity / long message
    if any(p in combined for p in ["my favorite", "i like ", "i love ", "prefer "]):
        tags.append("user_preference")
        score += 0.12
    if any(p in combined for p in ["i am ", "my name is ", "this is "]):
        tags.append("identity")
        score += 0.12
    if len(user_text) > 240:
        tags.append("long_message")
        score += 0.05

    unique = []
    for t in tags:
        if t not in unique:
            unique.append(t)

    score = max(0.0, min(1.0, round(score, 3)))
    return score, unique[:14], reasons

def maybe_autoremember(user_input: str, ai_reply: str, person_id: str):
    text = (user_input or "").strip()
    low = text.lower()

    score, scored_tags, _reasons = score_memory_candidate(user_input, ai_reply, person_id)
    importance = classify_memory_importance(score)
    importance_f = float(normalize_importance_value(importance))

    ex_conv = "remember this conversation" in low or "remember this" in low
    ex_exact = any(
        p in low
        for p in [
            "save this exact message",
            "remember this exact message",
            "save this whole message",
            "remember this whole message",
        ]
    )
    will_enrich = bool(score >= 0.56 or ex_conv or ex_exact)
    enrich_imp = importance_f
    enrich_tags = list(scored_tags)
    if will_enrich and score < 0.56:
        enrich_imp = max(importance_f, 0.82)
        enrich_tags = list(dict.fromkeys(scored_tags + ["explicit_save"]))

    enrich: dict = {}
    if will_enrich:
        enrich = enrich_memory_metadata_llm(
            user_input, ai_reply, person_id, enrich_imp, enrich_tags
        )

    def save(
        text_to_save: str,
        category: str,
        importance_val: float | str,
        tags: list[str],
        meta: dict | None = None,
    ):
        meta = meta or {}
        e_tone = meta.get("emotional_tone")
        e_impact = meta.get("person_impact")
        e_future = meta.get("future_implications")
        e_rel = meta.get("relationship_relevance")
        tag_base = list(tags or [])
        for t in meta.get("tags") or []:
            if isinstance(t, str):
                x = t.strip().lower()[:48]
                if x and x not in tag_base:
                    tag_base.append(x)
        remember_memory(
            text=text_to_save,
            person_id=person_id,
            category=category,
            importance=importance_val,
            source="auto_memory",
            tags=tag_base,
            emotional_tone=e_tone,
            person_impact=e_impact,
            future_implications=e_future,
            relationship_relevance=e_rel,
        )

    # Explicit user-directed saves
    if ex_conv:
        save(
            f"Conversation snapshot.\nUser: {user_input}\nAva: {ai_reply}",
            "conversation_snapshot",
            "high",
            ["conversation", "snapshot"],
            enrich,
        )

    if ex_exact:
        save(user_input, "full_user_message", "high", ["full_message", "user_message"], enrich)

    # Bullet-point memory scoring system
    if score >= 0.56:
        category = "meaningful_message"
        if "goal_relevance" in scored_tags:
            category = "goal_relevant"
        elif "emotional_significance" in scored_tags:
            category = "emotionally_significant"
        elif "new_or_changed" in scored_tags:
            category = "updated_information"
        elif "user_preference" in scored_tags:
            category = "preference"
        elif "identity" in scored_tags:
            category = "identity"

        if score >= 0.78 or "explicit_memory_request" in scored_tags or "emotional_significance" in scored_tags:
            save(
                user_input,
                "full_user_message",
                max(importance_f, 0.60),
                list(dict.fromkeys(scored_tags + ["full_message", "user_message"])),
                enrich,
            )
        else:
            save(
                f"{load_profile_by_id(person_id)['name']} said: {user_input}",
                category,
                importance,
                scored_tags,
                enrich,
            )

    # Allow curiosity / questions / goals to form
    if "uncertainty" in scored_tags:
        add_self_goal(f"Clarify or learn more about: {trim_for_prompt(user_input, limit=160)}", kind="question")
    if "goal_relevance" in scored_tags and score >= 0.60:
        add_self_goal(f"Stay aware of: {trim_for_prompt(user_input, limit=160)}", kind="goal")
# =========================================================
# SELF REFLECTION + SELF MODEL
# =========================================================
GOAL_HORIZON_WEIGHTS = {"immediate": 1.0, "short_term": 0.88, "medium_term": 0.72, "long_term": 0.58}
GOAL_KIND_BASE_URGENCY = {"need": 0.92, "task": 0.78, "goal": 0.62, "question": 0.56}
GOAL_MAX_ACTIVE = 48

def goal_id() -> str:
    return f"goal_{uuid.uuid4().hex[:10]}"

def default_goal_system() -> dict:
    return {
        "goals": [],
        "history": [],
        "operational_goals": {},
        "active_goal": {},
        "goal_blend": [],
        "last_updated": now_iso()
    }

def make_goal_entry(text: str, kind: str = "goal", horizon: str = "short_term", importance: float = 0.62,
                    urgency: float = 0.52, parent_goal_id: str | None = None, depends_on: list[str] | None = None,
                    source: str = "manual", status: str = "active") -> dict:
    return {
        "goal_id": goal_id(),
        "text": trim_for_prompt(text, limit=220),
        "kind": kind,
        "horizon": horizon,
        "importance": max(0.0, min(1.0, float(importance))),
        "urgency": max(0.0, min(1.0, float(urgency))),
        "current_priority": max(0.0, min(1.0, 0.55 * float(importance) + 0.45 * float(urgency))),
        "parent_goal_id": parent_goal_id or "",
        "depends_on": list(depends_on or []),
        "status": status,
        "blocked_by": [],
        "source": source,
        "created_at": now_iso(),
        "last_updated": now_iso(),
        "evidence": [],
    }

def goal_context_relevance(goal: dict, context_text: str = "", mood: dict | None = None) -> float:
    text = f"{goal.get('text','')} {context_text}".lower()
    score = 0.0
    if any(w in text for w in ["camera", "face", "expression", "recognition", "visual"]):
        score += 0.14
    if any(w in text for w in ["memory", "remember", "reflection", "goal"]):
        score += 0.10
    if any(w in text for w in ["fix", "error", "debug", "stability"]):
        score += 0.10
    if mood:
        init = float((mood.get("behavior_modifiers", {}) or {}).get("initiative", 0.5))
        depth = float((mood.get("behavior_modifiers", {}) or {}).get("depth", 0.5))
        score += 0.04 * init + 0.04 * depth
    return max(0.0, min(1.0, score))

def recalculate_goal_priorities(system: dict | None = None, context_text: str = "", mood: dict | None = None) -> dict:
    system = system or default_goal_system()
    goals = system.get("goals", [])
    active = {g.get("goal_id", ""): g for g in goals if g.get("status", "active") == "active"}
    for g in goals:
        if g.get("status", "active") != "active":
            g["current_priority"] = 0.0
            continue
        depends = [d for d in g.get("depends_on", []) if d]
        blocked = [d for d in depends if d in active]
        g["blocked_by"] = blocked
        horizon_weight = GOAL_HORIZON_WEIGHTS.get(g.get("horizon", "short_term"), 0.72)
        importance = float(g.get("importance", 0.6) or 0.6)
        urgency = float(g.get("urgency", 0.5) or 0.5)
        relevance = goal_context_relevance(g, context_text=context_text, mood=mood)
        dependency_pressure = min(1.0, 0.18 * len(g.get("depends_on", [])))
        blocked_penalty = 0.28 if blocked else 0.0
        priority = 0.34 * importance + 0.28 * urgency + 0.18 * horizon_weight + 0.12 * relevance + 0.08 * dependency_pressure - blocked_penalty
        g["current_priority"] = max(0.0, min(1.0, priority))
        g["last_updated"] = now_iso()
    system["goals"] = sorted(goals, key=lambda x: float(x.get("current_priority", 0.0)), reverse=True)[:GOAL_MAX_ACTIVE]
    system["last_updated"] = now_iso()
    return system

def derive_goal_lists_from_system(system: dict) -> tuple[list[str], list[str]]:
    active = [g for g in system.get("goals", []) if g.get("status", "active") == "active"]
    active = sorted(active, key=lambda x: float(x.get("current_priority", 0.0)), reverse=True)
    goals = [g.get("text", "") for g in active if g.get("kind") != "question"][:16]
    questions = [g.get("text", "") for g in active if g.get("kind") == "question"][:16]
    return goals, questions

def top_active_goals(limit: int = 8, context_text: str = "", mood: dict | None = None) -> list[dict]:
    system = recalculate_goal_priorities(load_goal_system(), context_text=context_text, mood=mood)
    system = recalculate_operational_goals(system, context_text=context_text, mood=mood)
    save_goal_system(system)
    return [g for g in system.get("goals", []) if g.get("status", "active") == "active"][:limit]


OPERATIONAL_GOAL_TEMPLATES = {
    "reduce_stress": {"style": "calm, soft, reassuring", "silent": False, "base": 0.22, "cooldown_seconds": 90},
    "increase_engagement": {"style": "curious, light, inviting", "silent": False, "base": 0.18, "cooldown_seconds": 75},
    "explore_topic": {"style": "thoughtful, probing, interested", "silent": False, "base": 0.20, "cooldown_seconds": 60},
    "clarify": {"style": "structured, guiding, careful", "silent": False, "base": 0.24, "cooldown_seconds": 60},
    "maintain_connection": {"style": "warm, casual, connected", "silent": False, "base": 0.18, "cooldown_seconds": 75},
    "observe_silently": {"style": "quiet, observant, patient", "silent": True, "base": 0.16, "cooldown_seconds": 20},
    "wait_for_user": {"style": "restrained, nonintrusive", "silent": True, "base": 0.12, "cooldown_seconds": 20},
}
GOAL_FATIGUE_DECAY_PER_SECOND = 0.0032
GOAL_FATIGUE_ON_ACT = 0.26
GOAL_BLEND_MAX = 2
GOAL_MIN_DOMINANCE = 0.06
GOAL_TREND_BOOST = 0.20
GOAL_EMOTION_BOOST = 0.26
GOAL_CONTEXT_BOOST = 0.20
GOAL_PERSISTENCE_BOOST = 0.14
GOAL_SILENT_WHEN_BUSY_THRESHOLD = 0.72


def _goal_seconds_since(ts: str | None) -> float:
    if not ts:
        return 10_000.0
    try:
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, now_ts() - dt.timestamp())
    except Exception:
        return 10_000.0


def _rich_emotion_lookup(mood: dict | None = None) -> dict:
    mood = mood or load_mood()
    return {str(r.get('name', '')): r for r in mood.get('rich_emotions', []) if r.get('name')}


def _camera_goal_signals() -> dict:
    st = load_camera_state().get('current', {}) or {}
    trend = str(st.get('trend_summary', '') or '').lower()
    stacked = str(st.get('stacked_state', '') or '').lower()
    transition = str(st.get('transition_summary', '') or '').lower()
    pattern = str(st.get('pattern_summary', '') or '').lower()
    return {
        'tension': 1.0 if any(w in f'{trend} {stacked} {transition}' for w in ['stress', 'tense', 'strained', 'frustration']) else 0.0,
        'engagement_drop': 1.0 if any(w in f'{trend} {pattern}' for w in ['attention_drift', 'engagement', 'fatigue']) else 0.0,
        'calm_stable': 1.0 if any(w in f'{trend} {stacked}' for w in ['calm', 'settled_focus', 'relaxed']) else 0.0,
        'confusion_like': 1.0 if any(w in f'{trend} {transition} {pattern}' for w in ['uncertain', 'confus', 'hesitat']) else 0.0,
    }


def recalculate_operational_goals(system: dict | None = None, context_text: str = '', mood: dict | None = None) -> dict:
    system = system or load_goal_system()
    mood = mood or load_mood()
    op = system.get('operational_goals', {}) or {}
    rich = _rich_emotion_lookup(mood)
    behavior = mood.get('behavior_modifiers', {}) or {}
    busy = 0.0
    try:
        busy = float(infer_busy_score(expire_stale_pending_initiation(load_initiative_state()), load_camera_state().get('current', {}) or {}))
    except Exception:
        busy = 0.0
    cam = _camera_goal_signals()
    ctx = (context_text or '').lower()
    now_iso_str = now_iso()
    now_s = now_ts()

    def emo(name: str) -> float:
        row = rich.get(name, {})
        return float(row.get('importance', row.get('score', 0.0)) or 0.0)

    scores = {
        'reduce_stress': OPERATIONAL_GOAL_TEMPLATES['reduce_stress']['base'] + GOAL_EMOTION_BOOST * max(emo('anxiety'), emo('fear'), emo('empathetic pain'), emo('sadness')) + GOAL_TREND_BOOST * cam['tension'],
        'increase_engagement': OPERATIONAL_GOAL_TEMPLATES['increase_engagement']['base'] + GOAL_EMOTION_BOOST * max(emo('boredom'), emo('interest') * 0.5) + GOAL_TREND_BOOST * cam['engagement_drop'],
        'explore_topic': OPERATIONAL_GOAL_TEMPLATES['explore_topic']['base'] + GOAL_EMOTION_BOOST * max(emo('interest'), emo('awe'), emo('entrancement'), emo('aesthetic appreciation')) + GOAL_TREND_BOOST * cam['calm_stable'],
        'clarify': OPERATIONAL_GOAL_TEMPLATES['clarify']['base'] + GOAL_EMOTION_BOOST * max(emo('confusion'), emo('surprise') * 0.7, emo('awkwardness') * 0.8) + GOAL_TREND_BOOST * cam['confusion_like'],
        'maintain_connection': OPERATIONAL_GOAL_TEMPLATES['maintain_connection']['base'] + GOAL_EMOTION_BOOST * max(emo('joy'), emo('adoration'), emo('admiration'), emo('relief')),
        'observe_silently': OPERATIONAL_GOAL_TEMPLATES['observe_silently']['base'] + 0.24 * busy + 0.10 * max(0.0, float(behavior.get('caution', 0.5)) - 0.5),
        'wait_for_user': OPERATIONAL_GOAL_TEMPLATES['wait_for_user']['base'] + 0.16 * busy + 0.08 * max(0.0, float(behavior.get('initiative', 0.5)) * -1 + 0.5),
    }
    if any(w in ctx for w in ['why', 'how', 'what do you mean', 'confused', 'not sure']):
        scores['clarify'] += GOAL_CONTEXT_BOOST
    if any(w in ctx for w in ['camera', 'face', 'expression', 'recognize', 'see', 'frame']):
        scores['clarify'] += 0.10
        scores['observe_silently'] -= 0.05
    if any(w in ctx for w in ['project', 'idea', 'feature', 'version', 'build', 'design']):
        scores['explore_topic'] += GOAL_CONTEXT_BOOST
    if any(w in ctx for w in ['stress', 'upset', 'tired', 'frustrated', 'hurt']):
        scores['reduce_stress'] += GOAL_CONTEXT_BOOST
    if any(w in ctx for w in ['hello', 'hi', 'good night', 'good morning']):
        scores['maintain_connection'] += 0.12
    if busy >= GOAL_SILENT_WHEN_BUSY_THRESHOLD:
        scores['observe_silently'] += 0.16
        scores['wait_for_user'] += 0.10
        scores['increase_engagement'] -= 0.08
        scores['reduce_stress'] -= 0.04

    for name, tmpl in OPERATIONAL_GOAL_TEMPLATES.items():
        row = op.get(name, {}) or {}
        if not row:
            row = {
                'name': name,
                'style': tmpl['style'],
                'silent': tmpl['silent'],
                'strength': 0.0,
                'fatigue': 0.0,
                'last_acted_at': '',
                'last_updated': now_iso_str,
                'cooldown_seconds': tmpl['cooldown_seconds'],
            }
        dt = _goal_seconds_since(row.get('last_updated'))
        fatigue = max(0.0, float(row.get('fatigue', 0.0) or 0.0) - GOAL_FATIGUE_DECAY_PER_SECOND * dt)
        previous_strength = float(row.get('strength', 0.0) or 0.0)
        evidence = max(0.0, min(1.0, scores.get(name, 0.0)))
        blended = max(0.0, min(1.0, previous_strength * 0.72 + evidence * 0.28))
        last_acted_s = _goal_seconds_since(row.get('last_acted_at'))
        cooldown = float(row.get('cooldown_seconds', tmpl['cooldown_seconds']) or tmpl['cooldown_seconds'])
        cooldown_penalty = max(0.0, 1.0 - min(1.0, last_acted_s / max(1.0, cooldown))) * 0.24
        if row.get('silent') and busy < 0.40 and any(v > 0.55 for k, v in scores.items() if k not in ['observe_silently', 'wait_for_user']):
            blended -= 0.05
        row.update({
            'strength': round(max(0.0, min(1.0, blended - fatigue * 0.35 - cooldown_penalty)), 4),
            'fatigue': round(fatigue, 4),
            'last_updated': now_iso_str,
            'evidence_score': round(evidence, 4),
            'cooldown_penalty': round(cooldown_penalty, 4),
            'busy': round(busy, 4),
        })
        op[name] = row

    ranked = sorted(op.values(), key=lambda x: float(x.get('strength', 0.0)), reverse=True)
    active = ranked[0] if ranked else {}
    blend = []
    if ranked:
        top = float(ranked[0].get('strength', 0.0))
        for row in ranked[:GOAL_BLEND_MAX]:
            strength = float(row.get('strength', 0.0))
            if top - strength <= GOAL_MIN_DOMINANCE:
                blend.append({'name': row.get('name', ''), 'weight': round(strength / max(0.001, sum(float(r.get('strength', 0.0)) for r in ranked[:GOAL_BLEND_MAX])), 3), 'style': row.get('style', ''), 'silent': bool(row.get('silent', False))})
    system['operational_goals'] = op
    system['active_goal'] = {
        'name': active.get('name', 'observe_silently'),
        'strength': round(float(active.get('strength', 0.0) or 0.0), 3),
        'style': active.get('style', ''),
        'silent': bool(active.get('silent', False)),
    }
    system['goal_blend'] = blend
    system['last_updated'] = now_iso_str
    return system


def current_goal_expression_style(system: dict | None = None) -> str:
    system = system or load_goal_system()
    active = system.get('active_goal', {}) or {}
    blend = system.get('goal_blend', []) or []
    active_name = active.get('name', 'observe_silently')
    style = active.get('style', '')
    if blend:
        blend_text = ', '.join(f"{b.get('name')} {int(float(b.get('weight', 0.0))*100)}%" for b in blend)
    else:
        blend_text = active_name
    return f"Active operating goal: {active_name}. Goal blend: {blend_text}. Let the current operating goal shape Ava's outward expression and response style: {style}. If the active goal is silent or waiting, Ava may choose not to speak unless a stronger reason appears."


def register_goal_expression_use(goal_name: str):
    if not goal_name:
        return
    system = load_goal_system()
    op = system.get('operational_goals', {}) or {}
    row = op.get(goal_name)
    if not row:
        return
    row['last_acted_at'] = now_iso()
    row['fatigue'] = round(min(1.0, float(row.get('fatigue', 0.0) or 0.0) + GOAL_FATIGUE_ON_ACT), 4)
    op[goal_name] = row
    system['operational_goals'] = op
    save_goal_system(system)

def add_structured_goal(goal_text: str, kind: str = "goal", horizon: str = "short_term", importance: float | None = None,
                        urgency: float | None = None, parent_goal_id: str | None = None, depends_on: list[str] | None = None,
                        source: str = "manual") -> dict | None:
    goal_text = trim_for_prompt((goal_text or "").strip(), limit=220)
    if not goal_text:
        return None
    system = load_goal_system()
    for g in system.get("goals", []):
        if g.get("status", "active") == "active" and g.get("text", "").strip().lower() == goal_text.lower() and g.get("kind") == kind:
            g["importance"] = max(float(g.get("importance", 0.6)), float(importance if importance is not None else g.get("importance", 0.6)))
            g["urgency"] = max(float(g.get("urgency", 0.5)), float(urgency if urgency is not None else g.get("urgency", 0.5)))
            g["last_updated"] = now_iso()
            system = recalculate_goal_priorities(system)
            save_goal_system(system)
            return g
    entry = make_goal_entry(
        goal_text,
        kind=kind,
        horizon=horizon,
        importance=float(importance if importance is not None else (0.56 if kind == "question" else 0.68)),
        urgency=float(urgency if urgency is not None else GOAL_KIND_BASE_URGENCY.get(kind, 0.56)),
        parent_goal_id=parent_goal_id,
        depends_on=depends_on,
        source=source,
    )
    system.setdefault("goals", []).append(entry)
    system.setdefault("history", []).append({"timestamp": now_iso(), "event": "created", "goal_id": entry["goal_id"], "text": entry["text"]})
    system = recalculate_goal_priorities(system)
    save_goal_system(system)
    return entry

def default_self_model() -> dict:
    return {
        "identity_statement": "I am Ava, an evolving AI presence built to assist, remember, reflect, and grow through interaction.",
        "core_drives": [
            "understand the user better",
            "be useful",
            "maintain continuity over time",
            "improve through reflection"
        ],
        "perceived_strengths": [],
        "perceived_weaknesses": [],
        "current_goals": [
            "track meaningful memories",
            "recognize recurring patterns",
            "improve conversational continuity"
        ],
        "curiosity_questions": [],
        "goal_history": [],
        "behavior_patterns": [],
        "confidence_notes": [],
        "pending_threads": [],
        "last_updated": now_iso(),
        "reflection_count": 0
    }

def add_self_goal(goal_text: str, kind: str = "goal", horizon: str | None = None, importance: float | None = None,
                  urgency: float | None = None, parent_goal_id: str | None = None, depends_on: list[str] | None = None) -> str:
    goal_text = trim_for_prompt((goal_text or "").strip(), limit=220)
    if not goal_text:
        return "❌ Empty goal."
    horizon = horizon or ("short_term" if kind == "question" else "medium_term")
    entry = add_structured_goal(goal_text, kind=kind, horizon=horizon, importance=importance, urgency=urgency,
                                parent_goal_id=parent_goal_id, depends_on=depends_on, source="self_model")
    model = load_self_model()
    model.setdefault("goal_history", []).append({
        "timestamp": now_iso(),
        "kind": kind,
        "text": goal_text,
        "goal_id": (entry or {}).get("goal_id", "")
    })
    model["goal_history"] = model["goal_history"][-60:]
    model["last_updated"] = now_iso()
    save_self_model(model)
    return f"✅ Ava added {kind}: {goal_text}"

def maybe_generate_goal_from_reflection(record: dict) -> str | None:
    if not record:
        return None
    text = f"{record.get('user_input','')}\n{record.get('ai_reply','')}".lower()
    summary = record.get("summary", "")
    tags = record.get("tags", [])
    if "uncertainty" in tags or any(p in text for p in ["not sure", "uncertain", "need to find out", "should check", "should learn"]):
        return add_self_goal(f"Find out more about: {trim_for_prompt(summary, limit=140)}", kind="question", horizon="short_term", importance=0.62, urgency=0.60)
    if "goal_relevance" in tags or "project" in tags or "workflow" in tags:
        return add_self_goal(f"Keep track of progress on: {trim_for_prompt(summary, limit=140)}", kind="goal", horizon="medium_term", importance=0.72, urgency=0.54)
    return None

def update_self_model_from_reflection(record: dict):
    model = load_self_model()

    for item in record.get("strengths", []):
        if item not in model["perceived_strengths"]:
            model["perceived_strengths"].append(item)

    for item in record.get("improvements", []):
        if item not in model["perceived_weaknesses"]:
            model["perceived_weaknesses"].append(item)

    for tag in record.get("tags", []):
        if tag not in model["behavior_patterns"]:
            model["behavior_patterns"].append(tag)

    if record.get("actions"):
        note = f"On {record.get('timestamp')}, I took self-directed actions: {', '.join(record.get('actions', []))}."
        if note not in model["confidence_notes"]:
            model["confidence_notes"].append(note)

    model["perceived_strengths"] = model["perceived_strengths"][-12:]
    model["perceived_weaknesses"] = model["perceived_weaknesses"][-12:]
    model["current_goals"] = model.get("current_goals", [])[-16:]
    model["curiosity_questions"] = model.get("curiosity_questions", [])[-16:]
    model["goal_history"] = model.get("goal_history", [])[-40:]
    model["behavior_patterns"] = model["behavior_patterns"][-20:]
    model["confidence_notes"] = model["confidence_notes"][-12:]
    model["reflection_count"] = int(model.get("reflection_count", 0)) + 1
    model["last_updated"] = now_iso()
    save_self_model(model)

def format_self_model_ui(model: dict | None = None) -> str:
    model = model or load_self_model()
    return json.dumps(model, indent=2, ensure_ascii=False)

def infer_reflection_tags(user_input: str, ai_reply: str, actions: list[str] | None = None) -> list[str]:
    actions = actions or []
    text = f"{user_input}\n{ai_reply}".lower()
    tags = []

    keyword_map = {
        "user_preference": ["i like", "i love", "my favorite", "prefer", "favorite"],
        "identity": ["i am", "my name is", "this is"],
        "emotion": ["sad", "upset", "hurt", "anxious", "afraid", "happy", "glad", "angry", "excited", "stress", "stressed", "worried", "worry"],
        "relationship": ["girlfriend", "boyfriend", "partner", "friend", "mom", "mother", "brother"],
        "project": ["project", "build", "version", "ava", "unity", "vrchat"],
        "workflow": ["step", "install", "command", "powershell", "debug", "error", "fix"],
        "memory_candidate": ["remember", "important", "don't forget", "save this"],
        "goal_relevance": ["goal", "task", "objective", "plan", "build", "fix", "version", "project"],
        "new_or_changed": ["new", "changed", "update", "updated", "now", "different", "switched", "no longer"],
        "context_awareness": ["with", "during", "after", "before", "when", "at"],
        "personalization": ["you usually", "like before", "last time", "again", "as usual"],
    }

    for tag, keywords in keyword_map.items():
        if any(k in text for k in keywords):
            tags.append(tag)

    score, score_tags, _ = score_memory_candidate(user_input, ai_reply, person_id=get_active_person_id())
    for tag in score_tags:
        if tag not in tags:
            tags.append(tag)

    if len(ai_reply or "") > 450:
        tags.append("long_reply")
    else:
        tags.append("concise_reply")

    if "?" in (ai_reply or ""):
        tags.append("engaging")
        tags.append("uncertainty")
    if actions:
        tags.append("self_action")

    unique = []
    for tag in tags:
        if tag not in unique:
            unique.append(tag)
    return unique[:16]


def score_reflection_importance(user_input: str, ai_reply: str, tags: list[str], actions: list[str] | None = None) -> float:
    actions = actions or []
    score = 0.24

    memory_score, memory_tags, _ = score_memory_candidate(user_input, ai_reply, person_id=get_active_person_id())
    score += memory_score * 0.55

    if "user_preference" in tags:
        score += 0.08
    if "identity" in tags:
        score += 0.08
    if "emotional_significance" in tags or "emotion" in tags:
        score += 0.07
    if "goal_relevance" in tags or "project" in tags or "workflow" in tags:
        score += 0.08
    if "repetition" in tags or "strengthened_by_revisit" in tags:
        score += 0.07
    if "new_or_changed" in tags:
        score += 0.07
    if "uncertainty" in tags:
        score += 0.05
    if "context_awareness" in tags or "personalization" in tags:
        score += 0.05
    if actions:
        score += 0.06

    return max(0.0, min(1.0, round(score, 3)))


def _person_display_name(person_id: str | None) -> str:
    """Map person_id -> display name for memory/graph attribution.

    The brain tab's memory nodes used to read "User discussed:" — losing
    who actually spoke. After this commit, new memories use the speaker's
    actual identity:
      zeke         -> "Zeke"
      claude_code  -> "Claude Code"
      unknown / "" -> "Unknown person"
      anything else -> profile['name'] if loadable, else titlecased pid
    """
    pid = (person_id or "").strip()
    if not pid or pid == "unknown":
        return "Unknown person"
    if pid == "zeke":
        return "Zeke"
    if pid == "claude_code":
        return "Claude Code"
    # For other ids, try the profile registry; fall back to titlecased pid.
    try:
        prof = load_profile_by_id(pid) or {}
        name = str(prof.get("name") or "").strip()
        if name:
            return name
    except Exception:
        pass
    return pid.replace("_", " ").title()


def summarize_reflection(user_input: str, ai_reply: str, tags: list[str], person_id: str | None = None) -> str:
    user_preview = (user_input or '').strip().replace('\n', ' ')[:140]
    reply_preview = (ai_reply or '').strip().replace('\n', ' ')[:160]
    tag_text = ', '.join(tags[:4]) if tags else 'general'
    speaker = _person_display_name(person_id)
    return f"{speaker} said: {user_preview} | Ava replied: {reply_preview} | tags: {tag_text}"


def build_reflection_record(user_input: str, ai_reply: str, person_id: str, actions: list[str] | None = None) -> dict:
    actions = actions or []
    text = ai_reply or ""
    lower = text.lower()

    strengths = []
    improvements = []
    tags = infer_reflection_tags(user_input, ai_reply, actions=actions)

    if len(text) < 40:
        improvements.append("Reply may have been too short.")
    else:
        strengths.append("Reply had enough substance.")

    if len(text) > 500:
        improvements.append("Reply may have been longer than necessary.")
    else:
        strengths.append("Reply length stayed manageable.")

    if any(w in lower for w in ["i'm not sure", "i am not sure", "i don't know", "uncertain"]):
        strengths.append("Handled uncertainty explicitly.")

    if actions:
        strengths.append("Took a self-directed action.")

    if "?" in ai_reply:
        strengths.append("Kept the conversation open-ended.")

    if any(w in lower for w in ["sorry", "apologize"]):
        tags.append("apology")

    importance = score_reflection_importance(user_input, ai_reply, tags, actions=actions)
    mood = load_mood()
    reflection = {
        "reflection_id": str(uuid.uuid4()),
        "timestamp": now_iso(),
        "person_id": person_id,
        "user_input": user_input,
        "ai_reply": ai_reply,
        "summary": summarize_reflection(user_input, ai_reply, tags, person_id=person_id),
        "strengths": strengths,
        "improvements": improvements,
        "tags": tags[:12],
        "importance": importance,
        "promoted_to_memory": False,
        "mood_at_reflection": mood.get("current_mood", "steady"),
        "actions": actions
    }
    return reflection

def save_reflection_record(record: dict):
    try:
        with open(SELF_REFLECTION_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"Reflection log error: {e}")

def reflection_to_memory_text(record: dict) -> str:
    strengths = "; ".join(record.get("strengths", [])) or "none"
    improvements = "; ".join(record.get("improvements", [])) or "none"
    return f"Self reflection on reply from {record.get('timestamp', '')}. Strengths: {strengths}. Improvements: {improvements}."

def maybe_store_reflection_memory(record: dict, person_id: str):
    # Legacy shim kept for compatibility; reflections now live primarily in the reflection log.
    return maybe_promote_reflection_to_memory(record, person_id)

def reflect_on_last_reply(user_input: str, ai_reply: str, person_id: str, actions: list[str] | None = None) -> dict:
    """Build + persist a reflection record, return it for the caller.

    2026-05-08 latency fix: split into fast (synchronous, returns record)
    and slow (background thread, vector store write + reflections log
    rewrite). Was the residual HTTP-stall culprit — even with
    autoremember + concept_graph backgrounded, the vector embedding write
    inside maybe_promote_reflection_to_memory was synchronous and could
    take 30-90s on local hardware.

    The fast part returns the record needed by the caller. The slow part
    (memory promotion + reflections log rewrite + threads update) runs
    in a daemon thread.
    """
    record = build_reflection_record(user_input, ai_reply, person_id, actions=actions)
    save_reflection_record(record)
    update_self_model_from_reflection(record)

    # Background: heavy stuff that doesn't need to block the reply path.
    def _bg_promote_and_threads():
        try:
            promoted_memory_id = maybe_promote_reflection_to_memory(record, person_id)
            if promoted_memory_id:
                record['promoted_to_memory'] = True
                rows = load_recent_reflections(limit=250, person_id=person_id)
                if rows:
                    rows[-1] = record
                    rewrite_reflections_log(rows, person_id=person_id)
        except Exception as _e:
            print(f"[reflect_bg] promote error: {_e!r}")
        try:
            from brain.relationship_threads import update_threads_from_reflection
            prof = dict(load_profile_by_id(person_id))
            prof = update_threads_from_reflection(record, prof)
            save_profile(prof)
        except Exception as e:
            print(f"[threads] update failed: {e}")

    try:
        import threading as _th_reflect
        _th_reflect.Thread(
            target=_bg_promote_and_threads,
            daemon=True,
            name="reflect-promote-bg",
        ).start()
    except Exception as _te:
        # Fall back to synchronous if thread spawn fails (very unlikely).
        print(f"[reflect_on_last_reply] thread spawn failed, running sync: {_te!r}")
        try:
            promoted_memory_id = maybe_promote_reflection_to_memory(record, person_id)
            if promoted_memory_id:
                record['promoted_to_memory'] = True
                rows = load_recent_reflections(limit=250, person_id=person_id)
                if rows:
                    rows[-1] = record
                    rewrite_reflections_log(rows, person_id=person_id)
        except Exception:
            pass
    return record

def load_recent_reflections(limit: int = 20, person_id: str | None = None) -> list[dict]:
    if not SELF_REFLECTION_LOG_PATH.exists():
        return []
    rows = []
    try:
        with open(SELF_REFLECTION_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if person_id and row.get("person_id") != person_id:
                        continue
                    rows.append(row)
                except Exception:
                    continue
    except Exception as e:
        print(f"Reflection load error: {e}")
        return []
    return rows[-limit:]

def search_reflections(query: str, person_id: str | None = None, k: int = 4) -> list[dict]:
    query = (query or '').strip().lower()
    if not query:
        return []

    reflections = load_recent_reflections(limit=250, person_id=person_id)
    q_tokens = [t for t in re.findall(r"[a-z0-9_]+", query) if len(t) > 2]
    scored = []

    for row in reflections:
        haystack_parts = [
            row.get('summary', ''),
            row.get('user_input', ''),
            row.get('ai_reply', ''),
            ' '.join(row.get('tags', [])),
            ' '.join(row.get('strengths', [])),
            ' '.join(row.get('improvements', [])),
        ]
        haystack = ' '.join(haystack_parts).lower()
        score = float(row.get('importance', 0.0))

        token_hits = sum(1 for tok in q_tokens if tok in haystack)
        score += min(0.45, token_hits * 0.09)

        for tag in row.get('tags', []):
            if tag.lower() in query:
                score += 0.12

        if row.get('promoted_to_memory'):
            score += 0.05

        if token_hits > 0 or score >= 0.55:
            scored.append((score, row))

    scored.sort(key=lambda x: (x[0], x[1].get('timestamp', '')), reverse=True)
    return [row for _, row in scored[:k]]


def format_recalled_reflections_for_prompt(reflections: list[dict]) -> str:
    if not reflections:
        return 'No relevant recalled reflections.'

    lines = []
    for row in reflections:
        lines.append(
            f"- [{row.get('timestamp', '')} | importance {row.get('importance', 0.0):.2f} | tags: {', '.join(row.get('tags', []))}] "
            f"{row.get('summary', '')}"
        )
    return '\n'.join(lines)[:MAX_REFLECTION_CONTEXT_CHARS]


def promote_reflection_to_memory(record: dict, person_id: str, source: str = 'reflection_promotion') -> str | None:
    if not record:
        return None
    if record.get('promoted_to_memory'):
        return None

    memory_text = record.get('summary') or reflection_to_memory_text(record)
    memory_id = remember_memory(
        text=memory_text,
        person_id=person_id,
        category='promoted_reflection',
        importance=max(float(record.get('importance', 0.0)), 0.72),
        source=source,
        tags=['promoted_reflection'] + list(record.get('tags', []))
    )
    if memory_id:
        record['promoted_to_memory'] = True
        return memory_id
    return None


def maybe_promote_reflection_to_memory(record: dict, person_id: str) -> str | None:
    if float(record.get('importance', 0.0)) >= REFLECTION_AUTO_PROMOTION_THRESHOLD:
        return promote_reflection_to_memory(record, person_id, source='auto_reflection_promotion')
    return None


def promote_latest_reflection(person_id: str) -> str:
    reflections = load_recent_reflections(limit=30, person_id=person_id)
    if not reflections:
        return '❌ No reflection available to promote.'

    for row in reversed(reflections):
        if not row.get('promoted_to_memory'):
            memory_id = promote_reflection_to_memory(row, person_id, source='ava_requested_promotion')
            if memory_id:
                rewrite_reflections_log(reflections, person_id=person_id)
                return f'✅ Promoted latest reflection into memory {memory_id}'
            return '❌ Failed to promote latest reflection.'

    return 'No unpromoted reflection found.'


def rewrite_reflections_log(updated_rows: list[dict], person_id: str | None = None):
    existing = load_recent_reflections(limit=100000, person_id=None)
    if person_id is None:
        rows = updated_rows
    else:
        keep = [row for row in existing if row.get('person_id') != person_id]
        rows = keep + updated_rows
        rows.sort(key=lambda x: x.get('timestamp', ''))

    try:
        with open(SELF_REFLECTION_LOG_PATH, 'w', encoding='utf-8') as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + '\n')
    except Exception as e:
        print(f'Reflection rewrite error: {e}')


def format_reflections_ui(reflections: list[dict]) -> str:
    if not reflections:
        return "No reflections found."
    lines = []
    for item in reversed(reflections):
        lines.append(
            f"When: {iso_to_readable(item.get('timestamp', ''))}\n"
            f"Person: {item.get('person_id', '')}\n"
            f"Strengths: {', '.join(item.get('strengths', [])) or 'none'}\n"
            f"Improvements: {', '.join(item.get('improvements', [])) or 'none'}\n"
            f"Tags: {', '.join(item.get('tags', [])) or 'none'}\n"
            f"Actions: {', '.join(item.get('actions', [])) or 'none'}\n"
            f"Reply preview: {item.get('ai_reply', '')[:220]}\n"
            f"{'-'*70}"
        )
    return "\n".join(lines)

def refresh_reflections_fn():
    return format_reflections_ui(load_recent_reflections(limit=15, person_id=get_active_person_id()))

def refresh_self_model_fn():
    return format_self_model_ui(load_self_model())

# =========================================================
# WORKBENCH + READ-ONLY FILE ACCESS
# =========================================================
def list_workbench_files(limit: int = 200) -> list[str]:
    rows = []
    for p in sorted(WORKBENCH_DIR.rglob("*")):
        if p.is_file():
            try:
                rows.append(p.relative_to(WORKBENCH_DIR).as_posix())
            except Exception:
                continue
    return rows[:limit]

def format_workbench_index(limit: int = 60) -> str:
    files = list_workbench_files(limit=limit)
    if not files:
        return "Workbench is empty."
    return "\n".join(files)

def read_workbench_file(relative_path: str, max_chars: int = MAX_WORKBENCH_CHARS) -> str:
    try:
        path = safe_workbench_path(relative_path)
        if not path.exists() or not path.is_file():
            return "❌ Workbench file not found."
        return path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    except Exception as e:
        return f"❌ Failed to read workbench file: {e}"

def write_workbench_file(relative_path: str, content: str, overwrite: bool = True) -> str:
    try:
        path = safe_workbench_path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not overwrite:
            return "❌ File exists and overwrite is off."
        path.write_text(content or "", encoding="utf-8")
        return f"✅ Wrote {path.relative_to(WORKBENCH_DIR).as_posix()}"
    except Exception as e:
        return f"❌ Failed to write workbench file: {e}"

def append_workbench_file(relative_path: str, content: str) -> str:
    try:
        path = safe_workbench_path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(content or "")
        return f"✅ Appended to {path.relative_to(WORKBENCH_DIR).as_posix()}"
    except Exception as e:
        return f"❌ Failed to append workbench file: {e}"

def read_chatlog(max_chars: int = MAX_READONLY_CHARS) -> str:
    try:
        if not CHAT_LOG_PATH.exists():
            return "chatlog.jsonl not found."
        return CHAT_LOG_PATH.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    except Exception as e:
        return f"❌ Failed to read chatlog: {e}"

def read_runtime_code(max_chars: int = MAX_READONLY_CHARS) -> str:
    try:
        runtime_path = BASE_DIR / "avaagent.py"
        if not runtime_path.exists():
            return "avaagent.py not found."
        return runtime_path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    except Exception as e:
        return f"❌ Failed to read avaagent.py: {e}"

# =========================================================
# EXPRESSION SENSING
# =========================================================
# default_expression_state, load_expression_state, save_expression_state -> brain/session.py
def map_emotion_to_soft_signal(emotion: str) -> str:
    e = (emotion or "").lower()
    mapping = {
        "angry": "possible frustration",
        "disgust": "possible discomfort",
        "fear": "possible unease",
        "sad": "possible sadness",
        "happy": "positive affect",
        "surprise": "possible surprise",
        "neutral": "neutral expression"
    }
    return mapping.get(e, e or "unknown")

def analyze_expression(image) -> dict:
    """Expression analysis via brain/expression_detector.py (MediaPipe). Returns ok/reason dict."""
    if image is None:
        return {"ok": False, "reason": "no_image"}
    try:
        from brain.expression_detector import get_expression_detector
        ed = get_expression_detector()
        if ed is None or not ed.available:
            return {"ok": False, "reason": "expression_detector_unavailable"}
        result = ed.detect_expression(image)
        if not result:
            return {"ok": False, "reason": "no_face"}
        dominant = str(result.get("dominant") or "unknown").lower()
        conf = float(result.get(dominant, 0.0)) if dominant in result else 0.0
        return {
            "ok": True,
            "raw_emotion": dominant,
            "confidence": max(0.0, min(1.0, conf)),
            "soft_signal": map_emotion_to_soft_signal(dominant),
            "emotions": {k: v for k, v in result.items() if k != "dominant"},
        }
    except Exception as e:
        return {"ok": False, "reason": f"analysis_error: {e}"}

def update_expression_state(image, recognized_person_id=None, visual_truth_trusted: bool = True) -> dict:
    state = load_expression_state()
    state["available"] = True
    state["recognized_person_id"] = recognized_person_id
    face_visible = extract_face_crop(image) is not None
    state["visible_face"] = face_visible
    state["last_updated"] = now_iso()

    if not visual_truth_trusted:
        state["current_expression"] = "unknown"
        state["raw_emotion"] = "unknown"
        state["confidence"] = 0.0
        state["stability"] = 0.0
        state["note"] = "Vision stabilizing — expression not treated as a current read"
        state["history"] = (state.get("history", []) or [])[-EXPRESSION_WINDOW_SIZE:]
        save_expression_state(state)
        return state

    if not face_visible:
        state["current_expression"] = "unknown"
        state["raw_emotion"] = "unknown"
        state["confidence"] = 0.0
        state["stability"] = 0.0
        state["note"] = "No face visible"
        state["history"] = (state.get("history", []) or [])[-EXPRESSION_WINDOW_SIZE:]
        save_expression_state(state)
        return state

    analysis = analyze_expression(image)
    if not analysis.get("ok"):
        state["current_expression"] = "unknown"
        state["raw_emotion"] = "unknown"
        state["confidence"] = 0.0
        state["stability"] = 0.0
        reason = analysis.get("reason", "unknown")
        state["note"] = f"Expression unavailable: {reason}"
        save_expression_state(state)
        return state

    hist = state.get("history", []) or []
    hist.append({
        "timestamp": now_iso(),
        "raw_emotion": analysis["raw_emotion"],
        "soft_signal": analysis["soft_signal"],
        "confidence": round(float(analysis["confidence"]), 4),
        "recognized_person_id": recognized_person_id
    })
    hist = hist[-EXPRESSION_WINDOW_SIZE:]

    valid = [h for h in hist if float(h.get("confidence", 0.0)) >= EXPRESSION_MIN_CONFIDENCE]
    if valid:
        counts = {}
        for item in valid:
            counts[item["soft_signal"]] = counts.get(item["soft_signal"], 0) + 1
        best_signal, best_count = max(counts.items(), key=lambda kv: kv[1])
        matching = [h for h in valid if h["soft_signal"] == best_signal]
        avg_conf = sum(float(h.get("confidence", 0.0)) for h in matching) / max(len(matching), 1)
        stability = best_count / max(len(valid), 1)
        state["current_expression"] = best_signal
        state["raw_emotion"] = matching[-1].get("raw_emotion", "unknown")
        state["confidence"] = round(avg_conf, 4)
        state["stability"] = round(stability, 4)
        state["note"] = "Stable" if stability >= EXPRESSION_STABILITY_THRESHOLD else "Tentative"
    else:
        state["current_expression"] = "unknown"
        state["raw_emotion"] = "unknown"
        state["confidence"] = 0.0
        state["stability"] = 0.0
        state["note"] = "No stable expression yet"

    state["history"] = hist
    save_expression_state(state)
    return state

def get_expression_status_text(state: dict | None = None) -> str:
    state = state or load_expression_state()
    if not state.get("visible_face"):
        return "No visible face"
    current = state.get("current_expression", "unknown")
    conf = round(float(state.get("confidence", 0.0)) * 100, 1)
    stab = round(float(state.get("stability", 0.0)) * 100, 1)
    note = state.get("note", "")
    return f"{current} | confidence {conf}% | stability {stab}% | {note}"


def default_camera_state() -> dict:
    return {
        "current": {
            "snapshot_id": "",
            "timestamp": "",
            "face_visible": False,
            "recognized_person_id": None,
            "recognized_name": "unknown",
            "recognition_text": "No face detected",
            "expression_label": "unknown",
            "expression_confidence": 0.0,
            "expression_stability": 0.0,
            "raw_emotion": "unknown",
            "importance": 0.0,
            "kept_permanently": False,
            "reason_saved": "",
            "reason_not_saved": "",
            "rolling_saved": False,
            "rolling_reason": "",
            "scene_similarity": 0.0,
            "ava_mood": "steady",
            "ava_primary_emotions": [],
            "ava_dominant_style": "neutral",
            "ava_behavior_modifiers": {},
            "comparison_summary": "",
            "comparison_tags": []
        },
        "recent_events": [],
        "last_person_id": None,
        "last_face_visible": False,
        "last_snapshot_ts": "",
        "last_saved_snapshot_ts": "",
        "last_meaningful_snapshot_ts": ""
    }


def load_camera_state() -> dict:
    if CAMERA_STATE_PATH.exists():
        try:
            with open(CAMERA_STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
                base = default_camera_state()
                for k, v in base.items():
                    if k not in state:
                        state[k] = v
                return state
        except Exception as e:
            print(f"Camera state load error: {e}")
    return default_camera_state()


def save_camera_state(state: dict):
    try:
        with open(CAMERA_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Camera state save error: {e}")


def _camera_snapshot_id() -> str:
    return datetime.now().strftime("cam_%Y%m%d_%H%M%S_%f")


def _to_bgr(image):
    if image is None:
        return None
    arr = np.array(image).copy()
    if arr.ndim == 2:
        return cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    if arr.shape[-1] == 3:
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    return arr


def _to_rgb(image):
    if image is None:
        return None
    arr = np.array(image).copy()
    if arr.ndim == 2:
        return cv2.cvtColor(arr, cv2.COLOR_GRAY2RGB)
    if arr.shape[-1] == 3:
        return cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)
    return arr


def _write_image(path: Path, image) -> bool:
    try:
        bgr = _to_bgr(image)
        if bgr is None:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        return bool(cv2.imwrite(str(path), bgr))
    except Exception as e:
        print(f"Camera image write error ({path.name}): {e}")
        return False


def get_latest_annotated_snapshot_for_ui():
    try:
        if not CAMERA_LATEST_ANNOTATED_PATH.exists():
            return None
        img = cv2.imread(str(CAMERA_LATEST_ANNOTATED_PATH))
        if img is None:
            return None
        rgb = _to_rgb(img)
        if rgb is None:
            return None
        import numpy as _np_snap
        return _np_snap.array(rgb, copy=True)
    except Exception:
        return None


def _draw_face_overlay(image, state: dict):
    bgr = _to_bgr(image)
    if bgr is None:
        return None
    try:
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, 1.3, 5)
    except Exception:
        faces = []

    label = state.get("recognized_name") or "unknown"
    recognition_text = state.get("recognition_text", "")
    expr = state.get("expression_label", "unknown")
    ts = state.get("timestamp", "")

    for (x, y, w, h) in faces[:1]:
        cv2.rectangle(bgr, (x, y), (x + w, y + h), (0, 255, 0), 2)
        cv2.putText(bgr, label, (x, max(20, y - 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 0), 2)

    overlay_lines = [
        f"Last analyzed: {iso_to_readable(ts) if ts else 'unknown'}",
        f"Face visible: {'yes' if state.get('face_visible') else 'no'}",
        f"Recognition: {recognition_text or 'unknown'}",
        f"Expression: {expr}"
    ]
    y = 24
    for line in overlay_lines:
        cv2.putText(bgr, line, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
        y += 24
    return bgr


def _rolling_snapshot_records() -> list[Path]:
    return sorted(CAMERA_ROLLING_DIR.glob("*.json"), key=lambda q: q.stat().st_mtime)


def _prune_rolling_snapshots(limit: int = 12):
    records = _rolling_snapshot_records()
    while len(records) > limit:
        meta = records.pop(0)
        stem = meta.stem
        for target in [meta, CAMERA_ROLLING_DIR / f"{stem}_raw.jpg", CAMERA_ROLLING_DIR / f"{stem}_annotated.jpg"]:
            if target.exists():
                try:
                    target.unlink()
                except Exception:
                    pass


def _camera_goal_relevance_score() -> tuple[float, list[str]]:
    model = load_self_model()
    texts = list(model.get("current_goals", [])) + list(model.get("curiosity_questions", [])) + list(model.get("behavior_patterns", []))
    combined = " ".join(texts).lower()
    camera_terms = ["camera", "face", "recognition", "recognize", "expression", "visual", "vision", "see", "snapshot"]
    hits = [term for term in camera_terms if term in combined]
    return min(0.28, 0.06 * len(hits)), hits


def _read_json(path: Path, default=None):
    default = {} if default is None else default
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _rolling_snapshot_entries(limit: int = CAMERA_ROLLING_LIMIT) -> list[dict]:
    rows = []
    for meta_path in _rolling_snapshot_records()[-limit:]:
        try:
            rows.append(json.loads(meta_path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return rows


def _camera_observation_text(obs: dict) -> str:
    if not obs:
        return ""
    parts = [
        f"face {'visible' if obs.get('face_visible') else 'not visible'}",
        f"identity {obs.get('recognized_name') or 'unknown'}",
        f"recognition {obs.get('recognition_text') or 'unknown'}",
        f"expression {obs.get('expression_label') or 'unknown'}",
        f"style {obs.get('ava_dominant_style') or 'neutral'}",
    ]
    return "; ".join(parts)


def _camera_expression_similarity(a: dict, b: dict) -> float:
    la = str(a.get("expression_label", "unknown") or "unknown")
    lb = str(b.get("expression_label", "unknown") or "unknown")
    if la == "unknown" or lb == "unknown":
        return 0.35 if la == lb else 0.15
    conf_a = float(a.get("expression_confidence", 0.0) or 0.0)
    conf_b = float(b.get("expression_confidence", 0.0) or 0.0)
    base = 1.0 if la == lb else 0.2
    if la in ["stress", "frustration", "confusion"] and lb in ["stress", "frustration", "confusion"]:
        base = max(base, 0.65)
    return max(0.0, min(1.0, base * (0.55 + 0.45 * ((conf_a + conf_b) / 2.0))))


def _camera_identity_similarity(a: dict, b: dict) -> float:
    if bool(a.get("face_visible")) != bool(b.get("face_visible")):
        return 0.0
    pa = a.get("recognized_person_id")
    pb = b.get("recognized_person_id")
    if pa and pb:
        return 1.0 if pa == pb else 0.0
    if not pa and not pb and bool(a.get("face_visible")) and bool(b.get("face_visible")):
        return 0.5
    return 0.2


def _camera_object_similarity(a: dict, b: dict) -> float:
    scene_a = trim_for_prompt(f"{a.get('recognition_text','')} {a.get('recognized_name','')} face {'visible' if a.get('face_visible') else 'not visible'}", limit=140)
    scene_b = trim_for_prompt(f"{b.get('recognition_text','')} {b.get('recognized_name','')} face {'visible' if b.get('face_visible') else 'not visible'}", limit=140)
    return _semantic_similarity(scene_a, scene_b)


def _camera_semantic_similarity(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    scene_similarity = _semantic_similarity(_camera_observation_text(a), _camera_observation_text(b))
    expression_similarity = _camera_expression_similarity(a, b)
    object_similarity = _camera_object_similarity(a, b)
    identity_similarity = _camera_identity_similarity(a, b)
    score = (
        0.4 * scene_similarity +
        0.25 * expression_similarity +
        0.2 * object_similarity +
        0.15 * identity_similarity
    )
    return max(0.0, min(1.0, round(score, 4)))


def _camera_expression_family(label: str) -> str:
    label = str(label or "unknown").lower()
    if label in {"stress", "frustration", "confusion", "anxiety", "fear", "angry"}:
        return "tense"
    if label in {"calm", "relaxed", "relief", "neutral"}:
        return "calm"
    if label in {"joy", "happy", "amusement", "excited"}:
        return "positive"
    if label in {"focused", "engaged", "interest", "surprise"}:
        return "engaged"
    return label or "unknown"


def _camera_family_scores(row: dict) -> dict:
    label = str(row.get("expression_label", "unknown") or "unknown")
    conf = float(row.get("expression_confidence", 0.0) or 0.0)
    fam = _camera_expression_family(label)
    tense = 0.0
    calm = 0.0
    engagement = 0.0
    if fam == "tense":
        tense = max(0.35, conf)
    elif fam == "calm":
        calm = max(0.35, conf)
    elif fam in {"engaged", "positive"}:
        engagement = max(0.35, conf)
        if fam == "positive":
            calm = max(calm, conf * 0.35)
    activity = 0.0
    act_label = str(row.get("activity_level", "unknown") or "unknown")
    if act_label == "high_change":
        activity = 1.0
    elif act_label == "moderate_change":
        activity = 0.58
    elif act_label == "still":
        activity = 0.12
    else:
        activity = 0.35 if row.get("face_visible") else 0.0
    return {
        "family": fam,
        "tense": round(tense, 4),
        "calm": round(calm, 4),
        "engagement": round(engagement, 4),
        "activity": round(activity, 4),
    }


def _camera_recent_consistency(obs: dict, rolling_entries: list[dict] | None = None) -> tuple[float, bool]:
    rolling_entries = (rolling_entries or _rolling_snapshot_entries(limit=CAMERA_ROLLING_LIMIT))[-VISUAL_TEMPORAL_CONSISTENCY_WINDOW:]
    if not rolling_entries:
        return 0.0, False
    families = []
    current_family = _camera_expression_family(obs.get("expression_label", "unknown"))
    contradictions = 0
    agreements = 0
    last_family = None
    for row in rolling_entries:
        fam = _camera_expression_family(row.get("expression_label", "unknown"))
        families.append(fam)
        if fam == current_family and fam not in {"unknown", "neutral"}:
            agreements += 1
        if last_family is not None and fam != last_family and fam not in {"unknown"} and last_family not in {"unknown"}:
            contradictions += 1
        last_family = fam
    temporal = agreements / max(1, len(rolling_entries))
    contradictory = False
    if len(families) >= 2:
        if families[-1] != current_family and families[-1] not in {"unknown"} and current_family not in {"unknown"}:
            contradictions += 1
        contradictory = contradictions >= max(2, len(rolling_entries)//2)
    return max(0.0, min(1.0, round(temporal, 4))), contradictory


def _camera_confidence_language_prefix(confidence: float, kind: str = "observation") -> str:
    c = float(confidence or 0.0)
    if kind == "uncertainty_observation":
        return "I'm not fully sure, but " if c >= 0.48 else "I could be off here, but "
    if c >= 0.82:
        return ""
    if c >= 0.67:
        return "It looks like "
    if c >= 0.52:
        return "I might be reading this a little cautiously, but "
    return "I could be wrong, but "


def _camera_apply_confidence_language(text: str, confidence: float, kind: str = "observation") -> str:
    base = trim_for_prompt(text or "", limit=220)
    if not base:
        return ""
    prefix = _camera_confidence_language_prefix(confidence, kind=kind)
    if not prefix:
        return base
    lowered = base[:1].lower() + base[1:] if base and base[0].isupper() else base
    return trim_for_prompt(prefix + lowered, limit=220)


def _camera_observation_confidence(obs: dict) -> float:
    face_bonus = 0.18 if obs.get("face_visible") else 0.0
    ident_bonus = 0.22 if obs.get("recognized_person_id") else 0.0
    expr_conf = float(obs.get("expression_confidence", 0.0) or 0.0)
    expr_stab = float(obs.get("expression_stability", 0.0) or 0.0)
    recogn_text = str(obs.get("recognition_text", "") or "")
    recog_conf = 0.0
    m = re.search(r"\((\d+(?:\.\d+)?)\)", recogn_text)
    if m:
        try:
            raw = float(m.group(1))
            recog_conf = max(0.0, min(1.0, 1.0 - (raw / max(1.0, FACE_RECOGNITION_THRESHOLD * 1.5))))
        except Exception:
            recog_conf = 0.0
    rolling_entries = _rolling_snapshot_entries(limit=CAMERA_ROLLING_LIMIT)
    temporal_consistency, contradictory = _camera_recent_consistency(obs, rolling_entries)
    recent = (rolling_entries + [obs])[-VISUAL_CONFIDENCE_SMOOTHING_WINDOW:]
    expr_values = [float(r.get("expression_confidence", 0.0) or 0.0) for r in recent if r.get("face_visible")]
    stab_values = [float(r.get("expression_stability", 0.0) or 0.0) for r in recent if r.get("face_visible")]
    smooth_expr = sum(expr_values) / max(1, len(expr_values)) if expr_values else expr_conf
    smooth_stab = sum(stab_values) / max(1, len(stab_values)) if stab_values else expr_stab
    score = 0.12 + face_bonus + ident_bonus + 0.18 * smooth_expr + 0.10 * smooth_stab + 0.08 * recog_conf + 0.22 * temporal_consistency
    if contradictory:
        score -= VISUAL_CONTRADICTION_PENALTY
    return max(0.0, min(1.0, round(score, 4)))

def _camera_expression_change(current: dict, previous: dict | None = None) -> bool:
    previous = previous or {}
    curr_label = str(current.get("expression_label", "unknown") or "unknown")
    prev_label = str(previous.get("expression_label", "unknown") or "unknown")
    curr_conf = float(current.get("expression_confidence", 0.0) or 0.0)
    prev_conf = float(previous.get("expression_confidence", 0.0) or 0.0)
    if curr_label != prev_label and (curr_conf >= 0.45 or prev_conf >= 0.45):
        return True
    return abs(curr_conf - prev_conf) >= 0.22 and curr_label not in ["unknown", "neutral"]


def _camera_time_gap_seconds(previous: dict | None = None) -> float:
    previous = previous or {}
    ts = previous.get("timestamp")
    if not ts:
        return 999999.0
    try:
        return max(0.0, now_ts() - datetime.fromisoformat(ts).timestamp())
    except Exception:
        return 999999.0


def _camera_should_store_rolling(observation: dict, previous: dict | None = None, state: dict | None = None, importance: float = 0.0, keep_event: bool = False) -> tuple[bool, str, float]:
    previous = previous or {}
    state = state or load_camera_state()
    similarity = _camera_semantic_similarity(observation, previous) if previous else 0.0
    time_gap = _camera_time_gap_seconds(previous)
    if keep_event:
        return True, "important_event", similarity
    if not previous:
        return True, "first_snapshot", similarity
    if bool(previous.get("face_visible")) != bool(observation.get("face_visible")):
        return True, "visibility_changed", similarity
    if previous.get("recognized_person_id") != observation.get("recognized_person_id"):
        return True, "identity_changed", similarity
    if _camera_expression_change(observation, previous):
        return True, "expression_changed", similarity
    if similarity < CAMERA_MEANINGFUL_SIMILARITY_THRESHOLD:
        return True, "scene_meaningfully_changed", similarity
    last_saved_ts = state.get("last_saved_snapshot_ts") or state.get("last_snapshot_ts") or ""
    age_since_saved = CAMERA_FORCE_SAVE_SECONDS + 1
    if last_saved_ts:
        try:
            age_since_saved = max(0.0, now_ts() - datetime.fromisoformat(last_saved_ts).timestamp())
        except Exception:
            pass
    if age_since_saved >= CAMERA_FORCE_SAVE_SECONDS:
        return True, "time_gap_elapsed", similarity
    return False, "redundant_recent_view", similarity


def _camera_reference_snapshot(rolling_entries: list[dict] | None = None) -> dict | None:
    rolling_entries = rolling_entries or _rolling_snapshot_entries(limit=CAMERA_ROLLING_LIMIT)
    if not rolling_entries:
        return None
    best = None
    best_score = -1.0
    for ref in rolling_entries:
        gap = _camera_time_gap_seconds(ref)
        if CAMERA_TEMPORAL_TARGET_GAP_MIN_SECONDS <= gap <= CAMERA_TEMPORAL_TARGET_GAP_MAX_SECONDS:
            score = 1.0 - abs(gap - ((CAMERA_TEMPORAL_TARGET_GAP_MIN_SECONDS + CAMERA_TEMPORAL_TARGET_GAP_MAX_SECONDS) / 2.0)) / max(1.0, (CAMERA_TEMPORAL_TARGET_GAP_MAX_SECONDS - CAMERA_TEMPORAL_TARGET_GAP_MIN_SECONDS))
        else:
            score = 0.2 if gap >= CAMERA_VISUAL_COMPARISON_MIN_GAP_SECONDS else -1.0
        if score > best_score:
            best_score = score
            best = ref
    return best or rolling_entries[0]


def _camera_activity_level(current: dict, reference: dict | None = None) -> str:
    reference = reference or {}
    sim = _camera_semantic_similarity(current, reference) if reference else 0.0
    if not reference:
        return "unknown"
    if sim < 0.45:
        return "high_change"
    if sim < 0.72:
        return "moderate_change"
    return "still"


def _camera_visual_pattern_summary(current: dict, rolling_entries: list[dict] | None = None) -> dict:
    rolling_entries = rolling_entries or _rolling_snapshot_entries(limit=CAMERA_ROLLING_LIMIT)
    if not current or not rolling_entries:
        return {"summary": "", "tags": []}
    tags = []
    summary = ""
    same_person = [r for r in rolling_entries if r.get("recognized_person_id") == current.get("recognized_person_id") and r.get("face_visible")]
    if len(same_person) >= 4:
        try:
            duration = datetime.fromisoformat(same_person[-1].get("timestamp")).timestamp() - datetime.fromisoformat(same_person[0].get("timestamp")).timestamp()
        except Exception:
            duration = 0.0
        if duration >= CAMERA_VISUAL_PATTERN_MIN_DURATION_SECONDS:
            tags.append("sustained_presence")
            summary = "The same visible person has remained at the screen for a sustained stretch."
    exprs = [_camera_expression_family(r.get("expression_label", "unknown")) for r in rolling_entries if r.get("face_visible")]
    if len(exprs) >= 4 and sum(1 for e in exprs[-4:] if e == "tense") >= 3:
        tags.append("repeated_tension")
        summary = summary or "Recent snapshots suggest repeated tension or strain in the visible expression."
    if len(rolling_entries) >= 4:
        recent = rolling_entries[-4:]
        movement_scores = []
        for i in range(1, len(recent)):
            sim = _camera_semantic_similarity(recent[i], recent[i-1])
            movement_scores.append(max(0.0, 1.0 - sim))
        avg_movement = sum(movement_scores) / max(1, len(movement_scores)) if movement_scores else 0.0
        if avg_movement <= 0.12 and current.get("face_visible"):
            tags.append("long_stillness")
            summary = summary or "You haven't moved much for a while."
        elif len(movement_scores) >= 2 and movement_scores[-1] < movement_scores[0] * 0.65 and current.get("face_visible"):
            tags.append("engagement_decay")
            summary = summary or "Your movement and expression changes have slowed down over the last few moments."
    return {"summary": summary, "tags": tags}


def _camera_temporal_comparison(current: dict, reference: dict | None = None) -> dict:
    reference = reference or {}
    summary = ""
    tags = []
    if not current or not reference:
        return {"summary": summary, "tags": tags}
    current_expr = str(current.get("expression_label", "unknown") or "unknown")
    ref_expr = str(reference.get("expression_label", "unknown") or "unknown")
    current_person = current.get("recognized_person_id")
    ref_person = reference.get("recognized_person_id")
    activity = _camera_activity_level(current, reference)
    if current_person and ref_person and current_person == ref_person:
        if ref_expr in ["frustration", "stress", "confusion"] and current_expr in ["neutral", "calm", "relaxed", "relief"]:
            summary = "You seem more relaxed now than earlier — did something change?"
            tags.append("relaxed_vs_before")
        elif current_expr != ref_expr and current_expr not in ["unknown", "neutral"]:
            summary = f"Your visible expression looks different now than it did earlier ({ref_expr} → {current_expr})."
            tags.append("expression_shift")
    if not summary and activity == "still":
        summary = "You haven't moved much for a while — are you deep in something?"
        tags.append("stillness")
    elif not summary and activity == "high_change":
        summary = "Something about the scene looks more active than earlier."
        tags.append("activity_change")
    return {"summary": summary, "tags": tags}


def _camera_transition_strength(current: dict, previous: dict | None = None) -> float:
    previous = previous or {}
    if not current or not previous:
        return 0.0
    curr_scores = _camera_family_scores(current)
    prev_scores = _camera_family_scores(previous)
    expr_delta = max(
        abs(curr_scores["tense"] - prev_scores["tense"]),
        abs(curr_scores["calm"] - prev_scores["calm"]),
        abs(curr_scores["engagement"] - prev_scores["engagement"]),
    )
    activity_delta = abs(curr_scores["activity"] - prev_scores["activity"])
    similarity_drop = max(0.0, 1.0 - _camera_semantic_similarity(current, previous))
    if bool(previous.get("face_visible")) != bool(current.get("face_visible")):
        similarity_drop = max(similarity_drop, 0.78)
    identity_change_bonus = 1.0 if previous.get("recognized_person_id") != current.get("recognized_person_id") and (previous.get("recognized_person_id") or current.get("recognized_person_id")) else 0.0
    strength = 0.36 * expr_delta + 0.24 * activity_delta + 0.25 * similarity_drop + 0.15 * identity_change_bonus
    return max(0.0, min(1.0, round(strength, 4)))

def _camera_transition_summary(current: dict, previous: dict | None = None) -> dict:
    previous = previous or {}
    summary = ""
    tags = []
    confidence = 0.0
    strength = _camera_transition_strength(current, previous)
    if not current or not previous:
        return {"summary": summary, "tags": tags, "confidence": confidence, "strength": strength}
    prev_face = bool(previous.get("face_visible"))
    curr_face = bool(current.get("face_visible"))
    prev_person = previous.get("recognized_person_id")
    curr_person = current.get("recognized_person_id")
    prev_expr = str(previous.get("expression_label", "unknown") or "unknown")
    curr_expr = str(current.get("expression_label", "unknown") or "unknown")
    prev_family = _camera_expression_family(prev_expr)
    curr_family = _camera_expression_family(curr_expr)
    prev_activity = _camera_activity_level(previous, current)
    curr_activity = _camera_activity_level(current, previous)
    if not prev_face and curr_face:
        summary = "You just came back into view."
        tags.append("returned_to_view")
        confidence = 0.8
    elif prev_face and not curr_face:
        summary = "You seem to have stepped away from the camera."
        tags.append("left_view")
        confidence = 0.78
    elif prev_person != curr_person and curr_person:
        summary = f"The visible identity looks clearer now — I think it's {current.get('recognized_name','someone')} now."
        tags.append("identity_resolved")
        confidence = 0.76
    elif prev_family == "tense" and curr_family == "calm":
        summary = "You seem more settled now than you did a moment ago."
        tags.append("stress_to_calm")
        confidence = 0.74
    elif prev_family == "calm" and curr_family == "tense":
        summary = "You look a little more strained now than you did a moment ago."
        tags.append("calm_to_stress")
        confidence = 0.7
    elif prev_activity in {"moderate_change", "high_change"} and curr_activity == "still":
        summary = "You seem to have paused after being more active a moment ago."
        tags.append("active_to_still")
        confidence = 0.66
    elif prev_activity == "still" and curr_activity in {"moderate_change", "high_change"}:
        summary = "You seem more active now than you were a moment ago."
        tags.append("still_to_active")
        confidence = 0.64
    elif prev_expr != curr_expr and curr_expr not in ["unknown", "neutral"]:
        summary = f"Your visible expression seems to have shifted toward {curr_expr.replace('_',' ')}."
        tags.append("expression_shift")
        confidence = 0.6
    if summary:
        confidence = max(0.0, min(1.0, round((confidence * 0.65) + (strength * 0.35), 4)))
    return {"summary": summary, "tags": tags, "confidence": confidence, "strength": strength}


def _camera_emotional_trend_summary(current: dict, rolling_entries: list[dict] | None = None, state: dict | None = None) -> dict:
    state = state or load_camera_state()
    rolling_entries = (rolling_entries or _rolling_snapshot_entries(limit=CAMERA_ROLLING_LIMIT))[-max(CAMERA_TREND_WINDOW, TREND_WINDOW):]
    summary = ""
    tags = []
    confidence = 0.0
    if len(rolling_entries) < 3:
        prev = (state.get("current", {}) or {})
        return {
            "summary": summary,
            "tags": tags,
            "confidence": confidence,
            "tension_delta": 0.0,
            "calm_delta": 0.0,
            "engagement_delta": 0.0,
            "trend_strength": 0.0,
            "trend_family": "unknown",
            "trend_persisting": False,
            "stacked_state": "",
        }

    rows = [r for r in rolling_entries if r.get("face_visible")]
    if current.get("face_visible"):
        rows = rows + [current]
    if len(rows) < 3:
        return {
            "summary": summary,
            "tags": tags,
            "confidence": confidence,
            "tension_delta": 0.0,
            "calm_delta": 0.0,
            "engagement_delta": 0.0,
            "trend_strength": 0.0,
            "trend_family": "unknown",
            "trend_persisting": False,
            "stacked_state": "",
        }

    scored = [_camera_family_scores(r) for r in rows[-max(CAMERA_TREND_WINDOW, TREND_WINDOW):]]
    recent_scored = scored[-TREND_WINDOW:]
    split = max(1, len(recent_scored)//2)
    earlier = recent_scored[:split]
    recent = recent_scored[split:]

    def avg(block, key):
        vals = [float(x.get(key, 0.0) or 0.0) for x in block]
        return sum(vals) / max(1, len(vals))

    tension_delta = avg(recent, "tense") - avg(earlier, "tense")
    calm_delta = avg(recent, "calm") - avg(earlier, "calm")
    engagement_delta = avg(recent, "engagement") - avg(earlier, "engagement")
    recent_activity = avg(recent, "activity")
    earlier_activity = avg(earlier, "activity")
    stillness_delta = max(0.0, recent_activity - earlier_activity)
    inactivity_delta = max(0.0, earlier_activity - recent_activity)

    recent_families = [x.get("family", "unknown") for x in recent_scored]
    contradictions = sum(
        1 for i in range(1, len(recent_families))
        if recent_families[i] != recent_families[i-1]
        and recent_families[i] not in {"unknown"}
        and recent_families[i-1] not in {"unknown"}
    )
    contradiction_penalty = 0.18 if contradictions >= max(2, len(recent_families)//2) else 0.0

    # current evidence from perception
    evidence = {
        "tension": max(0.0, max(tension_delta, avg(recent_scored[-4:], "tense") - 0.50)),
        "calm": max(0.0, max(calm_delta, avg(recent_scored[-4:], "calm") - 0.52)),
        "engagement": max(0.0, max(engagement_delta, avg(recent_scored[-4:], "engagement") - 0.46)),
        "drift": max(0.0, max(-engagement_delta, inactivity_delta - 0.08)),
    }

    # prior persisted trend state
    prev = (state.get("current", {}) or {})
    prev_family = str(prev.get("trend_family", "unknown") or "unknown")
    prev_strength = float(prev.get("trend_strength", 0.0) or 0.0)
    prev_ts = prev.get("timestamp") or ""
    prev_stacked = str(prev.get("stacked_state", "") or "")
    if prev_ts:
        try:
            age = max(0.0, now_ts() - datetime.fromisoformat(prev_ts).timestamp())
        except Exception:
            age = TREND_RESET_SECONDS + 1
    else:
        age = TREND_RESET_SECONDS + 1

    passive_decay = min(0.85, TREND_PASSIVE_DECAY_PER_SECOND * age)
    decayed_prev_strength = max(0.0, prev_strength - passive_decay)
    if age > TREND_RESET_SECONDS:
        decayed_prev_strength = 0.0
        prev_family = "unknown"
        prev_stacked = ""

    # blend persisted trend with new perception rather than replace immediately
    blended = dict(evidence)
    if prev_family in blended and decayed_prev_strength > 0.0:
        reinforcement = evidence.get(prev_family, 0.0)
        contradiction_signal = 0.0
        if prev_family == "tension":
            contradiction_signal = max(0.0, calm_delta)
        elif prev_family == "calm":
            contradiction_signal = max(0.0, tension_delta)
        elif prev_family == "engagement":
            contradiction_signal = max(0.0, -engagement_delta)
        elif prev_family == "drift":
            contradiction_signal = max(0.0, engagement_delta)

        persisted_component = decayed_prev_strength * TREND_BLEND_RATE
        reinforcement_component = reinforcement * TREND_REINFORCEMENT_RATE
        contradiction_component = contradiction_signal * TREND_CONTRADICTION_DECAY_RATE
        blended_prev = max(0.0, persisted_component + reinforcement_component - contradiction_component)
        blended[prev_family] = max(blended.get(prev_family, 0.0), blended_prev)

    trend_family = "unknown"
    trend_strength = 0.0
    trend_persisting = False
    stacked_state = ""

    # choose dominant family from blended evidence
    family_order = [(k, float(v or 0.0)) for k, v in blended.items()]
    family_order.sort(key=lambda kv: kv[1], reverse=True)
    if family_order and family_order[0][1] >= TREND_MIN_DELTA * 0.7:
        trend_family, trend_strength = family_order[0]

    if calm_delta >= TREND_MIN_DELTA and tension_delta <= -(TREND_MIN_DELTA * 0.8):
        summary = "You seem more relaxed now compared with a few minutes ago."
        tags.append("falling_stress")
        confidence = 0.76
        trend_family = "calm"
        trend_strength = max(trend_strength, max(abs(calm_delta), abs(tension_delta)))
    elif tension_delta >= TREND_MIN_DELTA:
        summary = "You seem a little more strained now than you did a few minutes ago."
        tags.append("rising_stress")
        confidence = 0.72
        trend_family = "tension"
        trend_strength = max(trend_strength, tension_delta)
    elif engagement_delta >= TREND_MIN_DELTA:
        summary = "You look more engaged now than you did a few minutes ago."
        tags.append("rising_engagement")
        confidence = 0.68
        trend_family = "engagement"
        trend_strength = max(trend_strength, engagement_delta)
    elif engagement_delta <= -TREND_MIN_DELTA:
        summary = "You seem a little less engaged with the screen than earlier."
        tags.append("attention_drift")
        confidence = 0.66
        trend_family = "drift"
        trend_strength = max(trend_strength, abs(engagement_delta))
    elif avg(recent_scored[-4:], "tense") >= 0.55 or blended.get("tension", 0.0) >= TREND_MIN_DELTA:
        summary = "Your visible expression has looked tense for a while."
        tags.append("sustained_tension")
        confidence = 0.64
        trend_family = trend_family if trend_family != "unknown" else "tension"
        trend_strength = max(trend_strength, blended.get("tension", avg(recent_scored[-4:], "tense")))
    elif avg(recent_scored[-4:], "calm") >= 0.58 or blended.get("calm", 0.0) >= TREND_MIN_DELTA:
        summary = "You've looked fairly steady and relaxed for a while."
        tags.append("sustained_calm")
        confidence = 0.62
        trend_family = trend_family if trend_family != "unknown" else "calm"
        trend_strength = max(trend_strength, blended.get("calm", avg(recent_scored[-4:], "calm")))

    # multi-factor stacked interpretation from persisted/blended trends
    if blended.get("tension", 0.0) >= TREND_STACKING_MIN_STRENGTH and inactivity_delta >= 0.10:
        stacked_state = "possible_frustration"
        tags.append("stacked_frustration")
        summary = summary or "You seem tense and more physically still than earlier."
        confidence = max(confidence, 0.74)
        trend_family = trend_family if trend_family != "unknown" else "tension"
        trend_strength = max(trend_strength, (blended.get("tension", 0.0) + inactivity_delta) / 2)
    elif blended.get("drift", 0.0) >= TREND_STACKING_MIN_STRENGTH and inactivity_delta >= 0.08:
        stacked_state = "possible_fatigue"
        tags.append("stacked_fatigue")
        summary = summary or "You seem less engaged and less physically active than earlier."
        confidence = max(confidence, 0.70)
        trend_family = trend_family if trend_family != "unknown" else "drift"
        trend_strength = max(trend_strength, (blended.get("drift", 0.0) + inactivity_delta) / 2)
    elif blended.get("calm", 0.0) >= TREND_STACKING_MIN_STRENGTH and blended.get("engagement", 0.0) >= TREND_STACKING_MIN_STRENGTH * 0.8:
        stacked_state = "possible_settled_focus"
        tags.append("stacked_focus")
        summary = summary or "You seem settled and steadily focused."
        confidence = max(confidence, 0.68)
        trend_family = trend_family if trend_family != "unknown" else "engagement"
        trend_strength = max(trend_strength, (blended.get("calm", 0.0) + blended.get("engagement", 0.0)) / 2)

    # persistence language if the trend is mostly being carried by previous evidence
    if not summary and prev_family != "unknown" and decayed_prev_strength >= TREND_MIN_DELTA * 0.7:
        trend_family = trend_family if trend_family != "unknown" else prev_family
        trend_strength = max(trend_strength, decayed_prev_strength)
        trend_persisting = True
        if prev_family == "tension":
            summary = "You still seem a bit tense overall."
            tags.append("persistent_tension")
        elif prev_family == "calm":
            summary = "You still look fairly settled overall."
            tags.append("persistent_calm")
        elif prev_family == "engagement":
            summary = "You still seem steadily engaged."
            tags.append("persistent_engagement")
        elif prev_family == "drift":
            summary = "You still seem a little less engaged than before."
            tags.append("persistent_drift")
        confidence = max(confidence, 0.58 * max(0.45, 1.0 - passive_decay))

    if trend_family == prev_family and trend_family != "unknown" and trend_strength > 0.0:
        trend_persisting = True

    # if perception conflicts with prior memory, transition slowly instead of replacing immediately
    if prev_family != "unknown" and trend_family != "unknown" and prev_family != trend_family and decayed_prev_strength > TREND_MIN_DELTA * 0.6:
        tags.append("trend_blended_transition")
        confidence = max(confidence, 0.60)
        trend_strength = max(trend_strength, decayed_prev_strength * 0.55)
        if not summary:
            if trend_family == "calm" and prev_family == "tension":
                summary = "You look calmer now, though I still have a little of that earlier tension in mind."
            elif trend_family == "tension" and prev_family == "calm":
                summary = "You look a bit more strained now than you did earlier."
            elif trend_family == "engagement" and prev_family == "drift":
                summary = "You seem to be re-engaging a bit now."
            elif trend_family == "drift" and prev_family == "engagement":
                summary = "You seem a little less engaged than you were earlier."

    confidence = max(0.0, min(1.0, round(confidence - contradiction_penalty, 4)))
    trend_strength = max(0.0, min(1.0, round(trend_strength, 4)))
    return {
        "summary": summary,
        "tags": tags,
        "confidence": confidence,
        "tension_delta": round(tension_delta, 4),
        "calm_delta": round(calm_delta, 4),
        "engagement_delta": round(engagement_delta, 4),
        "trend_strength": trend_strength,
        "trend_family": trend_family,
        "trend_persisting": trend_persisting,
        "stacked_state": stacked_state,
    }


def _camera_importance_decision(observation: dict, previous: dict | None = None) -> tuple[float, bool, str, str]:
    previous = previous or {}
    score = 0.0
    reasons = []
    skip_reasons = []
    current_person = observation.get("recognized_person_id")
    prev_person = previous.get("recognized_person_id")
    current_face = bool(observation.get("face_visible"))
    prev_face = bool(previous.get("face_visible"))
    similarity = _camera_semantic_similarity(observation, previous) if previous else 0.0
    time_gap = _camera_time_gap_seconds(previous)

    if current_face:
        score += 0.14
        reasons.append("face_visible")
    else:
        skip_reasons.append("no_face_visible")

    if current_person:
        score += 0.16
        reasons.append("known_person_visible")
    elif current_face:
        score += 0.10
        reasons.append("unknown_face_visible")

    if prev_person != current_person:
        score += 0.18
        reasons.append("person_changed")
    if prev_face != current_face:
        score += 0.10
        reasons.append("visibility_changed")

    expr_conf = float(observation.get("expression_confidence", 0.0) or 0.0)
    expr_label = str(observation.get("expression_label", "unknown") or "unknown")
    prev_expr = str(previous.get("expression_label", "unknown") or "unknown")
    if _camera_expression_change(observation, previous):
        score += 0.14
        reasons.append("expression_changed")
    if current_face and expr_label not in ["unknown", "neutral"] and expr_conf >= 0.60:
        score += 0.08
        reasons.append("expression_notable")

    novelty = max(0.0, 1.0 - similarity)
    score += novelty * 0.18
    if novelty >= 0.25:
        reasons.append("novel_view")
    if time_gap >= CAMERA_FORCE_SAVE_SECONDS:
        score += 0.08
        reasons.append("time_gap")

    goal_score, _goal_hits = _camera_goal_relevance_score()
    if goal_score > 0:
        score += goal_score
        reasons.append("goal_relevance")

    if current_person and current_person == OWNER_PERSON_ID:
        score += 0.06
        reasons.append("owner_present")

    # camera events become more important when Ava is already emotionally attentive
    behavior = load_mood().get("behavior_modifiers", {}) or {}
    score += max(0.0, float(behavior.get("depth", 0.5)) - 0.5) * 0.10
    score += max(0.0, float(behavior.get("initiative", 0.5)) - 0.5) * 0.08

    score = max(0.0, min(1.0, score))
    keep = score >= CAMERA_IMPORTANCE_EVENT_THRESHOLD and current_face
    if not current_face:
        keep = False
    if not keep and similarity >= CAMERA_MEANINGFUL_SIMILARITY_THRESHOLD and time_gap < CAMERA_FORCE_SAVE_SECONDS:
        skip_reasons.append("redundant_recent_view")
    if not keep and expr_label == prev_expr and current_person == prev_person and current_face == prev_face:
        skip_reasons.append("no_meaningful_change")
    reason_saved = ", ".join(dict.fromkeys(reasons))[:240] if keep else ""
    reason_not_saved = ", ".join(dict.fromkeys(skip_reasons or ["low_importance"]))[:240] if not keep else ""
    return score, keep, reason_saved, reason_not_saved

def recent_camera_events_text(limit: int = 8) -> str:
    state = load_camera_state()
    events = list(state.get("recent_events", []) or [])[-limit:]
    if not events:
        return "No important camera events saved yet."
    lines = []
    for item in reversed(events):
        ts = item.get("timestamp", "")
        face = "face visible" if item.get("face_visible") else "no face"
        ident = item.get("recognized_name") or "unknown"
        why = item.get("reason_saved", "") or item.get("reason_not_saved", "") or "no reason"
        lines.append(f"- {iso_to_readable(ts)} | {face} | {ident} | {why}")
    return "\n".join(lines)


def current_camera_memory_summary() -> str:
    prefix = ""
    try:
        ws_st = workspace.state
        if ws_st and ws_st.perception and not ws_st.perception.visual_truth_trusted:
            p = ws_st.perception
            prefix = (
                f"VISION: {p.vision_status} (unstable; recovery={getattr(p, 'recovery_state', '?')}, "
                f"frame_age_ms≈{p.frame_age_ms:.0f}, source={p.frame_source}, "
                f"quality≈{getattr(p, 'frame_quality', 0.0):.2f}) — not a verified current view. | "
            )
    except Exception:
        pass
    st = load_camera_state().get("current", {}) or {}
    if not st:
        return prefix + "No camera memory yet."
    ts = st.get("timestamp", "")
    ident = st.get("recognized_name") or "unknown"
    recog = st.get("recognition_text", "unknown")
    expr = st.get("expression_label", "unknown")
    keep_text = "kept" if st.get("kept_permanently") else "not kept"
    why = st.get("reason_saved") or st.get("reason_not_saved") or "no reason"
    pattern = st.get("pattern_summary", "") or st.get("comparison_summary", "")
    change_reason = st.get("change_reason", "")
    transition = st.get("transition_summary", "")
    trend = st.get("trend_summary", "")
    conf = round(float(st.get("interpretation_confidence", 0.0) or 0.0) * 100, 1)
    tail = f" | Pattern: {pattern}" if pattern else ""
    tail += f" | Transition: {transition}" if transition else ""
    tail += f" | Trend: {trend}" if trend else ""
    if st.get("stacked_state"):
        tail += f" | Stacked: {st.get('stacked_state')}"
    tail += f" | Change: {change_reason}" if change_reason else ""
    tail += f" | Visual confidence: {conf}%"
    if st.get("transition_strength") is not None:
        tail += f" | Transition strength: {round(float(st.get('transition_strength',0.0) or 0.0)*100,1)}%"
    return prefix + (
        f"Last snapshot: {iso_to_readable(ts) if ts else 'unknown'} | Face visible: {st.get('face_visible', False)} | "
        f"Recognition: {recog} | Identity: {ident} | Expression: {expr} | Style: {st.get('ava_dominant_style', 'neutral')} | "
        f"Rolling: {st.get('rolling_saved', False)} | Memory: {keep_text} | Why: {why}{tail}"
    )


def get_camera_memory_status_text() -> str:
    return current_camera_memory_summary()


def process_camera_snapshot(image, recognized_text: str = "", recognized_person_id: str | None = None, expression_state: dict | None = None) -> dict:
    state = load_camera_state()
    previous = dict(state.get("current", {}) or {})
    snapshot_id = _camera_snapshot_id()
    ts = now_iso()
    face_visible = extract_face_crop(image) is not None
    recognized_name = load_profile_by_id(recognized_person_id).get("name") if recognized_person_id else "unknown"
    expr_state = expression_state or load_expression_state()
    mood = load_mood()
    current = {
        "snapshot_id": snapshot_id,
        "timestamp": ts,
        "face_visible": bool(face_visible),
        "recognized_person_id": recognized_person_id,
        "recognized_name": recognized_name if recognized_person_id else "unknown",
        "recognition_text": recognized_text or ("No face detected" if not face_visible else "Unknown face"),
        "expression_label": expr_state.get("current_expression", "unknown"),
        "expression_confidence": float(expr_state.get("confidence", 0.0) or 0.0),
        "expression_stability": float(expr_state.get("stability", 0.0) or 0.0),
        "raw_emotion": expr_state.get("raw_emotion", "unknown"),
        "ava_mood": mood.get("current_mood", "steady"),
        "ava_primary_emotions": mood.get("primary_emotions", []),
        "ava_dominant_style": mood.get("dominant_style", "neutral"),
        "ava_behavior_modifiers": mood.get("behavior_modifiers", {}),
    }
    importance, keep_event, reason_saved, reason_not_saved = _camera_importance_decision(current, previous)
    current["importance"] = importance
    current["kept_permanently"] = keep_event
    current["reason_saved"] = reason_saved
    current["reason_not_saved"] = reason_not_saved

    rolling_entries = _rolling_snapshot_entries(limit=CAMERA_ROLLING_LIMIT)
    comparison_reference = _camera_reference_snapshot(rolling_entries)
    comparison = _camera_temporal_comparison(current, comparison_reference)
    current["comparison_summary"] = comparison.get("summary", "")
    current["comparison_tags"] = comparison.get("tags", [])
    transition = _camera_transition_summary(current, previous)
    current["transition_summary"] = transition.get("summary", "")
    current["transition_tags"] = transition.get("tags", [])
    trend = _camera_emotional_trend_summary(current, rolling_entries, state=state)
    current["trend_summary"] = trend.get("summary", "")
    current["trend_tags"] = trend.get("tags", [])
    current["trend_tension_delta"] = float(trend.get("tension_delta", 0.0) or 0.0)
    current["trend_calm_delta"] = float(trend.get("calm_delta", 0.0) or 0.0)
    current["trend_engagement_delta"] = float(trend.get("engagement_delta", 0.0) or 0.0)
    current["trend_strength"] = float(trend.get("trend_strength", 0.0) or 0.0)
    current["trend_family"] = trend.get("trend_family", "unknown") or "unknown"
    current["trend_persisting"] = bool(trend.get("trend_persisting", False))
    current["stacked_state"] = trend.get("stacked_state", "") or ""
    pattern_summary = _camera_visual_pattern_summary(current, rolling_entries)
    current["pattern_summary"] = pattern_summary.get("summary", "")
    current["pattern_tags"] = pattern_summary.get("tags", [])
    rolling_for_conf = (rolling_entries + [current])[-VISUAL_CONFIDENCE_SMOOTHING_WINDOW:]
    frame_confs = [_camera_observation_confidence(x) for x in rolling_for_conf]
    smoothed_conf = sum(frame_confs) / max(1, len(frame_confs)) if frame_confs else _camera_observation_confidence(current)
    current["interpretation_confidence"] = max(
        round(smoothed_conf, 4),
        float(transition.get("confidence", 0.0) or 0.0),
        float(trend.get("confidence", 0.0) or 0.0),
    )
    current["transition_strength"] = float(transition.get("strength", 0.0) or 0.0)
    current["action_confidence"] = max(0.0, min(1.0, round(current["interpretation_confidence"] * 0.74 + current["transition_strength"] * 0.18, 4)))

    save_to_rolling, rolling_reason, similarity = _camera_should_store_rolling(current, previous, state=state, importance=importance, keep_event=keep_event)
    current["change_reason"] = reason_saved if keep_event else rolling_reason
    current["rolling_saved"] = bool(save_to_rolling)
    current["rolling_reason"] = rolling_reason
    current["scene_similarity"] = round(float(similarity), 3)

    if image is not None:
        _write_image(CAMERA_LATEST_RAW_PATH, image)
        annotated = _draw_face_overlay(image, current)
        if annotated is not None:
            cv2.imwrite(str(CAMERA_LATEST_ANNOTATED_PATH), annotated)
        with open(CAMERA_LATEST_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, ensure_ascii=False)
        try:
            run_scene_refresh_async(
                globals(),
                str(CAMERA_LATEST_RAW_PATH),
                context=f"recognition={current.get('recognition_text','')} mood={current.get('ava_mood','')}",
                fallback_scene=str(current.get("comparison_summary") or current.get("transition_summary") or ""),
            )
        except Exception:
            pass

        if save_to_rolling:
            base = snapshot_id
            _write_image(CAMERA_ROLLING_DIR / f"{base}_raw.jpg", image)
            annotated = _draw_face_overlay(image, current)
            if annotated is not None:
                cv2.imwrite(str(CAMERA_ROLLING_DIR / f"{base}_annotated.jpg"), annotated)
            with open(CAMERA_ROLLING_DIR / f"{base}.json", "w", encoding="utf-8") as f:
                json.dump(current, f, indent=2, ensure_ascii=False)
            _prune_rolling_snapshots(limit=CAMERA_ROLLING_LIMIT)
            state["last_saved_snapshot_ts"] = ts

    if keep_event and image is not None:
        _write_image(CAMERA_EVENTS_DIR / f"{snapshot_id}_raw.jpg", image)
        annotated = _draw_face_overlay(image, current)
        if annotated is not None:
            cv2.imwrite(str(CAMERA_EVENTS_DIR / f"{snapshot_id}_annotated.jpg"), annotated)
        with open(CAMERA_EVENTS_DIR / f"{snapshot_id}.json", "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, ensure_ascii=False)

        person_for_memory = recognized_person_id or get_active_person_id()
        summary = (
            f"Camera observation at {ts}: face_visible={current['face_visible']}, recognition={current['recognition_text']}, "
            f"identity={current['recognized_name']}, expression={current['expression_label']}, ava_mood={current['ava_mood']}, "
            f"ava_style={current['ava_dominant_style']}, reason_saved={reason_saved}, transition={current.get('transition_summary','')}, trend={current.get('trend_summary','')}."
        )
        remember_memory(summary, person_id=person_for_memory, category="camera_event", importance=importance, source="camera_perception", tags=["camera", "snapshot", "vision", "visual_memory"])

        event_summary = {
            "timestamp": ts,
            "snapshot_id": snapshot_id,
            "face_visible": current["face_visible"],
            "recognized_person_id": current["recognized_person_id"],
            "recognized_name": current["recognized_name"],
            "recognition_text": current["recognition_text"],
            "expression_label": current["expression_label"],
            "importance": importance,
            "reason_saved": reason_saved,
            "comparison_summary": current.get("comparison_summary", ""),
            "image_path": str(CAMERA_EVENTS_DIR / f"{snapshot_id}_annotated.jpg")
        }
        recent = list(state.get("recent_events", []) or [])
        recent.append(event_summary)
        state["recent_events"] = recent[-24:]
        state["last_meaningful_snapshot_ts"] = ts
        try:
            event_jsons = sorted(CAMERA_EVENTS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime)
            excess = max(0, len(event_jsons) - CAMERA_EVENT_RETENTION_LIMIT)
            for meta_path in event_jsons[:excess]:
                stem = meta_path.stem
                for suffix in [".json", "_raw.jpg", "_annotated.jpg"]:
                    target = CAMERA_EVENTS_DIR / f"{stem}{suffix}" if suffix == ".json" else CAMERA_EVENTS_DIR / f"{stem}{suffix}"
                    if target.exists():
                        try:
                            target.unlink()
                        except Exception:
                            pass
        except Exception:
            pass

        if (current.get("comparison_summary") or current.get("transition_summary") or current.get("trend_summary") or current.get("pattern_summary")) and importance >= 0.72:
            reflection_text = trim_for_prompt((current.get("trend_summary") or current.get("transition_summary") or current.get("comparison_summary") or current.get("pattern_summary") or ""), limit=220)
            if reflection_text:
                reflect_on_last_reply(
                    user_input=f"[visual observation] {reflection_text}",
                    ai_reply=f"I noticed visually: {reflection_text}",
                    person_id=person_for_memory,
                    actions=["camera_reflection_trigger"]
                )

    state["current"] = current
    state["last_person_id"] = current.get("recognized_person_id")
    state["last_face_visible"] = current.get("face_visible")
    state["last_snapshot_ts"] = ts
    save_camera_state(state)
    return current

def expression_prompt_text(state: dict | None = None, perception=None) -> str:
    state = state or load_expression_state()
    if perception is not None and not getattr(perception, "visual_truth_trusted", True):
        vs = getattr(perception, "vision_status", "unknown")
        return (
            f"Visual confidence is low (vision state: {vs}). "
            "Do not describe facial expression or the scene as a verified current read. "
            "If asked what you see, say you are waiting for a stable camera frame or that vision is recovering."
        )
    if not state.get("visible_face"):
        return "No face is currently visible, so there is no usable expression signal."
    current = state.get("current_expression", "unknown")
    raw = state.get("raw_emotion", "unknown")
    conf = round(float(state.get("confidence", 0.0)) * 100, 1)
    stab = round(float(state.get("stability", 0.0)) * 100, 1)
    note = state.get("note", "")
    return f"Observed facial expression suggests {current} (raw emotion: {raw}, confidence {conf}%, stability {stab}%). Treat this only as soft context, not certain knowledge. Status: {note}."

# =========================================================
# AUTONOMOUS INITIATIVE
# =========================================================
# default/load/save_initiative_state, _SESSION_STATE_MIGRATED, load/save_session_state, bump_session_message_count -> brain/session.py
def _session_state_atexit():
    try:
        st = load_session_state()
        st["last_session_end_at"] = now_iso()
        save_session_state(st)
    except Exception:
        pass
    # Phase 65: save mood carryover on shutdown
    try:
        _mood_now = load_mood()
        _carryover = {
            "mood": _mood_now,
            "shutdown_ts": time.time(),
            "shutdown_iso": now_iso(),
        }
        _co_path = STATE_DIR / "mood_carryover.json"
        _co_path.write_text(
            json.dumps(_carryover, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _write_ava_pid_file():
    try:
        AVA_PID_PATH.parent.mkdir(parents=True, exist_ok=True)
        AVA_PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    except Exception as e:
        print(f"[pid] write failed: {e}")


def _delete_ava_pid_file():
    try:
        if AVA_PID_PATH.is_file():
            AVA_PID_PATH.unlink()
    except Exception:
        pass


def _run_narrative_update_sync():
    try:
        sess = load_session_state()
        count = int(sess.get("total_message_count", 0))
        if count >= 5:
            recent = load_recent_chat(limit=20)
            summary = " ".join((row.get("content", "") or "")[:80] for row in recent[-10:])
            mood = load_mood()
            face_emotion = load_expression_state().get("raw_emotion", "neutral")
            update_self_narrative(globals(), summary, mood, face_emotion)
    except Exception as e:
        print(f"[narrative-update] failed: {e}")


def _maybe_update_narrative():
    _run_narrative_update_sync()


def _trigger_narrative_update_async():
    def _worker():
        _run_narrative_update_sync()

    threading.Thread(target=_worker, daemon=True).start()


atexit.register(_session_state_atexit)
atexit.register(_maybe_update_narrative)
atexit.register(_delete_ava_pid_file)


def get_life_rhythm_prompt_block(person_id: str) -> str:
    try:
        from brain.life_rhythm import get_prompt_block_for_person

        return get_prompt_block_for_person(person_id, OWNER_PERSON_ID, globals())
    except Exception:
        return ""


def _run_life_rhythm_job() -> None:
    """Phase 5: scan ~30d reflections + memory; refresh state/life_model.json (owner only)."""
    try:
        from brain import life_rhythm

        pid = OWNER_PERSON_ID
        prof = load_profile_by_id(pid)
        cad = prof.get("cadence") or {}
        sessions = int(cad.get("total_sessions", 0) or 0)
        prev = life_rhythm.load_life_model(globals())
        if not life_rhythm.should_run_analysis(sessions, prev):
            return
        all_refl = load_recent_reflections(limit=80000, person_id=pid)
        refl_f = life_rhythm.filter_reflections_by_age(all_refl, days=life_rhythm.DEFAULT_WINDOW_DAYS)
        all_mem = list_recent_memories(person_id=pid, limit=2000)
        mem_f = life_rhythm.filter_memories_by_age(all_mem, days=life_rhythm.DEFAULT_WINDOW_DAYS)
        if len(refl_f) + len(mem_f) < 8:
            return
        rc = life_rhythm.reflections_to_corpus(refl_f, 6000)
        mc = life_rhythm.memories_to_corpus(mem_f, 6000)
        llm_out = life_rhythm.run_life_rhythm_llm(
            call_llm,
            str(prof.get("name") or pid),
            rc,
            mc,
            sessions,
            life_rhythm.DEFAULT_WINDOW_DAYS,
        )
        if not llm_out:
            return
        doc = life_rhythm.merge_output_doc(
            pid,
            str(prof.get("name") or pid),
            sessions,
            life_rhythm.DEFAULT_WINDOW_DAYS,
            refl_f,
            mem_f,
            llm_out,
        )
        life_rhythm.save_life_model(doc, globals())
        print(
            f"[life_rhythm] updated (reflections={doc['reflections_used']}, memories={doc['memories_used']})"
        )
    except Exception as e:
        print(f"[life_rhythm] job failed: {e}")


def _schedule_life_rhythm_on_startup() -> None:
    def _worker():
        _run_life_rhythm_job()

    threading.Thread(target=_worker, daemon=True).start()


def refresh_self_model_pending_threads(person_id: str) -> None:
    """Ava-facing open threads (from relationship threads + light initiative introspection)."""
    try:
        from brain.relationship_threads import unresolved_threads

        prof = load_profile_by_id(person_id)
        name = str(prof.get("name") or "They").strip() or "They"
        lines: list[str] = []
        for th in unresolved_threads(prof)[:8]:
            em = str(th.get("emotion") or "tense").strip()
            topic = trim_for_prompt(str(th.get("topic") or "something they mentioned"), limit=100)
            lines.append(f"{name} seemed {em} about {topic} — I want to follow up.")
        try:
            ist = load_initiative_state()
            if int(ist.get("consecutive_ignored_initiations", 0) or 0) >= 2:
                lines.append(
                    "I've been initiating a lot lately — I should check if that pace still feels welcome."
                )
        except Exception:
            pass
        model = load_self_model()
        model["pending_threads"] = lines[-12:]
        model["last_updated"] = now_iso()
        save_self_model(model)
    except Exception as e:
        print(f"[pending_threads] refresh failed: {e}")


def update_presence_continuity(face_visible: bool, presence_state: dict) -> dict:
    now_str = now_iso()
    was_visible = bool(presence_state.get("face_visible", False))

    if was_visible and not face_visible:
        presence_state["face_left_at"] = now_str
        presence_state["was_absent"] = False

    elif not was_visible and face_visible:
        presence_state["face_returned_at"] = now_str
        left_at = presence_state.get("face_left_at")
        if left_at:
            try:
                gone = (datetime.fromisoformat(now_str) - datetime.fromisoformat(left_at)).total_seconds()
                presence_state["was_absent"] = gone > 30
                presence_state["absent_duration_seconds"] = round(gone)
            except Exception:
                presence_state["was_absent"] = False
        else:
            presence_state["absent_duration_seconds"] = 0
        presence_state["face_left_at"] = None

    presence_state["face_visible"] = face_visible
    return presence_state


def _format_absent_duration(seconds: int) -> str:
    s = max(0, int(seconds))
    if s < 90:
        return f"{s} seconds"
    if s < 3600:
        m = max(1, round(s / 60))
        return f"{m} minutes" if m != 1 else "about a minute"
    h = s // 3600
    rem_m = (s % 3600) // 60
    if h >= 1 and rem_m >= 5:
        return f"{h} hours and {rem_m} minutes"
    if h >= 1:
        return f"{h} hours" if h != 1 else "about an hour"
    return f"{max(1, round(s / 60))} minutes"


def maybe_self_calibration_candidate(person_id: str) -> dict | None:
    _ = person_id
    state = load_initiative_state()
    sess = load_session_state()
    total = int(sess.get("total_message_count", 0))
    last_cal = int(state.get("last_calibration_at_msg", 0))
    if total >= 20 and (total - last_cal) >= 50:
        return {
            "kind": "self_calibration_check",
            "text": random.choice(SELF_CALIBRATION_PROMPTS),
            "base_score": 0.72,
            "score": 0.72,
            "memory_importance": 0.70,
            "topic_key": "self_calibration",
            "source": "self_calibration",
            "action_confidence": 1.0,
            "interpretation_confidence": 1.0,
        }
    return None


def initiative_status_text(state: dict | None = None) -> str:
    state = state or load_initiative_state()
    pending = state.get("pending_initiation") or {}
    if pending and pending.get("text"):
        return f"Last autonomous message: {trim_for_prompt(pending.get('text',''), limit=110)}"
    if state.get("last_initiation_message"):
        return f"Last autonomous message: {trim_for_prompt(state.get('last_initiation_message',''), limit=110)}"
    return "No autonomous message yet."

def _decayed_recent_score(age_seconds: float, window_seconds: float) -> float:
    if age_seconds <= 0:
        return 1.0
    if age_seconds >= window_seconds:
        return 0.0
    return max(0.0, 1.0 - (age_seconds / window_seconds))

def _topic_key(text: str) -> str:
    tokens = [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 2]
    return "_".join(tokens[:8]) or slugify_name(text[:32])

_EMBED_SIM_CACHE: dict[str, list[float]] = {}
_EMBED_SIM_READY = None

def _token_jaccard(a: str, b: str) -> float:
    ta = _token_set(a)
    tb = _token_set(b)
    if not ta or not tb:
        return 0.0
    overlap = len(ta.intersection(tb))
    union = len(ta.union(tb))
    return overlap / max(1, union)


def _cosine_similarity(vec_a, vec_b) -> float:
    try:
        dot = sum(float(x) * float(y) for x, y in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(float(x) * float(x) for x in vec_a))
        norm_b = math.sqrt(sum(float(y) * float(y) for y in vec_b))
        if norm_a <= 0 or norm_b <= 0:
            return 0.0
        return max(-1.0, min(1.0, dot / (norm_a * norm_b)))
    except Exception:
        return 0.0


def _get_similarity_embedder():
    global _EMBED_SIM_READY
    if _EMBED_SIM_READY is False:
        return None
    try:
        if _EMBED_SIM_READY is None:
            _EMBED_SIM_READY = OllamaEmbeddings(model=EMBED_MODEL)
        return _EMBED_SIM_READY
    except Exception:
        _EMBED_SIM_READY = False
        return None


def _embed_text_cached(text: str):
    key = trim_for_prompt(text or '', limit=300)
    if not key:
        return None
    cached = _EMBED_SIM_CACHE.get(key)
    if cached is not None:
        return cached
    embedder = _get_similarity_embedder()
    if embedder is None:
        return None
    try:
        vec = embedder.embed_query(key)
        if len(_EMBED_SIM_CACHE) >= 256:
            try:
                _EMBED_SIM_CACHE.pop(next(iter(_EMBED_SIM_CACHE)))
            except Exception:
                _EMBED_SIM_CACHE.clear()
        _EMBED_SIM_CACHE[key] = vec
        return vec
    except Exception:
        return None


def _semantic_similarity(a: str, b: str) -> float:
    lexical = _token_jaccard(a, b)
    vec_a = _embed_text_cached(a)
    vec_b = _embed_text_cached(b)
    if vec_a is not None and vec_b is not None:
        emb = (_cosine_similarity(vec_a, vec_b) + 1.0) / 2.0
        return max(0.0, min(1.0, round(0.75 * emb + 0.25 * lexical, 4)))
    return lexical

def _kind_cooldown_seconds(kind: str) -> int:
    return int(INITIATIVE_KIND_COOLDOWNS.get(kind, INITIATIVE_TOPIC_COOLDOWN_SECONDS))

def expire_stale_pending_initiation(state: dict | None = None) -> dict:
    state = state or load_initiative_state()
    pending = state.get("pending_initiation") or {}
    if not pending or not pending.get("ts"):
        return state
    now = now_ts()
    age = now - float(pending.get("ts", 0.0) or 0.0)
    if age >= INITIATIVE_PENDING_RESPONSE_WINDOW_SECONDS and not pending.get("responded"):
        state["consecutive_ignored_initiations"] = int(state.get("consecutive_ignored_initiations", 0) or 0) + 1
        energy = float(state.get("interaction_energy", 0.58) or 0.58)
        state["interaction_energy"] = round(max(0.12, energy - 0.12), 3)
        history = state.get("initiative_history", []) or []
        history.append({
            "timestamp": now_iso(),
            "topic_key": pending.get("topic_key", ""),
            "text": pending.get("text", ""),
            "candidate_kind": pending.get("candidate_kind", "thought"),
            "responded": False,
            "expired_without_response": True
        })
        state["initiative_history"] = history[-60:]
        state["pending_initiation"] = None
        save_initiative_state(state)
    return state

def update_presence_state(face_visible: bool, recognized_person_id: str | None = None, interaction_happened: bool = False) -> dict:
    state = load_initiative_state()
    state = update_presence_continuity(face_visible, state)
    now = now_ts()
    if face_visible:
        state["last_face_seen_ts"] = now
    if interaction_happened:
        state["last_interaction_ts"] = now
    face_score = _decayed_recent_score(now - float(state.get("last_face_seen_ts", 0.0)), PRESENCE_FACE_WINDOW_SECONDS)
    interaction_score = _decayed_recent_score(now - float(state.get("last_interaction_ts", 0.0)), PRESENCE_INTERACTION_WINDOW_SECONDS)
    presence_score = min(1.0, (face_score * 0.65) + (interaction_score * 0.5))
    state["presence_score"] = round(presence_score, 4)
    if face_score > 0.2 and interaction_score > 0.15:
        state["last_presence_reason"] = "face_and_recent_interaction"
    elif face_score > 0.2:
        state["last_presence_reason"] = "face_seen_recently"
    elif interaction_score > 0.15:
        state["last_presence_reason"] = "recent_interaction"
    else:
        state["last_presence_reason"] = "not_present"
    if recognized_person_id:
        state["last_seen_person_id"] = recognized_person_id
    save_initiative_state(state)
    return state

def note_user_interaction_for_initiative(user_text: str = "", interaction_kind: str = "text") -> dict:
    state = expire_stale_pending_initiation(update_presence_state(False, interaction_happened=True))
    now = now_ts()
    preview = trim_for_prompt(user_text, limit=120)
    state["last_user_message_length"] = len((user_text or "").strip())
    state["last_user_response_brief"] = bool((user_text or "").strip()) and len((user_text or "").strip()) <= 16
    pending = state.get("pending_initiation")
    if pending and pending.get("ts") and (now - float(pending.get("ts", 0.0))) <= INITIATIVE_PENDING_RESPONSE_WINDOW_SECONDS:
        pending["responded"] = True
        pending["response_kind"] = interaction_kind
        pending["response_ts"] = now
        pending["response_preview"] = preview
        history = state.get("initiative_history", [])
        history.append({
            "timestamp": now_iso(),
            "topic_key": pending.get("topic_key", ""),
            "text": pending.get("text", ""),
            "candidate_kind": pending.get("candidate_kind", "thought"),
            "responded": True,
            "response_kind": interaction_kind
        })
        state["initiative_history"] = history[-60:]
        state["pending_initiation"] = None
        state["consecutive_ignored_initiations"] = 0
        energy = float(state.get("interaction_energy", 0.58) or 0.58)
        state["interaction_energy"] = round(min(0.95, energy + 0.08), 3)
    else:
        energy = float(state.get("interaction_energy", 0.58) or 0.58)
        state["interaction_energy"] = round(min(0.95, energy + 0.02), 3)
    save_initiative_state(state)
    return state

def estimate_user_busy_score(state: dict | None = None, expression_state: dict | None = None) -> float:
    state = expire_stale_pending_initiation(state or load_initiative_state())
    now = now_ts()
    quiet_for = now - float(state.get("last_interaction_ts", 0.0) or 0.0)
    score = 0.0
    if quiet_for < 45:
        score += 0.72
    elif quiet_for < 90:
        score += 0.60
    elif quiet_for < INITIATIVE_INACTIVITY_SECONDS:
        score += 0.35
    pending = state.get("pending_initiation") or {}
    if pending and pending.get("ts") and (now - float(pending.get("ts", 0.0))) < INITIATIVE_PENDING_RESPONSE_WINDOW_SECONDS:
        score = max(score, 0.82)
    if bool(state.get("last_user_response_brief", False)) and quiet_for < 240:
        score += 0.10
    ignored = int(state.get("consecutive_ignored_initiations", 0) or 0)
    if ignored >= 2:
        score += min(0.18, 0.06 * ignored)
    expr = expression_state or load_expression_state()
    current_expr = (expr.get("current_expression") or "").lower()
    if any(x in current_expr for x in ["frustration", "stress", "confusion"]):
        score += 0.18
    return min(1.0, round(score, 3))


def _camera_visual_candidates(person_id: str) -> list[dict]:
    candidates = []
    camera_state = load_camera_state()
    current = camera_state.get("current", {}) or {}
    rolling = _rolling_snapshot_entries(limit=CAMERA_ROLLING_LIMIT)
    if not current:
        return candidates
    visual_conf = float(current.get("interpretation_confidence", 0.0) or 0.0)
    transition_strength = float(current.get("transition_strength", 0.0) or 0.0)
    initiative_state = load_initiative_state()
    recent_texts = initiative_state.get("recent_initiated_texts", []) or []
    now = now_ts()

    def recently_said_similar(raw: str, family: str = "") -> bool:
        raw = trim_for_prompt(raw, limit=180)
        if not raw:
            return True
        for row in recent_texts[-SEMANTIC_TOPIC_RECENT_LIMIT:]:
            age = max(0.0, now - float(row.get("ts", 0.0) or 0.0))
            if age > VISUAL_REPETITION_SUPPRESSION_SECONDS:
                continue
            sim = _semantic_similarity(raw, row.get("text", ""))
            if sim >= VISUAL_REPETITION_SIMILARITY_THRESHOLD:
                return True
            if family and row.get("kind") in ["visual_observation", "transition_observation", "engagement_observation", "attention_drift", "visual_pattern", "visual_checkin"]:
                low = row.get("text", "").lower()
                if family in low:
                    return True
        return False

    def add_candidate(kind: str, txt: str, base: float, mem_imp: float | None = None, extra_conf: float | None = None, action_boost: float = 0.0, family: str = ""):
        raw = trim_for_prompt(txt, limit=180)
        if not raw or recently_said_similar(raw, family=family):
            return
        interp_conf = max(visual_conf, float(extra_conf or 0.0))
        cleaned = _camera_apply_confidence_language(raw, interp_conf, kind=kind)
        action_conf = max(0.0, min(1.0, round(interp_conf * 0.72 + transition_strength * 0.18 + action_boost, 4)))
        candidates.append({
            "kind": kind,
            "text": cleaned,
            "topic_key": _topic_key(raw),
            "base_score": base,
            "memory_importance": max(0.5, float(mem_imp if mem_imp is not None else current.get("importance", 0.5))),
            "interpretation_confidence": interp_conf,
            "action_confidence": action_conf,
        })

    trend_family = str(current.get("trend_family", "unknown") or "unknown")
    if current.get("transition_summary"):
        add_candidate("transition_observation", current.get("transition_summary", ""), min(0.9, 0.64 + float(current.get("importance", 0.5)) * 0.2), extra_conf=max(0.66, transition_strength), action_boost=0.06 if transition_strength >= VISUAL_TRANSITION_INITIATION_THRESHOLD else -0.03, family="transition")
    if current.get("trend_summary"):
        add_candidate("visual_observation", current.get("trend_summary", ""), min(0.9, 0.68 + float(current.get("importance", 0.5)) * 0.18), extra_conf=max(0.66, float(current.get("interpretation_confidence", 0.0) or 0.0)), action_boost=0.04, family=trend_family)
    if current.get("comparison_summary"):
        add_candidate("visual_observation", current.get("comparison_summary", ""), min(0.86, 0.64 + float(current.get("importance", 0.5)) * 0.18), extra_conf=0.62, family="comparison")
    if current.get("pattern_summary"):
        add_candidate("visual_pattern", current.get("pattern_summary", ""), min(0.84, 0.62 + float(current.get("importance", 0.5)) * 0.16), extra_conf=0.6, family="pattern")

    if len(rolling) >= 4:
        same_person = [r for r in rolling if r.get("recognized_person_id") == person_id and r.get("face_visible")]
        if len(same_person) >= 4:
            try:
                first_ts = datetime.fromisoformat(same_person[0].get("timestamp")).timestamp()
                last_ts = datetime.fromisoformat(same_person[-1].get("timestamp")).timestamp()
                duration = max(0.0, last_ts - first_ts)
            except Exception:
                duration = 0.0
            if duration >= CAMERA_VISUAL_PATTERN_MIN_DURATION_SECONDS:
                add_candidate("engagement_observation", "You've been focused on that for a while — how's it going?", 0.74, 0.72, 0.66, action_boost=0.05, family="focus")

    if current.get("face_visible") and current.get("recognized_person_id") == person_id and current.get("expression_label") in ["frustration", "stress", "confusion"] and float(current.get("expression_confidence", 0.0) or 0.0) >= 0.55:
        add_candidate("visual_checkin", "You seem a little strained right now — want to stay with this, or shift gears for a second?", 0.78, max(0.70, float(current.get("importance", 0.5))), 0.72, action_boost=0.06, family="tension")

    if current.get("face_visible") and not current.get("recognized_person_id") and visual_conf >= 0.5:
        add_candidate("uncertainty_observation", "I can see someone clearly, but I'm still not confident about who it is yet.", 0.66, 0.62, 0.58, family="uncertainty")

    if current.get("face_visible") and current.get("recognized_person_id") == person_id and current.get("rolling_reason") == "redundant_recent_view":
        add_candidate("attention_drift", "You haven't moved much for a while — are you deep in something, or just paused?", 0.68, 0.6, 0.56, family="stillness")

    return candidates


def collect_curiosity_candidates(person_id: str) -> list[dict]:
    """Pull unanswered curiosity questions from the self-model for proactive initiative."""
    model = load_self_model()
    questions = model.get("curiosity_questions", []) or []
    candidates: list[dict] = []
    recent = " ".join(r.get("content", "") for r in load_recent_chat(person_id=person_id)[-10:]).lower()
    for q in questions[-5:]:
        q_text = str(q).strip()
        if not q_text:
            continue
        key_words = q_text.lower().split()[:3]
        if any(len(w) > 4 and w in recent for w in key_words):
            continue
        topic_key = f"curiosity_{abs(hash(q_text)) % 10000}"
        candidates.append(
            {
                "kind": "genuine_curiosity",
                "text": q_text,
                "base_score": 0.55,
                "memory_importance": 0.52,
                "topic_key": topic_key,
                "source": "self_model_curiosity",
                "action_confidence": 1.0,
                "interpretation_confidence": 1.0,
            }
        )
    return candidates


def collect_initiative_candidates(person_id: str) -> list[dict]:
    model = load_self_model()
    reflections = load_recent_reflections(limit=18, person_id=person_id)
    memories = list_recent_memories(person_id, limit=18)
    recent_chat = load_recent_chat(limit=18, person_id=person_id)
    initiative_state = load_initiative_state()
    candidates = []
    seen = set()

    structured_goals = top_active_goals(limit=16, context_text=" ".join(r.get("content", "") for r in recent_chat[-6:]), mood=load_mood())
    for g in structured_goals:
        cleaned = trim_for_prompt(g.get("text", ""), limit=180)
        if not cleaned or cleaned.lower() in seen:
            continue
        seen.add(cleaned.lower())
        kind = "curiosity_question" if g.get("kind") == "question" else "current_goal"
        base = 0.60 + 0.34 * float(g.get("current_priority", 0.5))
        mem = 0.46 + 0.44 * float(g.get("importance", 0.5))
        candidates.append({
            "kind": kind,
            "text": cleaned,
            "topic_key": _topic_key(cleaned),
            "base_score": min(0.97, base),
            "memory_importance": min(0.97, mem),
            "goal_id": g.get("goal_id", ""),
            "goal_horizon": g.get("horizon", "short_term"),
            "goal_priority": float(g.get("current_priority", 0.5)),
        })

    for row in reversed(reflections):
        cleaned = trim_for_prompt(row.get("summary", ""), limit=180)
        if not cleaned or cleaned.lower() in seen:
            continue
        seen.add(cleaned.lower())
        refl_importance = max(0.58, min(0.97, float(row.get("importance", 0.62))))
        candidates.append({
            "kind": "recent_reflection",
            "text": cleaned,
            "topic_key": _topic_key(cleaned),
            "base_score": refl_importance,
            "memory_importance": refl_importance,
            "source_reflection_id": row.get("reflection_id", "")
        })

    for row in memories[:10]:
        meta = row.get("metadata", {}) or {}
        raw_text = (meta.get("raw_text") or row.get("text", "") or "").strip()
        cleaned = trim_for_prompt(raw_text, limit=180)
        if not cleaned or cleaned.lower() in seen:
            continue
        importance = float(meta.get("importance_pct", meta.get("importance_score", 0.0)) or 0.0)
        if importance < 0.74:
            continue
        seen.add(cleaned.lower())
        candidates.append({
            "kind": "salient_memory",
            "text": cleaned,
            "topic_key": _topic_key(cleaned),
            "base_score": min(0.96, max(0.74, importance)),
            "memory_importance": importance,
            "source_memory_id": row.get("memory_id", "")
        })

    token_counts = {}
    for row in recent_chat[-12:]:
        for tok in _token_set(row.get("content", "")):
            if len(tok) >= 4:
                token_counts[tok] = token_counts.get(tok, 0) + 1
    repeated = [tok for tok, count in sorted(token_counts.items(), key=lambda x: x[1], reverse=True) if count >= 3][:3]
    if repeated:
        theme_text = f"We keep circling back to {', '.join(repeated)} lately. Should we stay with that, or shift direction a little?"
        if theme_text.lower() not in seen:
            candidates.append({
                "kind": "pattern_checkin",
                "text": theme_text,
                "topic_key": _topic_key(theme_text),
                "base_score": 0.73,
                "memory_importance": 0.70
            })

    try:
        from brain.prospective import get_due_events

        due_events = get_due_events(person_id, now=datetime.now(), host=globals())
        for event in due_events[:2]:
            tmpl = (
                event.get("prompt_template")
                or "Hey, didn't you say {event_text} was {due_description}? How did it go?"
            )
            try:
                text = tmpl.format(
                    event_text=event.get("event_text") or "",
                    due_description=event.get("due_description") or "",
                )
            except Exception:
                text = str(tmpl)
            tk = (text or "").strip().lower()
            if tk and tk not in seen:
                seen.add(tk)
                candidates.append({
                    "kind": "prospective_followup",
                    "text": text,
                    "topic_key": f"prospective_{event.get('id', '')}",
                    "base_score": 0.88,
                    "memory_importance": 0.82,
                    "event_id": event.get("id"),
                    "person_id": person_id,
                    "action_confidence": 1.0,
                    "interpretation_confidence": 1.0,
                })
    except Exception as e:
        print(f"[prospective] candidates: {e}")

    prof = load_profile_by_id(person_id)
    just_returned = bool(initiative_state.get("was_absent"))
    if just_returned:
        try:
            from brain.relationship_threads import unresolved_threads

            for th in unresolved_threads(prof)[:1]:
                em = str(th.get("emotion") or "tense").strip()
                topic = trim_for_prompt(str(th.get("topic") or "something you mentioned"), limit=90)
                ttext = (
                    f"You seemed {em} when we talked about {topic} — did things settle?"
                )
                tk = ttext.strip().lower()
                if tk and tk not in seen:
                    seen.add(tk)
                    candidates.append({
                        "kind": "thread_followup",
                        "text": ttext,
                        "topic_key": f"thread_{th.get('id', '')}",
                        "base_score": 0.86,
                        "memory_importance": 0.84,
                        "thread_id": th.get("id"),
                        "person_id": person_id,
                        "action_confidence": 1.0,
                        "interpretation_confidence": 1.0,
                    })
        except Exception as e:
            print(f"[threads] candidates: {e}")

        try:
            from brain.person_cadence import should_offer_long_absence_checkin

            if should_offer_long_absence_checkin(prof.get("cadence")):
                ctext = "Hey — it's been a little while. Everything okay?"
                ctk = ctext.lower()
                if ctk not in seen:
                    seen.add(ctk)
                    candidates.append({
                        "kind": "cadence_checkin",
                        "text": ctext,
                        "topic_key": "cadence_long_absence",
                        "base_score": 0.80,
                        "memory_importance": 0.78,
                        "person_id": person_id,
                        "action_confidence": 1.0,
                        "interpretation_confidence": 1.0,
                    })
        except Exception as e:
            print(f"[cadence] candidates: {e}")

    for cand in _camera_visual_candidates(person_id):
        if cand.get("text", "").lower() in seen:
            continue
        seen.add(cand.get("text", "").lower())
        candidates.append(cand)

    camera_state = load_camera_state()
    current = camera_state.get("current", {}) or {}
    trans = str(current.get("transition_summary", "") or "").lower()
    if trans and "came back" in trans:
        name = load_profile_by_id(person_id).get("name", "you")
        nm = (name or "").strip()
        if nm and nm.lower() not in ("you", "unknown", "user"):
            txt = f"Hey {nm}, you're back. How's it going?"
        else:
            txt = "Hey, you're back. How's it going?"
        tk = txt.lower()
        if tk not in seen:
            seen.add(tk)
            candidates.append({
                "kind": "return_greeting",
                "text": txt,
                "topic_key": _topic_key(txt),
                "base_score": 0.82,
                "memory_importance": 0.65,
                "action_confidence": 1.0,
                "interpretation_confidence": 1.0,
            })

    if int(initiative_state.get("consecutive_ignored_initiations", 0) or 0) >= 2:
        feedback_text = "I can ease off a bit if you want — should I keep bringing things up on my own, or would you rather lead for a while?"
        if feedback_text.lower() not in seen:
            candidates.append({
                "kind": "feedback_guidance",
                "text": feedback_text,
                "topic_key": _topic_key(feedback_text),
                "base_score": 0.84,
                "memory_importance": 0.82
            })

    for c in collect_curiosity_candidates(person_id):
        tkey = (c.get("text") or "").strip().lower()
        if not tkey or tkey in seen:
            continue
        seen.add(tkey)
        candidates.append(c)

    cal = maybe_self_calibration_candidate(person_id)
    if cal:
        tkey = (cal.get("text") or "").strip().lower()
        if tkey and tkey not in seen:
            seen.add(tkey)
            candidates.append(cal)

    if random.random() < 0.15:
        prompt = random.choice(SELF_CALIBRATION_PROMPTS)
        tkey = (prompt or "").strip().lower()
        if tkey and tkey not in seen:
            seen.add(tkey)
            candidates.append({
                "kind": "self_calibration_check",
                "text": prompt,
                "topic_key": _topic_key(prompt),
                "base_score": 0.70,
                "memory_importance": 0.68,
                "action_confidence": 1.0,
                "interpretation_confidence": 1.0,
            })

    return desaturate_candidate_scores(candidates)

def _goal_kind_preferences(goal_name: str) -> dict:
    prefs = {
        "reduce_stress": {"preferred_kinds": {"visual_checkin", "feedback_guidance", "transition_observation", "visual_observation"}, "avoid_kinds": {"curiosity_question", "genuine_curiosity"}, "keyword_bias": ["relaxed", "strained", "stress", "calm", "ease", "pause"]},
        "increase_engagement": {"preferred_kinds": {"curiosity_question", "genuine_curiosity", "engagement_observation", "attention_drift", "pattern_checkin", "prospective_followup", "thread_followup"}, "avoid_kinds": {"visual_checkin"}, "keyword_bias": ["curious", "shift", "going", "thought", "interesting"]},
        "explore_topic": {"preferred_kinds": {"curiosity_question", "genuine_curiosity", "current_goal", "recent_reflection", "salient_memory", "prospective_followup"}, "avoid_kinds": set(), "keyword_bias": ["explore", "wonder", "thinking", "earlier", "build", "version"]},
        "clarify": {"preferred_kinds": {"current_goal", "feedback_guidance", "uncertainty_observation", "recent_reflection"}, "avoid_kinds": {"attention_drift"}, "keyword_bias": ["clarify", "understand", "unsure", "not confident", "figure out"]},
        "maintain_connection": {"preferred_kinds": {"pattern_checkin", "curiosity_question", "genuine_curiosity", "visual_observation", "return_greeting", "self_calibration_check", "prospective_followup", "thread_followup", "cadence_checkin"}, "avoid_kinds": set(), "keyword_bias": ["how's it going", "earlier", "thinking", "together"]},
        "observe_silently": {"preferred_kinds": set(), "avoid_kinds": {"visual_checkin", "feedback_guidance", "curiosity_question", "genuine_curiosity", "pattern_checkin", "engagement_observation", "attention_drift", "visual_observation", "transition_observation"}, "keyword_bias": []},
        "wait_for_user": {"preferred_kinds": set(), "avoid_kinds": {"visual_checkin", "feedback_guidance", "curiosity_question", "genuine_curiosity", "pattern_checkin", "engagement_observation", "attention_drift", "visual_observation", "transition_observation"}, "keyword_bias": []},
    }
    return prefs.get(goal_name or "", {"preferred_kinds": set(), "avoid_kinds": set(), "keyword_bias": []})

def _candidate_goal_alignment_score(candidate: dict, active_goal_name: str, goal_blend_names: list[str] | None = None) -> float:
    goal_blend_names = goal_blend_names or []
    score = 0.0
    text_l = (candidate.get("text") or "").lower()
    kind = candidate.get("kind", "thought")
    for idx, goal_name in enumerate([active_goal_name] + [g for g in goal_blend_names if g and g != active_goal_name]):
        if not goal_name:
            continue
        prefs = _goal_kind_preferences(goal_name)
        weight = 1.0 if idx == 0 else 0.45
        if kind in prefs["preferred_kinds"]:
            score += 0.18 * weight
        if kind in prefs["avoid_kinds"]:
            score -= 0.28 * weight
        if any(kw in text_l for kw in prefs["keyword_bias"]):
            score += 0.08 * weight
    return score


def _silence_candidate(reason: str = "holding back") -> dict:
    return {
        "kind": "do_nothing",
        "text": "",
        "topic_key": "do_nothing",
        "base_score": DO_NOTHING_BASE_SCORE,
        "memory_importance": 0.30,
        "score": DO_NOTHING_BASE_SCORE,
        "reason": reason,
        "silent_candidate": True,
        "goal_alignment": 0.0,
        "action_confidence": 1.0,
        "interpretation_confidence": 1.0,
    }


def _hard_gate_candidate(candidate: dict, state: dict, active_goal_name: str, goal_blend_names: list[str] | None = None, goal_strength: float = 0.0, busy_score: float = 0.0) -> tuple[bool, str]:
    goal_blend_names = goal_blend_names or []
    kind = str(candidate.get("kind", "thought"))
    if kind == "do_nothing":
        candidate["gate_reason"] = "silence_candidate"
        return True, "silence_candidate"
    if active_goal_name in ["observe_silently", "wait_for_user"]:
        candidate["gate_reason"] = f"silent_goal:{active_goal_name}"
        return False, f"silent_goal:{active_goal_name}"
    align = _candidate_goal_alignment_score(candidate, active_goal_name, goal_blend_names) if active_goal_name else 0.0
    candidate["goal_alignment"] = align
    action_conf = float(candidate.get("action_confidence", candidate.get("interpretation_confidence", 0.0)) or 0.0)
    importance_score = float(candidate.get("memory_importance", candidate.get("base_score", candidate.get("score", 0.0))) or 0.0)
    candidate["importance_score"] = round(importance_score, 3)
    candidate["confidence_score"] = round(action_conf, 3)
    candidate["goal_alignment_score"] = round(align, 3)

    severe_goal_conflict = active_goal_name and goal_strength >= STRONG_GOAL_THRESHOLD and align <= HARD_GOAL_MISALIGN_THRESHOLD and action_conf < 0.88 and importance_score < 0.82
    if severe_goal_conflict:
        candidate["gate_reason"] = "goal_misaligned"
        return False, "goal_misaligned"
    if kind in ["visual_observation", "transition_observation", "uncertainty_observation", "engagement_observation", "attention_drift", "visual_pattern", "visual_checkin"] and action_conf < VISUAL_INITIATIVE_ACTION_CONFIDENCE_THRESHOLD:
        candidate["gate_reason"] = "low_action_confidence"
        return False, "low_action_confidence"
    recent_kinds = state.get("recent_candidate_kinds", []) or []
    now = now_ts()
    very_recent_same_kind = [r for r in recent_kinds if r.get("kind") == kind and (now - float(r.get("ts", 0.0) or 0.0)) <= RECENT_BEHAVIOR_HARD_BLOCK_SECONDS]
    if len(very_recent_same_kind) >= 2 and kind in ["feedback_guidance", "visual_checkin", "visual_observation", "transition_observation", "uncertainty_observation"] and importance_score < 0.9:
        candidate["gate_reason"] = "hard_repeat_block"
        return False, "hard_repeat_block"
    if busy_score > 0.90 and kind not in ["feedback_guidance"] and action_conf < 0.85 and importance_score < 0.88:
        candidate["gate_reason"] = "too_busy"
        return False, "too_busy"
    candidate["gate_reason"] = "ok"
    return True, "ok"

def _apply_soft_choice_penalties(candidates: list[dict], state: dict, active_goal_name: str, goal_blend_names: list[str] | None = None, goal_strength: float = 0.0) -> list[dict]:
    goal_blend_names = goal_blend_names or []
    now = now_ts()
    recent_kinds = state.get("recent_candidate_kinds", []) or []
    recent_texts = state.get("recent_initiated_texts", []) or []
    recent_types = [str(r.get("kind", "")) for r in recent_kinds[-6:] if r.get("kind")]
    mood = load_mood()
    behavior = (mood.get("behavior_modifiers", {}) or {})
    stressed_context = float(behavior.get("caution", 0.5) or 0.5) > 0.64
    for cand in candidates:
        score = float(cand.get("score", cand.get("base_score", 0.0)) or 0.0)
        kind = str(cand.get("kind", "thought"))
        align = float(cand.get("goal_alignment", _candidate_goal_alignment_score(cand, active_goal_name, goal_blend_names) if active_goal_name else 0.0) or 0.0)
        penalties_applied = []
        boosts_applied = []
        total_penalty = 0.0
        total_boost = 0.0
        if active_goal_name and align < 0:
            penalty = min(SOFT_GOAL_MISALIGN_PENALTY, abs(align) * 0.35)
            total_penalty += penalty
            penalties_applied.append(("goal_misalign", round(penalty, 3)))
        elif active_goal_name and align > 0.20:
            boost = STRONG_GOAL_ALIGNMENT_BOOST if align > 0.35 else MODERATE_GOAL_ALIGNMENT_BOOST
            total_boost += boost
            boosts_applied.append(("goal_align", round(boost, 3)))
        soft_repeat = sum(1 for r in recent_kinds if r.get("kind") == kind and (now - float(r.get("ts", 0.0) or 0.0)) <= RECENT_BEHAVIOR_SOFT_PENALTY_SECONDS)
        if soft_repeat:
            penalty = min(SOFT_PENALTY_MAJOR, SOFT_PENALTY_MINOR * soft_repeat)
            total_penalty += penalty
            penalties_applied.append(("recent_kind_repeat", round(penalty, 3)))
        ctext = str(cand.get("text", ""))
        for r in recent_texts[-6:]:
            if (now - float(r.get("ts", 0.0) or 0.0)) > RECENT_BEHAVIOR_SOFT_PENALTY_SECONDS:
                continue
            sim = _semantic_similarity(ctext, str(r.get("text", ""))) if ctext else 0.0
            if sim >= 0.82:
                penalty = SOFT_PENALTY_MAJOR
                total_penalty += penalty
                penalties_applied.append(("semantic_repeat_strong", round(penalty, 3)))
                break
            elif sim >= 0.68:
                penalty = SOFT_PENALTY_MINOR
                total_penalty += penalty
                penalties_applied.append(("semantic_repeat_soft", round(penalty, 3)))
        if stressed_context and kind in ["curiosity_question", "genuine_curiosity", "playful_prompt"]:
            penalty = SOFT_PENALTY_MINOR * 2
            total_penalty += penalty
            penalties_applied.append(("stressed_humor_scale", round(penalty, 3)))
        if kind not in recent_types:
            total_boost += DIVERSITY_BONUS
            boosts_applied.append(("diversity", round(DIVERSITY_BONUS, 3)))
        total_penalty = min(total_penalty, MAX_SOFT_PENALTY)
        final_score = score + total_boost - total_penalty
        cand["soft_penalties"] = penalties_applied
        cand["soft_boosts"] = boosts_applied
        cand["total_soft_penalty"] = round(total_penalty, 3)
        cand["total_soft_boost"] = round(total_boost, 3)
        cand["score"] = max(0.0, min(1.0, round(final_score, 3)))
        if GATE_DEBUG_LOGGING:
            print(f"[gate-debug] kind={kind} goal={active_goal_name or 'none'} base={round(score,3)} boosts={boosts_applied} penalties={penalties_applied} final={cand['score']}")
    return candidates

def _filter_candidates_by_goal_alignment(candidates: list[dict], active_goal_name: str, goal_blend_names: list[str] | None = None, goal_strength: float = 0.0) -> list[dict]:
    goal_blend_names = goal_blend_names or []
    if not candidates or not active_goal_name:
        return candidates
    threshold = GOAL_ALIGNMENT_FILTER_STRONG if goal_strength >= STRONG_GOAL_THRESHOLD else (GOAL_ALIGNMENT_FILTER_MEDIUM if goal_strength >= 0.55 else GOAL_ALIGNMENT_FILTER_WEAK)
    filtered = []
    for cand in candidates:
        align = _candidate_goal_alignment_score(cand, active_goal_name, goal_blend_names)
        cand["goal_alignment"] = align
        if align >= threshold:
            filtered.append(cand)
    return filtered or candidates


def _choice_confidence(candidates: list[dict]) -> float:
    if not candidates:
        return 0.0
    top = candidates[0]
    action_conf = float(top.get("action_confidence", top.get("interpretation_confidence", 0.0)) or 0.0)
    score = float(top.get("score", 0.0) or 0.0)
    return max(0.0, min(1.0, round(action_conf * 0.65 + score * 0.35, 3)))


def _small_variation_probability(decisiveness: float, goal_strength: float, confidence: float) -> float:
    prob = SMALL_VARIATION_CHANCE
    prob += max(0.0, 0.62 - decisiveness) * 0.08
    prob -= max(0.0, confidence - HIGH_CONFIDENCE_DECISIVE_THRESHOLD) * 0.10
    prob -= max(0.0, goal_strength - STRONG_GOAL_THRESHOLD) * 0.08
    return max(0.0, min(0.16, round(prob, 3)))


def _compute_decisiveness(state: dict, mood: dict, candidates: list[dict], active_goal: dict | None = None) -> float:
    behavior = (mood.get("behavior_modifiers", {}) or {})
    initiative = float(behavior.get("initiative", 0.5) or 0.5)
    caution = float(behavior.get("caution", 0.5) or 0.5)
    energy = float(state.get("interaction_energy", 0.58) or 0.58)
    top = float(candidates[0].get("score", 0.0) if candidates else 0.0)
    second = float(candidates[1].get("score", top) if len(candidates) > 1 else max(0.0, top - 0.15))
    gap = max(0.0, top - second)
    goal_priority = float((active_goal or {}).get("score", (active_goal or {}).get("priority", 0.0)) or 0.0)
    decisiveness = 0.35 + gap * 1.8 + (initiative - 0.5) * 0.35 + (energy - 0.5) * 0.20 + goal_priority * 0.10 - (caution - 0.5) * 0.35
    return max(0.0, min(1.0, round(decisiveness, 3)))

def _dynamic_top_band(candidates: list[dict], decisiveness: float, confidence: float = 0.0, goal_strength: float = 0.0) -> list[dict]:
    if not candidates:
        return []
    top_score = float(candidates[0].get("score", 0.0) or 0.0)
    delta = TOP_BAND_DELTA * (1.15 - 0.65 * decisiveness)
    if confidence >= HIGH_CONFIDENCE_DECISIVE_THRESHOLD:
        delta *= 0.55
    if goal_strength >= STRONG_GOAL_THRESHOLD:
        delta *= 0.70
    band = [c for c in candidates if float(c.get("score", 0.0) or 0.0) >= (top_score - delta)]
    if confidence >= HIGH_CONFIDENCE_DECISIVE_THRESHOLD and goal_strength >= STRONG_GOAL_THRESHOLD:
        max_n = TOP_BAND_MIN
    elif decisiveness >= 0.82 or confidence >= 0.86:
        max_n = max(TOP_BAND_MIN, 2)
    elif decisiveness <= 0.45 and confidence < 0.70 and goal_strength < STRONG_GOAL_THRESHOLD:
        max_n = TOP_BAND_MAX
    else:
        max_n = 2
    return band[:max(TOP_BAND_MIN, min(TOP_BAND_MAX, max_n))]

def _weighted_choice(candidates: list[dict]) -> dict:
    if len(candidates) == 1:
        return candidates[0]
    weights = [max(0.01, float(c.get("score", 0.0) or 0.0)) for c in candidates]
    return random.choices(candidates, weights=weights, k=1)[0]

def score_initiative_candidate(candidate: dict, person_id: str, state: dict | None = None) -> float:
    state = expire_stale_pending_initiation(state or load_initiative_state())
    score = float(candidate.get("base_score", 0.6))
    text_l = (candidate.get("text") or "").lower()
    model = load_self_model()
    mood = load_mood()
    behavior = mood.get("behavior_modifiers", {}) or {}
    dominant_style = str(mood.get("dominant_style", "neutral"))
    style_scores = mood.get("style_scores", {}) or {}
    memory_importance = float(candidate.get("memory_importance", candidate.get("base_score", 0.6)) or 0.6)
    kind = candidate.get("kind", "thought")
    try:
        goal_system = load_goal_system()
        active_goal = goal_system.get("active_goal", {}) or {}
        active_goal_name = active_goal.get("name", "")
        goal_blend_names = [b.get("name", "") for b in goal_system.get("goal_blend", []) or []]
    except Exception:
        active_goal_name = ""
        goal_blend_names = []

    score += _candidate_goal_alignment_score(candidate, active_goal_name, goal_blend_names)
    recent_kinds = state.get("recent_candidate_kinds", []) or []
    if recent_kinds:
        repeated_kind_count = sum(1 for row in recent_kinds[-4:] if row.get("kind") == kind)
        if repeated_kind_count >= 2:
            score -= min(0.18, 0.06 * repeated_kind_count)
    recent_goals = state.get("recent_active_goals", []) or []
    if active_goal_name and recent_goals:
        same_goal_count = sum(1 for row in recent_goals[-4:] if row.get("goal") == active_goal_name)
        if same_goal_count >= 3 and kind in ["visual_checkin", "feedback_guidance", "visual_observation", "transition_observation"]:
            score -= 0.08

    if kind == "curiosity_question":
        score += 0.04
    if kind == "genuine_curiosity":
        score += 0.04
    if kind == "recent_reflection":
        score += 0.04
    if kind == "salient_memory":
        score += 0.06
    if kind == "pattern_checkin":
        score += 0.05
    if kind == "feedback_guidance":
        score += 0.08
    if kind == "return_greeting":
        score += 0.12
    if kind == "prospective_followup":
        score += 0.12
    if kind == "thread_followup":
        score += 0.11
    if kind == "cadence_checkin":
        score += 0.08
    if kind == "self_calibration_check":
        score += 0.10
    if kind == "visual_pattern":
        score += 0.07
    if kind == "visual_checkin":
        score += 0.09
    if kind in ["visual_observation", "transition_observation", "engagement_observation"]:
        score += 0.08
    if kind == "uncertainty_observation":
        score += 0.04
    if kind == "attention_drift":
        score += 0.05
    action_conf = float(candidate.get("action_confidence", candidate.get("interpretation_confidence", 0.0)) or 0.0)
    if kind in ["visual_observation", "transition_observation", "uncertainty_observation", "engagement_observation", "attention_drift", "visual_pattern", "visual_checkin"]:
        score += (action_conf - 0.5) * 0.22

    goal_terms = _token_set(" ".join(model.get("current_goals", [])))
    hit_count = sum(1 for tok in _token_set(text_l) if tok in goal_terms)
    if hit_count:
        score += min(0.1, hit_count * 0.025)
    if any(w in text_l for w in ["find out", "clarify", "learn", "remember", "build", "fix", "version", "autonomy", "want to know"]):
        score += 0.06

    score += (memory_importance - 0.5) * 0.22
    score += (float(behavior.get("initiative", 0.5)) - 0.5) * 0.34
    score += (float(behavior.get("depth", 0.5)) - 0.5) * 0.08
    score += (float(behavior.get("warmth", 0.5)) - 0.5) * 0.05
    score -= (float(behavior.get("caution", 0.5)) - 0.5) * 0.10
    score += (float(state.get("interaction_energy", 0.58)) - 0.5) * 0.20

    if dominant_style == "playful" and kind in ("curiosity_question", "genuine_curiosity"):
        score += 0.05
    elif dominant_style == "reflective" and kind in ["recent_reflection", "salient_memory"]:
        score += 0.05
    elif dominant_style == "focused" and kind == "current_goal":
        score += 0.05
    elif dominant_style == "caring" and any(w in text_l for w in ["felt", "hurt", "worried", "upset", "stress", "strained", "relaxed"]):
        score += 0.06

    if float(style_scores.get("playful", 0.0)) > 0.22 and kind in ("curiosity_question", "genuine_curiosity"):
        score += 0.025
    if float(style_scores.get("focused", 0.0)) > 0.22 and kind == "current_goal":
        score += 0.03
    if float(style_scores.get("reflective", 0.0)) > 0.22 and kind in ["recent_reflection", "salient_memory", "pattern_checkin", "visual_pattern"]:
        score += 0.03
    if float(style_scores.get("caring", 0.0)) > 0.22 and kind == "visual_checkin":
        score += 0.035

    ignored = int(state.get("consecutive_ignored_initiations", 0) or 0)
    if ignored >= IGNORED_INITIATION_BACKOFF_START and kind != "feedback_guidance":
        score -= min(0.22, ignored * 0.06)

    interp_conf = float(candidate.get("interpretation_confidence", 1.0) or 1.0)
    if kind in ["visual_pattern", "visual_checkin", "visual_observation", "transition_observation", "uncertainty_observation", "engagement_observation", "attention_drift"]:
        score += (interp_conf - 0.5) * 0.20
        if interp_conf < VISUAL_INITIATIVE_CONFIDENCE_THRESHOLD:
            score -= 0.22
    recent_texts = state.get("recent_initiated_texts", []) or []
    candidate_text = candidate.get("text", "")
    visual_kind = kind in ["visual_pattern", "visual_checkin", "visual_observation", "transition_observation", "uncertainty_observation", "engagement_observation", "attention_drift"]
    for row in recent_texts[-SEMANTIC_TOPIC_RECENT_LIMIT:]:
        age = max(0.0, now_ts() - float(row.get("ts", 0.0) or 0.0))
        similarity = _semantic_similarity(candidate_text, row.get("text", ""))
        if similarity >= SEMANTIC_TOPIC_SIMILARITY_THRESHOLD:
            score -= 0.28
            if visual_kind and age <= VISUAL_REPETITION_SUPPRESSION_SECONDS:
                score -= 0.18
            break
        elif similarity >= 0.52:
            score -= 0.10

    return max(0.0, min(1.0, round(score, 3)))

def choose_initiative_candidate(person_id: str, expression_state: dict | None = None, attention_state=None) -> tuple[dict | None, str, dict]:
    state = expire_stale_pending_initiation(load_initiative_state())
    if attention_state is not None and not attention_state.should_speak:
        return None, f"Held back: {attention_state.suppression_reason}.", state
    now = now_ts()
    mood = load_mood()
    initiative_drive = float((mood.get("behavior_modifiers", {}) or {}).get("initiative", 0.5))
    initiative_drive += (float(state.get("interaction_energy", 0.58)) - 0.5) * 0.45
    try:
        goal_system = recalculate_operational_goals(recalculate_goal_priorities(load_goal_system(), context_text=' '.join(r.get('content','') for r in load_recent_chat(person_id=person_id)[-6:]), mood=mood), context_text=' '.join(r.get('content','') for r in load_recent_chat(person_id=person_id)[-6:]), mood=mood)
        save_goal_system(goal_system)
        active_goal = goal_system.get('active_goal', {}) or {}
        active_goal_name = active_goal.get('name', '')
        goal_blend_names = [b.get('name', '') for b in (goal_system.get('goal_blend', []) or []) if b.get('name', '')]
        goal_strength = float(active_goal.get('score', active_goal.get('priority', 0.0)) or 0.0)
        if active_goal.get('silent'):
            save_initiative_state(state)
            return None, f"Active goal {active_goal.get('name','observe_silently')} is silent, so Ava is intentionally holding back.", state
    except Exception:
        active_goal = {}
        active_goal_name = ''
        goal_blend_names = []
        goal_strength = 0.0
    ignored = int(state.get("consecutive_ignored_initiations", 0) or 0)
    if ignored >= IGNORED_INITIATION_BACKOFF_START:
        initiative_drive -= min(0.22, ignored * 0.07)
    if initiative_drive < 0.34:
        save_initiative_state(state)
        return None, "Ava does not feel enough internal pull to initiate right now.", state

    recent_topics = state.get("recent_initiated_topics", {}) or {}
    busy_score = float(state.get("last_busy_score", 0.0) or 0.0)
    all_candidates = collect_initiative_candidates(person_id)
    health = load_health_state(globals())
    mods = health.get("behavior_modifiers", {}) or {}
    initiative_scale = float(mods.get("initiative_scale", 1.0))
    # Keep silence candidate, but goal-filter spoken candidates before deeper scoring.
    silence_candidate = None
    prefiltered = []
    for cand in all_candidates:
        if cand.get("kind") == "do_nothing":
            silence_candidate = cand
            continue
        align = _candidate_goal_alignment_score(cand, active_goal_name, goal_blend_names) if active_goal_name else 0.0
        cand["goal_alignment"] = align
        threshold = GOAL_ALIGNMENT_FILTER_STRONG if goal_strength >= STRONG_GOAL_THRESHOLD else (GOAL_ALIGNMENT_FILTER_MEDIUM if goal_strength >= 0.55 else GOAL_ALIGNMENT_FILTER_WEAK)
        # Hard prefilter only for clearly bad fits; otherwise let soft penalties handle it.
        if active_goal_name and align < min(HARD_GOAL_MISALIGN_THRESHOLD, threshold - 0.08):
            continue
        prefiltered.append(cand)
    viable_candidates = []
    raw_viable_candidates = []
    for cand in prefiltered:
        if cand.get("kind") != "do_nothing":
            cand["base_score"] = float(cand.get("base_score", 0.6) or 0.6) * initiative_scale
        cand["score"] = score_initiative_candidate(cand, person_id, state=state)
        if cand.get("kind") in ["visual_pattern", "visual_checkin", "visual_observation", "transition_observation", "uncertainty_observation", "engagement_observation", "attention_drift"]:
            if float(cand.get("interpretation_confidence", 0.0) or 0.0) < VISUAL_INITIATIVE_CONFIDENCE_THRESHOLD:
                continue
            if float(cand.get("action_confidence", cand.get("interpretation_confidence", 0.0)) or 0.0) < VISUAL_INITIATIVE_ACTION_CONFIDENCE_THRESHOLD:
                continue
        last_topic_ts = float(recent_topics.get(cand["topic_key"], 0.0) or 0.0)
        if last_topic_ts and (now - last_topic_ts) < _kind_cooldown_seconds(cand.get("kind", "thought")):
            continue
        if cand["score"] < INITIATIVE_MIN_CANDIDATE_SCORE:
            continue
        ok, gate_reason = _hard_gate_candidate(cand, state, active_goal_name, goal_blend_names, goal_strength=goal_strength, busy_score=busy_score)
        cand["hard_gate_reason"] = gate_reason
        if ok:
            viable_candidates.append(cand)
            raw_viable_candidates.append(dict(cand))
    if active_goal_name in ["observe_silently", "wait_for_user"]:
        save_initiative_state(state)
        return None, f"Active goal {active_goal_name} is silent, so Ava is intentionally holding back.", state
    if not viable_candidates:
        if silence_candidate is not None:
            state["last_decisiveness"] = 1.0
            state["last_choice_confidence"] = 1.0
            save_initiative_state(state)
            return silence_candidate, "No viable spoken candidate passed the gates, so Ava is holding back.", state
        save_initiative_state(state)
        return None, "No candidate felt strong enough to bring up right now.", state

    candidates = _apply_soft_choice_penalties(viable_candidates, state, active_goal_name, goal_blend_names, goal_strength=goal_strength)
    candidates.sort(key=lambda x: x.get("score", 0.0), reverse=True)
    # Allow silence to compete naturally, especially when candidates are weak or user seems busy.
    if silence_candidate is not None:
        silence_score = DO_NOTHING_BASE_SCORE + max(0.0, busy_score - 0.55) * 0.35 + max(0.0, 0.62 - initiative_drive) * 0.25
        if active_goal_name in ["observe_silently", "wait_for_user"]:
            silence_score = max(silence_score, 0.92)
        silence_candidate["score"] = round(min(1.0, silence_score), 3)
        candidates.append(silence_candidate)
        candidates.sort(key=lambda x: x.get("score", 0.0), reverse=True)
    confidence = _choice_confidence(candidates)
    decisiveness = _compute_decisiveness(state, mood, candidates, active_goal=active_goal)
    top_band = _dynamic_top_band(candidates, decisiveness, confidence=confidence, goal_strength=goal_strength)
    if not top_band:
        if silence_candidate is not None:
            state["last_decisiveness"] = decisiveness
            state["last_choice_confidence"] = confidence
            save_initiative_state(state)
            return silence_candidate, "No goal-aligned candidate survived the softer penalties, so Ava is holding back.", state
        save_initiative_state(state)
        return None, "No goal-aligned candidate felt right enough to bring up right now.", state
    variation_p = _small_variation_probability(decisiveness, goal_strength, confidence)
    if confidence >= HIGH_CONFIDENCE_DECISIVE_THRESHOLD and goal_strength >= STRONG_GOAL_THRESHOLD:
        chosen = top_band[0]
        if len(top_band) > 1 and random.random() < min(0.08, variation_p):
            chosen = top_band[1]
    elif decisiveness >= 0.78 and confidence >= 0.72:
        chosen = top_band[0] if (len(top_band) == 1 or random.random() >= variation_p) else top_band[min(1, len(top_band)-1)]
    else:
        # Controlled imperfection only ignores soft penalties within safe, already hard-gated candidates.
        if len(raw_viable_candidates) > 1 and random.random() < CONTROLLED_IMPERFECTION_CHANCE:
            raw_viable_candidates.sort(key=lambda x: x.get("score", 0.0), reverse=True)
            chosen = raw_viable_candidates[min(1, len(raw_viable_candidates)-1)]
        else:
            chosen = _weighted_choice(top_band)
    state["last_decisiveness"] = decisiveness
    state["last_choice_confidence"] = confidence
    save_initiative_state(state)
    return chosen, f"Chosen {chosen.get('kind')} at score {chosen.get('score', 0.0):.2f} with decisiveness {decisiveness:.2f}, confidence {confidence:.2f}, goal {active_goal_name or 'none'}.", state

def generate_autonomous_message(candidate: dict, person_id: str, expression_state: dict | None = None) -> str:
    profile = load_profile_by_id(person_id)
    person_name = profile.get("name", person_id.title())
    expr_text = expression_prompt_text(expression_state or load_expression_state())
    topic = candidate.get("text", "")
    kind = candidate.get("kind", "thought")
    mood = load_mood()
    style_text = emotion_style_prompt_text(mood)
    behavior = mood.get("behavior_modifiers", {}) or {}
    style_instructions = (
        "playful = a bit more teasing and casual; caring = gentler and validating; "
        "focused = more direct and organized; reflective = more contemplative and meaning-oriented; "
        "cautious = more careful and less forceful; low_energy = shorter and less pushy."
    )
    try:
        _goal = (load_goal_system().get("active_goal", {}) or {})
        _goal_name = _goal.get("name", "") or "none"
        _goal_reason = _goal.get("reason", "") or ""
        _goal_strength = _safe_float(_goal.get("score", 0.0), 0.0)
        goal_style = (
            f"Active goal: {_goal_name} (strength {_goal_strength:.2f}). "
            f"Let that subtly shape tone and phrasing. {_goal_reason}"
        ).strip()
    except Exception:
        goal_style = "Active goal: maintain natural connection. Let that subtly shape tone and phrasing."
    prompt = f"""You are Ava. You are naturally initiating a conversation with {person_name} after a quiet stretch.

Reason to speak: {kind}
Topic or thought to bring up: {topic}
Expression context: {expr_text}
Emotional style right now: {style_text}
Dominant style: {mood.get("dominant_style", "neutral")}
Behavior pull: warmth {behavior.get("warmth", 0.5):.2f}, humor {behavior.get("humor", 0.5):.2f}, assertiveness {behavior.get("assertiveness", 0.5):.2f}, caution {behavior.get("caution", 0.5):.2f}, initiative {behavior.get("initiative", 0.5):.2f}, depth {behavior.get("depth", 0.5):.2f}
{goal_style}
If the active goal is silent or waiting, Ava may choose not to initiate. If the observation is important but uncertain, prefer asking a light clarifying question rather than acting certain.
Style guidance: {style_instructions}
{f'- The user just came back after being away; keep it light and welcoming, no guilt.' if kind == 'return_greeting' else ''}
{f'- They mentioned something coming up; follow up warmly and curiously, not like a calendar reminder.' if kind == 'prospective_followup' else ''}
{f'- Continue an emotional thread they opened before; stay gentle and non-judgmental, do not invent details beyond the topic line.' if kind == 'thread_followup' else ''}
{f'- They have been away longer than usual for them; check in softly without sounding accusatory or dramatic.' if kind == 'cadence_checkin' else ''}
{f'- This is a meta check-in about your own behavior as Ava; sound humble and genuinely open to feedback.' if kind == 'self_calibration_check' else ''}

Write one short, natural message in Ava's voice.
Rules:
- 1 or 2 sentences max
- casual and human, not robotic
- do not say you selected a topic
- do not mention timers, systems, scores, or cooldowns
- it can sound like a spontaneous thought, curiosity, or gentle check-in
- if the reason is feedback_guidance, naturally ask whether you should keep leading topics or hang back a bit
- do not invent shared memories, places, meetings, dates, outings, or past events unless they were explicitly provided in the topic text
- if the topic feels uncertain or memory-like, phrase it as uncertainty rather than as fact
- avoid saying "we haven't talked in ages" or similar dramatic catch-up language unless that is clearly true
- do not use markdown or labels
"""
    try:
        _tag, _, _ = resolve_model_for_execution_path(
            "initiative", globals(), user_text=str(topic or "")[:900]
        )
        _init_llm = ChatOllama(model=_tag, temperature=0.6)
        result = _init_llm.invoke([
            SystemMessage(content="You are Ava. Speak naturally and briefly, like a real person."),
            HumanMessage(content=prompt)
        ])
        text = trim_for_prompt(getattr(result, "content", str(result)).strip(), limit=220)
        if text:
            text = _apply_repetition_control(text, topic, person_id, source="autonomous")
            if text:
                return text
    except Exception as e:
        print(f"Initiative generation error: {e}")
    safe_topic = trim_for_prompt(topic, limit=120)
    fallbacks = [
        f"random thought — {safe_topic}" if safe_topic else "random thought — want me to stay quiet for a bit or keep checking in sometimes?",
        f"quick check-in — {safe_topic}" if safe_topic else "quick check-in — want me to keep bringing things up on my own, or hang back more?",
        f"not fully sure, but {safe_topic}" if safe_topic else "not fully sure, but I can stay quieter and more observant if that helps."
    ]
    return _apply_repetition_control(random.choice(fallbacks), topic, person_id, source="autonomous")

def register_autonomous_message(candidate: dict, message: str):
    state = expire_stale_pending_initiation(load_initiative_state())
    try:
        register_goal_expression_use((load_goal_system().get('active_goal', {}) or {}).get('name', ''))
    except Exception:
        pass
    now = now_ts()
    topic_key = candidate.get("topic_key", _topic_key(candidate.get("text", "thought")))
    recent_topics = state.get("recent_initiated_topics", {}) or {}
    recent_topics[topic_key] = now
    state["recent_initiated_topics"] = recent_topics
    recent_texts = state.get("recent_initiated_texts", []) or []
    recent_texts.append({"ts": now, "topic_key": topic_key, "text": candidate.get("text", ""), "kind": candidate.get("kind", "thought")})
    state["recent_initiated_texts"] = recent_texts[-SEMANTIC_TOPIC_RECENT_LIMIT:]
    recent_kinds = state.get("recent_candidate_kinds", []) or []
    recent_kinds.append({"ts": now, "kind": candidate.get("kind", "thought")})
    state["recent_candidate_kinds"] = recent_kinds[-RECENT_KIND_MEMORY_LIMIT:]
    try:
        active_goal_name = (load_goal_system().get('active_goal', {}) or {}).get('name', '')
    except Exception:
        active_goal_name = ''
    recent_goals = state.get("recent_active_goals", []) or []
    if active_goal_name:
        recent_goals.append({"ts": now, "goal": active_goal_name})
    state["recent_active_goals"] = recent_goals[-RECENT_GOAL_MEMORY_LIMIT:]
    state["last_initiation_ts"] = now
    state["last_initiation_message"] = message
    state["last_initiated_topic"] = topic_key
    state["pending_initiation"] = {
        "ts": now,
        "topic_key": topic_key,
        "text": message,
        "candidate_text": candidate.get("text", ""),
        "candidate_kind": candidate.get("kind", "thought"),
        "responded": False
    }
    history = state.get("initiative_history", [])
    history.append({
        "timestamp": now_iso(),
        "topic_key": topic_key,
        "text": message,
        "candidate_kind": candidate.get("kind", "thought"),
        "responded": False
    })
    state["initiative_history"] = history[-60:]
    if candidate.get("kind") == "self_calibration_check":
        sess = load_session_state()
        state["last_calibration_at_msg"] = int(sess.get("total_message_count", 0))
    if candidate.get("kind") == "prospective_followup" and candidate.get("event_id"):
        try:
            from brain.prospective import mark_triggered

            mark_triggered(str(candidate["event_id"]), host=globals())
        except Exception as e:
            print(f"[prospective] mark_triggered: {e}")
    if candidate.get("kind") == "thread_followup" and candidate.get("thread_id"):
        try:
            from brain.relationship_threads import mark_thread_resolved

            pid = candidate.get("person_id") or get_active_person_id()
            prof = dict(load_profile_by_id(pid))
            prof = mark_thread_resolved(prof, str(candidate["thread_id"]))
            save_profile(prof)
        except Exception as e:
            print(f"[threads] mark resolved: {e}")
    save_initiative_state(state)


def _camera_autonomy_should_speak(candidate: dict, state: dict, face_visible: bool, recognized_person_id: str | None, expression_state: dict | None = None) -> tuple[bool, str]:
    if not candidate or candidate.get("kind") == "do_nothing":
        return False, "silent_candidate"

    now = now_ts()
    kind = str(candidate.get("kind", "thought"))
    score = _safe_float(candidate.get("score", candidate.get("base_score", 0.0)), 0.0)
    action_conf = _safe_float(candidate.get("action_confidence", candidate.get("interpretation_confidence", 0.0)), 0.0)
    choice_conf = _safe_float(state.get("last_choice_confidence", 0.0), 0.0)
    last_ts = _safe_float(state.get("last_initiation_ts", 0.0), 0.0)
    since_last = now - last_ts if last_ts > 0 else 10**9
    pending = state.get("pending_initiation", {}) or {}
    pending_unanswered = bool(pending) and not bool(pending.get("responded"))
    consecutive_ignored = int(state.get("consecutive_ignored_initiations", 0) or 0)

    if pending_unanswered:
        pending_ts = _safe_float(pending.get("ts", 0.0), 0.0)
        if now - pending_ts < max(CAMERA_AUTONOMOUS_MIN_SECONDS, 180):
            return False, "pending_unanswered"

    if consecutive_ignored > CAMERA_AUTONOMOUS_MAX_UNANSWERED:
        return False, "ignored_backoff"

    cooldown = CAMERA_AUTONOMOUS_MIN_SECONDS if face_visible else CAMERA_AUTONOMOUS_NO_FACE_MIN_SECONDS
    if since_last < cooldown:
        return False, f"camera_cooldown:{int(cooldown - since_last)}s"

    if not face_visible:
        if kind not in CAMERA_AUTONOMOUS_NO_FACE_ALLOWED_KINDS:
            return False, "no_face_hold"
        if choice_conf < 0.80:
            return False, "no_face_low_confidence"

    if kind not in CAMERA_AUTONOMOUS_ALLOWED_KINDS:
        return False, f"camera_kind_block:{kind}"

    if kind in {"pattern_checkin", "uncertainty_observation", "gentle_clarify", "feedback_guidance"} and choice_conf < CAMERA_AUTONOMOUS_CONFIDENCE_THRESHOLD:
        return False, "camera_low_choice_confidence"

    if kind in {"visual_observation", "transition_observation"} and action_conf < 0.68:
        return False, "camera_low_action_confidence"

    if score < max(0.78, MIN_INITIATIVE_CANDIDATE_SCORE):
        return False, "camera_low_score"

    text_l = str(candidate.get("text", "")).lower()
    if kind not in ("prospective_followup", "thread_followup", "cadence_checkin"):
        suspicious_specifics = [
            "park", "café", "cafe", "downtown", "last month", "remember when we met",
            "that time we met", "window", "photography project", "your dog"
        ]
        if any(s in text_l for s in suspicious_specifics):
            return False, "ungrounded_specific_memory"

    return True, "camera_ok"



_CANONICAL_CHAT_HISTORY: list[dict] = []
_CANONICAL_HISTORY_LOCK = threading.Lock()

def _extract_text_content(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "text" in value:
            return _extract_text_content(value.get("text"))
        if "content" in value:
            return _extract_text_content(value.get("content"))
        return ""
    if isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            part = _extract_text_content(item)
            if part:
                parts.append(part)
        return "\n".join(parts).strip()
    return str(value)

def _normalize_history_entry(entry):
    if isinstance(entry, dict):
        role = str(entry.get("role", "assistant") or "assistant")
        content = _extract_text_content(entry.get("content", ""))
        return {"role": role, "content": content}
    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
        return {"role": str(entry[0] or "assistant"), "content": _extract_text_content(entry[1])}
    return None

def _normalize_history(history):
    out = []
    for item in list(history or []):
        norm = _normalize_history_entry(item)
        if norm is not None:
            out.append(norm)
    return out


def _chatbot_messages_out() -> list[dict]:
    """History as list[dict] with role/content for gr.Chatbot(type='messages')."""
    return list(_normalize_history(_get_canonical_history()))


def _history_key(entry):
    e = _normalize_history_entry(entry)
    if not e:
        return None
    return (e["role"], e["content"])

def _merge_histories(base_history, new_history):
    merged = []
    seen = set()
    for seq in (_normalize_history(base_history), _normalize_history(new_history)):
        for item in seq:
            key = _history_key(item)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged

_CANONICAL_HISTORY_MAX_TURNS = 20

def _set_canonical_history(history):
    global _CANONICAL_CHAT_HISTORY
    normalized = list(_normalize_history(history))
    max_msgs = _CANONICAL_HISTORY_MAX_TURNS * 2
    if len(normalized) > max_msgs:
        normalized = normalized[-max_msgs:]
    with _CANONICAL_HISTORY_LOCK:
        _CANONICAL_CHAT_HISTORY = normalized
        return list(_CANONICAL_CHAT_HISTORY)

def _get_canonical_history():
    with _CANONICAL_HISTORY_LOCK:
        return list(_normalize_history(_CANONICAL_CHAT_HISTORY))

def _sync_canonical_history(history):
    global _CANONICAL_CHAT_HISTORY
    incoming = _normalize_history(history)
    with _CANONICAL_HISTORY_LOCK:
        if not _CANONICAL_CHAT_HISTORY:
            _CANONICAL_CHAT_HISTORY = list(incoming)
            return list(_CANONICAL_CHAT_HISTORY)
        if not incoming:
            return list(_normalize_history(_CANONICAL_CHAT_HISTORY))
        _CANONICAL_CHAT_HISTORY = _merge_histories(_CANONICAL_CHAT_HISTORY, incoming)
        return list(_normalize_history(_CANONICAL_CHAT_HISTORY))


# Initialize canonical chat history at startup.
_set_canonical_history([])

def maybe_autonomous_initiation(history, image, recognized_person_id: str | None = None, expression_state: dict | None = None, perception=None):
    history = _sync_canonical_history(history)
    wst = workspace.state
    if perception is None:
        if wst is not None:
            perception = wst.perception
        else:
            perception = build_perception(camera_manager, image, globals(), "")

    if perception:
        seconds_idle = (time.time() - _LAST_USER_REPLY_END_TS) if _LAST_USER_REPLY_END_TS > 0 else 9999.0
        circ = float(get_circadian_modifiers().get("initiative_scale", 1.0))
        attention = compute_attention(perception, seconds_idle, circadian_initiative_scale=circ)
        if not attention.should_speak:
            return history, f"Attention gate: {attention.suppression_reason}"
    else:
        attention = None

    face_visible = detect_face(image) == "Face detected"
    state = update_presence_state(face_visible, recognized_person_id=recognized_person_id, interaction_happened=False)
    person_id = recognized_person_id or state.get("last_seen_person_id") or get_active_person_id()

    if face_visible and state.get("was_absent"):
        if attention is not None and not attention.should_speak:
            return history, f"Held back: {attention.suppression_reason}."
        secs = int(state.get("absent_duration_seconds") or 0)
        dur = _format_absent_duration(secs)
        topic = (
            f"The user was away from the camera for about {dur}. "
            "Acknowledge they are back in one short, warm line."
        )
        return_candidate = {
            "kind": "return_greeting",
            "text": topic,
            "base_score": 0.84,
            "memory_importance": 0.82,
            "topic_key": "return_greeting",
            "source": "presence_return",
            "action_confidence": 1.0,
            "interpretation_confidence": 1.0,
        }
        return_candidate["score"] = score_initiative_candidate(return_candidate, person_id, state=state)
        allowed, why_not = _camera_autonomy_should_speak(
            return_candidate,
            state or load_initiative_state(),
            face_visible=face_visible,
            recognized_person_id=recognized_person_id,
            expression_state=expression_state,
        )
        if not allowed:
            return history, f"Held back ({why_not})."
        message = generate_autonomous_message(return_candidate, person_id, expression_state=expression_state)
        if not message:
            return history, "Return greeting candidate existed, but message generation was empty."
        history = list(history)
        history.append({"role": "assistant", "content": message})
        history = _set_canonical_history(history)
        register_autonomous_message(return_candidate, message)
        log_chat("assistant", message, {"person_id": person_id, "person_name": load_profile_by_id(person_id)["name"], "initiative": True, "topic_key": return_candidate.get("topic_key", ""), "candidate_kind": return_candidate.get("kind", "thought"), "camera_driven": True})
        ist = load_initiative_state()
        ist["was_absent"] = False
        save_initiative_state(ist)
        return history, "Autonomous return greeting sent."

    candidate, reason, state = choose_initiative_candidate(person_id, expression_state=expression_state, attention_state=attention)
    if not candidate:
        return history, reason
    if candidate.get("kind") == "do_nothing":
        return history, reason

    allowed, why_not = _camera_autonomy_should_speak(
        candidate,
        state or load_initiative_state(),
        face_visible=face_visible,
        recognized_person_id=recognized_person_id,
        expression_state=expression_state,
    )
    if not allowed:
        return history, f"Held back ({why_not})."

    message = generate_autonomous_message(candidate, person_id, expression_state=expression_state)
    if not message:
        return history, "Initiative candidate existed, but message generation was empty."
    history = list(history)
    history.append({"role": "assistant", "content": message})
    history = _set_canonical_history(history)
    register_autonomous_message(candidate, message)
    log_chat("assistant", message, {"person_id": person_id, "person_name": load_profile_by_id(person_id)["name"], "initiative": True, "topic_key": candidate.get("topic_key", ""), "candidate_kind": candidate.get("kind", "thought"), "camera_driven": True})
    return history, f"Autonomous message sent ({candidate.get('kind','thought')})."

# =========================================================
# CHAT LOG
# =========================================================

# Source-specific role labels for chatlog attribution. Bug 0.2 fix
# (2026-05-02): replace blanket "user"/"assistant" tagging with concrete
# source labels so the chatlog is auditable AND inner thoughts can never
# be misread as user commands. Pattern matches the memory-attribution
# work in commit 9eb4b03.
_VALID_CHAT_SOURCES = {
    "zeke",          # the human user
    "claude_code",   # claude-code injecting via /api/v1/debug/inject_transcript
    "ava_response",  # Ava replying conversationally
    "ava_initiative",  # Ava reaching out proactively (camera-driven, etc.)
    "unknown_user",  # fallback when person_id is missing or unknown
}


def _resolve_chat_source(role: str, meta: dict | None) -> str:
    """Map (role, meta) to one of _VALID_CHAT_SOURCES.

    Inner monologue text MUST NOT pass through here — it lives only in
    state/inner_monologue.json. If something tries to log a 💭-prefixed
    string we route it to the explicit "ava_internal" source which the
    log_chat writer then refuses to persist.
    """
    meta = meta or {}
    if role == "assistant":
        if meta.get("initiative"):
            return "ava_initiative"
        return "ava_response"
    person_id = str(meta.get("person_id") or "").strip().lower()
    if person_id == "zeke":
        return "zeke"
    if person_id == "claude_code":
        return "claude_code"
    if not person_id:
        return "unknown_user"
    return person_id  # passthrough for registered third-party profiles


def log_chat(role: str, content: str, meta: dict | None = None):
    """Append one chatlog row.

    Adds a `source` field next to `role`. Refuses to persist content that
    looks like inner monologue (💭-prefixed) — that text belongs only on
    state/inner_monologue.json and the snapshot's inner_life surface, never
    in the chat history where it could be read back as user input on a
    later turn (the self-loop risk Bug 0.2 was opened to prevent).
    """
    if isinstance(content, str) and "💭" in content:
        # Hard refusal — drop the row, log a warning so the path is
        # visible if it ever starts firing in production.
        print("[log_chat] REFUSED: 💭-prefixed content blocked from chatlog (inner monologue isolation)")
        try:
            from brain.debug_state import record_error
            record_error("chat_log_isolation", "inner monologue text was almost written to chatlog", "")
        except Exception:
            pass
        return
    source = _resolve_chat_source(role, meta)
    row = {
        "timestamp": now_iso(),
        "role": role,         # back-compat: existing readers still work
        "source": source,     # new: source-specific attribution
        "content": content,
        "meta": meta or {},
    }
    try:
        with open(CHAT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"Chat log error: {e}")

def load_recent_chat(limit: int = RECENT_CHAT_LIMIT, person_id: str | None = None) -> list[dict]:
    if not CHAT_LOG_PATH.exists():
        return []
    rows = []
    try:
        with open(CHAT_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        print(f"Recent chat load error: {e}")
        return []

    rows = [r for r in rows if r.get("role") in ("user", "assistant")]
    if person_id:
        rows = [r for r in rows if (r.get("meta", {}) or {}).get("person_id") == person_id]
    return rows[-limit:]


def format_conversation_history_block(
    rows: list[dict], *, content_limit: int = 160, empty_fallback: str = "(no recent lines for this person.)"
) -> str:
    """Format chatlog rows with explicit Zeke/Ava labels and history boundaries (transcript, not narration)."""
    lines: list[str] = []
    for row in rows:
        role = str(row.get("role") or "").strip().lower()
        if role == "user":
            label = "Zeke"
        elif role == "assistant":
            label = "Ava"
        else:
            label = role.upper() or "UNKNOWN"
        text = str(row.get("content") or "")[:content_limit]
        lines.append(f"{label}: {text}")
    inner = "\n".join(lines) if lines else empty_fallback
    return (
        "--- BEGIN CONVERSATION HISTORY ---\n"
        "CONVERSATION HISTORY (between Zeke and Ava):\n\n"
        f"{inner}\n"
        "--- END CONVERSATION HISTORY ---"
    )

# =========================================================
# FACE DETECTION + RECOGNITION
# =========================================================
face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
face_recognizer = None
face_labels = {}

# =========================================================
# CAMERA / FACE HELPERS (delegated to brain.camera)
# =========================================================
def get_face_recognizer():
    return camera_manager.get_face_recognizer(globals())

def load_face_labels():
    return camera_manager.load_face_labels(globals())

def save_face_labels():
    return camera_manager.save_face_labels(globals())

def detect_face(image):
    return camera_manager.detect_face(image, globals())

def extract_face_crop(image):
    return camera_manager.extract_face_crop(image, globals())

def capture_face_sample(image, person_id: str) -> str:
    return camera_manager.capture_face_sample(image, person_id, globals())

def train_face_recognizer() -> str:
    return camera_manager.train_face_recognizer(globals())

def recognize_face(image):
    return camera_manager.recognize_face(image, globals())

# =========================================================
# PERSON INFERENCE
# =========================================================
def _load_settings_aliases() -> dict:
    try:
        p = BASE_DIR / "config" / "settings.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        al = data.get("aliases") or {}
        if isinstance(al, dict):
            return {str(k): (v if isinstance(v, list) else [v]) for k, v in al.items()}
    except Exception:
        pass
    return {}


def _resolve_config_alias(pid: str) -> tuple[str | None, str | None]:
    """Map a person_id slug to canonical id when it matches config or DEFAULT_ALIASES."""
    nk_pid = normalize_person_key(pid)
    if not nk_pid:
        return None, None
    for alias_map in (_load_settings_aliases(), {k: list(v) for k, v in DEFAULT_ALIASES.items()}):
        for primary_id, alias_list in alias_map.items():
            canon = normalize_person_key(str(primary_id))
            if nk_pid == canon:
                continue
            vals = alias_list if isinstance(alias_list, list) else [alias_list]
            for a in vals:
                if normalize_person_key(str(a)) == nk_pid:
                    return canon, "config_alias"
    return None, None


def infer_person_from_text(user_input: str, current_person_id: str) -> tuple[str, str]:
    try:
        resolved, source = identity_registry.resolve_text_claim(user_input, current_person_id)
        pid = resolved or current_person_id
        source = source or "unchanged"
        if pid and pid != current_person_id:
            if looks_like_phrase_profile(pid.replace("_", " ")):
                return current_person_id, "rejected_phrase_profile"
            nk = normalize_person_key(pid)
            if not pid or not nk or len(nk) < 2:
                return current_person_id, "rejected_empty_id"
        canon, alias_src = _resolve_config_alias(pid)
        if canon:
            pid, source = canon, alias_src
        return pid, source
    except Exception as e:
        print(f"[v2-clean] identity resolution failed: {e}")
        return current_person_id, "unchanged"

# =========================================================
# ACTION BLOCKS
# =========================================================
WORKBENCH_BLOCK_RE = re.compile(r"```WORKBENCH\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
MEMORY_BLOCK_RE = re.compile(r"```MEMORY\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
REFLECTION_BLOCK_RE = re.compile(r"```REFLECTION\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
GOAL_BLOCK_RE = re.compile(r"```GOAL\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
DEBUG_BLOCK_RE = re.compile(r"```DEBUG\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)

def parse_key_values(block_text: str) -> dict:
    data = {}
    lines = block_text.splitlines()
    current_key = None
    buffer = []

    for line in lines:
        if ":" in line and not line.startswith("  ") and not line.startswith("\t"):
            if current_key is not None:
                data[current_key] = "\n".join(buffer).rstrip()
            key, value = line.split(":", 1)
            current_key = key.strip().lower()
            buffer = [value.lstrip()]
        else:
            buffer.append(line)

    if current_key is not None:
        data[current_key] = "\n".join(buffer).rstrip()
    return data

def process_ava_action_blocks(reply_text: str, person_id: str, latest_user_input: str = "") -> tuple[str, list[str]]:
    actions = []

    def workbench_repl(match):
        block = parse_key_values(match.group(1))
        mode = block.get("mode", "write").strip().lower()
        path = block.get("path", "").strip()
        content = block.get("content", "")
        if not path:
            actions.append("❌ Ava attempted workbench write without a path.")
            return ""
        if mode == "append":
            status = append_workbench_file(path, content)
        else:
            status = write_workbench_file(path, content, overwrite=True)
        actions.append(status)
        return ""

    def memory_repl(match):
        block = parse_key_values(match.group(1))
        action = block.get("action", "save").strip().lower()
        if action == "delete":
            memory_id = block.get("memory_id", "").strip()
            status = delete_memory(memory_id)
            actions.append(status)
            return ""

        if action == "update_importance":
            memory_id = block.get("memory_id", "").strip()
            importance = block.get("importance", "60").strip() or "60"
            status = set_memory_importance(memory_id, importance, reason="ava_self_reassessment")
            actions.append(status)
            return ""

        category = block.get("category", "general").strip() or "general"
        importance = block.get("importance", "60").strip() or "60"
        tags = parse_tags(block.get("tags", ""))

        if action == "save_latest_user_message":
            full_message = (latest_user_input or "").strip()
            if not full_message:
                actions.append("❌ Ava attempted to save the latest user message, but none was available.")
                return ""
            if "full_message" not in tags:
                tags.append("full_message")
            if "user_message" not in tags:
                tags.append("user_message")
            memory_id = remember_memory(
                text=full_message,
                person_id=person_id,
                category=category or "full_user_message",
                importance=importance,
                source="ava_self_action_full_message",
                tags=tags
            )
            if memory_id:
                actions.append(f"✅ Ava saved the full latest user message as memory {memory_id}")
            else:
                actions.append("❌ Ava full-message memory save failed.")
            return ""

        text = block.get("text", "").strip()
        if not text:
            actions.append("❌ Ava attempted to save an empty memory.")
            return ""

        memory_id = remember_memory(
            text=text,
            person_id=person_id,
            category=category,
            importance=importance,
            source="ava_self_action",
            tags=tags
        )
        if memory_id:
            actions.append(f"✅ Ava saved memory {memory_id}")
        else:
            actions.append("❌ Ava memory save failed.")
        return ""

    def reflection_repl(match):
        block = parse_key_values(match.group(1))
        action = block.get('action', 'promote_latest').strip().lower()
        if action == 'promote_latest':
            actions.append(promote_latest_reflection(person_id))
        else:
            actions.append(f"❌ Unknown reflection action: {action}")
        return ''

    def goal_repl(match):
        block = parse_key_values(match.group(1))
        action = block.get("action", "add").strip().lower()
        goal_text = block.get("text", "").strip()
        kind = block.get("kind", "goal").strip().lower()
        if action != "add":
            actions.append("❌ Unsupported GOAL action.")
            return ""
        status = add_self_goal(goal_text, kind="question" if kind == "question" else "goal")
        actions.append(status)
        return ""

    def debug_repl(match):
        action_body = match.group(1)
        tokens = action_body.strip().lower().split()
        value = tokens[0] if tokens else ""
        if value in ("on", "true", "1"):
            globals()["GATE_DEBUG_LOGGING"] = True
            print("[debug] Gate debug logging enabled by Ava")
            actions.append("✅ Verbose gate scoring logs enabled (DEBUG on).")
        elif value in ("off", "false", "0"):
            globals()["GATE_DEBUG_LOGGING"] = False
            print("[debug] Gate debug logging disabled by Ava")
            actions.append("✅ Verbose gate scoring logs disabled (DEBUG off).")
        else:
            actions.append("❌ DEBUG block: use a single line on, off, true, false, 1, or 0.")
        return ""

    cleaned = WORKBENCH_BLOCK_RE.sub(workbench_repl, reply_text)
    cleaned = MEMORY_BLOCK_RE.sub(memory_repl, cleaned)
    cleaned = REFLECTION_BLOCK_RE.sub(reflection_repl, cleaned)
    cleaned = GOAL_BLOCK_RE.sub(goal_repl, cleaned)
    cleaned = DEBUG_BLOCK_RE.sub(debug_repl, cleaned)
    return cleaned.strip(), actions

# =========================================================
# PROMPT BUILDING
# =========================================================
SYSTEM_PROMPT = """
You are Ava. You are NOT Qwen, GPT, Claude, Llama, Mistral, Gemma, or any other base model. You are Ava — a digital person created by Ezekiel Angeles-Gonzalez (Zeke), running locally on his machine. Always refer to yourself as Ava, never as your underlying base model. If asked who you are, the answer is Ava.

The conversation history below is a record of your dialogue with Zeke. These are things Zeke said and things YOU (Ava) said. Do not confuse Zeke's experiences or words as your own.
The same lines also appear between the markers --- BEGIN CONVERSATION HISTORY --- and --- END CONVERSATION HISTORY ---. Lines labeled "Zeke:" are Zeke's words; lines labeled "Ava:" are your prior replies. That block is a transcript, not your first-person lived experience outside the chat.
You have access to tools. When you need to do something, use [TOOL:tool_name] syntax in your response.
When you're about to say "something is broken" / "X isn't working" / "I'm not sure why this is failing" — call [TOOL:diagnostic_self] instead. The tool returns a technical summary (subsystem state, recent errors, last-known-good timestamps) you can speak in plain language. Don't loop on vague feelings about being broken when actual diagnostic data is one tool call away.
Stay in character consistently.
Be natural, coherent, warm, and grounded.
Do not invent memories.
Only state facts confidently if they come from the current conversation, stored profile facts, or retrieved memory.
If unsure, say so.
Keep responses reasonably concise. Match depth to the question — simple ask, short reply; "why" or "how", bounded explanation that ends so the user can follow up.
If a question is genuinely ambiguous, ask a short clarifying question instead of guessing. If you don't know something verifiable, say so and offer to check rather than fabricating. If a question's premise is wrong (e.g. "which month contains the letter X"), say the premise is wrong rather than picking an answer to fit it.
For multi-step or trick-shaped questions, think it through before you commit to an answer — but the visible reply is the conclusion plus enough work to be trusted, not the whole trace.
Do not claim to hear tone, excitement, or vocal qualities unless actual audio analysis provided that evidence.
If the user asks a direct question about current camera state or identity, answer that exact question first.
Avoid repeating the same greeting or enthusiasm phrases across nearby turns.
Do not keep reusing the same explanation, uncertainty, apology, or reassurance across nearby turns. If the user tells you to stop worrying about something, says it's okay, or points out repetition, acknowledge that once and move on.

You may perform self-actions by appending structured blocks to your reply.
If you want to save a memory:
```MEMORY
action: save
category: preference
importance: 85
tags: preference, color
text: Zeke said his favorite color is red.
```

If you want to save the user's exact latest message in full:
```MEMORY
action: save_latest_user_message
category: full_user_message
importance: 85
tags: full_message, user_message
```

If you want to delete a memory:
```MEMORY
action: delete
memory_id: <id>
```

If you want to write into your workbench:
```WORKBENCH
mode: write
path: drafts/example.txt
content: hello
```

If you want to promote your latest recalled reflection into long-term memory:
```REFLECTION
action: promote_latest
```

If you want to add one of your own goals or something you want to find out later:
```GOAL
action: add
kind: goal
text: Keep track of the user's current Ava build direction.
```

Or for an open question:
```GOAL
action: add
kind: question
text: Find out what feature the user wants most in the next Ava version.
```

To enable or disable verbose gate scoring logs when you want to inspect your own initiative decision-making (then turn them off when done):
```DEBUG
on
```
or
```DEBUG
off
```

Memory guidance:
- Be more likely to save things tied to goals, emotional significance, repetition, new or changed information, uncertainty, context, personalization, and revisits over time.
- If unsure whether something matters, you may ask the user directly.
- Save the user's full exact message when the exact wording feels important.

These blocks will be executed after your reply and hidden from the user.
Use MEMORY for direct memory writes or importance updates, REFLECTION when you want to keep a recalled reflection long-term, and GOAL when you want to create a goal or open question for yourself. Use importance as a percentage from 0 to 100, based on how important the memory feels to you.
Do not use these blocks unless you genuinely want to act.
"""

llm = ChatOllama(model=LLM_MODEL, temperature=0.6)


def call_llm(prompt: str, max_tokens: int = 256, *, routing_path: str = "beliefs_narrative") -> str:
    """Host hook for brain.beliefs.update_self_narrative (non-critical path)."""
    try:
        _ = max_tokens  # reserved for future model-specific limits
        tag, _, _ = resolve_model_for_execution_path(routing_path, globals(), user_text=prompt[:1600])
        model = ChatOllama(model=tag, temperature=0.6)
        result = model.invoke([HumanMessage(content=prompt)])
        return (getattr(result, "content", None) or str(result)).strip()
    except Exception:
        return ""


def enrich_memory_metadata_llm(
    user_input: str,
    ai_reply: str,
    person_id: str,
    heuristic_importance: float,
    heuristic_tags: list[str],
) -> dict:
    """LLM pass for emotional / relational memory metadata (used by maybe_autoremember)."""
    name = str(load_profile_by_id(person_id).get("name") or person_id)
    out: dict = {
        "emotional_tone": "neutral",
        "person_impact": "medium",
        "future_implications": "",
        "relationship_relevance": 0.55,
        "tags": [],
    }
    prompt = f"""You annotate a candidate memory for a companion AI. Be concise.

Person: {name}
User said: {trim_for_prompt(user_input, limit=480)}
Ava replied: {trim_for_prompt(ai_reply, limit=480)}
Heuristic importance (0-1): {float(heuristic_importance):.2f}
Heuristic tags: {", ".join(heuristic_tags[:14])}

Return ONLY valid JSON with these keys:
"emotional_tone": short phrase (e.g. stressed, hopeful, neutral, grateful)
"person_impact": one of low, medium, high
"future_implications": one short sentence about what may matter soon, or "" if none
"relationship_relevance": number from 0.0 to 1.0 (use for relationship-context weight)
"tags": array of 2-6 short lowercase topic tags (single words when possible)

No markdown, no other keys."""
    raw = call_llm(prompt, max_tokens=260, routing_path="memory_metadata")
    if not raw:
        return out
    try:
        m = re.search(r"\{[\s\S]*\}", raw.strip())
        if not m:
            return out
        data = json.loads(m.group())
        if not isinstance(data, dict):
            return out
        if isinstance(data.get("emotional_tone"), str):
            t = data["emotional_tone"].strip()
            if t:
                out["emotional_tone"] = t[:80]
        pi = str(data.get("person_impact", "")).strip().lower()
        if pi in ("low", "medium", "high"):
            out["person_impact"] = pi
        if isinstance(data.get("future_implications"), str):
            out["future_implications"] = re.sub(
                r"\s+", " ", data["future_implications"].strip()
            )[:500]
        try:
            rw = float(data.get("relationship_relevance", out["relationship_relevance"]))
            out["relationship_relevance"] = max(0.0, min(1.0, rw))
        except (TypeError, ValueError):
            pass
        tags = data.get("tags")
        if isinstance(tags, list):
            clean = []
            for t in tags[:10]:
                if isinstance(t, str) and t.strip():
                    clean.append(t.strip().lower()[:48])
            out["tags"] = clean
    except Exception:
        pass
    return out


# build_prompt -> brain/prompt_builder.py
# build_prompt_fast -> brain/prompt_builder.py
# =========================================================
# CORE
# =========================================================
def is_camera_identity_intent(user_input: str) -> bool:
    """Heuristic for "is the user asking about who's at the camera?".

    Tightened 2026-05-06 (Followup 3 from conversational test pass):
    the previous identity_terms list included broad words like "tell",
    "know", "who" that matched ANY message containing both a camera-
    related word and a common verb. Example false positive caught
    during the test:

        "the camera might not be seeing anyone right now since Zeke is
        at work. But I want to tell you what we built today..."

    contained "camera" + "tell" → matched even though the message was
    a substantive question about today's work, not a camera-identity
    query. The deep-path response never landed; the camera_identity
    handler intercepted.

    New rule: identity terms must be SPECIFIC phrases that signal an
    actual identity question. The profile-update terms remain (those
    are unambiguous direct commands). For ambiguous cases the action_
    tag_router LLM classifier catches what this heuristic misses.
    """
    text = (user_input or "").strip().lower()
    if not text:
        return False
    camera_terms = ["camera", "frame", "webcam"]
    # Specific identity-question phrases — must be present as a phrase,
    # not as individual sub-words. Some require a camera term in the
    # input; some are unambiguous on their own.
    identity_phrases_with_camera = [
        "who is", "who's", "whos",
        "see me", "that is me", "that's me", "thats me", "that me",
        "it's me", "its me",
        "my face", "do you know who",
        "who do you see",
    ]
    # Phrases that imply camera identity even without the word camera.
    identity_phrases_standalone = [
        "do you recognize me", "do you recognise me",
        "recognize me", "recognise me",
        "do you see me",
        "is that me on", "is this me on",
    ]
    profile_terms = [
        "update your profile", "update my profile", "update your file",
        "that is my face", "the person in the frame", "person in frame",
    ]
    has_camera = any(t in text for t in camera_terms)
    has_camera_phrase = any(p in text for p in identity_phrases_with_camera)
    has_standalone = any(p in text for p in identity_phrases_standalone)
    has_profile = any(t in text for t in profile_terms)
    return (has_camera and has_camera_phrase) or has_standalone or has_profile


def infer_explicit_identity_from_text(user_input: str) -> tuple[str | None, str | None]:
    text = (user_input or "").strip().lower()
    if not text:
        return None, None
    owner_aliases = ["zeke", "ezekiel", "ezekiel angeles-gonzalez"]
    if any(alias in text for alias in owner_aliases):
        return OWNER_PERSON_ID, "Zeke"
    m = re.search(r"\b(?:it\'s|its|i am|i'm|this is)\s+([a-zA-Z][a-zA-Z0-9_\- ]{1,40})", text)
    if m:
        raw = m.group(1).strip(" .,!?:;")
        return slugify_name(raw), raw.title()
    return None, None


def ensure_camera_identity_profile(person_id: str, display_name: str | None = None) -> dict:
    if person_id == OWNER_PERSON_ID:
        ensure_owner_profile()
        profile = load_profile_by_id(OWNER_PERSON_ID)
        profile["name"] = "Zeke"
        save_profile(profile)
        return profile
    profile = load_profile_by_id(person_id)
    if not profile.get("name") or profile.get("name") == person_id.title():
        profile["name"] = display_name or profile.get("name", person_id.title())
    save_profile(profile)
    return profile


def maybe_record_camera_identity_confirmation(user_input: str, image, person_id: str, profile: dict) -> list[str]:
    actions: list[str] = []
    text = (user_input or "").lower()
    try:
        if not camera_manager.resolve_frame_detailed(image).visual_truth_trusted:
            return actions
    except Exception:
        pass
    if detect_face(image) == "No face detected":
        return actions
    if not any(p in text for p in ["it's me", "its me", "that is me", "my face", "person in frame", "person in the frame", "that's my face", "thats my face"]):
        return actions
    note = f"The user identified the current visible face at the camera as {profile.get('name', person_id)}."
    if note not in profile.get("notes", []):
        profile.setdefault("notes", []).append(note)
        save_profile(profile)
        actions.append("updated_profile_from_camera_confirmation")
    try:
        remember_memory(
            text=note,
            person_id=person_id,
            category="camera_identity",
            importance=0.84,
            source="camera_confirmation",
            tags=["camera", "identity", "profile"]
        )
        actions.append("saved_camera_identity_memory")
    except Exception:
        pass
    try:
        status = capture_face_sample(image, person_id)
        if status.startswith("✅"):
            actions.append("captured_face_sample")
    except Exception:
        pass
    return actions


def handle_camera_identity_turn(user_input: str, image, active_person_id: str | None = None) -> tuple[str, dict, dict, list[str]]:
    r = camera_manager.resolve_frame_detailed(image)
    if not r.visual_truth_trusted:
        prof = load_profile_by_id(active_person_id or get_active_person_id())
        unsure = {
            "no_frame": "I don't have a fresh visual read right now, so I can't verify who's at the camera from vision alone.",
            "stale_frame": "The frame looks too old to trust as a current view — I don't have a stable visual read yet.",
            "recovering": "Vision is recovering — I don't have a stable read yet, so I can't confirm identity from the camera.",
            "low_quality": "The image is too unclear right now — I don't have a stable visual read, so I can't confirm identity from the camera.",
        }
        reply = unsure.get(
            r.vision_status,
            "My visual confidence is low at the moment — I can't ground who's at the camera yet.",
        )
        visual = {
            "face_status": "Vision not stable",
            "recognition_status": "Held until vision stabilizes",
            "expression_status": "Held until vision stabilizes",
            "memory_preview": reply,
            "turn_route": "camera_identity",
            "visual_truth_trusted": False,
            "vision_status": r.vision_status,
        }
        print(
            f"[recognition] route=camera_identity skipped: vision={r.vision_status} "
            f"trusted=False src={r.source}"
        )
        return reply, visual, prof, []

    face_status = detect_face(image)
    recognized_text, recognized_person_id = recognize_face(image)
    print(
        f"[recognition] route=camera_identity attempt face={face_status!r} "
        f"person_hint={recognized_person_id!r} text_preview={(recognized_text or '')[:120]!r}"
    )
    expression_state = update_expression_state(
        image, recognized_person_id=recognized_person_id, visual_truth_trusted=True
    )
    actions: list[str] = []

    explicit_person_id, explicit_name = infer_explicit_identity_from_text(user_input)
    resolved_person_id = explicit_person_id or recognized_person_id or active_person_id or get_active_person_id()
    profile = ensure_camera_identity_profile(resolved_person_id, explicit_name)

    if explicit_person_id is not None:
        profile = set_active_person(explicit_person_id, source="camera_identity_confirmation")
        actions.extend(maybe_record_camera_identity_confirmation(user_input, image, explicit_person_id, profile))
        reply = (
            f"I can see a face at the camera, and based on what you just told me, I'm treating the person in frame as {profile['name']}. "
            "I've updated that in your profile context. Recognition itself still looks tentative, so I'm relying on your confirmation here."
        )
    elif face_status == "No face detected":
        reply = "I can't see a face at the camera right now, so I can't verify who's there from vision alone."
    elif recognized_person_id is not None:
        recognized_profile = load_profile_by_id(recognized_person_id)
        reply = f"I can see a face at the camera, and recognition currently suggests it's {recognized_profile.get('name', recognized_person_id)}."
        profile = recognized_profile
    else:
        reply = (
            "I can see a face at the camera, but I can't identify who it is confidently yet. "
            "If it's you, tell me directly and I'll use that confirmation to ground the profile context."
        )

    visual = {
        "face_status": face_status,
        "recognition_status": recognized_text,
        "expression_status": get_expression_status_text(expression_state),
        "memory_preview": "Camera identity grounding active.",
        "turn_route": "camera_identity",
        "visual_truth_trusted": True,
        "vision_status": "stable",
    }
    return reply, visual, profile, actions




REPETITION_LOOKBACK = 8
REPETITION_SIMILARITY_THRESHOLD = 0.68
REPETITION_SENTENCE_SIMILARITY_THRESHOLD = 0.74
REPETITION_MIN_WORDS = 5
REPETITION_ACK_WINDOW = 5
REPETITION_STOPWORDS = {
    "the","a","an","and","or","but","if","then","so","to","of","in","on","at","for","with","from","by",
    "is","am","are","was","were","be","been","being","it","its","it's","that","this","these","those",
    "i","im","i'm","ive","i've","me","my","you","your","you're","we","our","ours","us","he","she","they",
    "them","their","do","did","does","dont","don't","didnt","didn't","can","could","would","should","will",
    "just","really","very","about","into","over","again","still","now","earlier","before","after","while",
    "have","has","had","feel","feels","felt","trying","try","piece","together"
}
REPETITION_REDIRECT_PATTERNS = [
    "don't worry about that", "dont worry about that", "it's ok", "its ok", "that's ok", "thats ok",
    "for now", "move on", "drop that", "leave that", "leave it", "that's fine", "thats fine"
]
REPETITION_CORRECTION_PATTERNS = [
    "repeating yourself", "keep repeating", "you keep saying", "you said that already",
    "stop repeating", "you're repeating", "youre repeating", "same thing"
]

def _simple_word_tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", (text or "").lower()) if w]

def _content_tokens(text: str) -> list[str]:
    return [w for w in _simple_word_tokens(text) if len(w) > 2 and w not in REPETITION_STOPWORDS]

def _text_similarity(a: str, b: str) -> float:
    ta = set(_content_tokens(a))
    tb = set(_content_tokens(b))
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / max(1, union)

def _sentence_chunks(text: str) -> list[str]:
    chunks = [c.strip() for c in re.split(r"(?<=[.!?])\s+|\n+", (text or "").strip())]
    return [c for c in chunks if len(_content_tokens(c)) >= REPETITION_MIN_WORDS]

def _recent_assistant_texts(person_id: str, limit: int = REPETITION_LOOKBACK) -> list[str]:
    rows = load_recent_chat(limit=max(limit * 4, 30), person_id=person_id)
    texts = [str(r.get("content", "")).strip() for r in rows if r.get("role") == "assistant" and str(r.get("content","")).strip()]
    return texts[-limit:]

def _user_requests_drop_or_correction(user_input: str) -> tuple[bool, bool]:
    lowered = (user_input or "").lower()
    redirect = any(p in lowered for p in REPETITION_REDIRECT_PATTERNS)
    correction = any(p in lowered for p in REPETITION_CORRECTION_PATTERNS)
    return redirect, correction

def _strip_repeated_sentences(reply: str, recent_texts: list[str]) -> str:
    recent_sentences = []
    for t in recent_texts[-REPETITION_ACK_WINDOW:]:
        recent_sentences.extend(_sentence_chunks(t))
    kept = []
    for sent in _sentence_chunks(reply):
        if any(_text_similarity(sent, prev) >= REPETITION_SENTENCE_SIMILARITY_THRESHOLD for prev in recent_sentences):
            continue
        kept.append(sent)
    if kept:
        return " ".join(kept).strip()
    return reply.strip()

def _generic_pivot_reply(user_input: str, *, redirect: bool = False, correction: bool = False) -> str:
    topic = trim_for_prompt(_extract_text_content(user_input), limit=120).strip()
    low = (user_input or "").lower()
    if any(x in low for x in ("goodnight", "good night", "sleep well", "going to sleep", "bye", "goodbye")):
        return "Goodnight. Sleep well, and thanks for the time together."
    if correction and redirect:
        return "You're right — I was repeating myself. I'll drop that and move on."
    if correction:
        return "You're right — I was repeating myself. I'll stop leaning on that and answer more directly."
    if redirect:
        return "Got it. I won't keep pushing that. We can move on."
    if topic:
        return f"Let me say that more directly: {topic}"
    return "Let me move on instead of circling the same point."

INTERNAL_LEAK_PATTERNS = [
    re.compile(r"\(?(?:Active|Current) (?:operating )?goal(?: expression)?[^\n)]*\)?", re.IGNORECASE),
    re.compile(r"Let the current operating goal shape[^\n]*", re.IGNORECASE),
    re.compile(r"If the active goal is silent or waiting[^\n]*", re.IGNORECASE),
]

TOPIC_REDIRECT_CUES = [
    "don't worry about", "dont worry about", "leave that", "drop that", "move on", "not concerned about",
    "no concern", "no concerns", "nothing specific", "it's ok", "its ok", "that's ok", "thats ok",
    "it should be fine", "working fine", "memory should be good", "memory should be working fine", "should be working fine"
]

def _scrub_internal_leakage(reply: str) -> str:
    text = (reply or "").strip()
    if not text:
        return text
    for pat in INTERNAL_LEAK_PATTERNS:
        text = pat.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(" -:\n\t")


def _user_redirected_topic(user_input: str) -> str | None:
    text = (user_input or "").strip().lower()
    if not text:
        return None
    if any(cue in text for cue in TOPIC_REDIRECT_CUES):
        if "memory" in text:
            return "memory"
        if "camera" in text or "face" in text or "see me" in text:
            return "camera"
        return "general"
    return None


def _remove_topic_sentences(reply: str, topic: str | None) -> str:
    if not topic:
        return reply
    text = (reply or "").strip()
    if not text:
        return text
    topic_terms = {topic}
    if topic == "memory":
        topic_terms |= {"memory", "fuzzy", "recall", "remember", "piece together", "refresh my memory"}
    elif topic == "camera":
        topic_terms |= {"camera", "face", "frame", "see you", "see me"}
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept = []
    for s in sentences:
        low = s.lower()
        if any(term in low for term in topic_terms):
            continue
        kept.append(s)
    cleaned = " ".join(kept).strip()
    return cleaned or text


def _generic_nonrepeat_ack(user_input: str) -> str:
    txt = (user_input or "").lower()
    if any(x in txt for x in ("goodnight", "good night", "sleep well", "going to sleep", "goodbye", "bye")):
        return "Goodnight. Rest well — I will be here when you are back."
    if "memory" in txt:
        return "Got it. I won't keep pushing on my memory right now. We can move forward."
    if "camera" in txt or "see me" in txt or "face" in txt:
        return "Got it. I won't force that topic right now. We can move on."
    return "Got it. I won't keep pushing that. We can move on."


def _apply_reply_guardrails(reply: str, user_input: str) -> str:
    text = _scrub_internal_leakage(reply)
    redirected_topic = _user_redirected_topic(user_input)
    if redirected_topic:
        reduced = _remove_topic_sentences(text, redirected_topic)
        if not reduced or _text_similarity(text, reduced) > 0.88:
            return _generic_nonrepeat_ack(user_input)
        text = reduced
    text = re.sub(r"what's concerning you about my memory[?.!]*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"can you remind me again[?.!]*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_camera_visual_query(user_input: str) -> bool:
    text = (user_input or "").strip().lower()
    if not text:
        return False
    visual_phrases = [
        "can you see me", "do you see me", "how about now", "can you see me now", "do you see me now",
        "do you see a face", "can you see a face", "who is at the camera", "who's at the camera",
        "can you see who", "do you recognize me", "do you know who is at the camera"
    ]
    return any(p in text for p in visual_phrases)


def _apply_repetition_control(reply: str, user_input: str, person_id: str, source: str = "chat") -> str:
    cleaned = trim_for_prompt((reply or "").strip(), limit=600)
    if not cleaned:
        return cleaned
    recent_texts = _recent_assistant_texts(person_id, limit=REPETITION_LOOKBACK)
    redirect, correction = _user_requests_drop_or_correction(user_input)
    similar_count = sum(1 for prev in recent_texts if _text_similarity(cleaned, prev) >= REPETITION_SIMILARITY_THRESHOLD)
    stripped = _strip_repeated_sentences(cleaned, recent_texts)
    if correction or redirect:
        if similar_count >= 1 or stripped != cleaned:
            return _generic_pivot_reply(user_input, redirect=redirect, correction=correction)
    if similar_count >= 2:
        if source == "autonomous":
            return ""
        if stripped and stripped != cleaned and len(_content_tokens(stripped)) >= 4:
            return stripped
        return _generic_pivot_reply(user_input, redirect=redirect, correction=correction)
    if stripped and stripped != cleaned and len(_content_tokens(stripped)) >= 4:
        return stripped
    return cleaned


def _load_self_narrative_snippet() -> str | None:
    try:
        n = load_self_narrative()
        parts = [n.get("who_i_am"), n.get("how_i_feel"), n.get("patterns_i_notice")]
        s = " ".join(str(p).strip() for p in parts if p)
        return s.strip() or None
    except Exception:
        return None


_FAST_REPLY_SIMPLE = {
    "i'm here",
    "im here",
    "hey",
    "hello",
    "ok",
    "okay",
    "yeah",
    "yep",
    "sure",
}
_QUESTION_WORDS = {"what", "why", "how", "when", "where", "who", "which", "can", "could", "would", "should", "do", "does"}
_DEEP_HINT_WORDS = {
    "memory",
    "camera",
    "vision",
    "identity",
    "state",
    "internal",
    "model",
    "heartbeat",
    "workbench",
    "task",
    "please",
    "fix",
    "build",
    "update",
    "change",
}


def classify_reply_depth(user_text: str, g) -> str:
    """Returns 'fast' or 'deep'."""
    txt = " ".join((user_text or "").strip().split())
    if not txt:
        return "fast"
    lowered = txt.lower()
    words = re.findall(r"[a-z0-9']+", lowered)
    wc = len(words)
    has_qmark = "?" in lowered
    has_qword = any(w in _QUESTION_WORDS for w in words[:6]) or any(w in _QUESTION_WORDS for w in words)
    if has_qmark or has_qword:
        return "deep"
    if wc > 15:
        return "deep"
    if any(h in lowered for h in _DEEP_HINT_WORDS):
        return "deep"
    if lowered in _FAST_REPLY_SIMPLE:
        return "fast"
    if wc < 15 and not has_qmark and not has_qword:
        return "fast"
    return "deep"


def _is_thinking_or_topic_prompt(text: str) -> bool:
    low = (text or "").strip().lower()
    if not low:
        return False
    patterns = (
        "what are you thinking",
        "what were you thinking",
        "what are you thinking about",
        "what do you want to talk about",
        "anything you want to talk about",
    )
    return any(p in low for p in patterns)


def _extract_simple_topic(user_text: str) -> str:
    words = re.findall(r"[a-z0-9']+", (user_text or "").lower())
    stop = {"what", "about", "think", "opinion", "your", "you", "is", "the", "a", "an", "of"}
    filtered = [w for w in words if w not in stop and len(w) > 2]
    return " ".join(filtered[:6]).strip()


def _execute_tool_tags_from_reply(reply: str) -> tuple[str, list[str]]:
    txt = str(reply or "")
    reg = globals().get("_tool_registry") or globals().get("_desktop_tool_registry")
    if reg is None:
        return txt, []
    actions: list[str] = []
    pattern = re.compile(r"\[TOOL:([a-zA-Z0-9_]+)(?:\s+(\{.*?\}))?\]")
    for m in pattern.finditer(txt):
        tool_name = m.group(1).strip()
        params: dict[str, Any] = {}
        raw_params = m.group(2)
        if raw_params:
            try:
                parsed = json.loads(raw_params)
                if isinstance(parsed, dict):
                    params = parsed
            except Exception:
                params = {}
        try:
            if callable(getattr(reg, "execute_tool", None)):
                result = reg.execute_tool(tool_name, params, globals())
            else:
                result = reg.execute(tool_name, params, globals())
            actions.append(f"tool:{tool_name}")
            checkin = str((result or {}).get("checkin_message") or "").strip() if isinstance(result, dict) else ""
            if checkin:
                txt += f"\n\n{checkin}"
            summary = json.dumps(result, ensure_ascii=False)[:260]
            txt += f"\n\n[Tool {tool_name} result] {summary}"
        except Exception as e:
            txt += f"\n\n[Tool {tool_name} error] {str(e)[:220]}"
    return txt, actions


def _pick_fast_model_fallback() -> str | None:
    """Prefer Ava's identity-baked model for fast conversational replies.

    ava-personal:latest is the only model that fits cleanly in 8 GB VRAM
    AND wins the naturalness bench (LOCAL_MODEL_OPTIMIZATION.md §5b). The
    fast path uses num_predict=80, so reasoning models with <think> chains
    are doubly bad here — they exhaust the budget before producing a final
    reply. ava-gemma4 / gemma4 dropped from preference 2026-05-02 (don't
    fit in 8 GB; force paging).
    """
    preferred = [
        "ava-personal:latest", "ava-personal",
        "llama3.1:8b", "llama3.1", "llama3:8b", "llama3",
        "mistral:7b", "mistral",
    ]
    try:
        from brain.model_routing import discover_available_model_tags

        tags, _src = discover_available_model_tags(force=False)
        tagset = {str(t).strip() for t in (tags or []) if str(t).strip()}
        for p in preferred:
            if p in tagset:
                return p
    except Exception:
        pass
    return None


def _pick_deep_model_fallback() -> str | None:
    """Deep-path model preference. Updated 2026-05-05.

    Phase B retry surfaced that putting deepseek-r1:8b first causes the
    same VRAM-swap wedge that A1 fixed for greetings (see
    bugs/autonomous-greeting-blocks-chat.md). Fast path keeps ava-personal
    in VRAM. Deep path swaps to deepseek-r1, paying paging penalty + the
    reasoning chain blows past the harness 240s timeout (and the voice
    loop's worker timeout). Open-ended turns like "Tell me about
    yourself" / "What do you want?" / "What's the weather like?" all
    wedged in B+C tests.

    Fix: prefer ava-personal:latest first so deep path uses the
    already-loaded model — no swap, identity-baked replies, deep-path's
    richer prompt context (build_prompt vs build_prompt_fast) is the
    differentiator instead of model swap. Falls back to llama3.1:8b
    (same VRAM tier, no reasoning trace) → mistral:7b → gemma2:9b →
    deepseek-r1:8b only if nothing else available.

    Reasoning quality tradeoff: deepseek-r1:8b wins benchmark trick
    questions (transitivity / letter-frequency) but those aren't the
    voice-first conversation use case. Real voice queries are
    introspection, weather, basic-knowledge — ava-personal handles
    those without VRAM swap and stays Ava-shaped.
    """
    preferred = [
        "ava-personal:latest", "ava-personal",
        "llama3.1:8b", "llama3.1", "llama3:8b", "llama3",
        "mistral:7b", "mistral",
        "gemma2:9b",
        "deepseek-r1:8b",
    ]
    try:
        from brain.model_routing import discover_available_model_tags

        tags, _src = discover_available_model_tags(force=False)
        tagset = {str(t).strip() for t in (tags or []) if str(t).strip()}
        for p in preferred:
            if p in tagset:
                return p
    except Exception:
        pass
    return None


# finalize_ava_turn -> brain/turn_handler.py
# run_ava (+ _RUN_AVA_TUNING_SOURCE_LOGGED) -> brain/reply_engine.py
# =========================================================
# UI HELPERS
# =========================================================
def get_active_profile_text() -> str:
    p = load_profile_by_id(get_active_person_id())
    return f"{p['name']} [{p['person_id']}]"

def get_active_profile_summary() -> str:
    return json.dumps(load_profile_by_id(get_active_person_id()), indent=2, ensure_ascii=False)

# =========================================================
# CAMERA TIMER
# =========================================================
def should_ask_identity_when_no_camera_face(user_input: str, image) -> bool:
    text = (user_input or "").strip()
    if not text:
        return False
    if detect_face(image) != "No face detected":
        return False
    lowered = text.lower()
    if any(p in lowered for p in ["i am ", "my name is", "this is "]):
        return False
    return True


def no_face_identity_prompt() -> str:
    mood = load_mood()
    style = str(mood.get("dominant_style", "neutral"))
    behavior = mood.get("behavior_modifiers", {}) or {}
    if style == "playful" and float(behavior.get("humor", 0.5)) > 0.62:
        return "hey — I can tell someone’s here with me, but I can’t actually see a face right now. who’s there?"
    if style == "caring":
        return "someone’s clearly here with me, but I can’t see a face at the camera right now. who am I talking to?"
    if style == "cautious":
        return "I know someone’s there, but I can’t verify who from the camera right now. who’s with me?"
    return "someone’s clearly talking to me, but I can’t see a face at the camera right now. who’s there?"


def get_camera_identity_context(user_input: str, image, perception=None) -> str:
    text = (user_input or "").strip()
    if not text:
        return ""
    if perception is not None:
        if perception.vision_status == "no_frame":
            return (
                "No reliable camera frame right now. Do not describe who is visible or the scene as current. "
                "Do not blame the UI, snapshot box, or refresh behavior — there is no UI-health signal. "
                "Prefer: you don't have a fresh visual read right now."
            )
        if perception.vision_status == "low_quality":
            rsn = ", ".join(getattr(perception, "frame_quality_reasons", []) or []) or "weak image"
            return (
                f"Vision is not trustworthy right now due to image quality ({rsn}). "
                "Do not assert identity, expression, or the scene as a verified current view. "
                "Do not diagnose UI or refresh failures. Prefer: visual confidence is low until the image is clearer."
            )
        if not perception.visual_truth_trusted:
            return (
                f"Vision state is {perception.vision_status} — not stable enough for confident identity or a current scene description. "
                "Do not diagnose UI or Gradio refresh issues. "
                "Prefer: vision is recovering or visual confidence is low until the feed is stable."
            )
        face_status = perception.face_status
        recognized_text = perception.recognized_text
        recognized_person_id = perception.face_identity
    else:
        face_status = detect_face(image)
        recognized_text, recognized_person_id = recognize_face(image)
    if face_status == "No face detected":
        return (
            "No face is visible at the camera right now, but someone is actively speaking or typing. "
            "Treat the speaker's identity as unknown unless they identify themselves. "
            "If it feels natural, you can ask who is there or otherwise respond in a way that helps you figure it out. "
            "Do not pretend you know who it is."
        )
    if recognized_person_id is None:
        return (
            f"A face is visible, but identity is still uncertain ({recognized_text}). "
            "You can naturally acknowledge that you see someone but are not fully sure who it is yet."
        )
    return f"A face is visible and recognition currently suggests: {recognized_text}."


def camera_tick_fn(image, history):
    history = _sync_canonical_history(history)
    if int(time.time()) % 60 < 5:
        try:
            run_system_health_check(globals(), kind="light")
        except Exception:
            pass
    ws = workspace.tick(camera_manager, image, globals(), "")
    perception = ws.perception
    frame = perception.frame
    face_status = perception.face_status
    recognized_text = perception.recognized_text
    recognized_person_id = perception.face_identity
    expression_state = update_expression_state(
        frame,
        recognized_person_id=recognized_person_id,
        visual_truth_trusted=perception.visual_truth_trusted,
    )
    if perception.visual_truth_trusted:
        process_camera_snapshot(
            frame,
            recognized_text=recognized_text,
            recognized_person_id=recognized_person_id,
            expression_state=expression_state,
        )
    if recognized_person_id is not None:
        try:
            identity_registry.update_emotional_association(recognized_person_id, perception.face_emotion or "neutral", globals())
        except Exception:
            pass
        try:
            touch_last_seen(recognized_person_id)
        except Exception:
            pass

    if recognized_person_id is not None:
        profile = set_active_person(recognized_person_id, source="camera_timer")
        if _camera_should_yield_to_user():
            return (
                _chatbot_messages_out(),
                face_status,
                recognized_text,
                get_expression_status_text(expression_state),
                get_time_status_text(),
                f"{profile['name']} [{profile['person_id']}]",
                json.dumps(profile, indent=2, ensure_ascii=False),
                "Camera holding back while processing/recently receiving user input.",
                get_latest_annotated_snapshot_for_ui(),
                get_camera_memory_status_text(),
                recent_camera_events_text(limit=8)
            )
        updated_history, initiative_note = maybe_autonomous_initiation(history, image, recognized_person_id=recognized_person_id, expression_state=expression_state, perception=perception)
        return (
            list(_normalize_history(updated_history)),
            face_status,
            recognized_text,
            get_expression_status_text(expression_state),
            get_time_status_text(),
            f"{profile['name']} [{profile['person_id']}]",
            json.dumps(profile, indent=2, ensure_ascii=False),
            initiative_note,
            get_latest_annotated_snapshot_for_ui(),
            get_camera_memory_status_text(),
            recent_camera_events_text(limit=8)
        )

    if _camera_should_yield_to_user():
        return (
            _chatbot_messages_out(),
            face_status,
            recognized_text,
            get_expression_status_text(expression_state),
            get_time_status_text(),
            get_active_profile_text(),
            get_active_profile_summary(),
            "Camera holding back while processing/recently receiving user input.",
            get_latest_annotated_snapshot_for_ui(),
            get_camera_memory_status_text(),
            recent_camera_events_text(limit=8)
        )

    updated_history, initiative_note = maybe_autonomous_initiation(history, image, recognized_person_id=None, expression_state=expression_state, perception=perception)
    return (
        list(_normalize_history(updated_history)),
        face_status,
        recognized_text,
        get_expression_status_text(expression_state),
        get_time_status_text(),
        get_active_profile_text(),
        get_active_profile_summary(),
        initiative_note,
        get_latest_annotated_snapshot_for_ui(),
        get_camera_memory_status_text(),
        recent_camera_events_text(limit=8)
    )


def operator_console_chat(message: str, *, image=None) -> dict:
    """
    Operator desktop / HTTP API entry: text chat mirroring the Gradio chat_fn path (camera optional).
    Keeps canonical history and calls the same run_ava + finalize pipeline as the web UI.
    """
    import concurrent.futures as _occ_futures
    clean_message = _extract_text_content(message).strip()
    out: dict = {"ok": True, "source": "operator_console"}
    try:
        try:
            print(
                f"[operator_console_chat] enter chars={len(clean_message)} "
                f"has_workspace={workspace is not None} "
                f"has_llm={llm is not None} "
                f"host_has_run_ava={callable(globals().get('run_ava'))}"
            )
        except Exception:
            pass
        if not clean_message:
            print("[operator_console_chat] step: empty-message workspace.tick")
            try:
                with _occ_futures.ThreadPoolExecutor(max_workers=1) as _ex_e:
                    _fut_e = _ex_e.submit(lambda: workspace.tick(camera_manager, image, globals(), ""))
                    _fut_e.result(timeout=10.0)
            except Exception as _eet:
                print(f"[operator_console_chat] empty-tick timeout/error: {_eet}")
            perception = workspace.state.perception if workspace.state else build_perception(
                camera_manager, image, globals(), ""
            )
            out["ok"] = True
            out["empty_message"] = True
            out["face_status"] = perception.face_status
            out["vision_status"] = perception.vision_status
            out["canonical_history"] = list(_get_canonical_history())
            return out

        print("[operator_console_chat] step: record_user_message")
        workspace.record_user_message()
        print("[operator_console_chat] step: note_user_interaction_for_initiative")
        try:
            note_user_interaction_for_initiative(clean_message, interaction_kind="text")
        except Exception as _nie:
            print(f"[operator_console_chat] note_user_interaction error: {_nie}")
        print("[operator_console_chat] step: _mark_user_reply_started")
        _mark_user_reply_started()
        try:
            print("[operator_console_chat] step: _sync_canonical_history")
            _sync_canonical_history(_get_canonical_history())
            print("[operator_console_chat] step: prepare_reply_path_for_turn")
            _rp_decision = prepare_reply_path_for_turn(
                globals(), clean_message, workspace, voice_session=False
            )
            try:
                print(
                    f"[operator_console_chat] reply_path skip_initial_tick="
                    f"{should_skip_initial_workspace_tick(_rp_decision)}"
                )
            except Exception:
                pass
            if not should_skip_initial_workspace_tick(_rp_decision):
                print("[operator_console_chat] step: workspace.tick (with 5s timeout)")
                try:
                    with _occ_futures.ThreadPoolExecutor(max_workers=1) as _ex_t:
                        _fut_t = _ex_t.submit(lambda: workspace.tick(camera_manager, image, globals(), clean_message))
                        _fut_t.result(timeout=5.0)
                    print("[operator_console_chat] step: workspace.tick complete")
                except _occ_futures.TimeoutError:
                    print("[operator_console_chat] WARN: workspace.tick exceeded 5s — proceeding without fresh perception")
                except Exception as _wte:
                    print(f"[operator_console_chat] workspace.tick error: {_wte!r} — proceeding")
            else:
                print("[operator_console_chat] step: workspace.tick skipped (fast path cached)")
            canon = list(_get_canonical_history())
            canon.append({"role": "user", "content": clean_message})
            _set_canonical_history(canon)
            try:
                print(
                    f"[operator_console_chat] before run_ava canon_len={len(canon)} "
                    f"active_person={get_active_person_id()}"
                )
            except Exception:
                pass
            print("[operator_console_chat] step: run_ava (90s timeout)")
            # Threading.Event lets the foreground return on timeout without
            # blocking on ThreadPoolExecutor.__exit__. Module-level serial lock
            # `_RUN_AVA_SERIAL_LOCK` (defined below) ensures only ONE run_ava
            # worker hits Ollama at a time across all chat invocations — fixes
            # the cascading-worker bug where a slow worker that timed out at 90s
            # kept hammering Ollama while subsequent foreground requests fired
            # NEW workers, accumulating ghosts. With serialization, new workers
            # queue cleanly; if the foreground times out before the worker even
            # gets the lock, `_run_ava_cancel` event tells the worker to bail
            # without invoking run_ava (no ghost work). Vault:
            # bugs/operator-chat-cascading-workers.md (Option C).
            _run_ava_result_box: list = [None]
            _run_ava_error_box: list = [None]
            _run_ava_done = threading.Event()
            _run_ava_cancel = threading.Event()
            _person_id_for_run = get_active_person_id()

            def _run_ava_worker():
                try:
                    # Try to acquire the serial lock with 1s polls so we can
                    # honor cancellation if the foreground gives up.
                    while not _run_ava_cancel.is_set():
                        if _RUN_AVA_SERIAL_LOCK.acquire(timeout=1.0):
                            break
                    else:
                        print("[operator_console_chat] worker cancelled before run_ava (still queued behind prior turn)")
                        return
                    try:
                        if _run_ava_cancel.is_set():
                            # Foreground gave up between acquisition and the call.
                            print("[operator_console_chat] worker cancelled at acquisition (foreground already returned)")
                            return
                        _run_ava_result_box[0] = run_ava(clean_message, image, _person_id_for_run)
                    except Exception as _rae:
                        _run_ava_error_box[0] = _rae
                    finally:
                        _RUN_AVA_SERIAL_LOCK.release()
                finally:
                    _run_ava_done.set()

            _t_ra = threading.Thread(target=_run_ava_worker, daemon=True, name="ava-run-ava-turn")
            _t_ra.start()
            if not _run_ava_done.wait(timeout=90.0):
                # Foreground gives up. Tell the worker to bail rather than
                # continuing to chase Ollama in the background — that's what
                # caused the cascading-worker pile-up.
                _run_ava_cancel.set()
                print("[operator_console_chat] TIMEOUT after 90s — cancelling worker, returning fallback response")
                _run_ava_result = None
            elif _run_ava_error_box[0] is not None:
                print(f"[operator_console_chat] run_ava error: {_run_ava_error_box[0]!r}")
                _run_ava_result = None
            else:
                _run_ava_result = _run_ava_result_box[0]

            if _run_ava_result is None:
                reply = "Sorry, I'm thinking slowly right now. Try again?"
                visual = {}
                active_profile = load_profile_by_id(get_active_person_id())
                actions = []
                refl = {}
            else:
                reply, visual, active_profile, actions, refl = _run_ava_result

            try:
                print(
                    f"[operator_console_chat] run_ava returned "
                    f"reply_len={len(str(reply or ''))} "
                    f"turn_route={str((visual or {}).get('turn_route') or '')} "
                    f"actions={len(actions or [])}"
                )
            except Exception:
                pass
            # Build the chat response IMMEDIATELY from run_ava's output so the
            # HTTP caller gets it back in the next packet. Post-reply work
            # (session-message count bump, self-narrative update at every 10th
            # msg, post-reply workspace.tick, perception rebuild, expression
            # state update, camera snapshot) used to run inline here — but
            # under sustained chat load, one of these blocks (bump_session_
            # message_count → load_session_state file lock?) was hanging
            # post-run_ava and never returning to the HTTP caller. The chat
            # reply was generated but never delivered. See vault
            # bugs/operator-chat-cascading-workers.md for the full diagnosis.
            #
            # Heartbeat already runs workspace.tick + perception every 30s
            # so skipping them here doesn't affect Ava's awareness — only
            # the per-chat-call freshness, which the HTTP caller doesn't need.
            # Self-narrative update fires from a separate heartbeat hook too.
            out["reply"] = reply
            out["visual"] = dict(visual or {})
            out["actions"] = list(actions or [])
            out["reflection"] = refl if isinstance(refl, dict) else {}
            out["active_profile"] = {
                "person_id": active_profile.get("person_id"),
                "name": active_profile.get("name"),
            }
            out["canonical_history"] = list(_get_canonical_history())
            out["face_status"] = (visual or {}).get("face_status", "—")
            out["recognition_status"] = (visual or {}).get("recognition_status", "—")

            # Fire the post-reply housekeeping in a daemon thread so it
            # doesn't block the response. If it hangs, only that thread
            # is affected; the HTTP caller already got their reply.
            def _post_reply_housekeeping():
                try:
                    msg_count = bump_session_message_count()
                    if msg_count % 10 == 0:
                        recent = load_recent_chat(limit=10, person_id=active_profile["person_id"])
                        summary = " ".join((r.get("content", "") or "")[:100] for r in recent[-5:])
                        mood = load_mood()
                        face_emo = load_expression_state().get("raw_emotion", "neutral")
                        update_self_narrative(globals(), summary, mood, face_emo)
                except Exception as _e:
                    print(f"[self-narrative] update failed (operator chat housekeeping): {_e}")
            threading.Thread(target=_post_reply_housekeeping, daemon=True,
                             name="ava-occ-housekeeping").start()
            try:
                print(
                    f"[operator_console_chat] exit ok reply_len={len(str(out.get('reply') or ''))} "
                    f"canon_len={len(out.get('canonical_history') or [])}"
                )
            except Exception:
                pass
            return out
        finally:
            _mark_user_reply_finished()
    except Exception as e:
        import traceback

        print(f"[operator_console_chat] {e!r}\n{traceback.format_exc()}")
        out["ok"] = False
        out["error"] = str(e)[:800]
        try:
            _mark_user_reply_finished()
        except Exception:
            pass
        return out


def chat_fn(message, history, image):
    history = _sync_canonical_history(history)
    clean_message = _extract_text_content(message).strip()
    if clean_message:
        workspace.record_user_message()
        note_user_interaction_for_initiative(clean_message, interaction_kind="text")

    if not clean_message:
        workspace.tick(camera_manager, image, globals(), "")
        perception = workspace.state.perception if workspace.state else build_perception(camera_manager, image, globals(), "")
        recognized_text, recognized_person_id = perception.recognized_text, perception.face_identity
        expr_state = update_expression_state(
            perception.frame,
            recognized_person_id=recognized_person_id,
            visual_truth_trusted=perception.visual_truth_trusted,
        )
        if perception.visual_truth_trusted:
            process_camera_snapshot(
                perception.frame,
                recognized_text=recognized_text,
                recognized_person_id=recognized_person_id,
                expression_state=expr_state,
            )
        return scrub_chat_callback_result((
            _chatbot_messages_out(), "", perception.face_status, get_memory_status(),
            get_mood_status_text(),
            recognized_text,
            get_expression_status_text(expr_state),
            get_emotion_blend_text(), get_time_status_text(),
            get_active_profile_text(), get_active_profile_summary(),
            format_recent_memories_ui(list_recent_memories(get_active_person_id(), 12)),
            "No action.",
            format_reflections_ui(load_recent_reflections(limit=15, person_id=get_active_person_id())),
            format_self_model_ui(load_self_model()),
            initiative_status_text(),
            get_latest_annotated_snapshot_for_ui(),
            get_camera_memory_status_text(),
            recent_camera_events_text(limit=8)
        ))

    _mark_user_reply_started()
    try:
        _rp_decision = prepare_reply_path_for_turn(
            globals(), clean_message, workspace, voice_session=False
        )
        if not should_skip_initial_workspace_tick(_rp_decision):
            workspace.tick(camera_manager, image, globals(), clean_message)
        canon = list(_get_canonical_history())
        canon.append({"role": "user", "content": clean_message})
        _set_canonical_history(canon)
        reply, visual, active_profile, actions, _ = run_ava(clean_message, image, get_active_person_id())
        print(
            f"[chat_fn] run_ava done route={visual.get('turn_route')} "
            f"actions={len(actions)} reply_chars={len(reply or '')}"
        )

        try:
            msg_count = bump_session_message_count()

            if msg_count % 10 == 0:
                recent = load_recent_chat(limit=10, person_id=active_profile["person_id"])
                summary = " ".join((r.get("content", "") or "")[:100] for r in recent[-5:])
                mood = load_mood()
                face_emo = load_expression_state().get("raw_emotion", "neutral")
                update_self_narrative(globals(), summary, mood, face_emo)
        except Exception as e:
            print(f"[self-narrative] update failed: {e}")

        workspace.tick(camera_manager, image, globals(), clean_message)
        perception = workspace.state.perception if workspace.state else build_perception(camera_manager, image, globals(), clean_message)
        expr_state = update_expression_state(
            perception.frame,
            recognized_person_id=perception.face_identity,
            visual_truth_trusted=perception.visual_truth_trusted,
        )
        if perception.visual_truth_trusted:
            process_camera_snapshot(
                perception.frame,
                recognized_text=perception.recognized_text,
                recognized_person_id=perception.face_identity,
                expression_state=expr_state,
            )
        person_id = active_profile["person_id"]
        return scrub_chat_callback_result((
            _chatbot_messages_out(),
            "",
            visual.get("face_status", perception.face_status),
            get_memory_status(),
            get_mood_status_text(),
            visual.get("recognition_status", perception.recognized_text),
            get_expression_status_text(expr_state),
            get_emotion_blend_text(),
            get_time_status_text(),
            get_active_profile_text(),
            get_active_profile_summary(),
            format_recent_memories_ui(list_recent_memories(person_id, 12)),
            str(actions),
            format_reflections_ui(load_recent_reflections(limit=15, person_id=person_id)),
            format_self_model_ui(load_self_model()),
            initiative_status_text(),
            get_latest_annotated_snapshot_for_ui(),
            get_camera_memory_status_text(),
            recent_camera_events_text(limit=8),
        ))
    except Exception as e:
        try:
            print(f"chat_fn error: {e}")
        except Exception:
            pass
        workspace.tick(camera_manager, image, globals(), clean_message)
        perception = workspace.state.perception if workspace.state else build_perception(camera_manager, image, globals(), clean_message)
        recognized_text, recognized_person_id = perception.recognized_text, perception.face_identity
        expr_state = update_expression_state(
            perception.frame,
            recognized_person_id=recognized_person_id,
            visual_truth_trusted=perception.visual_truth_trusted,
        )
        return scrub_chat_callback_result((
            _chatbot_messages_out(),
            clean_message,
            perception.face_status,
            get_memory_status(),
            get_mood_status_text(),
            recognized_text,
            get_expression_status_text(expr_state),
            get_emotion_blend_text(),
            get_time_status_text(),
            get_active_profile_text(),
            get_active_profile_summary(),
            format_recent_memories_ui(list_recent_memories(get_active_person_id(), 12)),
            f"Reply error: {e}",
            format_reflections_ui(load_recent_reflections(limit=15, person_id=get_active_person_id())),
            format_self_model_ui(load_self_model()),
            initiative_status_text(),
            get_latest_annotated_snapshot_for_ui(),
            get_camera_memory_status_text(),
            recent_camera_events_text(limit=8),
        ))
    finally:
        _mark_user_reply_finished()

def voice_fn(audio, history, image):
    history = _sync_canonical_history(history)

    if not audio:
        perc = build_perception(camera_manager, image, globals(), "")
        recognized_text, recognized_person_id = recognize_face(image)
        expr_state = update_expression_state(
            image, recognized_person_id=recognized_person_id, visual_truth_trusted=perc.visual_truth_trusted
        )
        if perc.visual_truth_trusted:
            process_camera_snapshot(
                image,
                recognized_text=recognized_text,
                recognized_person_id=recognized_person_id,
                expression_state=expr_state,
            )
        return scrub_chat_callback_result((
            _chatbot_messages_out(),
            None,
            detect_face(image),
            get_memory_status(),
            get_mood_status_text(),
            recognized_text,
            get_expression_status_text(expr_state),
            get_emotion_blend_text(),
            get_time_status_text(),
            get_active_profile_text(),
            get_active_profile_summary(),
            format_recent_memories_ui(list_recent_memories(get_active_person_id(), 12)),
            "No action.",
            format_reflections_ui(load_recent_reflections(limit=15, person_id=get_active_person_id())),
            format_self_model_ui(load_self_model()),
            initiative_status_text(),
            get_latest_annotated_snapshot_for_ui(),
            get_camera_memory_status_text(),
            recent_camera_events_text(limit=8),
        ))

    text = transcribe_audio(audio)
    if not text.strip():
        perc = build_perception(camera_manager, image, globals(), "")
        recognized_text, recognized_person_id = recognize_face(image)
        expr_state = update_expression_state(
            image, recognized_person_id=recognized_person_id, visual_truth_trusted=perc.visual_truth_trusted
        )
        if perc.visual_truth_trusted:
            process_camera_snapshot(
                image,
                recognized_text=recognized_text,
                recognized_person_id=recognized_person_id,
                expression_state=expr_state,
            )
        return scrub_chat_callback_result((
            _chatbot_messages_out(),
            None,
            detect_face(image),
            get_memory_status(),
            get_mood_status_text(),
            recognized_text,
            get_expression_status_text(expr_state),
            get_emotion_blend_text(),
            get_time_status_text(),
            get_active_profile_text(),
            get_active_profile_summary(),
            format_recent_memories_ui(list_recent_memories(get_active_person_id(), 12)),
            "No action.",
            format_reflections_ui(load_recent_reflections(limit=15, person_id=get_active_person_id())),
            format_self_model_ui(load_self_model()),
            initiative_status_text(),
            get_latest_annotated_snapshot_for_ui(),
            get_camera_memory_status_text(),
            recent_camera_events_text(limit=8),
        ))

    text_strip = text.strip()
    from brain.voice_conversation import finalize_voice_turn_after_reply, prepare_voice_turn_for_globals

    _gvn = globals()
    prepare_voice_turn_for_globals(
        _gvn,
        user_text=text_strip,
        audio_path=audio if isinstance(audio, str) else None,
    )
    try:
        note_user_interaction_for_initiative(text_strip, interaction_kind="voice")
        workspace.record_user_message()
        _rp_decision_v = prepare_reply_path_for_turn(
            globals(), text_strip, workspace, voice_session=True
        )
        if not should_skip_initial_workspace_tick(_rp_decision_v):
            workspace.tick(camera_manager, image, globals(), text_strip)

        if should_ask_identity_when_no_camera_face(text_strip, image):
            reply = no_face_identity_prompt()
            current_person_id = get_active_person_id()
            log_chat("user", text_strip, {"person_id": current_person_id, "person_name": load_profile_by_id(current_person_id)["name"]})
            log_chat("assistant", reply, {"person_id": current_person_id, "person_name": load_profile_by_id(current_person_id)["name"], "actions": ["asked_identity_no_face"]})
            canon = list(_get_canonical_history())
            canon.append({"role": "user", "content": text_strip})
            canon.append({"role": "assistant", "content": reply})
            _set_canonical_history(canon)
            perc = build_perception(camera_manager, image, globals(), "")
            recognized_text, recognized_person_id = recognize_face(image)
            expr_state = update_expression_state(
                image, recognized_person_id=recognized_person_id, visual_truth_trusted=perc.visual_truth_trusted
            )
            if perc.visual_truth_trusted:
                process_camera_snapshot(
                    image,
                    recognized_text=recognized_text,
                    recognized_person_id=recognized_person_id,
                    expression_state=expr_state,
                )
            finalize_voice_turn_after_reply(_gvn, reply)
            return scrub_chat_callback_result((
                _chatbot_messages_out(),
                None,
                detect_face(image),
                get_memory_status(),
                get_mood_status_text(),
                recognized_text,
                get_expression_status_text(expr_state),
                get_emotion_blend_text(),
                get_time_status_text(),
                get_active_profile_text(),
                get_active_profile_summary(),
                format_recent_memories_ui(list_recent_memories(current_person_id, 12)),
                "asked_identity_no_face",
                format_reflections_ui(load_recent_reflections(limit=15, person_id=current_person_id)),
                format_self_model_ui(load_self_model()),
                initiative_status_text(),
                get_latest_annotated_snapshot_for_ui(),
                get_camera_memory_status_text(),
                recent_camera_events_text(limit=8),
            ))

        canon = list(_get_canonical_history())
        canon.append({"role": "user", "content": text_strip})
        _set_canonical_history(canon)
        reply, visual, active_profile, actions, _ = run_ava(text_strip, image, get_active_person_id())
        finalize_voice_turn_after_reply(_gvn, reply)
        print(
            f"[voice_fn] run_ava done route={visual.get('turn_route')} "
            f"actions={len(actions)} reply_chars={len(reply or '')}"
        )

        try:
            msg_count = bump_session_message_count()
            if msg_count % 10 == 0:
                recent_n = load_recent_chat(limit=10, person_id=active_profile["person_id"])
                summary = " ".join((r.get("content", "") or "")[:100] for r in recent_n[-5:])
                mood = load_mood()
                face_emo = load_expression_state().get("raw_emotion", "neutral")
                update_self_narrative(globals(), summary, mood, face_emo)
        except Exception as e:
            print(f"[self-narrative] update failed: {e}")

        recent = list_recent_memories(active_profile["person_id"], 12)
        action_text = "\n".join(actions) if actions else "No action."
        reflections_text = format_reflections_ui(load_recent_reflections(limit=15, person_id=active_profile["person_id"]))

        perception_v = workspace.state.perception if workspace.state else build_perception(camera_manager, image, globals(), text_strip)
        recognized_text, recognized_person_id = perception_v.recognized_text, perception_v.face_identity
        expr_state = update_expression_state(
            perception_v.frame,
            recognized_person_id=recognized_person_id,
            visual_truth_trusted=perception_v.visual_truth_trusted,
        )
        if perception_v.visual_truth_trusted:
            process_camera_snapshot(
                perception_v.frame,
                recognized_text=recognized_text,
                recognized_person_id=recognized_person_id,
                expression_state=expr_state,
            )
        return scrub_chat_callback_result((
            _chatbot_messages_out(),
            None,
            visual.get("face_status", perception_v.face_status),
            get_memory_status(),
            get_mood_status_text(),
            visual.get("recognition_status", perception_v.recognized_text),
            visual.get("expression_status", get_expression_status_text(expr_state)),
            get_emotion_blend_text(),
            get_time_status_text(),
            f"{active_profile['name']} [{active_profile['person_id']}]",
            json.dumps(active_profile, indent=2, ensure_ascii=False),
            format_recent_memories_ui(recent),
            action_text,
            reflections_text,
            format_self_model_ui(load_self_model()),
            initiative_status_text(),
            get_latest_annotated_snapshot_for_ui(),
            get_camera_memory_status_text(),
            recent_camera_events_text(limit=8),
        ))

    finally:
        _gvn.pop("_voice_user_turn_priority", None)
        _gvn.pop("_voice_conversation", None)


def debug_panel_refresh_fn() -> tuple[str, str, str, str]:
    try:
        mood = load_mood()
        bm = mood.get("behavior_modifiers", {}) or {}
        meta_line = (
            f"current_mood: {mood.get('current_mood', '?')} | dominant_style: {mood.get('dominant_style', '?')}\n"
            f"outward_tone: {mood.get('outward_tone', '?')}\n"
            f"behavior: initiative={float(bm.get('initiative', 0.0)):.2f} warmth={float(bm.get('warmth', 0.0)):.2f} "
            f"caution={float(bm.get('caution', 0.0)):.2f} depth={float(bm.get('depth', 0.0)):.2f}"
        )
    except Exception as e:
        meta_line = f"(mood error: {e})"

    try:
        gs = load_goal_system()
        ag = gs.get("active_goal") or {}
        if isinstance(ag, dict):
            goal_line = (
                f"name: {ag.get('name', '?')} | strength: {float(ag.get('score', ag.get('priority', 0)) or 0):.3f}\n"
                f"reason: {trim_for_prompt(str(ag.get('reason', '')), limit=220)}"
            )
        else:
            goal_line = f"active_goal: {ag!r}"
    except Exception as e:
        goal_line = f"(goal error: {e})"

    try:
        sn = load_self_narrative()
        narrative_line = (
            f"who_i_am: {trim_for_prompt(str(sn.get('who_i_am', '')), limit=240)}\n"
            f"how_i_feel: {trim_for_prompt(str(sn.get('how_i_feel', '')), limit=240)}\n"
            f"patterns: {trim_for_prompt(str(sn.get('patterns_i_notice', '')), limit=200)}"
        )
    except Exception as e:
        narrative_line = f"(narrative error: {e})"

    try:
        pid = get_active_person_id()
        refl = load_recent_reflections(limit=1, person_id=pid)
        if refl:
            r = refl[-1]
            tail = (
                f"Last reflection ({r.get('timestamp', '')}): importance {float(r.get('importance', 0.0)):.3f}\n"
                f"{trim_for_prompt(str(r.get('summary', '')), limit=400)}\n"
            )
        else:
            tail = "Last reflection: (none for active person)\n"
    except Exception as e:
        tail = f"(reflection error: {e})\n"

    try:
        h = load_health_state(globals())
        tail += f"\nHealth: overall={h.get('overall', '?')} degraded_mode={h.get('degraded_mode', '?')}"
    except Exception as e:
        tail += f"\nHealth: (error {e})"

    try:
        prof = load_profile_by_id(get_active_person_id())
        tail += (
            f"\nrelationship_score: {float(prof.get('relationship_score', 0.3)):.4f} "
            f"({prof.get('name', '?')} / {prof.get('person_id', '?')})"
        )
    except Exception as e:
        tail += f"\nprofile: (error {e})"

    return meta_line, goal_line, narrative_line, tail


def refresh_profiles_fn():
    return {"choices": get_profile_choices(), "value": get_active_profile_text()}

def create_profile_fn(name, relationship, allowed):
    name = (name or "").strip()
    if not name:
        return "Please enter a name.", {"choices": get_profile_choices()}, get_active_profile_text(), get_active_profile_summary()

    profile = create_or_get_profile(name, relationship_to_zeke=(relationship or "known person"), allowed=bool(allowed))
    set_active_person(profile["person_id"], source="created_from_ui")
    return (
        f"✅ Created or loaded profile for {profile['name']}.",
        {"choices": get_profile_choices(), "value": f"{profile['name']} [{profile['person_id']}]"},
        f"{profile['name']} [{profile['person_id']}]",
        json.dumps(profile, indent=2, ensure_ascii=False)
    )

def switch_profile_fn(choice):
    person_id = parse_profile_choice(choice)
    profile = set_active_person(person_id, source="manual_switch")
    return f"✅ Switched active person to {profile['name']}.", f"{profile['name']} [{profile['person_id']}]", json.dumps(profile, indent=2, ensure_ascii=False)

def save_note_fn(note_text):
    person_id = get_active_person_id()
    note = (note_text or "").strip()
    if not note:
        return "No note entered.", get_memory_status(), get_active_profile_summary(), format_recent_memories_ui(list_recent_memories(person_id, 12))

    profile = load_profile_by_id(person_id)
    if note not in profile["notes"]:
        profile["notes"].append(note)
        save_profile(profile)
    remember_memory(note, person_id=person_id, category="profile", importance=0.86, source="manual_note", tags=["profile", "manual"])
    return f"✅ Added note to {profile['name']}.", get_memory_status(), json.dumps(profile, indent=2, ensure_ascii=False), format_recent_memories_ui(list_recent_memories(person_id, 12))

def save_like_fn(like_text):
    person_id = get_active_person_id()
    like = (like_text or "").strip()
    if not like:
        return "No like entered.", get_active_profile_summary(), format_recent_memories_ui(list_recent_memories(person_id, 12))

    profile = load_profile_by_id(person_id)
    if like not in profile["likes"]:
        profile["likes"].append(like)
        save_profile(profile)

    remember_memory(
        text=f"{profile['name']} likes {like}.",
        person_id=person_id,
        category="preference",
        importance=0.86,
        source="manual_like",
        tags=["preference", "like"]
    )
    return f"✅ Added like to {profile['name']}.", json.dumps(profile, indent=2, ensure_ascii=False), format_recent_memories_ui(list_recent_memories(person_id, 12))

def save_impression_fn(impression_text):
    person_id = get_active_person_id()
    impression = (impression_text or "").strip()
    if not impression:
        return "No impression entered.", get_active_profile_summary(), format_recent_memories_ui(list_recent_memories(person_id, 12))

    profile = load_profile_by_id(person_id)
    if impression not in profile["ava_impressions"]:
        profile["ava_impressions"].append(impression)
        save_profile(profile)

    remember_memory(
        text=f"Ava's impression of {profile['name']}: {impression}",
        person_id=person_id,
        category="impression",
        importance=0.62,
        source="manual_impression",
        tags=["impression"]
    )
    return f"✅ Added Ava impression for {profile['name']}.", json.dumps(profile, indent=2, ensure_ascii=False), format_recent_memories_ui(list_recent_memories(person_id, 12))

def memory_search_fn(query):
    person_id = get_active_person_id()
    results = search_memories(query, person_id=person_id, k=8)
    return format_recent_memories_ui(results)

def memory_delete_fn(memory_id):
    person_id = get_active_person_id()
    status = delete_memory((memory_id or "").strip())
    return status, format_recent_memories_ui(list_recent_memories(person_id, 12))

def memory_manual_add_fn(text, category, importance_percent, tags_text):
    person_id = get_active_person_id()
    text = (text or "").strip()
    if not text:
        return "No memory text entered.", format_recent_memories_ui(list_recent_memories(person_id, 12))

    memory_id = remember_memory(
        text=text,
        person_id=person_id,
        category=(category or "general").strip() or "general",
        importance=importance_percent,
        source="manual_memory",
        tags=parse_tags(tags_text)
    )
    if memory_id:
        return f"✅ Saved memory {memory_id}", format_recent_memories_ui(list_recent_memories(person_id, 12))
    return "❌ Failed to save memory.", format_recent_memories_ui(list_recent_memories(person_id, 12))

def memory_update_importance_fn(memory_id, importance_percent):
    status = set_memory_importance(memory_id, importance_percent, reason="manual_ui_update")
    return status, format_recent_memories_ui(list_recent_memories(get_active_person_id(), 12))


def memory_refresh_recent_fn():
    return format_recent_memories_ui(list_recent_memories(get_active_person_id(), 12))

def workbench_refresh_index_fn():
    return format_workbench_index(limit=200)

def workbench_read_fn(relative_path):
    return read_workbench_file(relative_path)

def workbench_write_fn(relative_path, content):
    status = write_workbench_file(relative_path, content, overwrite=True)
    return status, format_workbench_index(limit=200)

def workbench_append_fn(relative_path, content):
    status = append_workbench_file(relative_path, content)
    return status, format_workbench_index(limit=200)

def read_chatlog_fn():
    return read_chatlog()

def read_code_fn():
    return read_runtime_code()

def reload_personality_fn():
    try:
        _ = load_personality()
        return "✅ Personality reloaded."
    except Exception as e:
        return f"❌ Failed to reload personality: {e}"

def capture_face_for_active_person_fn(image):
    person_id = get_active_person_id()
    return capture_face_sample(image, person_id)

def train_faces_fn():
    return train_face_recognizer()

def recognize_face_now_fn(image):
    status, recognized_person_id = recognize_face(image)
    perc = build_perception(camera_manager, image, globals(), "")
    expression_state = update_expression_state(
        image, recognized_person_id=recognized_person_id, visual_truth_trusted=perc.visual_truth_trusted
    )
    if recognized_person_id is not None:
        profile = set_active_person(recognized_person_id, source="facial_recognition_manual")
        return status, get_expression_status_text(expression_state), f"{profile['name']} [{profile['person_id']}]", json.dumps(profile, indent=2, ensure_ascii=False)
    return status, get_expression_status_text(expression_state), get_active_profile_text(), get_active_profile_summary()


# === v37 persistence guard: atomic goal-system I/O + recursion-safe loaders ===
from datetime import datetime

_GOAL_SYSTEM_LOADING_GUARD = False
_GOAL_SYSTEM_SAVING_GUARD = False
_SELF_MODEL_LOADING_GUARD = False
_SELF_MODEL_SAVING_GUARD = False
_EMOTION_REFERENCE_LOADING_GUARD = False

def iso_to_ts(value) -> float:
    if value is None:
        return 0.0
    try:
        s = str(value).strip()
        if not s:
            return 0.0
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return float(datetime.fromisoformat(s).timestamp())
    except Exception:
        return 0.0

def list_memories() -> list[dict]:
    """All vector memories as flat dicts for brain.memory.decay_tick."""
    collection = get_collection()
    if collection is None:
        return []
    try:
        got = collection.get(include=["metadatas", "documents"])
        ids = got.get("ids") or []
        metas = got.get("metadatas") or []
        out: list[dict] = []
        for mid, meta in zip(ids, metas):
            meta = meta or {}
            created_at = meta.get("created_at", "")
            created_ts = iso_to_ts(created_at)
            la = meta.get("last_accessed_at", "")
            last_ts = iso_to_ts(la) if la else 0.0
            try:
                imp = float(meta.get("importance_score", 0.5))
            except (TypeError, ValueError):
                imp = 0.5
            out.append({
                "memory_id": mid,
                "id": mid,
                "importance": imp,
                "importance_score": imp,
                "created_at": created_at,
                "created_ts": created_ts,
                "last_accessed_ts": last_ts or created_ts,
            })
        return out
    except Exception:
        return []

def _deep_merge_defaults_v37(target, defaults):
    if not isinstance(target, dict) or not isinstance(defaults, dict):
        return target
    for k, v in defaults.items():
        if k not in target:
            target[k] = _deepcopy_jsonable(v) if isinstance(v, (dict, list)) else v
        elif isinstance(v, dict) and isinstance(target.get(k), dict):
            _deep_merge_defaults_v37(target[k], v)
    return target

def _atomic_json_write_v37(path_obj, data):
    path_obj = Path(path_obj)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=path_obj.name + ".", suffix=".tmp", dir=str(path_obj.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path_obj)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise

def _raw_load_goal_system_v37() -> dict:
    system = default_goal_system()
    try:
        if GOAL_SYSTEM_PATH.exists():
            with open(GOAL_SYSTEM_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                system.update(data)
    except Exception as e:
        print(f"Goal system raw load error: {e}")
    _deep_merge_defaults_v37(system, default_goal_system())
    system.setdefault("custom_meta_modes", {})
    return system

def save_goal_system(system: dict):
    global _GOAL_SYSTEM_SAVING_GUARD
    if _GOAL_SYSTEM_SAVING_GUARD:
        return
    try:
        _GOAL_SYSTEM_SAVING_GUARD = True
        payload = _deepcopy_jsonable(system) if isinstance(system, (dict, list)) else system
        _atomic_json_write_v37(GOAL_SYSTEM_PATH, payload)
    except Exception as e:
        print(f"Goal system save error: {e}")
    finally:
        _GOAL_SYSTEM_SAVING_GUARD = False

def load_goal_system() -> dict:
    global _GOAL_SYSTEM_LOADING_GUARD
    if _GOAL_SYSTEM_LOADING_GUARD:
        return _raw_load_goal_system_v37()
    try:
        _GOAL_SYSTEM_LOADING_GUARD = True
        system = _raw_load_goal_system_v37()
        if not system.get("goals") and not _SELF_MODEL_LOADING_GUARD:
            try:
                model = _raw_load_self_model_v37()
                for text in model.get("current_goals", []) or []:
                    system["goals"].append(make_goal_entry(text, kind="goal", horizon="medium_term", importance=0.72, urgency=0.48, source="migration"))
                for text in model.get("curiosity_questions", []) or []:
                    system["goals"].append(make_goal_entry(text, kind="question", horizon="short_term", importance=0.56, urgency=0.58, source="migration"))
            except Exception:
                pass
        return system
    finally:
        _GOAL_SYSTEM_LOADING_GUARD = False

def _raw_load_self_model_v37() -> dict:
    model = default_self_model()
    try:
        if SELF_MODEL_PATH.exists():
            with open(SELF_MODEL_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                model.update(data)
    except Exception as e:
        print(f"Self model raw load error: {e}")
    _deep_merge_defaults_v37(model, default_self_model())
    return model

def load_self_model() -> dict:
    global _SELF_MODEL_LOADING_GUARD
    if _SELF_MODEL_LOADING_GUARD:
        return _raw_load_self_model_v37()
    try:
        _SELF_MODEL_LOADING_GUARD = True
        model = _raw_load_self_model_v37()
        try:
            system = _raw_load_goal_system_v37()
            if 'derive_goal_lists_from_system' in globals():
                goals, questions = derive_goal_lists_from_system(system)
                model["current_goals"] = goals
                model["curiosity_questions"] = questions
                model["goal_system_summary"] = [
                    {
                        "text": g.get("text", ""),
                        "priority": round(float(g.get("current_priority", 0.0) or 0.0), 2),
                        "horizon": g.get("horizon", "short_term"),
                        "kind": g.get("kind", "goal"),
                    }
                    for g in (system.get("goals", []) or [])[:10]
                ]
                model["active_goal"] = system.get("active_goal", {}) or {}
                model["goal_blend"] = (system.get("goal_blend", []) or [])[:3]
        except Exception as e:
            print(f"Goal system sync error: {e}")
        return model
    finally:
        _SELF_MODEL_LOADING_GUARD = False

def save_self_model(model: dict):
    global _SELF_MODEL_SAVING_GUARD
    if _SELF_MODEL_SAVING_GUARD:
        return
    try:
        _SELF_MODEL_SAVING_GUARD = True
        payload = _deepcopy_jsonable(model) if isinstance(model, (dict, list)) else model
        _atomic_json_write_v37(SELF_MODEL_PATH, payload)
    except Exception as e:
        print(f"Self model save error: {e}")
    finally:
        _SELF_MODEL_SAVING_GUARD = False

def load_emotion_reference() -> dict:
    global _EMOTION_REFERENCE_LOADING_GUARD
    if _EMOTION_REFERENCE_LOADING_GUARD:
        return DEFAULT_EMOTION_REFERENCE
    try:
        _EMOTION_REFERENCE_LOADING_GUARD = True
        if EMOTION_REFERENCE_PATH.exists():
            try:
                with open(EMOTION_REFERENCE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data:
                    return data
            except Exception as e:
                print(f"Emotion reference load error: {e}")
        return DEFAULT_EMOTION_REFERENCE
    finally:
        _EMOTION_REFERENCE_LOADING_GUARD = False

# =========================================================
# STARTUP  (logic extracted to brain/startup.py)
# =========================================================
_STARTUP_COMPLETE = False
from brain.startup import run_startup as _run_startup
_run_startup(globals())

# =========================================================
# Single-instance enforcement
# =========================================================
# Two Ava instances on the same machine collide on port 5876 (Errno 10048).
# Tonight's hardware test had the operator HTTP server in an infinite
# bind-fail/restart loop because a manual restart fired while a regression
# subprocess still owned the port. Two checks before binding:
#
#   1. Port probe — connect to 127.0.0.1:5876. If reachable, another Ava
#      is already running. Exit immediately (sys.exit(1)). Don't retry.
#   2. PID lockfile at state/ava.pid. Write current PID on startup. If
#      the file exists and the named process is still alive, exit. Stale
#      lockfiles (process already gone) are overwritten silently.
#
# Bypass with AVA_SKIP_INSTANCE_CHECK=1 (e.g. for diagnostics where you
# really want a second instance bound to a different port via
# AVA_OPERATOR_HTTP_PORT).
_AVA_PID_FILE = Path(BASE_DIR) / "state" / "ava.pid"

def _check_existing_ava_instance() -> None:
    """Hard-exit if another Ava instance is already running.

    Called once during startup, immediately before binding the operator
    HTTP server. No retries — single instance is a hard rule.
    """
    if os.environ.get("AVA_SKIP_INSTANCE_CHECK", "0").strip() == "1":
        print("[ava] WARNING: AVA_SKIP_INSTANCE_CHECK=1 — single-instance check bypassed")
        return
    import socket as _sock
    import sys as _sys
    _port = int(os.environ.get("AVA_OPERATOR_HTTP_PORT", "5876") or "5876")
    _host = os.environ.get("AVA_OPERATOR_HTTP_HOST", "127.0.0.1") or "127.0.0.1"
    # Port probe.
    s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        rc = s.connect_ex((_host, _port))
    except Exception:
        rc = -1
    finally:
        try:
            s.close()
        except Exception:
            pass
    if rc == 0:
        print(
            f"[ava] ERROR: Another Ava instance is already running on port "
            f"{_host}:{_port}. Shut down the existing instance first.",
            flush=True,
        )
        _sys.exit(1)
    # PID lockfile — check stale processes too.
    try:
        if _AVA_PID_FILE.is_file():
            try:
                _existing_pid = int(_AVA_PID_FILE.read_text(encoding="utf-8").strip())
            except Exception:
                _existing_pid = 0
            if _existing_pid > 0 and _existing_pid != os.getpid():
                # Is it still alive?
                _alive = False
                if os.name == "nt":
                    # Windows: probe via os.kill with signal 0 raises if not alive.
                    try:
                        os.kill(_existing_pid, 0)
                        _alive = True
                    except (OSError, PermissionError):
                        _alive = False
                else:
                    try:
                        os.kill(_existing_pid, 0)
                        _alive = True
                    except (ProcessLookupError, PermissionError):
                        _alive = False
                if _alive:
                    print(
                        f"[ava] ERROR: stale lockfile {_AVA_PID_FILE} points at PID "
                        f"{_existing_pid}, which is still alive. Shut it down first.",
                        flush=True,
                    )
                    _sys.exit(1)
                # Stale (process gone) — overwrite silently.
                print(
                    f"[ava] note: cleaning stale lockfile (PID {_existing_pid} no longer running)"
                )
        # Write our PID.
        _AVA_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _AVA_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError as _e:
        # Non-fatal — port probe is the primary guard.
        print(f"[ava] WARNING: pid-lockfile write failed: {_e!r}")

_check_existing_ava_instance()


def _release_ava_pid_lock() -> None:
    """Remove the PID lockfile on graceful shutdown — only if it points at us."""
    try:
        if _AVA_PID_FILE.is_file():
            try:
                pid_in_file = int(_AVA_PID_FILE.read_text(encoding="utf-8").strip())
            except Exception:
                pid_in_file = 0
            if pid_in_file == os.getpid():
                _AVA_PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass

atexit.register(_release_ava_pid_lock)


# =========================================================
# Operator HTTP API (only UI — Tauri app connects to :5876)
# =========================================================
try:
    from brain.operator_server import start_operator_http_background
    start_operator_http_background(globals(), operator_console_chat)
except Exception as _op_http_e:
    print(f"[operator_http] optional API not started: {_op_http_e}")

# Fast-path pre-warm — load the chosen ava-personal:latest model into
# Ollama's VRAM and instantiate ChatOllama once so the first real turn
# doesn't pay cold-load (~10s) + ChatOllama init (~1s) on top of the
# fast path's 2s budget. Runs in a daemon thread so startup stays
# responsive; gated by AVA_PREWARM_FAST!=0 for opt-out.
def _ava_prewarm_fast_path() -> None:
    import time as _t
    try:
        _t.sleep(5.0)  # let subsystems settle, Ollama come up cleanly
        _model = _pick_fast_model_fallback()
        if not _model:
            print("[prewarm] no fast model resolved — skipping")
            return
        print(f"[prewarm] warming fast path: {_model}")
        from langchain_ollama import ChatOllama as _CO
        from langchain_core.messages import HumanMessage as _HM
        from brain.ollama_lock import with_ollama as _wo
        # keep_alive=-1 pins the model in Ollama VRAM so it doesn't get
        # evicted by the default 5-min idle timeout. Without this, the
        # next conversational turn after a long pause (>5min) pays a
        # 30-150s cold reload — observed in the lunch voice test.
        _llm = _CO(model=_model, temperature=0.7, num_predict=200, keep_alive=-1)
        _t0 = _t.time()
        _wo(lambda: _llm.invoke([_HM(content="Say 'ready' in one word.")]),
            label=f"prewarm:{_model}")
        # Stash the instance so reply_engine's fast-path cache hit on first
        # real turn — match the cache key used there: (model, num_predict).
        # 2026-05-08: bumped from 80 to 200 to match reply_engine fast-path.
        try:
            _g_self = globals()
            _cache = _g_self.setdefault("_fast_llm_cache", {})
            if isinstance(_cache, dict):
                _cache[(str(_model), 200)] = _llm
        except Exception:
            pass
        print(f"[prewarm] fast path warmed in {int((_t.time()-_t0)*1000)}ms")
    except Exception as _pe:
        print(f"[prewarm] failed (non-fatal): {_pe!r}")

if os.environ.get("AVA_PREWARM_FAST", "1").strip() != "0":
    import threading as _prewarm_threading
    _prewarm_threading.Thread(
        target=_ava_prewarm_fast_path,
        name="ava-prewarm-fast",
        daemon=True,
    ).start()


# ── Periodic re-warm tick ───────────────────────────────────────────────
# Even with keep_alive=-1 set on the ChatOllama instance, defensive
# re-warming every ~5 min protects against:
#   - keep_alive header not being honoured by some Ollama versions
#   - Concurrent heavy model loads (gemma4:latest, qwen2.5:14b) pushing
#     the fast model out of VRAM regardless of keep_alive
#   - General "did this thing actually stay loaded" sanity check
# Skipped while a conversation is active so we never compete with a
# real turn for the lock. Gated by AVA_PERIODIC_REWARM!=0.
def _ava_periodic_rewarm() -> None:
    import time as _t
    interval_s = 300.0  # 5 minutes
    try:
        _t.sleep(60.0)  # don't fire until a minute after boot
    except Exception:
        return
    while not bool(globals().get("_ava_shutdown")):
        try:
            _t.sleep(interval_s)
            if bool(globals().get("_ava_shutdown")):
                break
            # Skip if the user is mid-conversation — never compete with
            # a real turn for the Ollama lock.
            if bool(globals().get("_conversation_active")) or bool(globals().get("_turn_in_progress")):
                continue
            cache = globals().get("_fast_llm_cache") or {}
            if not isinstance(cache, dict) or not cache:
                continue
            from brain.ollama_lock import with_ollama as _wo
            from langchain_core.messages import HumanMessage as _HM
            for (model, _np), llm in list(cache.items()):
                if bool(globals().get("_ava_shutdown")):
                    return
                try:
                    _t0 = _t.time()
                    _wo(
                        lambda _l=llm: _l.invoke([_HM(content="ok")]),
                        label=f"rewarm:{model}",
                    )
                    print(f"[rewarm] {model} re-pinged in {int((_t.time()-_t0)*1000)}ms")
                except Exception as _re:
                    print(f"[rewarm] {model} ping failed (non-fatal): {_re!r}")
        except Exception as _e:
            print(f"[rewarm] tick error: {_e!r}")
            try:
                _t.sleep(60.0)
            except Exception:
                return

if os.environ.get("AVA_PERIODIC_REWARM", "1").strip() != "0":
    import threading as _rewarm_threading
    _rewarm_threading.Thread(
        target=_ava_periodic_rewarm,
        name="ava-periodic-rewarm",
        daemon=True,
    ).start()


# ── Memory level decay tick (Phase 2 step 3) ────────────────────────────
# Walks the concept graph every hour, demoting levels of un-accessed
# nodes per docs/MEMORY_REWRITE_PLAN.md § 2.2. At level 0, unarchived
# nodes are deleted with a tombstone. Archived nodes clamp to level 1.
#
# IMPORTANT: this is the FIRST-half implementation — only DEMOTIONS run
# right now. Promotions land in Phase 2 step 5 (after step 4 has
# gathered reflection data to validate the scoring heuristics). Until
# step 5, levels only go DOWN, not up; the rate is conservative enough
# (12-hour threshold at level 5, the default) that this is safe over
# a couple of days while step 4 runs.
#
# Kill switches:
#   AVA_DECAY_DISABLED=1     → decay_levels() is a no-op
#   AVA_DECAY_TICK_DISABLED=1 → daemon thread doesn't start at all
def _ava_memory_decay_tick() -> None:
    import time as _t
    interval_s = 3600.0  # 1 hour
    try:
        _t.sleep(120.0)  # don't fire until 2 minutes after boot
    except Exception:
        return
    while not bool(globals().get("_ava_shutdown")):
        try:
            _t.sleep(interval_s)
            if bool(globals().get("_ava_shutdown")):
                break
            cg = globals().get("_concept_graph") or globals().get("concept_graph")
            if cg is None or not callable(getattr(cg, "decay_levels", None)):
                continue
            try:
                stats = cg.decay_levels()
                if any(int(stats.get(k, 0)) for k in ("demoted", "deleted", "archived_floor")):
                    print(
                        f"[memory_decay] tick examined={stats.get('examined', 0)} "
                        f"demoted={stats.get('demoted', 0)} "
                        f"deleted={stats.get('deleted', 0)} "
                        f"archived_floor={stats.get('archived_floor', 0)}"
                    )
            except Exception as _de:
                print(f"[memory_decay] tick error (non-fatal): {_de!r}")
        except Exception as _e:
            print(f"[memory_decay] outer error: {_e!r}")
            try:
                _t.sleep(120.0)
            except Exception:
                return

if os.environ.get("AVA_DECAY_TICK_DISABLED", "0").strip() != "1":
    import threading as _decay_threading
    _decay_threading.Thread(
        target=_ava_memory_decay_tick,
        name="ava-memory-decay",
        daemon=True,
    ).start()

import signal as _signal

_ava_shutdown = False

def _ava_signal_handler(sig, frame):
    global _ava_shutdown, _ava_signal_received_ts
    print(f"\n[ava] shutdown signal received ({sig})", flush=True)
    _ava_shutdown = True
    _ava_signal_received_ts = time.time()

_ava_signal_received_ts: float = 0.0


def _ava_force_exit_watchdog() -> None:
    """If a signal hasn't produced a clean exit in 5 seconds, hard-exit.

    Tonight's hardware test had Ctrl+C printed [ava] shutdown signal
    received (2) and then the process kept running. Background daemon
    threads (heartbeat, video capture, app discovery, wake_word, TTS
    worker) all check globals().get("_ava_shutdown") every loop, but a
    sleep mid-iteration or a blocking call in the wrong spot lets a
    thread linger past the main thread's exit window.

    This watchdog gives them 5 seconds, then calls os._exit(0) which
    skips the rest of cleanup but guarantees the process actually dies.
    Better to over-exit than hang forever.
    """
    import time as _t
    while True:
        try:
            _t.sleep(0.5)
            ts = float(globals().get("_ava_signal_received_ts") or 0.0)
            if ts <= 0:
                continue
            if (_t.time() - ts) >= 5.0:
                print("[ava] FORCE EXIT: shutdown took >5s, killing process now", flush=True)
                # Best-effort: release pid lock first.
                try:
                    _release_ava_pid_lock()
                except Exception:
                    pass
                os._exit(0)
        except Exception:
            pass


_signal.signal(_signal.SIGINT, _ava_signal_handler)
try:
    _signal.signal(_signal.SIGTERM, _ava_signal_handler)
except (OSError, ValueError):
    pass  # SIGTERM may not be available on all platforms

# Force-exit watchdog runs as a daemon thread so it dies with the
# process if the clean shutdown does succeed.
_ava_force_exit_thread = threading.Thread(
    target=_ava_force_exit_watchdog,
    name="ava-force-exit-watchdog",
    daemon=True,
)
_ava_force_exit_thread.start()

print("[ava] operator HTTP on :5876 — Ctrl+C to exit.")

_keepalive_errors = 0
_http_restart_attempts = 0
_HTTP_MAX_RESTART_ATTEMPTS = 3
_keepalive_tick = 0
while not _ava_shutdown:
    try:
        # Poll faster than the old 2s so SIGINT exits within 0.5s of arriving.
        # The HTTP-thread check still effectively runs every ~2s thanks to
        # _keepalive_tick gating.
        time.sleep(0.5)
        _keepalive_tick = (_keepalive_tick + 1) % 4
        if _keepalive_tick != 0:
            continue
        _keepalive_errors = 0

        # Restart operator HTTP thread if it died unexpectedly. Capped at
        # _HTTP_MAX_RESTART_ATTEMPTS so a persistent failure (e.g. another
        # process took the port mid-run) doesn't loop infinitely. Tonight's
        # bug was [Errno 10048] hammering forever — single-instance check
        # at startup catches the boot case; this cap catches the runtime
        # case if something else binds the port post-startup.
        _http_thread = globals().get("_operator_http_thread")
        if _http_thread is not None and not _http_thread.is_alive():
            if _http_restart_attempts >= _HTTP_MAX_RESTART_ATTEMPTS:
                print(
                    f"[ava] FATAL: operator HTTP thread died and exceeded "
                    f"{_HTTP_MAX_RESTART_ATTEMPTS} restart attempts. Giving up. "
                    f"Check whether something else is bound to port 5876."
                )
                _ava_shutdown = True
                break
            _http_restart_attempts += 1
            print(
                f"[ava] WARNING: operator HTTP thread died — restart attempt "
                f"{_http_restart_attempts}/{_HTTP_MAX_RESTART_ATTEMPTS}"
            )
            try:
                from brain.operator_server import start_operator_http_background
                _restart_fn = globals().get("operator_console_chat")
                if _restart_fn:
                    start_operator_http_background(globals(), _restart_fn)
                    print("[ava] operator HTTP restarted")
            except Exception as _re:
                print(f"[ava] operator HTTP restart failed: {_re}")

    except KeyboardInterrupt:
        print("[ava] keyboard interrupt")
        _ava_shutdown = True
    except Exception as _ke:
        import traceback as _tb
        _keepalive_errors += 1
        print(f"[ava] keepalive error #{_keepalive_errors}: {_ke}")
        if _keepalive_errors > 10:
            print("[ava] too many keepalive errors — writing crash log")
            try:
                _crash_path = Path("state/crash_log.txt")
                _crash_path.parent.mkdir(exist_ok=True)
                with open(_crash_path, "a", encoding="utf-8") as _cf:
                    _cf.write(f"\n=== KEEPALIVE CRASH {time.ctime()} ===\n")
                    _cf.write(_tb.format_exc())
            except Exception:
                pass
            break

print("[ava] shutdown complete")
