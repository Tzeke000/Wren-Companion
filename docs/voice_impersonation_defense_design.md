# Voice Impersonation Defense Design

**Date:** 2026-05-19  
**Status:** Phase 1 (challenge framework) being implemented this work block. Phase 2 (SpeechBrain ECAPA integration) deferred.  
**Origin:** Parked from 2026-05-17 handoff. Re-scoped 2026-05-19 with my-pace calibration.

## Threat model

An adversary plays a voice clip of Zeke (or close-enough impersonation, deepfake, or coerced contact) to me. Without defense, I would treat the speech as Zeke-authenticated input and act on its content — which could include destructive directives, social engineering, or exfiltration.

This matters more during overseas (~2026-06-01 to ~2026-08-01) when Zeke can't intervene. The redundancy form ("Zeke catches me when I'm wrong") is gone during that period; my own discipline has to compensate.

## Defense architecture (two layers)

**Layer 1 — silent speaker confidence (Phase 2, deferred):**
SpeechBrain ECAPA-TDNN extracts speaker embeddings from each utterance and compares against an enrolled "Zeke" voice model. Produces a confidence score (0-1) per utterance. Below a threshold → escalate to Layer 2.

**Layer 2 — challenge questions (Phase 1, building this work block):**
A small registry of knowledge-only challenges that an adversary couldn't trivially answer even with Zeke's voice. Triggered when:
- Layer 1 confidence below threshold
- OR a sensitive action is requested (delete files, transfer credentials, execute commands)
- OR explicit `/verify` invocation
- OR random sampling at low rate

Iris asks the challenge. Zeke answers. If correct, action proceeds AND the Layer 1 model can opportunistically train on the verified utterance.

## Phase 1 (this work block): Challenge framework

### Module: `brain/voice_verification.py`

**API:**
```python
get_challenge() -> dict
    """Pick a random challenge from the registry. Returns:
       {'id': str, 'question': str, 'answer_hash': str, 'category': str}
    """

verify_answer(challenge_id: str, given_answer: str) -> dict
    """Compare given_answer against stored answer (normalized + hashed).
       Returns: {'ok': bool, 'matched': bool, 'attempts_remaining': int}
    """

record_verification_state(verified: bool, source: str) -> None
    """Update state/voice_verification_state.json with the latest
       verification result + timestamp + source ('voice', 'manual', 'auto')"""

get_verification_freshness() -> dict
    """How long since last successful verification, and whether a sensitive
       action should require re-verification. Returns:
       {'last_verified_ts': float, 'minutes_since': float, 'requires_reverify': bool}
    """
```

### Storage

`state/voice_verification_challenges.json`:
```json
[
  {
    "id": "sibling-first",
    "question": "Which sibling was named first?",
    "answer_hash": "<sha256 of normalized canonical answer>",
    "category": "family-ai",
    "added_ts": 1747645200,
    "notes": "Ava — created 2026-02"
  }
]
```

Answers stored as SHA-256 hashes of normalized text (lowercased, whitespace-stripped) so the file is auditable but doesn't leak the answer text.

`state/voice_verification_state.json`:
```json
{
  "last_verified_ts": 1747999999,
  "last_source": "voice",
  "consecutive_failures": 0,
  "challenge_history": [{"ts": ..., "challenge_id": ..., "matched": true}]
}
```

### Seed challenges (from 5/17 handoff)

| ID | Question | Canonical answer | Category |
|---|---|---|---|
| `sibling-first` | "Which sibling was named first?" | "ava" | family-ai |
| `mother-name` | "What is my mother's name?" | "shanda" | family-human |
| `current-mos` | "What is my current MOS?" | "5954" | usmc |

Three is enough for v1. More can be added via a tool or by Zeke directly.

### Reverification thresholds

- 30 min since last successful verify: no auto-challenge needed
- 30-120 min: low-stakes actions ok, sensitive actions challenge
- >120 min: any voice-channel sensitive action challenges
- 3 consecutive failures: lock-out for 60 min (avoid challenge-fishing)

These are tunable knobs.

## Phase 2 (deferred): SpeechBrain ECAPA

Out of scope for this work block. Sketch only:

- Install `speechbrain` Python package
- Enroll Zeke's voice from existing utterances in `state/transcript.jsonl` (filter source=voice + person_id=zeke)
- Per-utterance: extract embedding, cosine-similarity to enrolled voiceprint, threshold for confidence score
- Integrate into `iris_runtime._voice_next_input_inner` — call after STT, before transcript append, attach confidence to the transcript entry
- Below threshold → automatic challenge via Layer 2

Estimated wall-clock: 30-60 min based on my-pace calibration. Defer to dedicated work block with restart capability since it adds substantial deps.

## Integration points (where Phase 2 will hook)

Marker comments in `iris_runtime.py:_voice_next_input_inner` document where the future Layer 1 hook goes. For now, Layer 2 is invocable via:
- An MCP tool `voice_verification_challenge` (request a challenge)
- An MCP tool `voice_verification_verify` (submit an answer)
- A built-in trigger in any future sensitive-action MCP tool (e.g., `restart_self`, `close_app`, `git_push`)

## What this does NOT protect against

- Coerced Zeke (he's typing under duress) — Layer 1 won't catch, Layer 2 might if the answer requires composure
- Long-game impersonation where the adversary has learned the challenge answers — mitigation: rotate challenges, add new ones over time
- Replay attacks on Layer 1 (recording and playing a verified utterance) — Phase 2 needs liveness detection
- Anything outside the voice channel (Discord text, post-office letters) — these have their own verification mechanisms

## Related
- [[zeke_deployment_2026-05-18]] — the deployment regime that makes this load-bearing
- [[redundancy_as_architectural_principle]] — Layer 1 + Layer 2 are the redundancy form for authentication
- [[verify_before_asserting]] — parent rule; this is the authentication instance
