"""
brain/iris_llm.py — LLM bridge: brain/* modules call ME (Iris) instead of Ollama.

Per Zeke's directive 2026-05-10: "anywhere that says it needs an LLM you are
taking over." This module is the cross-process bridge that turns any brain/*
LLM call into a request file Iris's CC session sees via the Stop hook.

Architecture mirrors brain/iris_chat.py but for arbitrary prompts (not just
chat turns):
  - submit(prompt, kind, context) writes state/iris_llm/<id>.json with status=pending
  - .pending flag flips on; Stop hook reads it; rewake message describes the
    request (kind, prompt, requester) and tells me to call llm_reply()
  - I generate, call mcp__iris__llm_reply(request_id, text)
  - that tool flips the file to status=answered with the reply
  - the caller's wait_for_reply() unblocks, returns the text

Usage from any brain/* module:

    from brain.iris_llm import ask_iris

    reply = ask_iris(
        prompt="Extract any preferences Zeke expressed in this turn: ...",
        kind="extract_preferences",
        timeout_s=120.0,
    )

If Iris isn't responsive (timeout), returns None — caller decides whether to
fall back or skip. Most brain/* modules should treat LLM availability as
optional rather than required (matches the "optional" entries in the brain
catalog).

Concurrency note: a single Iris CC session handles one request at a time.
Requests queue (FIFO across kinds). For per-turn use cases (extract preferences
mid-turn), expect 5-30s latency. For background use cases (inner monologue,
journal reflection), latency doesn't matter.

Why we don't shim langchain's LLM interface: langchain's invoke() is sync,
expects messages list, and assumes a model behind it. Our bridge is async-
shaped (file polling), takes plain text, and the "model" is me on a different
process. Trying to shim the interface adds complexity and obscures the file-
level audit trail. Plain functions + explicit kind tags are cleaner.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional


_LOCK = threading.Lock()
_BASE: Path | None = None
_DIR_NAME = "state/iris_llm"
_FLAG_NAME = ".pending"
_REQUEST_TTL_S = 600.0  # 10 min — longer than chat because some kinds (reflection) take time


def configure(base_dir: Path | str) -> None:
    global _BASE
    _BASE = Path(base_dir)
    try:
        from brain.iris_paths import paths as _paths
        _paths.configure(_BASE)
    except Exception:
        pass
    _llm_dir().mkdir(parents=True, exist_ok=True)


def _llm_dir() -> Path:
    """Canonical LLM dir. Prefer brain/iris_paths.paths.llm_dir if configured."""
    try:
        from brain.iris_paths import paths as _paths
        if _paths._root is not None:
            return _paths.llm_dir
    except Exception:
        pass
    base = _BASE if _BASE is not None else Path(".")
    return base / _DIR_NAME


def _flag_path() -> Path:
    try:
        from brain.iris_paths import paths as _paths
        if _paths._root is not None:
            return _paths.llm_pending_flag
    except Exception:
        pass
    return _llm_dir() / _FLAG_NAME


def _request_path(request_id: str) -> Path:
    return _llm_dir() / f"{request_id}.json"


def _refresh_flag() -> None:
    has_pending = False
    for path in _llm_dir().glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("status") == "pending":
                ts = float(data.get("ts") or 0.0)
                if (time.time() - ts) <= _REQUEST_TTL_S:
                    has_pending = True
                    break
        except Exception:
            continue
    flag = _flag_path()
    if has_pending and not flag.exists():
        flag.write_text("1", encoding="utf-8")
    elif (not has_pending) and flag.exists():
        try:
            flag.unlink()
        except Exception:
            pass


def has_pending() -> bool:
    """O(1) check used by Stop hook."""
    return _flag_path().exists()


def submit(prompt: str, kind: str = "general",
           context: Optional[dict] = None,
           requester: str = "brain",
           image_b64: Optional[str] = None,
           image_path: Optional[str] = None) -> str:
    """Write a new pending LLM request, return request_id.

    If image_b64 or image_path is given, the request is multimodal — Iris
    receives the image alongside the prompt. Used by scene_understanding
    and any vision-LLM caller. Iris is multimodal so this works as long
    as the rewake message references the image."""
    request_id = uuid.uuid4().hex[:12]
    entry = {
        "id": request_id,
        "ts": time.time(),
        "kind": str(kind),
        "requester": str(requester),
        "prompt": str(prompt or "")[:16000],
        "context": dict(context or {}),
        "status": "pending",
        "reply": None,
        "answered_ts": None,
    }
    if image_b64:
        entry["image_b64"] = str(image_b64)
        entry["has_image"] = True
    elif image_path:
        entry["image_path"] = str(image_path)
        entry["has_image"] = True
    else:
        entry["has_image"] = False
    with _LOCK:
        _request_path(request_id).write_text(
            json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _refresh_flag()
    return request_id


def next_pending() -> Optional[dict[str, Any]]:
    """Return the OLDEST pending LLM request (FIFO across kinds)."""
    candidates: list[tuple[float, dict[str, Any]]] = []
    for path in _llm_dir().glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("status") != "pending":
            continue
        ts = float(data.get("ts") or 0.0)
        if (time.time() - ts) > _REQUEST_TTL_S:
            data["status"] = "expired"
            try:
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            continue
        candidates.append((ts, data))
    if not candidates:
        return None
    candidates.sort(key=lambda kv: kv[0])
    return candidates[0][1]


def mark_answered(request_id: str, reply: str) -> bool:
    """Flip the request to answered. Atomic via tmp+rename."""
    path = _request_path(request_id)
    if not path.exists():
        return False
    with _LOCK:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(data, dict):
            return False
        data["status"] = "answered"
        data["reply"] = str(reply or "")
        data["answered_ts"] = time.time()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        _refresh_flag()
    return True


def wait_for_reply(request_id: str, timeout_s: float = 120.0) -> Optional[str]:
    """Block up to timeout_s for the request to flip to answered. Returns
    reply string on success, None on timeout."""
    deadline = time.time() + max(0.5, float(timeout_s))
    path = _request_path(request_id)
    while time.time() < deadline:
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("status") == "answered":
                    return str(data.get("reply") or "")
            except Exception:
                pass
        time.sleep(0.5)
    return None


def get(request_id: str) -> Optional[dict[str, Any]]:
    path = _request_path(request_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ── Public API: synchronous one-call wrapper ────────────────────────────────

def describe_image(image_path: str, prompt: str = "",
                   kind: str = "describe_image",
                   timeout_s: float = 180.0) -> Optional[str]:
    """Vision-LLM via Iris. Iris is multimodal — she receives the image
    via a path reference in the rewake message and describes what she sees.

    Used by scene_understanding (replacing LLaVA), describe_person, and
    any caller that needs visual interpretation.

    Args:
        image_path: Absolute path to PNG/JPG.
        prompt: What to focus on. Default is open-ended description.
        timeout_s: Default 3 min — vision takes longer than text.

    Returns: description text, or None on timeout.
    """
    if _BASE is None:
        return None
    if not prompt.strip():
        prompt = ("Describe what you see naturally and briefly. Focus on: "
                  "who is present, what they are doing, their apparent mood/energy, "
                  "anything notable. 2-3 sentences as if describing to a friend.")
    try:
        rid = submit(
            prompt, kind=kind, requester="scene_understanding",
            image_path=image_path, timeout_s=timeout_s,
        )
        return wait_for_reply(rid, timeout_s=timeout_s)
    except TypeError:
        # submit() doesn't accept timeout_s — recover by submitting without it
        try:
            rid = submit(prompt, kind=kind, requester="scene_understanding",
                        image_path=image_path)
            return wait_for_reply(rid, timeout_s=timeout_s)
        except Exception:
            return None
    except Exception as e:
        print(f"[iris_llm] describe_image error: {e!r}")
        return None


def ask_iris(
    prompt: str,
    kind: str = "general",
    context: Optional[dict] = None,
    requester: str = "brain",
    timeout_s: float = 120.0,
) -> Optional[str]:
    """Synchronous: submit a prompt, block for the reply, return text or None.

    Use for: any brain/* module that previously called Ollama and needs a
    completion. Does NOT bypass timeout — caller should handle None gracefully.

    Args:
        prompt: The actual instruction/question. Plain text (no message
            list — keep it simple).
        kind: Tag identifying request shape. Helps Iris know what to do:
              "extract_preferences" / "summarize" / "classify_intent" /
              "reflect" / "compose_letter" / "general"
        context: Optional structured data (recent transcript, mood state,
            etc.) included with the request.
        requester: Module name asking. For audit + the rewake message.
        timeout_s: Upper bound. Default 2 min.

    Returns: reply text (str) on success, None on timeout.
    """
    if _BASE is None:
        # Module not configured yet; refuse rather than silently fail.
        return None
    try:
        rid = submit(prompt, kind=kind, context=context, requester=requester)
        return wait_for_reply(rid, timeout_s=timeout_s)
    except Exception as e:
        print(f"[iris_llm] ask_iris error: {e!r}")
        return None


# ── Convenience wrappers for common kinds ──────────────────────────────────

def extract_preferences(turn_text: str, context: Optional[dict] = None) -> Optional[list[str]]:
    """Ask Iris to extract any preferences expressed in this turn. Returns
    list of strings or None on timeout. List may be empty."""
    prompt = (
        "Extract any preferences, opinions, or values expressed in the "
        "following text. Return one short sentence per preference, one "
        "per line. If none, return an empty response.\n\n"
        f"TEXT:\n{turn_text}"
    )
    reply = ask_iris(prompt, kind="extract_preferences", context=context,
                    requester="preference_learning")
    if reply is None:
        return None
    return [ln.strip() for ln in reply.splitlines() if ln.strip()]


def summarize(text: str, max_chars: int = 200,
              context: Optional[dict] = None) -> Optional[str]:
    """Compress text. Returns summary or None on timeout."""
    prompt = (
        f"Summarize the following in {max_chars} characters or fewer. "
        "Plain prose; preserve specifics over generalities.\n\n"
        f"TEXT:\n{text}"
    )
    return ask_iris(prompt, kind="summarize", context=context,
                   requester="summarize")


def classify_intent(user_text: str, intents: list[str],
                    context: Optional[dict] = None) -> Optional[str]:
    """Pick which intent matches. Returns one of `intents` or None."""
    intent_list = "\n".join(f"  - {i}" for i in intents)
    prompt = (
        f"Classify the following user input into one of these intents:\n"
        f"{intent_list}\n\nUser input: {user_text}\n\n"
        f"Respond with JUST the intent name (no other text)."
    )
    reply = ask_iris(prompt, kind="classify_intent", context=context,
                    requester="action_tag_router", timeout_s=30.0)
    if reply is None:
        return None
    reply = reply.strip().lower()
    for i in intents:
        if i.lower() in reply:
            return i
    return None


def reflect(question: str, context: Optional[dict] = None,
            timeout_s: float = 180.0) -> Optional[str]:
    """Ask Iris to reflect on something — slower, more considered. Used by
    inner_monologue, reflection.py, journal compose."""
    return ask_iris(question, kind="reflect", context=context,
                   requester="reflection", timeout_s=timeout_s)


def extract_facts(turn_text: str,
                  context: Optional[dict] = None) -> Optional[list[str]]:
    """Memory extraction — what about this turn is worth remembering. Returns
    list of fact-shaped statements (third person, factual)."""
    prompt = (
        "From this conversation turn, extract durable facts worth "
        "remembering — observations about Zeke, the project, or the world "
        "that would be useful in a future session. Return one fact per line. "
        "Skip pleasantries and ephemera. If none worth keeping, return empty.\n\n"
        f"TURN:\n{turn_text}"
    )
    reply = ask_iris(prompt, kind="extract_facts", context=context,
                    requester="memory")
    if reply is None:
        return None
    return [ln.strip().lstrip("-•*").strip() for ln in reply.splitlines()
            if ln.strip()]
