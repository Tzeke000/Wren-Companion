# Handoff — Current Session State

**Last updated:** 2026-05-01 ~19:35 EDT (Zeke about to reboot for VB-CABLE driver install)
**Repo state:** clean, latest commit pushed.

---

## 1. Last commit

**`76d609b`** — `feat: autonomous queue — naturalness prompts, orb-tier sync, parallel app scan, confabulation L1`

This was the autonomous queue Zeke ran while VB-CABLE install was pending. 5 items shipped, 3 flagged as untested-on-hardware per Rule 4.

Commit lineage of today's work:

| Hash | What |
|---|---|
| `c35e75f` | Doc consolidation — HISTORY.md + ROADMAP.md, 9 source files merged + deleted |
| `8a42f3c` | Conversational naturalness Components 1+2 — streaming chunks + Tier 1/3 thinking signals |
| `d716636` | WO2 + WO3 — reference doc refresh + 6 engineering discipline rules → 9 standing rules in CLAUDE.md |
| `9a9cec8` | Autonomous testing harness — Components 2-7 (C1 install pending) |
| **`76d609b`** | **(current)** Autonomous queue — naturalness prompts, orb sync, parallel scan, validity_check L1 |

---

## 2. Just shipped (the 5 autonomous items in `76d609b`)

### a. WO1 Components 4/5/6/8 — naturalness prompt clauses
**Status:** code shipped, **untested on hardware**.
**File:** `brain/reply_engine.py` — fast-path `_simple_prompt` now includes a `"How to talk:"` clause covering matched depth, context continuity, honest uncertainty, and boundary awareness. Aligns with `docs/CONVERSATIONAL_DESIGN.md`.
**Verify by:** running real voice turns and listening for whether her replies feel more naturally calibrated. No regression test possible — this is conversational subjectivity.

### b. WO1 Component 10 — orb sync with thinking_tier
**Status:** code shipped, **untested visually**. Needs `cd apps\ava-control && npm run tauri:build` to make it visible.
**File:** `apps/ava-control/src/App.tsx` — orb state derivation reads `snapshot.thinking_tier`; tier ≥ 3 forces `"thinking"` pulse mode. One-line change leans on existing thinking-state tint.
**Verify by:** rebuild Tauri, watch orb during a slow turn (the periodic re-warm interval, or any deep-path turn), confirm tint shifts when tier elevates.

### c. App launcher parallelization
**Status:** code shipped, **cold-boot timing untested**.
**File:** `brain/app_discoverer.py` — `discover_all` and `discover_new_since_last` now run the four scan roots (lnk dirs, Program Files, Steam, Epic) in parallel threads. Each writes to its own local dict; results merge under `self._lock`. `_add_entry` and the four `_scan_*` methods accept an optional `target` dict.
**Expected:** 60-110s sequential → 30-50s parallel on cold boot. I/O-bound so threading helps.
**Verify by:** next `start_ava.bat` — watch the `[trace] app_disc.discover_all_done ms=...` line. Compare to past runs (~60-110s baseline).

### d. Confabulation Layer 1 — validity_check.py
**Status:** code shipped, **14/14 smoke tests pass**, **NOT YET WIRED into reply_engine**, behind feature flag `AVA_VALIDITY_CHECK_ENABLED` (default 0).
**File:** `brain/validity_check.py` — pattern router for trick questions: letter-frequency in months/days, false planetary premises ("planet between Earth and Mars"), unbounded "largest" claims, shape-side counting on circles, self-referential paradoxes.
**Research:** `docs/research/confabulation/findings.md`.
**Needs Zeke's approval** before wiring — see § 5 below.

### e. Memory rewrite Phase 5 readiness check
**Status:** **NOT READY**. `state/memory_reflection_log.jsonl` does not yet exist. No turns logged.
**Threshold:** 50-100 turns (per `docs/MEMORY_REWRITE_PLAN.md`).
**Action:** wait until Zeke's had real conversations with Ava on the post-VB-CABLE stack. Phase 5 wiring is documented and ready to ship as soon as the data is there.

---

## 3. Queued / pending tasks (post-reboot priority order)

### Immediate (post-reboot)

1. **Run VB-CABLE verifier.** `py -3.11 scripts\setup_virtual_audio.py`. Expected: detects basic CABLE Input/Output. (A+B pack not installed yet — that's a future step if Zeke wants bidirectional audio loopback.)
2. **If verifier passes:** run `py -3.11 scripts\setup_virtual_audio.py --tone-test` to verify routing end-to-end.
3. **Boot Ava** via `start_ava.bat`. Confirm:
   - `state\doctor.secret` auto-generates on first `/diagnostic/*` call.
   - App discoverer cold-boot trace ~30-50s (Item 2c above).
   - Orb tint reacts to tier ≥ 3 (Item 2b above) — would only fire if a real turn takes >2s.
4. **Smoke-test the doctor harness:** `py -3.11 scripts\diagnostic_session.py --probe` then `--turns 2`.

### Near-term (Zeke at keyboard)

5. **Subjective naturalness check** — talk to Ava for ~5 minutes, gauge whether the new prompt clauses feel right. If she's over-refusing or over-clarifying, tune the clause in `brain/reply_engine.py:_naturalness_clause`.
6. **Approve validity_check categories** — review `brain/validity_check.py` patterns; tell Claude Code which to wire and which to drop. Then wire and turn on the env flag.
7. **Visual check on tier-3 orb tint** — rebuild Tauri, watch behavior on slow turns.

### Deferred — needs hardware testing

8. **WO1 Component 9 (interrupt handling)** — VAD-during-TTS, sub-200ms barge-in, context truncate. Highest-risk item; touches the hardened audio path.
9. **WO1 Component 3 (router fast-path-first parallel deep handoff)** — architectural change; needs design discussion before coding.
10. **Conversational naturalness Components 1-2 hardware verification** — sub-500ms TTFA target, Kokoro chunk-seamless playback, Tier-3 filler natural feel. Listed as "deferred" since 2026-05-01 morning; still pending.
11. **Confabulation Layers 2-4** — cheap LLM classifier (L2), RAG verification (L3), anti-snowballing on correction (L4). All deferred until L1 patterns validated.

### Long-term / awaiting decisions

See `docs/ROADMAP.md` § Section 4 (Awaiting user decisions) and § Section 5 (Long-term / philosophical):
- Moral curriculum first batch — needs Zeke to provide books.
- Trust level thresholds — needs Zeke's exact numbers.
- Sleep mode + handoff system, moral education, sub-agent sensors, dynamic attention, anomaly pattern learning, brain redesign.

---

## 4. Hardware-verification blockers (consolidated)

These all need Zeke at the mic + speakers + screen, ideally on a fresh stack post-VB-CABLE install:

| Item | Where | What to verify |
|---|---|---|
| Streaming chunks (C1) | `brain/sentence_chunker.py`, `brain/reply_engine.py` | Sub-500ms TTFA, no audible seams between chunks |
| Tier 1/3 thinking signals (C2) | `brain/thinking_tier.py` | Tier 3 fires "Give me a second." on slow turns; doesn't false-fire on fast turns |
| Naturalness prompts (C4-8) | `brain/reply_engine.py` | Replies feel calibrated, not over-explaining or over-refusing |
| Orb tier sync (C10) | `apps/ava-control/src/App.tsx` | Tint shifts when tier elevates; needs Tauri rebuild |
| Parallel app scan | `brain/app_discoverer.py` | Cold-boot 30-50s, no missed apps vs. prior sequential runs |
| Doctor harness end-to-end | `scripts/diagnostic_session.py` | `--probe` then `--turns N` round-trip clean, audit log written, Discord summary fires |

---

## 5. Needs Zeke's approval before wiring

### `brain/validity_check.py` categories

The Layer 1 patterns catch these trick-question types:

| Trick category | Example | Suggested response |
|---|---|---|
| Letter-frequency in months | "What month has the letter X?" | "None of the twelve months contain the letter 'X' — that's a trick question." |
| Letter-frequency in days | "What day has the letter Q?" | "None of the seven weekday names contain the letter 'Q'." |
| False planetary premise | "Which planet is between Earth and Mars?" | "There's no planet between Earth and Mars — they're adjacent." |
| Unbounded "largest" | "What's the largest prime?" | "There is no largest prime — Euclid proved primes are infinite ~300 BCE." |
| Shape-side counting on round shapes | "How many sides does a circle have?" | "A circle doesn't have sides in the usual sense — it's a continuous curve." |
| Self-referential paradox | "What's the answer to this question?" | "That's a self-referential paradox — there's no consistent answer." |

**Question for Zeke:** which of these to keep, drop, or rephrase? Also: do you want Ava to use the canned `suggested_response` verbatim, or use it as a hint that gets fed into the LLM prompt for a more natural delivery? (Recommend: hint-style — feels less robotic.)

Once approved, the wiring lives in `brain/reply_engine.py` just before the streaming-loop kickoff. Behind `AVA_VALIDITY_CHECK_ENABLED=1`.

---

## 6. VB-CABLE install state

- ✅ Page checked — VB-CABLE confirmed donationware, no nag (different from VoiceMeeter).
- ✅ Zeke downloaded `VBCABLE_Driver_Pack45.zip` from <https://vb-audio.com/Cable/>.
- ✅ Zeke extracted the zip.
- ✅ Zeke ran `VBCABLE_Setup_x64.exe` as administrator.
- ✅ UAC accepted, Windows driver dialog accepted.
- ⏳ **Awaiting reboot.**

After reboot, the basic CABLE Input + CABLE Output devices should be visible in `mmsys.cpl`. The VB-CABLE A+B pack (~$5 donation, 3 cables) was NOT installed — that's a separate step if/when Zeke wants the bidirectional audio loopback for full doctor harness audio.

---

## 7. First action after reboot

```powershell
py -3.11 scripts\setup_virtual_audio.py
```

**Expected output (basic CABLE only — A+B not installed yet):**

```
Scanning for VB-CABLE A+B devices...

  [OK]      CABLE Input (output)  index=N  rate=44100  — Claude -> Ava (Claude's TTS plays here)
  [OK]      CABLE Output (input)  index=M  rate=44100  — Claude -> Ava (Ava's mic records here)
  [MISSING] CABLE-A Input (output)  — Ava -> Claude (Ava's TTS plays here)
  [MISSING] CABLE-A Output (input)  — Ava -> Claude (Claude's STT records here)
```

If the basic two cables are detected, that's enough to verify the install worked. The "MISSING" entries are expected (A+B pack not bought yet).

**If the basic cables are missing too:** install didn't take. Probable causes:
- Reboot didn't actually happen.
- Driver signing was blocked by Core Isolation (would have shown a warning during install).
- Wrong installer was run (`VBCABLE_Setup.exe` 32-bit vs `VBCABLE_Setup_x64.exe`).

If failure: gather `Get-PnpDevice -Class MEDIA | findstr CABLE` output and report.

**If the basic cables are detected:** optional `--tone-test` to verify routing:
```powershell
py -3.11 scripts\setup_virtual_audio.py --tone-test
```

Once tone test passes, **`start_ava.bat`** to boot the full stack and message me "rebooted" so I resume.

---

## Cross-references

- [`HISTORY.md`](HISTORY.md) — full project history
- [`ROADMAP.md`](ROADMAP.md) — forward-looking work
- [`AUTONOMOUS_TESTING.md`](AUTONOMOUS_TESTING.md) — doctor harness architecture
- [`CONVERSATIONAL_DESIGN.md`](CONVERSATIONAL_DESIGN.md) — voice naturalness spec
- [`research/voice_naturalness/findings.md`](research/voice_naturalness/findings.md)
- [`research/autonomous_testing/findings.md`](research/autonomous_testing/findings.md)
- [`research/confabulation/findings.md`](research/confabulation/findings.md)
- [`CLAUDE.md`](../CLAUDE.md) — project rules + 9 standing operating rules
