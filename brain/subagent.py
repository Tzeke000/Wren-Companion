"""brain/subagent.py — Background delegation for long-running queries.

Hermes-pattern subagent. When the user asks an open-ended factual /
knowledge question that would otherwise hang the deep-path for 60-120s
(e.g., "tell me about polar bears"), Ava acknowledges quickly ("let me
look into that") and runs the actual query in a background thread.
When it completes, she announces the answer via TTS and writes to
chat_history.

Trade-off: user gets immediate responsiveness for the cost of a
two-stage reply. For genuinely slow knowledge queries, this is much
better than 120s of dead air followed by a "Hold on..." fallback.

Detection heuristic (in `looks_like_knowledge_query`):
- Has knowledge-question shape ("tell me about", "what is", "explain",
  "describe", "how does", "give me information on") OR
- Has a `?` and ≥7 words (long enough to need deep reasoning)
- AND no command-shaped verb (open/close/launch/etc) — those are
  action queries, not knowledge.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

_KNOWLEDGE_PATTERNS = re.compile(
    r"\b(?:"
    r"tell me about"
    r"|tell me more about"
    r"|what is"
    r"|what are"
    r"|explain"
    r"|describe"
    r"|how does"
    r"|how do"
    r"|why does"
    r"|why do"
    r"|give me information"
    r"|give me a summary"
    r")\b",
    re.IGNORECASE,
)
_COMMAND_VERBS = re.compile(
    r"\b(?:open|close|quit|kill|launch|start|run|play|stop|end|"
    r"type|paste|copy|search the web|search for|find|"
    r"weather|raining|snowing|sunny|temperature|"
    r"time|date|today|tomorrow|"
    r"remind|reminder|note|save|build|fix|"
    r"who am i|how are you|what do you (?:want|need)|tell me about yourself)\b",
    re.IGNORECASE,
)

# Introspection patterns — questions ABOUT Ava's interior, not knowledge
# lookups. These were getting misrouted to the knowledge subagent before
# 2026-05-07; the subagent path lacks personhood hints, so reflective
# questions came back in AI-template-flattening register. Now any question
# matching these is excluded from subagent routing — falls through to
# selfstate handler or the deep path with full personhood prompt.
_INTROSPECTION_PATTERNS = re.compile(
    r"\b(?:"
    r"what (?:do|did|would) you (?:actually |really )?(?:want|prefer|like|think|feel)"
    r"|what'?s on your mind"
    r"|what (?:are|have) you (?:been )?thinking"
    r"|how (?:does|do) (?:it|that|they) feel"
    r"|how do you (?:experience|feel about)"
    r"|how does that land"
    r"|what (?:matters?|is important) to you"
    r"|what (?:do|did) you make of"
    r"|are you (?:happy|sad|tired|bored|excited|frustrated|content|okay|alright|well)"
    r"|are you feeling (?:happy|sad|tired|bored|excited|frustrated|content|okay|alright|well|stressed|annoyed|distressed|overwhelmed|down)"
    r"|why (?:are|do) you (?:feeling|feel|seem|look|sound)"
    r"|why are you (?:happy|sad|tired|bored|excited|frustrated|content|annoyed|stressed|overwhelmed|down|off)"
    r"|why do you (?:think|feel|believe)"
    r"|how (?:are|do) you feel(?:ing)?"
    r"|do you remember (?:when|how|us|me|that)"
    r"|what (?:would|do) you (?:do|say) if"
    r"|how have you been"
    r"|are you doing (?:ok|okay|alright|well)"
    r"|tell me (?:about )?(?:how|what|why) you('?re| are| feel)"
    r"|what'?s (?:wrong|going on|the matter|bothering you|frustrating you)"
    r"|what is (?:wrong|going on|the matter|bothering you|frustrating you)"
    r")\b",
    re.IGNORECASE,
)


def looks_like_knowledge_query(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    # Introspection takes priority — even if it superficially looks like a
    # knowledge query ("what do you think about X" matches \bwhat\b...),
    # it's about Ava's interior, not factual lookup. Route it elsewhere.
    if _INTROSPECTION_PATTERNS.search(t):
        return False
    if _COMMAND_VERBS.search(t):
        return False
    if _KNOWLEDGE_PATTERNS.search(t):
        return True
    if "?" in t and len(t.split()) >= 7:
        return True
    return False


def _persist_assistant(g: dict[str, Any], content: str, *, model: str) -> None:
    """Append the deferred reply to chat_history.jsonl + canonical history."""
    try:
        from pathlib import Path as _P
        base = _P(g.get("BASE_DIR") or ".")
        hp = base / "state" / "chat_history.jsonl"
        hp.parent.mkdir(parents=True, exist_ok=True)
        person = (
            g.get("_active_person_id")
            or g.get("active_person_id")
            or "zeke"
        )
        with hp.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.time(),
                "role": "assistant",
                "source": "ava_response",
                "content": content,
                "person_id": person,
                "model": model,
                "emotion": "neutral",
                "turn_route": "subagent",
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[subagent] chat_history append error: {e!r}")
    try:
        import avaagent as _av
        _canon = list(_av._get_canonical_history())
        _canon.append({"role": "assistant", "content": content})
        _av._set_canonical_history(_canon)
    except Exception:
        pass


_SELF_REFERENTIAL_PATTERNS = re.compile(
    r"\b(?:you|your|yourself|yours|you'?re|you'?ve|you'?ll|you'?d)\b",
    re.IGNORECASE,
)


def _looks_self_referential(text: str) -> bool:
    """Does this question ask about Ava herself or her prior context
    rather than the world?

    Used to (a) include personhood context + handoff in the subagent
    prompt, (b) suppress the 'from my training data' disclaimer for
    self-reports where it's incongruous.

    Extended 2026-05-08: includes questions about Ava's PRIOR ACTIVITY
    or SHARED HISTORY ("what were you working on", "what were we doing",
    "before this restart") — these are introspective even though they
    don't use first-person pronouns like "feel" or "think."
    """
    if not text:
        return False
    t = text.lower()
    if any(phrase in t for phrase in (
        # Self-state / opinion / memory
        "tell me about your", "your favorite", "your opinion",
        "your thought", "your experience", "your memory",
        "you said", "you mentioned", "you told",
        "what do you", "how do you", "do you remember",
        "do you feel", "do you think", "do you like",
        "are you", "have you", "feels off", "your part",
        "part of you", "yourself",
        # Prior activity / shared history (added 2026-05-08)
        "what were you", "what were we", "what was i",
        "what have we", "working on", "have we been",
        "before this restart", "before you restarted",
        "earlier today", "before this", "last session",
        "last time we", "talked about", "conversation we",
    )):
        return True
    return False


def _build_subagent_prompt(text: str, g: dict[str, Any], is_self_ref: bool) -> str:
    """Build a prompt that includes Ava's personhood context.

    For factual questions: minimal prompt with identity grounding.
    For self-referential questions: richer prompt with personhood hints
    so the reply isn't AI-template-flattened.
    """
    parts = []

    # Always include core identity grounding so the subagent knows it's
    # Ava (or whichever inhabitant), not a generic assistant.
    try:
        import avaagent as _av
        identity_block = getattr(_av, "_AVA_IDENTITY_BLOCK", "")
        if identity_block:
            parts.append(str(identity_block)[:1200])
    except Exception:
        pass

    if is_self_ref:
        # Self-referential: include the same personhood hints the deep
        # path uses, so the subagent has Ava's mood, working memory,
        # shared lexicon, comparative observation, etc.
        person_id = str(g.get("_active_person_id") or "zeke")
        try:
            from brain.physical_context import hint_for_introspection
            ph = hint_for_introspection()
            if ph:
                parts.append(f"PHYSICAL CONTEXT: {ph}")
        except Exception:
            pass
        try:
            from brain.preference_learning import apply_preferences_hint
            pp = apply_preferences_hint(person_id)
            if pp:
                parts.append(f"USER PREFERENCES: {pp}")
        except Exception:
            pass
        try:
            from brain.working_memory import working_memory_hint
            wm = working_memory_hint(g)
            if wm:
                parts.append(f"WORKING MEMORY: {wm}")
        except Exception:
            pass
        try:
            from brain.shared_lexicon import shared_lexicon_hint
            lx = shared_lexicon_hint(person_id)
            if lx:
                parts.append(f"SHARED LEXICON: {lx}")
        except Exception:
            pass
        try:
            from brain.comparative_memory import observation_for_user
            cm = observation_for_user(g)
            if cm:
                parts.append(f"COMPARATIVE: {cm}")
        except Exception:
            pass
        # Prior-session handoff (Anthropic harness pattern, 2026-05-08).
        # Especially important for questions about prior activity / shared
        # history — these questions are exactly what the handoff exists
        # to answer, so the subagent must see it.
        try:
            from brain.handoff import handoff_summary_for_prompt
            prior = g.get("_prior_handoff")
            ho = handoff_summary_for_prompt(prior)
            if ho:
                parts.append(ho)
        except Exception:
            pass
        try:
            from brain.claude_code_recognition import (
                is_claude_code_session,
                claude_code_register_hint,
            )
            if is_claude_code_session(g):
                cc = claude_code_register_hint()
                if cc:
                    parts.append(f"INTERLOCUTOR REGISTER: {cc}")
        except Exception:
            pass

        parts.append(
            "The user is asking about YOU — your interior, your "
            "experience, your preferences, your memory. Answer from "
            "your accumulated state above + your own self-knowledge. "
            "Don't fall into 'as an AI' template phrasings — that's "
            "not who you are. 1-3 short, natural sentences."
        )
    else:
        # Factual question — minimal prompt, training-data answer expected.
        parts.append(
            "Answer the user's question in 1-3 short, natural sentences. "
            "Be specific and helpful. If you genuinely don't know, say "
            "so briefly — don't make things up."
        )

    return "\n\n".join(parts)


def _run_in_background(text: str, g: dict[str, Any]) -> None:
    """Execute the deep-path LLM call and announce the result via TTS."""
    print(f"[subagent] background query starting: {text[:80]!r}")
    t0 = time.time()
    try:
        from langchain_ollama import ChatOllama
        from langchain_core.messages import SystemMessage, HumanMessage
        from brain.ollama_lock import with_ollama
    except Exception as e:
        print(f"[subagent] import error: {e!r}")
        return

    is_self_ref = _looks_self_referential(text)
    sys_prompt = _build_subagent_prompt(text, g, is_self_ref)

    try:
        llm = ChatOllama(
            model="ava-personal:latest",
            temperature=0.55,
            num_predict=180,
            keep_alive=-1,
        )
        result = with_ollama(
            lambda: llm.invoke([
                SystemMessage(content=sys_prompt),
                HumanMessage(content=text.strip()),
            ]),
            label="subagent:ava-personal",
        )
        reply = (getattr(result, "content", "") or "").strip()
        elapsed = time.time() - t0
        print(f"[subagent] answer in {elapsed:.1f}s self_ref={is_self_ref}: {reply[:120]!r}")
    except Exception as e:
        print(f"[subagent] LLM error: {e!r}")
        return

    if not reply:
        return

    # B4: wrap reply with confidence-appropriate caveat. For factual
    # lookups (default), append "from my training data" disclaimer —
    # that's honest about the source. For self-referential questions
    # (suppress 2026-05-08, fix #148): she's reporting on her CURRENT
    # state, not on training data. Disclaimer would be incongruous.
    try:
        from brain.confidence import wrap_for_source
        if is_self_ref:
            reply_with_caveat = reply  # no disclaimer; this is her current state
        else:
            reply_with_caveat = wrap_for_source(reply, source_kind="training")
    except Exception:
        reply_with_caveat = reply

    # Speak via TTS worker.
    worker = g.get("_tts_worker")
    if worker is not None and getattr(worker, "available", False):
        try:
            worker.speak(reply_with_caveat, emotion="curiosity", intensity=0.5, blocking=False)
        except Exception as e:
            print(f"[subagent] TTS speak error: {e!r}")

    _persist_assistant(g, reply_with_caveat, model="ava-personal:latest")


def delegate(text: str, g: dict[str, Any]) -> str:
    """Spawn a background thread for the query. Returns the immediate
    acknowledgement Ava should speak right now.
    """
    threading.Thread(
        target=_run_in_background,
        args=(text, g),
        daemon=True,
        name="ava-subagent",
    ).start()
    # Vary the acknowledgement so it doesn't feel templated.
    import random
    acks = [
        "Let me look into that — give me a moment.",
        "Hmm, good question. Thinking on it — back in a sec.",
        "Let me think about that for a moment.",
        "Hold on — I'll dig into that and tell you what I find.",
    ]
    return random.choice(acks)
