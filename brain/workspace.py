from __future__ import annotations

import time
from dataclasses import dataclass, field
from .attention import AttentionState, compute_attention
from .beliefs import SELF_LIMITS, get_self_narrative_for_prompt
from .emotion import process_visual_emotion
from .memory import recall_for_person
from .perception import PerceptionState, build_perception


@dataclass
class WorkspaceState:
    perception: PerceptionState = field(default_factory=PerceptionState)
    attention: AttentionState = field(
        default_factory=lambda: AttentionState(False, False, False, "uninitialized")
    )
    active_memory: list[str] = field(default_factory=list)
    active_goals: dict = field(default_factory=dict)
    emotional_state: dict = field(default_factory=dict)
    self_narrative: str = ""
    active_person: dict = field(default_factory=dict)
    health: dict = field(default_factory=dict)
    self_limits: list[str] = field(default_factory=lambda: list(SELF_LIMITS))
    timestamp: float = field(default_factory=time.time)


class Workspace:
    """
    Single source of truth for Ava's current awareness.
    Call tick() once per camera_tick_fn and once per chat_fn (and as needed).
    """

    def __init__(self) -> None:
        self._state: WorkspaceState | None = None
        self._last_user_message_ts: float = time.time()
        self._last_recognized_person: str | None = None
        self._last_ws_tick_log_key: tuple | None = None

    def record_user_message(self) -> None:
        self._last_user_message_ts = time.time()

    def tick(self, camera_manager, image, g: dict, user_text: str = "") -> WorkspaceState:
        ws = WorkspaceState(timestamp=time.time())

        try:
            ws.perception = build_perception(camera_manager, image, g, user_text)
        except Exception as e:
            print(f"[workspace] perception failed: {e}")

        try:
            g["_last_perception_emotion"] = ws.perception.face_emotion or "neutral"
        except Exception:
            pass

        try:
            seconds_idle = time.time() - self._last_user_message_ts
            circ_fn = g.get("get_circadian_modifiers")
            c_scale = 1.0
            if callable(circ_fn):
                try:
                    c_scale = float(circ_fn().get("initiative_scale", 1.0))
                except Exception:
                    c_scale = 1.0
            ws.attention = compute_attention(ws.perception, seconds_idle, circadian_initiative_scale=c_scale)
        except Exception as e:
            print(f"[workspace] attention failed: {e}")

        try:
            load_mood_fn = g.get("load_mood")
            save_mood_fn = g.get("save_mood")
            current_mood = load_mood_fn() if callable(load_mood_fn) else {}
            ws.emotional_state = process_visual_emotion(ws.perception, current_mood)
            if callable(save_mood_fn):
                save_mood_fn(ws.emotional_state)
        except Exception as e:
            print(f"[workspace] emotion failed: {e}")

        try:
            gid = g.get("get_active_person_id")
            lp = g.get("load_profile_by_id")
            if callable(gid) and callable(lp):
                ws.active_person = lp(gid()) or {}
            else:
                get_pf = g.get("get_active_profile") or g.get("get_active_person_profile")
                ws.active_person = get_pf() if callable(get_pf) else {}

            person_id = ws.perception.face_identity

            if person_id and person_id != self._last_recognized_person:
                self._last_recognized_person = person_id
                ws.active_memory = recall_for_person(g, person_id, limit=5)
                if ws.active_memory:
                    print(f"[workspace] recalled {len(ws.active_memory)} memories for {person_id}")
            elif self._state and self._state.active_memory:
                ws.active_memory = list(self._state.active_memory)
        except Exception as e:
            print(f"[workspace] memory recall failed: {e}")

        try:
            load_goals_fn = g.get("load_goal_system")
            recalc_fn = g.get("recalculate_operational_goals")
            if callable(load_goals_fn):
                system = load_goals_fn()
                if callable(recalc_fn):
                    ws.active_goals = recalc_fn(
                        system, context_text=user_text or "", mood=ws.emotional_state or None
                    )
                else:
                    ws.active_goals = system if isinstance(system, dict) else {}
        except Exception as e:
            print(f"[workspace] goals failed: {e}")

        try:
            ws.self_narrative = get_self_narrative_for_prompt()
        except Exception as e:
            print(f"[workspace] narrative failed: {e}")

        try:
            load_health_fn = g.get("load_health_state")
            if callable(load_health_fn):
                try:
                    ws.health = load_health_fn(g)
                except TypeError:
                    ws.health = load_health_fn()
            else:
                ws.health = {}
        except Exception as e:
            print(f"[workspace] health failed: {e}")

        ws.self_limits = list(SELF_LIMITS)

        self._state = ws

        ag = ws.active_goals.get("active_goal", "?")
        goal_s = ag.get("name", ag) if isinstance(ag, dict) else ag
        log_key = (
            bool(ws.perception.face_detected),
            str(ws.perception.face_emotion or ""),
            bool(ws.attention.should_speak),
            str(goal_s),
            len(ws.active_memory),
        )
        if log_key != self._last_ws_tick_log_key:
            self._last_ws_tick_log_key = log_key
            pv = ws.perception
            print(
                f"[workspace] tick | vision={getattr(pv, 'vision_status', '?')} "
                f"acq={getattr(pv, 'acquisition_freshness', '?')} "
                f"recovery={getattr(pv, 'recovery_state', '?')} "
                f"trusted={getattr(pv, 'visual_truth_trusted', True)} "
                f"ql={getattr(pv, 'quality_label', '?')} "
                f"fq={getattr(pv, 'frame_quality', 0.0):.2f} "
                f"streak={getattr(pv, 'fresh_frame_streak', 0)} "
                f"id_conf={getattr(pv, 'identity_confidence', 0.0):.2f} "
                f"last_stable={getattr(pv, 'last_stable_identity', None) or '-'} "
                f"face={pv.face_detected} emotion={pv.face_emotion} "
                f"speak={ws.attention.should_speak} goal={goal_s!s} memories={len(ws.active_memory)}"
            )

        return ws

    @property
    def state(self) -> WorkspaceState | None:
        return self._state
