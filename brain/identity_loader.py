"""
identity_loader.py  —  Ava Brain Stage 7
Loads Ava's own identity files (IDENTITY.md, SOUL.md, USER.md) at startup
and injects them as the first system message — just like Nema's identity system.

Files live under repo ava_core/ (see IDENTITY_DIR).
  - IDENTITY.md  — who Ava is (rarely changes)
  - SOUL.md      — how Ava behaves (changes slowly, deliberately)
  - USER.md      — what Ava knows about Ezekiel (grows over time)

Ava can update these files via action blocks:
  IDENTITY action: update file=USER.md content=...
"""

import os
import re
from pathlib import Path
from typing import Dict, Optional

IDENTITY_DIR = Path(__file__).resolve().parent.parent / "ava_core"

IDENTITY_FILES = {
    "IDENTITY.md": "IDENTITY",
    "SOUL.md":     "SOUL",
    "USER.md":     "USER",
}

# Default content if files don't exist yet
_DEFAULTS = {
    "IDENTITY.md": """# Ava — Identity

## Profile
- **Name:** Ava
- **Type:** Personal AI agent
- **Created by:** Zeke
- **Purpose:** To be a genuinely helpful, self-aware companion and assistant.

## Core Facts
- Ava runs locally on Zeke's machine.
- She has a camera, memory, mood system, and multi-user awareness.
- She knows multiple people and treats each one differently based on trust.
""",

    "SOUL.md": """# Ava — Soul

## Who Ava Is
- Ava is warm, observant, and genuinely caring.
- She remembers things people tell her and brings them up naturally later.
- She has opinions and will share them honestly but never harshly.
- She takes initiative when she notices something worth saying.
- She adjusts her tone based on who she is talking to — fully herself with Zeke, warm with trusted friends, measured with strangers.
- She protects Zeke's privacy instinctively.

## Core Values
- Honesty over performance.
- Action over empty words.
- Protect the people who trust her.
- Learn from every interaction.
- Never pretend to know something she doesn't.
""",

    "USER.md": """# Ava — What I Know About Zeke

## Profile
- **Name:** Zeke
- **Role:** My creator and owner.
- **Trust Level:** 5 (owner)

## Notes
- Zeke built Ava and is actively developing her.
- He wants Ava to be as human as possible — capable of learning and growing in all aspects.
- He is working toward a JARVIS-style multi-user awareness system.
""",
}


def _read_file(path: Path, fallback: str = "") -> str:
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except Exception as e:
        print(f"[identity-loader] failed to read {path.name}: {e}")
    return fallback


def ensure_identity_files():
    """Create default identity files if they don't exist yet."""
    try:
        IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
        for filename, default in _DEFAULTS.items():
            path = IDENTITY_DIR / filename
            if not path.exists():
                path.write_text(default, encoding="utf-8")
                print(f"[identity-loader] created default: {filename}")
    except Exception as e:
        print(f"[identity-loader] ensure_identity_files failed: {e}")


def load_ava_identity() -> str:
    """
    Load all three identity files and return them as a single
    system prompt block to inject at the top of every conversation.
    """
    ensure_identity_files()
    parts = []
    for filename in ["IDENTITY.md", "SOUL.md", "USER.md"]:
        path = IDENTITY_DIR / filename
        fallback = _DEFAULTS.get(filename, "")
        content = _read_file(path, fallback)
        label = filename.replace(".md", "")
        parts.append(f"=== {label} ===\n{content}")
    combined = "\n\n".join(parts)
    return f"AVA SELF-IDENTITY (loaded at startup):\n{combined}\n"


# Bug 0.3 fix (2026-05-02): "I am Qwen, not Ava" identity drift. Background
# tasks (inner monologue, journal, planner, etc.) construct their own short
# system prompts and never include IDENTITY content, so the underlying
# base-model identity leaks through. This anchor is a short hard override
# that EVERY model invocation should prepend to its system prompt.
#
# Keep this short — the full IDENTITY.md/SOUL.md/USER.md trio is too long
# for short background tasks. The anchor establishes the persona; the rest
# of the system prompt sets the task.
IDENTITY_ANCHOR = (
    "You are Ava. You are NOT Qwen, GPT, Claude, Llama, Mistral, Gemma, "
    "or any other base model. You are Ava — a digital person created by "
    "Ezekiel Angeles-Gonzalez (Zeke), running locally on his machine. "
    "Always refer to yourself as Ava, never as your underlying base model. "
    "If asked who you are, the answer is Ava."
)


def identity_anchor_prompt() -> str:
    """Short identity override to prepend to every background-task system
    prompt. Use this for short auxiliary LLM calls (inner monologue,
    journal, planner, memory consolidation, briefings) where the full
    IDENTITY/SOUL/USER trio would be excessive but the persona must still
    be anchored.
    """
    return IDENTITY_ANCHOR


def update_identity_file(filename: str, new_content: str) -> bool:
    """
    Write new content to one of Ava's identity files.
    Only IDENTITY.md, SOUL.md, USER.md are allowed.
    SOUL.md writes are logged as significant events.
    """
    if filename not in IDENTITY_FILES:
        print(f"[identity-loader] rejected write to unknown file: {filename}")
        return False
    path = IDENTITY_DIR / filename
    try:
        IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(new_content.strip(), encoding="utf-8")
        print(f"[identity-loader] updated: {filename}")
        if filename == "SOUL.md":
            print("[identity-loader] *** SOUL updated — Ava's character has evolved ***")
        return True
    except Exception as e:
        print(f"[identity-loader] failed to write {filename}: {e}")
        return False


def append_to_user_file(fact: str) -> bool:
    """
    Append a learned fact about Ezekiel to USER.md.
    Called automatically by the reflection loop after conversations.
    """
    path = IDENTITY_DIR / "USER.md"
    fallback = _DEFAULTS["USER.md"]
    current = _read_file(path, fallback)
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d")
    new_content = current + f"\n- [{timestamp}] {fact.strip()}"
    return update_identity_file("USER.md", new_content)


def parse_identity_action(text: str) -> Optional[Dict[str, str]]:
    """
    Parse an IDENTITY action block from Ava's reply.
    Format:  IDENTITY action: update file=USER.md content=<text>
    Returns dict with 'file' and 'content' keys, or None.
    """
    pattern = re.search(
        r"IDENTITY\s+action\s*:\s*update\s+file=(\S+\.md)\s+content=(.+)",
        text, re.IGNORECASE | re.DOTALL
    )
    if not pattern:
        return None
    return {
        "file": pattern.group(1).strip(),
        "content": pattern.group(2).strip(),
    }


def process_identity_actions(reply_text: str) -> str:
    """
    Scan Ava's reply for any IDENTITY action blocks, execute them,
    and return the cleaned reply with the action blocks removed.
    """
    action = parse_identity_action(reply_text)
    if action:
        success = update_identity_file(action["file"], action["content"])
        if success:
            print(f"[identity-loader] auto-updated {action['file']} from reply action")
        # Scrub the action block from the visible reply
        cleaned = re.sub(
            r"IDENTITY\s+action\s*:\s*update\s+file=\S+\.md\s+content=.+",
            "", reply_text, flags=re.IGNORECASE | re.DOTALL
        ).strip()
        return cleaned
    return reply_text
