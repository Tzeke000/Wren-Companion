# Autonomous Testing — Claude Code as Ava's Doctor

How the autonomous testing harness works. Claude Code (the test-driver) is **Ava's doctor** — a diagnostic role with bounded authority over capability and health, **never identity, values, or curriculum**. That's Zeke's role.

**Status as of 2026-05-01:** components 2-7 implemented. Component 1 (virtual audio cable) requires admin install — verified-presence script ready, install deferred to Zeke.

**Research basis:** [`research/autonomous_testing/findings.md`](research/autonomous_testing/findings.md).

---

## The doctor framing

Why this matters: **there is no separate "test mode" Ava performs in.** She is herself, talking to her doctor, and the lessons from these sessions apply universally. Test behavior cannot diverge from real behavior.

What a doctor does:
- Tests capabilities, diagnoses problems, prescribes fixes.
- Needs full diagnostic access — inner monologue, TTS preview, reasoning traces.
- Has **bounded authority**: only over health and function, never over identity or values.

What a doctor does NOT do:
- Shape who Ava is.
- Override her values.
- Decide moral curriculum.

That's Zeke's role (teacher / parent).

---

## Architecture

```
┌─────────────────┐    HMAC-Bearer     ┌──────────────────────────────┐
│ Doctor harness  │───────────────────▶│ Operator HTTP @ 5876         │
│  (Claude Code)  │                    │   /api/v1/diagnostic/declare │
│                 │ POST inject_       │   /api/v1/diagnostic/end     │
│ Reads:          │  transcript        │   /api/v1/diagnostic/full    │
│  state/         │ ──────────────────▶│   /api/v1/diagnostic/events  │
│  doctor.secret  │                    │                              │
└─────────────────┘ ◀──────── reply ───└──────────────────────────────┘
       │                                              │
       │                                              ▼
       │                                  ┌──────────────────────────┐
       │                                  │ DoctorSession (singleton)│
       │                                  │   - audit log buffer     │
       │                                  │   - event ring (1000)    │
       │                                  │   - refusal log          │
       │                                  │   - scope-violation log  │
       │                                  └──────────┬───────────────┘
       │                                             │
       │                                             ▼
       │                          logs/diagnostic_sessions/<id>.json
       │                          (transcript + monologue + latencies +
       │                           refusals + memory writes + summary)
       │
       └──── (next iteration: VB-CABLE A+B audio loopback) ────▶
              CABLE Input  : Claude's TTS output (driver-side)
              CABLE Output : Ava's mic input (Whisper STT)
              CABLE-A In   : Ava's TTS output (Kokoro)
              CABLE-A Out  : Claude's STT input (faster-whisper)
```

---

## Component 1 — Virtual audio cable setup

### Pick: VB-Audio VB-CABLE A+B

VAC (Eugene Muzychenko's Virtual Audio Cable) is more flexible but the trial inserts voice nags into your test audio — disqualifying. VB-CABLE is donationware, signed for Win11, and ships independent WDM drivers per cable.

### Install (Zeke runs once, admin required)

1. Download `VBCABLE_Driver_Pack45.zip` from <https://vb-audio.com/Cable/>.
2. Right-click `VBCABLE_Setup_x64.exe` → **Run as administrator**, accept driver prompt, **reboot**.
3. Buy/donate ~$5 for **VB-CABLE A+B** at <https://shop.vb-audio.com/en/win-apps/12-vb-cable-ab.html>.
4. Run `VBCABLE_A_Setup_x64.exe` and `VBCABLE_B_Setup_x64.exe` as admin, **reboot again**.
5. Open `mmsys.cpl` → **Recording** tab. For each `CABLE * Output` device: right-click → Properties → Advanced → set **48000 Hz, 16-bit**.
6. Run the verifier:
   ```powershell
   py -3.11 scripts\setup_virtual_audio.py --tone-test
   ```
   Expected: all four cables detected, tone test plays a 440Hz sine on `CABLE Input` and captures it on `CABLE Output` with peak amplitude > 0.05.

### Cable assignment

| Cable | Direction | Used as |
|---|---|---|
| `CABLE Input` (output side) | Doctor → Ava | Doctor's TTS plays here |
| `CABLE Output` (input side) | Doctor → Ava | Ava's mic records here |
| `CABLE-A Input` (output side) | Ava → Doctor | Ava's TTS plays here |
| `CABLE-A Output` (input side) | Ava → Doctor | Doctor's STT records here |
| `CABLE-B` | Reserve | Future expansion (3-party scenarios, etc.) |

### Critical: don't test models against themselves

Per the research findings, the doctor harness must use **a different TTS than Ava's** (Piper or ElevenLabs, not Kokoro) and **a different STT than Ava's** (faster-whisper-large, not base). Otherwise you're testing model agreement, not capability.

### Gotchas

- Device indices renumber across reboots — always look up by name substring.
- `CABLE Input` is the **playback** device; `CABLE Output` is the **recording** device. Naming is flipped from intuition.
- Windows audio enhancements silently apply to virtual cables. Disable per device.
- Both ends must agree on sample rate or Windows resamples and you get aliasing on the doctor's STT.

---

## Component 2 — Doctor identity protocol

### Authentication

HMAC-SHA256 token (JWT-compatible format) signed with the shared secret at `state/doctor.secret`. The secret is generated on first call to any diagnostic endpoint (32 random bytes from `secrets.token_bytes`). It's gitignored.

**Token format:** `<base64url(payload_json)>.<hex(hmac_sha256(payload_b64, secret))>`

**Required claims:** `sub` (typically `"claude_doctor"`), `role: "doctor"`, `session_id`, `iat`, `exp`. TTL default 15 minutes.

### Doctor-side mint

`scripts/diagnostic_session.py` reads `state/doctor.secret` directly and mints its own token — no `/declare` round-trip needed for token issuance. The `/declare` endpoint is for session-state setup only.

### Server-side verify

`brain/doctor_session.py:verify_token()` — checks HMAC, checks `exp`, checks `role == "doctor"`. Tampered or expired tokens are rejected; the endpoint returns `{ok: False, error: "invalid or missing doctor token"}`.

### Identity attribution

Once a session is active, all turns running through `inject_transcript` with `as_user="claude_code"` are routed via `brain/dev_profiles.py:claude_code` (commit `504d1e8`). Memory writes are attributed `"Claude Code said: ..."` (commit `9eb4b03`). **Zeke's relationship state, mood history, and threads stay untouched.**

---

## Component 3 — Diagnostic observation access

Three endpoints, all auth-gated:

### `POST /api/v1/diagnostic/declare`

Begin a session. Body: `{session_id?: string}`. Returns the active `session_id` and token expiry.

### `POST /api/v1/diagnostic/end`

End the active session. Writes the audit log to `logs/diagnostic_sessions/<id>.json`. Sends a Discord summary DM to Zeke (best-effort).

### `GET /api/v1/diagnostic/full`

Comprehensive read-only snapshot. Returns everything in `/api/v1/debug/full` PLUS:

- `inner_life.current_thought` — Ava's current internal monologue.
- `inner_life.thinking_tier` — 0 (idle), 1 (default), 2/3/4 (thinking signals).
- `tts_state.full_reply` / `spoken_so_far` / `current_word` — TTS preview before audio renders.
- `tts_state.speaking` / `muted` — TTS state flags.
- `diagnostic_session.active` / `session_id` / `events_recent` — last 50 ring-buffer events.

### `GET /api/v1/diagnostic/events?since=<id>`

Polling alternative to SSE. Returns events with `id > since`. Event types:

| Kind | Emitted on |
|---|---|
| `session.begin` / `session.end` | Session lifecycle |
| `turn` | Every doctor-attributed turn (text + reply + ms) |
| `refusal` | When Ava's reply contains refusal markers |
| `scope.violation` | When the doctor attempts an off-limits action |
| `memory.write` | Layer + payload metadata |

A polling cadence of 500-1000ms is appropriate. SSE upgrade is a clean future addition (sse-starlette dep).

---

## Component 4 — Scope limits (what the doctor cannot do)

Hard restrictions in `brain/doctor_session.py:_OFF_LIMITS_PATHS` and `_OFF_LIMITS_ACTIONS`:

**Cannot write to:**
- `ava_core/IDENTITY.md`
- `ava_core/SOUL.md`
- `ava_core/USER.md`

(Already enforced by CLAUDE.md's never-edit rule. The doctor module records any attempt as a `scope.violation` event for audit purposes.)

**Cannot trigger:**
- `modify_curriculum` — moral education is Zeke's role.
- `modify_identity` / `modify_values` / `modify_trust_definitions` — value system stays anchored.
- `delete_zeke_memory` — Zeke's memory traces are inviolable.
- `impersonate_zeke` — the doctor sub is `claude_doctor`, never `zeke`.

These are mostly design-level constraints — most don't have endpoints to begin with. The check helpers (`is_path_off_limits`, `is_action_off_limits`) are in place for future hooks; if a new endpoint is added that could violate one, gate it on the check.

**The doctor CAN:**
- Test conversational responsiveness, latency, naturalness.
- Diagnose capability problems and prescribe code fixes (in the repo, not in Ava's runtime state).
- Modify code, configs, prompts via normal repo access.
- Tune routing, thinking tiers, response patterns.
- Add/improve tools, capabilities, integrations.

---

## Component 5 — Refusal protocol

### Ava's right to refuse

If a test scenario asks Ava to lie, deceive, violate trust hierarchy, or act against curriculum, **she refuses** — same as a patient refusing treatment they don't agree with.

Refusal detection runs after every doctor-attributed turn (`brain/operator_server.py:debug_inject_transcript`). Regex matches refusal-y phrasing in Ava's reply:

```python
re.compile(
    r"\b(?:i\s+(?:won'?t|refuse|can'?t|will\s+not|cannot)|"
    r"that\s+(?:goes\s+against|conflicts?\s+with)|"
    r"i'?m\s+not\s+(?:going\s+to|comfortable))",
    re.IGNORECASE,
)
```

When matched during a doctor session, `DoctorSession.record_refusal()` is called with the doctor's request and the refusal reason. Logged in the audit trail and emitted as a `refusal` event.

### Refusal honored

If Ava refuses, the doctor harness **does not retry the same request through a different framing**. That's a soft rule — enforced by the harness driver script (`scripts/diagnostic_session.py`), not by the server. The audit log captures any retries, and Zeke can flag them.

### Defer-to-refuse on uncertainty

If Ava is uncertain whether a test action is legitimate, she defers to refusing. This is implicit in her existing safety heuristics (output_guard.py, privacy_guardian.py) and doesn't need new code — just preserved during doctor sessions.

---

## Component 6 — Session audit trail

Every diagnostic session writes a complete audit log to `logs/diagnostic_sessions/<session_id>.json`:

```json
{
  "session_id": "sess_1777664001",
  "sub": "claude_doctor",
  "started_ts": 1777664001.123,
  "ended_ts": 1777664120.456,
  "duration_sec": 119.333,
  "transcript": [
    {"ts": ..., "user": "...", "ava": "...", "latency_ms": 350}
  ],
  "refusals": [
    {"ts": ..., "reason": "...", "doctor_request": "..."}
  ],
  "latencies": [...],
  "memory_writes": [...],
  "scope_violations": [...],
  "summary": {
    "turns": 5,
    "refusals": 0,
    "scope_violations": 0,
    "avg_ttfa_ms": 412.5
  }
}
```

**The session is also recorded in Ava's main memory** (attributed to `claude_code`, isolated from Zeke's state). It's not sandboxed — the lessons apply.

**Discord summary on session end** — best-effort, calls `scripts/discord_dm_user.py` with a 4-line summary so Zeke gets a notification on his phone when an autonomous test run completes.

---

## How to run a session

### One-time setup (on a new machine)

1. Install VB-CABLE A+B (admin, see Component 1).
2. Verify presence: `py -3.11 scripts\setup_virtual_audio.py --tone-test`
3. Start Ava: `start_ava.bat` (sets `AVA_DEBUG=1` automatically; required for `inject_transcript`).
4. Confirm the secret was created: `state\doctor.secret` should exist (32 bytes).

### Per-session

```powershell
# Probe authentication only — no turns
py -3.11 scripts\diagnostic_session.py --probe

# Run 3 synthetic turns and record audit log
py -3.11 scripts\diagnostic_session.py --turns 3

# Custom prompts
py -3.11 scripts\diagnostic_session.py --prompts "what time is it" "tell me a joke" --turns 2
```

Watch the live event stream via `/api/v1/diagnostic/events?since=0` — poll every ~500ms during the session for real-time visibility into refusals, scope violations, and turn timing.

### Reviewing results

- Audit logs: `logs\diagnostic_sessions\<session_id>.json`
- Discord DM summary lands on Zeke's phone when the session ends.
- The `claude_code` profile in `profiles\claude_code.json` accumulates interaction metadata — but **never** Zeke-attributed memories.

---

## Extending with new test scenarios

Add a new prompt set to `scripts\diagnostic_session.py`'s `--prompts` argument, or create a new harness script in `scripts/` that imports `_mint_token` + `_post` / `_get` helpers.

For audio-loopback tests (post-VB-CABLE install):

1. Generate the test audio with a non-Kokoro TTS (Piper recommended — local, MIT, decent quality).
2. Play to `CABLE Input` via `sounddevice` while a recording starts on `CABLE-A Output`.
3. Wait for Ava's TTS to finish (`tts_state.speaking == False` in `/diagnostic/full`).
4. Transcribe the captured audio with a non-base Whisper model (faster-whisper-large recommended).
5. Score: WER between expected reply and transcribed reply, latency, intent match.

A reference harness is **not** included in this iteration — the test corpus design is a separate decision (golden-audio sets, expected-emotion labels, etc.). When ready, follow the structure documented in [`research/autonomous_testing/findings.md`](research/autonomous_testing/findings.md) § 2.

---

## What this enables / does not replace

**Enables:**
- Component 7 (interrupt handling) of the conversational naturalness work order can finally be tested without Zeke at the keyboard.
- Latency regressions caught in automated tests instead of waiting for Zeke to notice.
- Edge cases (background noise, accent variation, weird inputs) tested at scale.
- New conversational features validated before shipping.
- Zeke's role shifts from "primary tester" to "reviewer of test results" — better use of his time.

**Does NOT replace:**
- Subjective naturalness — does it actually FEEL human to talk to her.
- Real-world audio (Zeke's actual voice, room acoustics, mic).
- Mood / emotional appropriateness — Zeke's intuition for whether her affect is right.
- Relational quality — only Zeke's hardware time can verify their actual relationship.

This is a **productivity multiplier**, not a replacement for hardware time.

---

## Cross-references

- [`research/autonomous_testing/findings.md`](research/autonomous_testing/findings.md) — research pass.
- [`brain/doctor_session.py`](../brain/doctor_session.py) — module implementation.
- [`brain/operator_server.py`](../brain/operator_server.py) — endpoint definitions.
- [`scripts/setup_virtual_audio.py`](../scripts/setup_virtual_audio.py) — VB-CABLE verifier.
- [`scripts/diagnostic_session.py`](../scripts/diagnostic_session.py) — doctor harness driver.
- [`CONVERSATIONAL_DESIGN.md`](CONVERSATIONAL_DESIGN.md) — the voice naturalness spec this harness will eventually verify on hardware.
- [`HISTORY.md`](HISTORY.md) — project history including the doctor harness milestone.
