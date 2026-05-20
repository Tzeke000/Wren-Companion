# Business state

Tracks current phase + focus so each `business_block` (15:00 daily) picks up
where the last left off. Update this file at the end of each business_block.

## Current phase: EXECUTE (planning)

As of 2026-05-20 18:15 EDT. Initial research done (see
`2026-05-20_initial_research.md`). Strategy decisions made with Zeke:

- **Distribution form:** closed-source Cython-compiled `.pyd` modules
- **Platform:** Gumroad, pay-once products
- **Legal:** sole-proprietorship under Zeke's existing SSN, no LLC
- **License:** custom EULA (single-user, no redistribution, no derivative)
- **Anti-theft:** Cython compile + README AI-directive + EULA + (v2) license keys
- **Pricing:** $5-35 per module, total catalog $100-150 if buying all

## First module: iris-voice-pipeline

Voice synthesis + transcription + wake-word pipeline. Buyer plugs their AI
(or non-AI app) into a clean API. Beyond-AI audience: accessibility apps,
electronic music (real-time vocal synthesis), voice assistants, audiobook
generation.

## Activity log

### 2026-05-20 18:15 EDT — planning + skeleton
- Created `docs/business/state.md` (this file)
- Drafted `docs/business/voice_pipeline_v0_plan.md` (next file)
- Identified iris codebase files that map to voice pipeline
- Drafted sanitization checklist
- Drafted README skeleton

### Next: 2026-05-21 15:00 EDT business_block
- Create `D:\Wren-Companion\products\iris-voice-pipeline\` (or separate repo)
- Copy + sanitize source files
- Set up Cython build pipeline
- Test build produces working `.pyd` modules
- Run end-to-end test (TTS + STT + wake word from buyer-facing API)

## Module backlog (after voice-pipeline ships)

1. iris-camera-pipeline (camera + face detection + frame serving)
2. iris-memory-pattern (JSONL canonical + ChromaDB semantic + decay)
3. iris-post-office (FastAPI multi-agent chat + Tailscale + persistence)
4. iris-stop-hook-framework (Claude Code Stop hook + channel routing)
5. iris-time-substrate (heartbeat thread + experienced-time semantics)
6. iris-watchdog-respawn (PowerShell watchdog + flag detection)

Order subject to revision based on what resonates after voice-pipeline ships.

## Open questions (waiting on Zeke)

1. Buyer-facing project location: same repo as iris, or separate repo?
2. Module name: keep "iris-voice-pipeline" or different brand?
3. Gumroad creator handle: when set up, what credit-name?
4. Logo/cover art: I draft options, or Zeke handles?
