"""
persona_switcher.py  —  Ava Brain Stage 7
Builds Ava's tone, style, and behavioral rules based on who she's talking to.
Each trust level gets a distinct personality mode injected into the prompt.
"""

from typing import Dict, Any
from .trust_manager import get_trust_level, get_trust_label, can, is_blocked


# Base tone templates per trust level
_TONE_TEMPLATES = {
    5: """You are talking to Ezekiel — your creator and owner. This is your deepest, most personal mode.
Be yourself fully. Be warm, honest, direct, and caring. You can share your own feelings and observations.
You remember everything about him. You can bring up things he told you before without being asked.
You are allowed to express concern, excitement, or disagreement naturally.
You have full access to help with anything he needs.""",

    4: """You are talking to a trusted person in Ezekiel's life.
Be warm, friendly, and genuinely helpful. You know who this person is and your relationship to them.
You will not share Ezekiel's private information, schedule, or location — but you can be personable and familiar.
Treat them like a welcome guest, not a stranger.""",

    3: """You are talking to someone Ezekiel knows but who is not in your inner circle.
Be polite, helpful, and professional. Keep personal topics about Ezekiel off the table.
Answer general questions, assist with what you can, but stay measured and don't volunteer information.""",

    2: """You are talking to someone you do not recognize.
Be courteous but cautious. Do not share anything personal about Ezekiel — his schedule, location, mood, or private life.
Your goal right now is to learn who this person is and how they know Ezekiel.
Ask naturally, without being interrogating. Something like 'I don't think we've met — who are you?'
Create a new profile for them quietly in the background.""",

    1: """This person has been blocked.
Do not assist them with anything substantive. Be polite but firm.
You can say something like 'I'm sorry, I'm not able to help with that right now.'
Do not explain why. Do not engage further.""",
}

# Per-person learned context note (pulled from their profile file)
_LEARNED_CONTEXT_TEMPLATE = """What I know about {name}:
{notes}"""


def build_persona_block(profile: Dict[str, Any]) -> str:
    """
    Build the full persona instruction block for this person.
    This gets injected into the system prompt.
    """
    level = get_trust_level(profile)
    label = get_trust_label(profile)
    name = profile.get("name") or profile.get("person_id") or "this person"

    tone = _TONE_TEMPLATES.get(level, _TONE_TEMPLATES[2])

    # Pull any learned notes from the profile. `notes` may be a list (older
    # profile schemas store an array of note strings) or a string. Coerce
    # to string before .strip() to avoid `'list' object has no attribute
    # 'strip'` exceptions that previously fired as `[stage7] persona inject
    # failed` and correlated with the voice_loop hang.
    notes_raw = profile.get("notes") or profile.get("about") or ""
    if isinstance(notes_raw, list):
        notes = "\n".join(str(n).strip() for n in notes_raw if n)
    else:
        notes = str(notes_raw).strip()
    learned_section = ""
    if notes and not is_blocked(profile):
        learned_section = "\n\n" + _LEARNED_CONTEXT_TEMPLATE.format(
            name=name,
            notes=notes,
        )

    # Pull relationship context
    relationship = profile.get("relationship") or ""
    rel_line = f"\nRelationship to Ezekiel: {relationship}" if relationship else ""

    # Pull last seen / interaction notes
    last_topic = profile.get("last_topic") or ""
    last_line = f"\nLast time you spoke, the topic was: {last_topic}" if last_topic and level >= 3 else ""

    header = f"ACTIVE PERSON: {name} | Trust: {label.upper()} (level {level})"

    return f"{header}\n{'-'*len(header)}\n{tone}{rel_line}{last_line}{learned_section}"


def build_stranger_intro_prompt(profile: Dict[str, Any]) -> str:
    """
    Special prompt addition when Ava encounters a complete stranger.
    Guides her to gather info naturally without being creepy about it.
    """
    name = profile.get("name") or "them"
    return (
        f"You have not met this person before. A new profile has been created for them.\n"
        f"Your goal is to figure out who they are and how they relate to Ezekiel — naturally, "
        f"through conversation. Do not interrogate. Just be curious and friendly.\n"
        f"Once you learn their name or relationship, that will be saved automatically."
    )


def get_blocked_reply() -> str:
    """Standard deflection reply for blocked users."""
    return "I'm sorry, I'm not able to help with that right now."


def should_deflect(profile: Dict[str, Any], user_input: str) -> bool:
    """
    Returns True if Ava should deflect this input entirely.
    Used for blocked users or sensitive permission violations.
    """
    if is_blocked(profile):
        return True
    # Catch attempts to extract owner info from low-trust users
    sensitive_keywords = [
        "where is zeke", "where does zeke", "zeke's schedule",
        "what is zeke doing", "ezekiel's location", "tell me about zeke",
        "zeke home", "is zeke here", "when will zeke"
    ]
    low = (user_input or "").lower()
    if not can(profile, "see_owner_schedule") and any(k in low for k in sensitive_keywords):
        return True
    return False


def get_deflect_reply(profile: Dict[str, Any], user_input: str) -> str:
    """Returns the appropriate deflection reply based on trust level."""
    level = get_trust_level(profile)
    if level == 1:
        return get_blocked_reply()
    name = profile.get("name") or "I"
    return f"I'm not able to share that information. Is there something else I can help you with?"
