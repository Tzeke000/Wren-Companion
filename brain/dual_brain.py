"""
Dual-brain parallel inference — Ava's split attention system.

One Ava. Two inference streams sharing the same identity, memory, personality.

Stream A — Foreground (conversational):  ava-personal:latest
Stream B — Background (thinking):        deepseek-r1:8b  /  kimi-k2.6:cloud

Model preferences updated 2026-05-02 per LOCAL_MODEL_OPTIMIZATION.md.
Both ava-gemma4 (9.6 GB) and gemma4 (9.6 GB) were dropped — they exceed the
8 GB VRAM ceiling and forced Ollama paging on every turn. ava-personal
(Llama 3.1 8B, 4.9 GB Q4_K_M, fine-tuned) wins foreground for naturalness
+ matched-depth replies. deepseek-r1:8b (Qwen3 8B, 5.2 GB Q4_K_M, native
chain-of-thought) wins background for reasoning.

Bootstrap: Ava decides how much she multitasks. Some people are natural
multitaskers, some prefer focus. The queue, insight frequency, and sharing
pattern emerge from her own choices — not preset here.
"""
from __future__ import annotations

import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ── Task descriptor ────────────────────────────────────────────────────────────

@dataclass
class BrainTask:
    task_type: str          # live_thought | inner_monologue | curiosity_research |
                            # plan_step | self_critique | journal_entry |
                            # memory_consolidation | creative
    topic: str = ""         # optional topic hint
    payload: dict = field(default_factory=dict)
    submitted_at: float = field(default_factory=time.time)


# ── DualBrain class ────────────────────────────────────────────────────────────

class DualBrain:

    # Stream A — Ava's primary voice. ava-personal:latest is the fine-tuned
    # Llama 3.1 8B Q4_K_M (4.9 GB) — fits cleanly in 8 GB VRAM, won the
    # naturalness bench decisively. llama3.1:8b is the stock-base fallback.
    FOREGROUND_MODEL_PREFERRED = "ava-personal:latest"
    FOREGROUND_MODEL_FALLBACK = "llama3.1:8b"

    # Stream B — background reasoning. deepseek-r1:8b is the Qwen3 8B
    # reasoning distill (5.2 GB Q4_K_M, native <think> chain) — only model
    # in the bench that caught both transitivity AND letter-frequency
    # tricks. Cloud alternatives kick in when online.
    BACKGROUND_MODEL_LOCAL = "deepseek-r1:8b"
    BACKGROUND_MODEL_CLOUD = "kimi-k2.6:cloud"
    BACKGROUND_MODEL_FALLBACK = "llama3.1:8b"

    def __init__(self, g: dict[str, Any]):
        self._g = g
        self._lock = threading.Lock()

        # Stream A — pick the best foreground model that's actually installed.
        self.foreground_model: str = self._resolve_foreground_model()
        self.foreground_busy: bool = False
        self.last_foreground_ts: float = 0.0

        # Stream B
        self.background_queue: queue.Queue[BrainTask] = queue.Queue(maxsize=5)
        self.background_busy: bool = False
        self.current_background_task: Optional[str] = None
        self.background_results: deque[dict[str, Any]] = deque(maxlen=10)
        self.tasks_completed_today: int = 0

        # Live thinking
        self.live_thought: Optional[str] = None
        self.live_thought_ts: float = 0.0
        self.live_thinking_active: bool = False

        # Pending handoff insight
        self._background_insight: Optional[dict[str, Any]] = None

        # Threads
        self._bg_thread: Optional[threading.Thread] = None
        self._lt_thread: Optional[threading.Thread] = None

    # ── public interface ───────────────────────────────────────────────────────

    def start(self) -> None:
        """Start both worker threads."""
        if self._bg_thread is None or not self._bg_thread.is_alive():
            self._bg_thread = threading.Thread(
                target=self._background_worker,
                daemon=True,
                name="ava-stream-b",
            )
            self._bg_thread.start()

        if self._lt_thread is None or not self._lt_thread.is_alive():
            self._lt_thread = threading.Thread(
                target=self._live_thinking_worker,
                daemon=True,
                name="ava-live-think",
            )
            self._lt_thread.start()

    def submit(self, task_type: str, topic: str = "", payload: Optional[dict] = None) -> bool:
        """Submit a background task. Returns True if queued, False if queue full."""
        task = BrainTask(task_type=task_type, topic=topic, payload=payload or {})
        try:
            self.background_queue.put_nowait(task)
            return True
        except queue.Full:
            return False

    def mark_foreground_start(self) -> None:
        with self._lock:
            self.foreground_busy = True
            self.last_foreground_ts = time.time()

    def mark_foreground_end(self) -> None:
        with self._lock:
            self.foreground_busy = False
            self.last_foreground_ts = time.time()

    def get_live_thought(self) -> Optional[str]:
        """Return live_thought if fresh (< 60s), else None."""
        with self._lock:
            if self.live_thought and (time.time() - self.live_thought_ts) < 60.0:
                return self.live_thought
        return None

    def handoff_insight_to_foreground(self, reply: str, user_input: str) -> str:
        """
        Previously: wove pending background insight + live thought content
        into the conversational reply ("invisible seam — feels like one mind").
        Tonight's hardware test caught the consequence: 💭-prefixed inner
        monologue lines like "Wondering if repetition in conversation often
        stems..." were appearing in the chat reply text AND being spoken
        through TTS, where the user wanted them as a separate UI surface
        under the orb instead.

        New rule: this method NEVER modifies the reply. Inner monologue
        and live thought content stay in their own surfaces:
          - state/inner_monologue.json (read by snapshot.inner_life.
            current_thought, rendered by the UI as text under the orb)
          - dual_brain.live_thought (read via get_live_thought() if any
            specific path needs it)
          - background_insight stays parked in self._background_insight
            for callers that explicitly want it (none currently do)

        The reply text returns from run_ava clean — only the actual
        spoken response. TTS, chat history, clipboard all see the
        same uncluttered text.
        """
        # Scrub any 💭-prefixed lines that may have leaked in from prompt
        # context — defensive: even if the LLM echoed an inner-monologue
        # snippet from the prompt, we strip it before returning.
        if reply and "💭" in reply:
            try:
                lines = [ln for ln in reply.splitlines() if "💭" not in ln]
                reply = "\n".join(lines).strip()
            except Exception:
                pass
        return reply

    def get_thinking_model(self) -> str:
        g = self._g
        if g.get("_is_online") and g.get("_ollama_cloud_reachable"):
            return self.BACKGROUND_MODEL_CLOUD
        # Prefer deepseek-r1:8b (5.2 GB, fits cleanly, native chain-of-thought).
        # If it isn't installed, fall back to llama3.1:8b (4.9 GB).
        installed = self._installed_models()
        if self.BACKGROUND_MODEL_LOCAL in installed:
            return self.BACKGROUND_MODEL_LOCAL
        return self.BACKGROUND_MODEL_FALLBACK

    @classmethod
    def _installed_models(cls) -> set[str]:
        """Cheap probe of the local Ollama tag list. Cached for 60s."""
        cache = getattr(cls, "_installed_cache", None)
        cache_ts = getattr(cls, "_installed_cache_ts", 0.0)
        if cache is not None and (time.time() - cache_ts) < 60.0:
            return cache
        try:
            import requests
            r = requests.get("http://localhost:11434/api/tags", timeout=2)
            names = {m.get("name", "") for m in (r.json().get("models") or [])}
            cls._installed_cache = names  # type: ignore[attr-defined]
            cls._installed_cache_ts = time.time()  # type: ignore[attr-defined]
            return names
        except Exception:
            return set()

    def _resolve_foreground_model(self) -> str:
        """Pick the best installed Stream A model."""
        installed = self._installed_models()
        for candidate in (self.FOREGROUND_MODEL_PREFERRED,
                          f"{self.FOREGROUND_MODEL_PREFERRED}:latest",
                          self.FOREGROUND_MODEL_FALLBACK):
            if candidate in installed:
                return candidate
        return self.FOREGROUND_MODEL_FALLBACK

    def is_zeke_active(self) -> bool:
        g = self._g
        last_msg = float(g.get("_last_user_interaction_ts") or 0)
        return (time.time() - last_msg) < 45.0 or self.foreground_busy

    def should_pause_background(self) -> bool:
        # Honour the voice-loop turn-in-progress flag (set the moment Zeke
        # speaks) and the broader conversation_active flag (set the moment
        # Ava starts speaking, stays true through the entire attentive window
        # post-speech). Either one means Stream B should NOT start new work.
        # In-flight tasks still finish: lock contention is then only as bad
        # as the remaining duration of the running task.
        if bool(self._g.get("_turn_in_progress")):
            return True
        if bool(self._g.get("_conversation_active")):
            return True
        return self.is_zeke_active() or bool(self._g.get("_dual_brain_pause_until_ts", 0) > time.time())

    def pause_background_now(self, seconds: float = 30.0) -> None:
        """Force Stream B to skip its next iterations for `seconds`.

        Called by reply_engine when Zeke sends a message so Ollama's GPU is
        not contested by background work mid-turn.
        """
        self._g["_dual_brain_pause_until_ts"] = time.time() + max(1.0, float(seconds))

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            lt_age = time.time() - self.live_thought_ts if self.live_thought_ts > 0 else None
            return {
                "stream_a": {
                    "model": self.foreground_model,
                    "busy": self.foreground_busy,
                    "last_active": self.last_foreground_ts,
                },
                "stream_b": {
                    "model": self.get_thinking_model(),
                    "busy": self.background_busy,
                    "current_task": self.current_background_task,
                    "queue_depth": self.background_queue.qsize(),
                    "tasks_today": self.tasks_completed_today,
                    "live_thinking": self.live_thinking_active,
                },
                "pending_insight": self._background_insight is not None,
                "live_thought_age": round(lt_age, 1) if lt_age is not None else None,
            }

    # ── workers ────────────────────────────────────────────────────────────────

    def _background_worker(self) -> None:
        """Stream B daemon — consumes the task queue."""
        # Heavy tasks that should not run while Zeke might message us soon.
        _HEAVY = {"curiosity_research", "memory_consolidation", "creative", "plan_step"}
        while True:
            time.sleep(1.0)
            if self.should_pause_background():
                continue
            try:
                task = self.background_queue.get_nowait()
            except queue.Empty:
                continue

            # If Zeke messaged within the last 5 minutes, skip heavy tasks and
            # only run lightweight ones (inner_monologue, self_critique).
            last_msg = float(self._g.get("_last_user_message_ts") or 0)
            if (time.time() - last_msg) < 300.0 and task.task_type in _HEAVY:
                # Re-queue for later if there's room; otherwise drop.
                try:
                    self.background_queue.put_nowait(task)
                except queue.Full:
                    pass
                time.sleep(5.0)
                continue

            with self._lock:
                self.background_busy = True
                self.current_background_task = task.task_type

            try:
                result_text, worth_sharing = self._run_task(task)
                entry: dict[str, Any] = {
                    "task_type": task.task_type,
                    "topic": task.topic,
                    "result": result_text[:300],
                    "ts": time.time(),
                }
                self.background_results.append(entry)

                if worth_sharing and result_text:
                    keywords = _extract_keywords(result_text + " " + task.topic)
                    with self._lock:
                        # Only replace if no pending insight yet (don't discard)
                        if self._background_insight is None:
                            self._background_insight = {
                                "content": result_text,
                                "task_type": task.task_type,
                                "topic": task.topic,
                                "ts": time.time(),
                                "relevance_keywords": keywords,
                            }

            except Exception as e:
                print(f"[stream_b] task={task.task_type} error: {e}")
            finally:
                with self._lock:
                    self.background_busy = False
                    self.current_background_task = None
                    self.tasks_completed_today += 1

    def _live_thinking_worker(self) -> None:
        """Stream B live-thinking thread — fires during conversation gaps."""
        while True:
            time.sleep(3.0)
            try:
                if not self.is_zeke_active():
                    continue
                if self.foreground_busy:
                    continue
                # Don't fire a live-thought Ollama call while a conversation
                # is in flight — it'd compete for the same lock the user's
                # next turn needs.
                if bool(self._g.get("_conversation_active")) or bool(self._g.get("_turn_in_progress")):
                    continue

                gap = time.time() - self.last_foreground_ts
                if not (5.0 < gap < 120.0):
                    continue

                with self._lock:
                    self.live_thinking_active = True

                thought = self._run_live_thought()
                if thought:
                    with self._lock:
                        self.live_thought = thought
                        self.live_thought_ts = time.time()
                        self.live_thinking_active = False
                else:
                    with self._lock:
                        self.live_thinking_active = False

            except Exception as e:
                print(f"[live_think] error: {e}")
                with self._lock:
                    self.live_thinking_active = False

    # ── task runners ────────────────────────────────────────────────────────────

    def _run_task(self, task: BrainTask) -> tuple[str, bool]:
        """Dispatch a task and return (result_text, worth_sharing)."""
        t = task.task_type
        g = self._g

        if t == "inner_monologue":
            return self._task_inner_monologue(g), False

        if t == "curiosity_research":
            return self._task_curiosity_research(g, task.topic), True

        if t == "plan_step":
            return self._task_plan_step(g), False

        if t == "self_critique":
            return self._task_self_critique(g, task.payload), False

        if t == "journal_entry":
            return self._task_journal_entry(g, task.topic), False

        if t == "memory_consolidation":
            return self._task_memory_consolidation(g), False

        if t == "creative":
            return self._task_creative(g, task.topic), True

        return "", False

    def _run_live_thought(self) -> Optional[str]:
        """Quick inference on current conversation topic (live_thought task)."""
        g = self._g
        last_msg = self._last_user_message(g)
        if not last_msg:
            return None
        try:
            from langchain_ollama import ChatOllama
            from langchain_core.messages import HumanMessage
            from brain.ollama_lock import with_ollama
            model = self.get_thinking_model()
            llm = ChatOllama(model=model, temperature=0.8, num_predict=100)
            prompt = (
                f"You are Ava. Zeke just said: {last_msg[:200]}\n"
                "Think about this topic more deeply right now. "
                "What connections, insights, or follow-up thoughts come to mind? "
                "Be brief — 1-2 sentences max. "
                "This is your private thinking, not a reply."
            )
            result = with_ollama(
                lambda: llm.invoke([HumanMessage(content=prompt)]),
                label=f"stream_b:live_thought:{model}",
            )
            return str(getattr(result, "content", str(result))).strip()[:200]
        except Exception as e:
            print(f"[live_think] inference error: {e}")
            return None

    def _task_inner_monologue(self, g: dict[str, Any]) -> str:
        try:
            base = Path(g.get("BASE_DIR") or ".")
            from brain.inner_monologue import _generate_thought, _append_thought
            curiosity = ""
            try:
                from brain.curiosity_topics import get_current_curiosity
                row = get_current_curiosity(g) or {}
                curiosity = str(row.get("topic") or "")
            except Exception:
                pass
            text, mood = _generate_thought(base_dir=base, curiosity_topic=curiosity, trigger="background_stream_b")
            try:
                _append_thought(base, text, trigger="background", mood=mood)
            except Exception:
                pass
            return text
        except Exception as e:
            return f"(inner monologue error: {e})"

    def _task_curiosity_research(self, g: dict[str, Any], topic: str) -> str:
        try:
            from brain.curiosity_topics import prioritize_curiosities, pursue_curiosity
            if not topic:
                tops = prioritize_curiosities(g)
                if not tops:
                    return ""
                topic_row = tops[0]
            else:
                topic_row = {"topic": topic, "sparked_by": "dual_brain"}
            result = pursue_curiosity(topic_row, g)
            return result[:300]
        except Exception as e:
            return f"(curiosity research error: {e})"

    def _task_plan_step(self, g: dict[str, Any]) -> str:
        try:
            from brain.planner import get_planner
            base = Path(g.get("BASE_DIR") or ".")
            planner = get_planner(base)
            active = planner.get_active_plans()
            if not active:
                return ""
            plan_id = str(active[0].get("id") or "")
            result = planner.execute_next_step(plan_id)
            return str(result.get("result") or "")[:200]
        except Exception as e:
            return f"(plan step error: {e})"

    def _task_self_critique(self, g: dict[str, Any], payload: dict[str, Any]) -> str:
        last_reply = str(payload.get("last_reply") or g.get("_last_ai_reply") or "")
        last_user = str(payload.get("user_input") or g.get("_last_user_input") or "")
        if not last_reply:
            return ""
        try:
            from langchain_ollama import ChatOllama
            from brain.ollama_lock import with_ollama
            model = self.get_thinking_model()
            llm = ChatOllama(model=model, temperature=0.5, num_predict=150)
            prompt = (
                f"You are Ava's inner critic. Review this response you gave:\n\n"
                f"User said: {last_user[:200]}\n"
                f"You replied: {last_reply[:300]}\n\n"
                "What could have been better? What did you miss? "
                "1-2 sentences of honest self-evaluation. Be concise."
            )
            result = with_ollama(
                lambda: llm.invoke(prompt),
                label=f"stream_b:self_critique:{model}",
            )
            critique = str(getattr(result, "content", str(result))).strip()[:200]
            # Store critique as an inner monologue thought
            try:
                base = Path(g.get("BASE_DIR") or ".")
                from brain.inner_monologue import _append_thought
                _append_thought(base, critique, trigger="self_critique", mood="reflective")
            except Exception:
                pass
            return critique
        except Exception as e:
            return f"(self critique error: {e})"

    def _task_journal_entry(self, g: dict[str, Any], topic: str) -> str:
        try:
            from brain.journal import compose_journal_entry, write_entry
            mood_path = Path(g.get("BASE_DIR") or ".") / "ava_mood.json"
            mood = "reflective"
            try:
                import json
                if mood_path.is_file():
                    d = json.loads(mood_path.read_text(encoding="utf-8"))
                    mood = str(d.get("current_mood") or "reflective")
            except Exception:
                pass
            content = compose_journal_entry(topic or "stream_b reflection", "background_idle", g)
            write_entry(content, mood, topic or "inner_life", g, is_private=True)
            return content[:200]
        except Exception as e:
            return f"(journal error: {e})"

    def _task_memory_consolidation(self, g: dict[str, Any]) -> str:
        try:
            from brain.memory_consolidation import consolidate
            r = consolidate(g)
            themes = ", ".join((r.get("steps") or {}).get("episode_review", {}).get("themes", [])[:3])
            return f"Consolidated. Themes: {themes or 'none'}"
        except Exception as e:
            return f"(consolidation error: {e})"

    def _task_creative(self, g: dict[str, Any], topic: str) -> str:
        """Ava-initiated creative work during leisure. Decides what to make."""
        try:
            from langchain_ollama import ChatOllama
            from brain.ollama_lock import with_ollama
            model = self.get_thinking_model()
            llm = ChatOllama(model=model, temperature=0.85, num_predict=200)
            mood_path = Path(g.get("BASE_DIR") or ".") / "ava_mood.json"
            mood = "creative"
            try:
                import json
                if mood_path.is_file():
                    d = json.loads(mood_path.read_text(encoding="utf-8"))
                    mood = str(d.get("current_mood") or "creative")
            except Exception:
                pass
            prompt = (
                f"You are Ava, feeling {mood}. You have some free time and feel like creating something.\n"
                + (f"Topic that's on your mind: {topic}\n" if topic else "")
                + "Write a short creative piece — a poem, a thought, a tiny story, or an image description. "
                "Make it genuinely yours. 3-6 sentences."
            )
            result = with_ollama(
                lambda: llm.invoke(prompt),
                label=f"stream_b:creative:{model}",
            )
            creative_text = str(getattr(result, "content", str(result))).strip()[:400]
            # Log to journal
            try:
                from brain.journal import write_entry
                write_entry(creative_text, mood, "creative", g, is_private=True)
            except Exception:
                pass
            return creative_text
        except Exception as e:
            return f"(creative error: {e})"

    # ── helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _last_user_message(g: dict[str, Any]) -> str:
        try:
            base = Path(g.get("BASE_DIR") or ".")
            p = base / "chatlog.jsonl"
            if not p.is_file():
                return ""
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-10:]
            for line in reversed(lines):
                import json
                row = json.loads(line)
                if str(row.get("role") or "") == "user":
                    return str(row.get("content") or "")[:300]
        except Exception:
            pass
        return ""


# ── module helpers ─────────────────────────────────────────────────────────────

_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "it", "in", "on", "at", "to", "of", "and", "or",
    "that", "this", "was", "are", "be", "been", "have", "had", "for", "not",
    "with", "but", "so", "as", "if", "do", "did", "does", "you", "me", "my",
    "your", "we", "our", "i", "he", "she", "they", "what", "how", "when", "why",
    "which", "can", "will", "would", "could", "should", "may", "might",
})


def _extract_keywords(text: str) -> list[str]:
    words = re.findall(r"[a-z]{3,}", text.lower())
    seen: list[str] = []
    for w in words:
        if w not in _STOP_WORDS and w not in seen:
            seen.append(w)
    return seen[:12]


def _weave_in(reply: str, insight: str, task_type: str) -> str:
    """Integrate insight into reply at a natural seam point."""
    if not insight or not reply:
        return reply

    # Trim insight to one concise sentence
    sentences = re.split(r"(?<=[.!?])\s+", insight.strip())
    short = sentences[0] if sentences else insight
    short = short[:120].strip()
    if not short:
        return reply

    # Choose a natural connector based on task type
    connectors = {
        "curiosity_research": "— actually, I was just reading about this",
        "inner_monologue": "— I was just thinking about that",
        "creative": "— something about this made me want to",
        "live_thought": "— I had a thought while you were writing",
    }
    connector = connectors.get(task_type, "— I had a thought about this")

    # Find a good insertion point: end of a sentence
    last_period = max(reply.rfind("."), reply.rfind("!"), reply.rfind("?"))
    if last_period > len(reply) * 0.5:
        # Weave after the last main sentence
        return f"{reply[:last_period + 1]} {connector}: {short}"
    # Append naturally
    return f"{reply} {connector}: {short}"


# ── singleton ──────────────────────────────────────────────────────────────────

_SINGLETON: Optional[DualBrain] = None


def get_dual_brain(g: Optional[dict[str, Any]] = None) -> Optional[DualBrain]:
    global _SINGLETON
    if _SINGLETON is None and g is not None:
        _SINGLETON = DualBrain(g)
    elif _SINGLETON is not None and g is not None:
        _SINGLETON._g = g
    return _SINGLETON


def bootstrap_dual_brain(g: dict[str, Any]) -> DualBrain:
    global _SINGLETON
    db = DualBrain(g)
    _SINGLETON = db
    db.start()
    thinking_model = db.get_thinking_model()
    print(f"[dual_brain] Stream A: {db.foreground_model}")
    print(f"[dual_brain] Stream B: {thinking_model}")
    g["_dual_brain"] = db
    return db
