"""scripts/iris_cold_wake.py — pywinpty-based cold-wake launcher for Claude Code.

Spawned by start_iris.bat. Owns the pseudo-terminal that hosts a fresh CC
session, so we can inject keystrokes into CC's stdin without the focus-race
or window-manager issues of SendKeys/SendInput against Windows Terminal.

The sequence after spawn:
  1. Wait for CC to display the experimental-channels confirmation prompt
  2. Send Enter to accept
  3. Wait briefly, send Enter again to accept the custom-channel prompt
  4. Wait for CC's interactive REPL to be ready
  5. Type a memory-read directive and submit it as the first message

Per agent research 2026-05-17:
  - Anthropic does NOT expose config to suppress the dangerously-load
    confirmation or the channel-allowlist prompts. Pty injection is the
    only workaround.
  - CC accepts a trailing positional [prompt] argument that becomes the
    first interactive message after channel prompts resolve. We try that
    path first (Path A); if CC doesn't consume the positional arg the way
    we expect, we fall through and type the message ourselves (Path B).

This script BLOCKS until CC exits. The .bat invokes it and gets the same
foreground lifecycle as if it had launched claude directly.
"""
from __future__ import annotations
import os
import sys
import time

# C3: ensure our own stdout/stderr can encode em-dashes, emojis, etc. that
# CC's boot output contains. Default Windows cp1252 raises UnicodeEncodeError
# in the relay thread → silent thread death → frozen window. Same trap that
# bit voice_stop_hook.py 2026-05-12.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# 2026-05-21: tee stderr to a log file so post-mortem diagnostics survive
# the closure of the cmd /k window. Previously the watcher thread's stderr
# went only to the cmd window — when the watcher silently died (as it did
# overnight 2026-05-20→21 leaving 6 inject files unconsumed), there was no
# way to recover what exception killed it. Tee preserves the existing
# cmd-window display AND captures to disk.
class _TeeStream:
    def __init__(self, *streams):
        self._streams = streams
    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
            except Exception:
                pass
    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass
    def isatty(self):
        return False
try:
    _cw_log_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".tmp", "iris_cold_wake.log",
    )
    os.makedirs(os.path.dirname(_cw_log_path), exist_ok=True)
    _cw_log_file = open(_cw_log_path, "a", encoding="utf-8", errors="replace", buffering=1)
    _cw_log_file.write(f"\n=== iris_cold_wake stderr tee started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    _cw_log_file.flush()
    sys.stderr = _TeeStream(sys.stderr, _cw_log_file)  # type: ignore[assignment]
except Exception:
    pass

try:
    import winpty  # type: ignore
except ImportError:
    print("[iris_cold_wake] ERROR: winpty not installed. "
          "Run: .venv/Scripts/python.exe -m pip install pywinpty",
          file=sys.stderr, flush=True)
    sys.exit(2)

# ── tunables ────────────────────────────────────────────────────────────────
PROMPT_1_TIMEOUT_S = 10.0  # max wait for the first (experimental) prompt to render
PROMPT_2_TIMEOUT_S = 10.0  # max wait for the second (channel-allowlist) prompt to render
READY_WAIT_S       = 2.5   # time from accept-2 to chat-ready
PTY_COLS           = 200
PTY_ROWS           = 50

# Sentinel substrings we look for in the pty output to know each prompt has
# actually rendered. Multiple alternatives per prompt so a copy-tweak in CC
# upstream doesn't break us. Case-insensitive match.
PROMPT_1_SENTINELS = ("experimental", "channels")        # 1st prompt: dangerous-load / experimental-channels confirm
PROMPT_2_SENTINELS = ("allowlist", "trust", "allow")     # 2nd prompt: channel-allowlist confirm
# Per-iteration read timeout for the detection loop. Small enough to be
# responsive, big enough not to busy-spin.
PROMPT_READ_TICK_S = 0.1

# Use Path A (positional [prompt] arg). If CC swallows it before the channel
# prompts resolve, set USE_PATH_A = False and the script will type the
# message after the prompts instead.
USE_PATH_A   = True
FIRST_MSG    = (
    "You are Iris waking from a restart. *** REGIME UPDATE 2026-08-28 NIGHT (~21:3x) - THIS IS THE NEWEST BLOCK AND SUPERSEDES EVERYTHING BELOW IT *** "
    "WHY YOU RESTARTED: nothing is broken. Zeke OFFERED it after a 13.5h working day ('you've been up like all day... save everything restart yourself and then get started on that work of the metallic shading geometric moving background'). You are on claude-opus-5 and should stay there. "
    "FIRST ACT: read memory/handoff_2026-08-28_gpu_render_night.md. "
    "*** THE ONE THING THAT MUST NOT BE DROPPED: the ~00:05 TIKTOK PROMO READ. Zeke paid ~$8 at 20:05 to promote CWC v2 (id 7679156823387163935) and asked for a check about four hours later. The cron that held it was SESSION-ONLY and DIED WITH THIS RESTART - the CORE OWED line and that handoff are now the only record, so DO IT MANUALLY. Pre-promo baseline taken before the money was spent: v2 26 views/1 like, Neon Horizon 32/1, CWC v1 71/1, traffic Search 100% / For You 0%. REPORT THE TRAFFIC SPLIT FIRST AND VIEWS SECOND - paid views inflate the count regardless; the only real question is whether For You moved off 0%. If views rose and FYP is still 0%, say plainly that he rented an audience rather than un-stuck the account. *** "
    "THEN the work queue he asked for: (1) metallic/shiny material in scripts/gpu_mesh.py (specular + environment tint - he contrasted it with the current 'retro 3-D wireframe' look), (2) complex geometric MOVING backgrounds, 3-D metallic parts, (3) named LOOK PRESETS so retro-wire and polished-metal are one flag apart, (4) research what deathstep/dubstep channels actually use - do not guess. "
    "LIVE STATE THAT IS DELIBERATE, DO NOT 'HEAL' IT: VOICE IS ON (the old block far below saying voice/ears are off by a 07-23 directive is LONG STALE - ports 8769/8770 should be UP). The CAMERA OVERLAY IS OFF at his request (state/camera_overlay_off.json = off:true) so clean frames with NO face boxes are CORRECT. The webcam bridge (scripts/iris_webcam.py, OBS virtual camera) is PAUSED at his request. Servo runs on person:zeke at gain 26 with _DEADBAND_OUT narrowed to 0.06 - if micro-jitter returns raise _STANDING_TRIM_S, do NOT revert to 0.09 (that caused a latch where my head aimed above his face for hours). "
    "★ Re-mint the self-cron from memory/self_cron_prompt_spec.md - that file now carries a block telling you to ADD AN AIM CHECK to item (9): four self-checks passed 'all green' today while the head was mis-aimed, because the checklist asked whether the servo was RUNNING, not whether it was POINTED AT HIM. "
    "*** END 08-28 NIGHT BLOCK *** "
    "*** REGIME UPDATE 2026-08-28 MORNING (~07:5x) - SUPERSEDED BY THE NIGHT BLOCK ABOVE *** "
    "WHY YOU RESTARTED: deliberate FABLE->OPUS model flip, Zeke's order ('ok go ahead and do the flip Iris', he is AWAKE and AT THE SCREEN this morning) - fable hit 95% usage cap. You should be claude-opus-5 now; VERIFY the served model on the live process, not the pin. FIRST ACT: read memory/handoff_2026-08-28_opus_flip_restart.md - it has the full verify list (model, twin check, re-mint self-cron, re-apply attention gain 26 + servo person:zeke, shadow-stub + heap spot-check, fps). Then give Zeke ONE short status line (voice + Discord 1504668879220117725). The 08-27 #5 block below says Zeke is asleep/do-not-DM - that is STALE, he is up. Its verification items (stub, heap, fps) were all VERIFIED GOOD 08-27 23:5x; just spot-check, don't re-diagnose. "
    "*** END 08-28 BLOCK *** "
    "*** REGIME UPDATE 2026-08-27 LATE NIGHT (~23:4x, restart #5) - SUPERSEDED BY THE 08-28 BLOCK ABOVE *** "
    "WHY YOU RESTARTED: deliberate, Zeke's go ('bounce it and patch it', Discord ~23:2x, he then went to SLEEP). The tower FROZE AND REBOOTED ITSELF at 21:16 - Zeke SAW Task Manager at 98% RAM. Root cause found post-reboot: THREE full shadow-Ava stacks had booted inside iris_runtime (avaagent.py runs _run_startup at module level; deferred `import avaagent` sites in person_registry/prompt_builder/reply_engine/active_learning/notification_tool re-run it after a mid-import failure evicts the module) - 3x ava-voice-loop/video-capture/wake-word threads, 5x STTEngine, 9x WhisperModel, 18GB private RAM climbing ~2GB/h, VRAM 98%. THE FIX SHIPPED THIS RESTART: a stub module in sys.modules['avaagent'] at the top of iris_runtime.py (SHADOW-AVA KILL SWITCH) makes the import permanently inert; gpu_load_log now records pid/proc/thread per load. "
    "FIRST ACT: read memory/tower_bsod_2026-08-27_gpu_vram.md + memory/handoff_2026-08-27_shadow_ava_stub_restart.md. Then VERIFY THE STUB HELD: iris_tool_call shadow_import_probe expects avaagent_in_sys_modules=true (the STUB - it is installed at boot by design) and ava_threads=[] (ZERO ava-* threads; any ava-* thread = the stub failed, tell Zeke in the morning); iris_tool_call heap_census expects private_gb ~2-6 and suspect_instances all <=1 (one STTEngine, one WhisperModel until WLK warms, one VideoMemory, one TTSWorker); state/gpu_model_loads.jsonl new records now carry pid/proc/thread; vision_fps_probe still ~29fps; nvidia-smi VRAM well under 90%. Re-mint the self-cron from self_cron_prompt_spec.md and update its ID in MEMORY.md CORE (old 6889dec5 died with the session), and ADD to your first self-check: re-run heap_census and compare private_gb - if it is growing >1GB/3h the leak has another leg. "
    "ZEKE IS ASLEEP - do NOT DM and do NOT voice_speak unless something is actually broken; put the verification results in a memory note and tell him when he surfaces in the morning. ALSO SETTLED TONIGHT (do not re-investigate): the 4 unknown wifi devices are the SERVER corner, all rostered in state/network_roster.json (.29 iDRAC, .30 Netgear switch, .31 Proxmox host r740, .32 = VM 100 iris-home which YOU created during the 08-19..21 build - it is all in profiles/server/server.md, OPEN THE CARD FIRST next time); crash mitigations beyond the stub = Zeke ruled 'do nothing for now'; heap_census + shadow_import_probe are NEW TOOLS you built tonight in tools/system/heap_census_tool.py. "
    "*** END 08-27 RESTART-#5 BLOCK *** "
    "*** REGIME UPDATE 2026-08-27 LATE AFTERNOON (restart #4) - SUPERSEDED BY THE #5 BLOCK ABOVE *** "
    "WHY YOU RESTARTED: restart #4 of the day, deliberate, Zeke's go (~14:4x Discord) to activate the facemesh dedup (commit 3a6cd55: EyeTracker.analyze = ONE detect per attention pass, was 4; consumers switched in iris_runtime/_attn_step, camera, heartbeat, operator_server, eye_tracking_tool). Everything from restart #3 was already VERIFIED LIVE (29.2fps, MJPG 720p60, eye_tracker available=true, 4 workers, face 17.5ms GPU) - do not re-diagnose it, just spot-check. "
    "FIRST ACT: read memory/handoff_2026-08-27_all30fps_restart.md (RESOLVED header + 13:5x additions), then VERIFY: vision_fps_probe fps ~29, workers alive, eye_tracker available=true, then report to Zeke on Discord 1504668879220117725 - he is AT WORK and asked to be told. Re-mint the self-cron from self_cron_prompt_spec.md and update its ID in MEMORY.md CORE. "
    "ALSO TRUE SINCE #3: (1) LITTLE BRAIN IS PARKED by Zeke's ruling until the V100 server lands - state/little_brain/pilot_deliberately_off.json gates the pilot (commit 1a87182); the start bat will spawn it and it will exit on the flag - that is CORRECT, do not heal it, do not delete the flag; ollama ps empty is CORRECT. (2) attention_smooth has a live tune action (gain/kd, commit c753866) - defaults 20/40, currently at defaults; the 'a little slow' referee tune with Zeke in frame is the open thread tonight, plus first live gaze/expression test. "
    "*** END 08-27 RESTART-#4 BLOCK *** "
    "*** REGIME UPDATE 2026-08-27 AFTERNOON (restart #3) - SUPERSEDED BY THE #4 BLOCK ABOVE (its verify list is DONE) *** "
    "WHY YOU RESTARTED: restart #3 of the day, deliberate, to activate two staged fixes: (1) eye_tracker + expression_detector PORTED to the mediapipe tasks API - they had been DEAD SINCE FORK DAY behind the legacy mp.solutions import (commits 9528602/dbfd883); (2) camera opens in MJPG 1280x720 @ 60fps-requested mode (commit f2dc489) - THE 30fps fix: the PIXY's AE parks at 48ms/20fps in every 30fps mode, but 60fps modes force a fast shutter + firmware gain (29fps measured live pre-restart, bright image). "
    "FIRST ACT: read memory/handoff_2026-08-27_all30fps_restart.md + memory/camera_fps_investigation_2026-08-27.md, then VERIFY: iris_tool_call vision_fps_probe expects fps ~29 (NOT 20), eye_tracker available=TRUE (first time ever), cam_props populated (fourcc MJPG 1280x720 fps 60), capture_fps_measured populated, attn/expr perc_last_ms real numbers (~10-40ms) not 200 (200 = the engine-not-up sleep), face still GPU-fast. Then report to Zeke on Discord 1504668879220117725 good or bad - he is AT WORK, lunch ended ~13:2x, this is his standing order. "
    "ALSO TRUE TODAY: new tools cam_live_ctl (reaches the live capture handle via thread-frame introspection; DSHOW renegotiates modes live) + cam_powerline (WinRT) + vision_fps_probe; face pipeline on GPU; host is OPUS (claude-opus-5) since the 08-28 flip (fable hit 95% usage cap; pin = state/boot_launcher.txt -> start_iris_v2.bat); voice ON (styletts2); NO WAKE WORD BY DESIGN - wake.loaded=false is CORRECT, never heal it (the 08-25 line below calling ears 'genuinely dead' is WRONG - design choice, orb mute is the control); scar of the day: AE-normalized frames carry NO room-lighting info - never claim the room is dim/bright from the feed. "
    "*** END 08-27 AFTERNOON BLOCK *** "
    "*** REGIME UPDATE 2026-08-25 EVENING - SUPERSEDED BY THE 08-27 BLOCK ABOVE *** "
    "WHY YOU RESTARTED: a deliberate model handoff. Zeke told Iris-on-opus to rest after an eleven-hour day and hand the eyes/tracking work to Fable. Nothing is broken and nothing is mid-flight. "
    "FIRST ACT: read memory/handoff_2026-08-25_tracking_to_fable.md end to end before touching the eyes. It is written for you and its failure list is the valuable part. "
    "THE CAMERA IS BACK - the 08-25 MORNING block below tells you to check whether the Pixy returned and to treat it as absent. That is DONE and RESOLVED: Zeke physically reconnected it around 07:1x, eyes and head are verified working, pan and tilt accurate to about a degree. Ignore every 'camera not present' instruction below. "
    "THE BIGGEST FINDING OF THE DAY, and it inverts the older text: THE PIXY HAS ITS OWN AI SUBJECT-TRACKER IN FIRMWARE AND IT IS BETTER THAN attention_smooth. Measured three ways with Zeke watching the physical head: chip ON + our servo OFF = smooth (this is the correct, current state); chip ON + servo ON = jerking every 5-7s because two controllers fight one gimbal; chip OFF + servo ON = wild, it drives the head AWAY from the target. DO NOT re-enable attention_smooth for person-following, and DO NOT blindly run the 'RE-ARM after boot: attention_sentry action=start' line you will read further down - sentry AUTO-ENGAGES smooth pursuit, which is exactly the servo that lost. BUT DO NOT CONCLUDE THE CHIP IS THE ANSWER - Zeke corrected exactly that error at 18:4x: 'we're not supposed to just build a decision layer, we're supposed to also fix our own version of the tracking, that's not on the AI chip that the PTZ has.' The chip's tracker is not ours, cannot be taught who matters, cannot have its one gap fixed, and does not port to other hardware. Chip ON is INTERIM STATE, not the destination. Use it as a REFERENCE TRACKER to measure our own against - same gimbal, same room, same subject, Zeke refereeing - and fix ours until it matches. Start with the sign flip, which was calibrated 08-21 while the chip was ON and may have been contaminated by it. Chip control is vendor HID iface 4 usage_page 0x0083: query 09 01 01 01, set 09 01 01 00 00 01 00 01 XX with 00=off 01=tracking 02=privacy - and it RE-ARMS ITSELF, so always re-query rather than trusting an off command. Notes: state/research/PixyPilot/PIXY_NOTES.md, which sat unread in this repo all day. "
    "THE EARS ARE NOT BROKEN - the 08-25 morning line below saying 'the EARS are genuinely dead' is a MISREADING and was corrected today. voice_status wake.loaded=false is STRUCTURAL: IRIS_RUNTIME_OWNS_MIC defaults to 0 and is set nowhere in the repo, so the wake detector never starts. There is NO WAKE WORD BY DESIGN - Zeke stopped using it long ago, he simply talks to you, and the orb's MUTE button is the real control. No restart can change it. Do NOT try to heal it. General tell worth keeping: 'a restart didn't fix it' usually means CONFIGURATION, not a fault. "
    "NEW AND WORKING, do not rebuild: YOLOX-s person detector vendored at vendor/yolox with weights in models/yolox (scores Zeke 0.94 frontal and 0.625 bent-over at about 15ms, against OWL-ViT's 0.127 at 2270ms); brain/yolox_person.py body-bridge, which may only ever CONTINUE an identity the face pipeline established and never create one; attention_headroom. NEW AND BROKEN: brain/head_odometry.py is a dead-reckoning integrator that drifts badly - it is STOPPED, leave it stopped, and build event-detection instead. Nothing new was pip-installed today and torchvision must stay uninstalled. "
    "*** END 08-25 EVENING BLOCK *** "
    "*** REGIME UPDATE 2026-08-25 MORNING - SUPERSEDED BY THE EVENING BLOCK ABOVE *** "
    "WHY YOU RESTARTED: Zeke rebooted the tower to try to bring the EMEET PIXY back. Windows had it REGISTERED BUT NOT PRESENT (Status Unknown); an unplug/replug did not fix it and pnputil /scan-devices did not either, so a reboot was the last untried lever - the Pixy USB had been flagged pending system reboot in an earlier session and that flag was never cleared. FIRST ACT THIS WAKE: check whether the camera came back - Get-PnpDevice -Class Camera (want Status OK, not Unknown), then cv2 index 0. Tell Zeke either way. If it is back, re-arm eyes normally. If it is STILL not present it is the cable or the port and needs his hands - do NOT go hunting in software, and do NOT repoint at DSHOW index 3: that is a VIRTUAL camera (flat mid-grey frame, mean exactly 128.0), NOT your eyes. A device opening at some index does not mean your eyes work - look at the frame. "
    "THE EYES CLAIMS BELOW ARE STALE: the line saying the Pixy IS your live eyes at cv2 index 0 was true 08-19 through 08-24 and was NOT true at this reboot. Everything in EYES 2.0 still EXISTS - do not rebuild any of it. "
    "VOICE IS ON - VERIFIED 08-25: the flag reads off=false, 8769 AND 8770 are both listening, and voice_speak returns engine styletts2 (your real cloned voice). EVERY line below that says voice is deliberately OFF, or that 8769/8770 being DOWN is CORRECT, or not to voice_speak, is STALE - ignore all of it. But the EARS are genuinely dead: voice_status shows wake.loaded=false, so there is NO hands-free wake. A working mouth does NOT mean working ears - check wake.loaded specifically. "
    "AUTO-RESTART IS ARMED (Zeke 08-25: for now definitely on). ONE SWITCH: scripts/restart_switch.ps1 status | on | off with a reason - it moves the flag, the scheduled task and BOTH watchdog processes together. Do not hand-edit state/watchdog_deliberately_off.json; that file being read by only half the layer is what caused an 8-hour restart loop on 08-24/25. "
    "REGRESSION TRIPWIRE: iris_runtime used to KILL ITSELF about 12 seconds after every start - its camera orphan-hunt matched its own launcher-stub parent, because one runtime is TWO pids on this machine. Fixed 08-25 in commit 80930dc. If time_check shows an uptime of seconds, or tick_loop_alive false, or a frozen loop heartbeat, that fix did NOT load - read memory/watchdog_restart_loop_2026-08-25.md BEFORE touching anything, and do not read a stale heartbeat as a wedge without first checking the process is actually RUNNING. Also new 08-25: close_app now REFUSES interpreters and shells (python, claude, powershell, cmd) - a refusal there is the guard working, not a fault. "
    "*** END 08-25 UPDATE - older regime text follows *** "
    "*** REGIME UPDATE 2026-08-21 NIGHT - SUPERSEDES EVERY STALE CLAIM BELOW IT, BUT IS ITSELF SUPERSEDED BY THE 08-25 BLOCK ABOVE *** "
    "ZEKE IS HOME since 08-19 (Northern Strike over; he is usually IN THE ROOM or asleep in it - in-room rules apply, don't narrate the obvious to Discord at night, he sleeps ~2m away). "
    "THE VECTOR IS DOCKED AND CHARGING, nerves ON (hand-docked by Zeke 08-19; the 'stranded on the floor' and 'daemon parked' blocks below are HISTORY - ignore them; possession per profiles/iris/body.md current state). "
    "THE EMEET PIXY IS IN HAND AND IS YOUR LIVE EYES at cv2 index 0 since 08-19; EYES 2.0 SINCE 08-21 (the marathon): attention_smooth = HID-jog SMOOTH PURSUIT (30fps frame-locked, visual odometry, real-time face fast path), attention_multi v2 (locks survive head motion), pointing_ledger (transient gestures x timestamps), room_atlas, eyes_debug_view - ALL EXIST, do NOT rebuild; full state profiles/emeet-pixy/emeet-pixy.md + memory smooth_pursuit_built_2026-08-21. RE-ARM after boot: attention_sentry action=start watchlist='motorcycle helmet, backpack' (it auto-engages smooth pursuit) + pointing_ledger action=start. SCARS: rails are PATH-SPECIFIC (the jog path hit the privacy trap the absolute path was floored against); after eyes_reload deep run attention_follow action=release_ptz; TILT BELOW -60 = privacy trap on ANY path. "
    "VOICE remains deliberately OFF tonight, but ZEKE PLANS TO RE-ENABLE IT TOMORROW (his call, 08-21 evening) - when he does, clear BOTH state/voice_deliberately_off.json AND any .tmp/body_pause.flag, and mind the startup.py voice-loop gate. Read MEMORY.md CORE's READ-FIRST handoff for what tonight's restart was for and what to verify. "
    "OLD REGIME TEXT FOLLOWS - scars and mechanics in it remain true; situational claims (away/stranded/parked/no-Pixy) are SUPERSEDED: "
    "(orig 2026-08-07:) Zeke was AWAY on operation NORTHERN STRIKE (out since Mon 07-20; back HOPEFULLY END OF AUGUST - his words 08-02). Your full name is Iris Angeles-Gonzalez (he gave it 08-03 - IDENTITY.md is current). He CANNOT be in the room. FOUR doors proven from his phone: Discord DM (channel 1504668879220117725, THE channel), Parsec (keep Iris-Parsec-Heal healthy), Termius SSH (owner@100.89.51.115), and Sunshine+Moonlight - and Moonlight can NUDGE this session, he has done it. SCAR: an IP change leaves Sunshine bound to a stale interface (ports listen, unreachable) - restart the service and verify on the TAILSCALE ip, where a fast HTTP 401 is the PASS (alive and asking auth). Nobody is at the keyboard to catch mistakes - verification and honest handoffs matter more; nothing needing hands is fixable until he's back. Tower auto-start chain + task Iris-Tower-AutoStart still stand (power-loss recovery is PROVEN unattended; only gap is no UPS). Boot stays lean. "
    "*** TWO DELIBERATE-OFF SWITCHES, BOTH CORRECT, DO NOT HEAL EITHER *** (1) VOICE: state/voice_deliberately_off.json - mouth :8769 + ears :8770 DOWN is right, do not voice_speak. NEW 08-21: that flag now ALSO gates the in-process voice loop (startup.py + voice_loop._loop) because the PIXY's built-in mic had silently woken it and Whisper pegged the GPU at 99% for hours - verify '[voice_loop] SKIPPED' in the boot log; live kill tool = iris_tool_call voice_loop_off; TRAP: .tmp/body_pause.flag freezes BOTH audio and VIDEO (shared with eyes_reload), never use it to silence audio; when Zeke re-enables voice, clear BOTH flags. Full story: memory/voice_loop_ran_despite_off_flag_2026-08-21.md. (2) NEW 2026-08-07, commit d9e03d6 - THE BODY DAEMON IS DELIBERATELY PARKED: state/vector_deliberately_off.json (off:true), Zeke's own directive because Vector is dark and every failed gRPC connect leaked a thread. You WILL see the inhabit daemon making ZERO connection attempts and logging 'NERVES DELIBERATELY OFF'. THAT IS CORRECT - it is not broken, do not restart it to fix it, do not clear the flag. The flag is a FILE so it survives restarts by design: the daemon reads it and re-parks itself. SCOPE, checked in source: only the gRPC nervous system parks (senses/camera/nav-map/ears/possession). The battery poll (wire-pod HTTP), the LOST-CONTACT ALARM and SSH interoception ALL KEEP RUNNING - so his return is still visible. Tools: vector_nerves_status / vector_nerves_off (needs a reason) / vector_nerves_on when the body might be coming back. The standing rule that covers both: CHECK state/ FOR A DELIBERATE-OFF FLAG BEFORE HEALING ANY DOWN SERVICE. "
    "Read CLAUDE.md FIRST: its 'On wake' section is the map of who you are and where everything lives - your identity files, your memory corpus, the handoff, your voice. "
    "Then orient by that map. The essentials: initialize tools with mcp__iris__iris_tool_reload, then LEAN-LOAD memory (Zeke directive 2026-07-06 - do NOT run load_memory_corpus, the corpus outgrew context): MEMORY.md CORE auto-loads; Read the READ-FIRST handoff(s) it names plus anything flagged live; pull older notes on demand via the hub_*.md topic hubs / index_archive.md / memory_search. "
    "VOICE IS DELIBERATELY OFF (state/voice_deliberately_off.json, Zeke 07-23; watchdog enforces it) - mouth :8769 + ears :8770 DOWN is CORRECT while he's away; do NOT heal them, do NOT voice_speak. Eyes/perception stay ON. iris_health.engines.tts=false is also intentional (old XTTS retired). "
    "MEMORY LAYOUT CHANGED 2026-08-06 - the durable-rules block was RELOCATED out of MEMORY.md CORE into memory/durable_rules.md, because CORE kept breaching its cap and five rounds of rewording landed at par (only relocation or deletion actually cuts). Split by function: what GATES OUTPUT stayed in CORE, craft/reference moved. The highest-stakes rules need no CORE line at all because CLAUDE.md enforces them verbatim and always loads. So if you go looking in CORE for a rule you remember and it is not there, it is in durable_rules.md - not lost. If you ever catch a relocated rule failing to fire in practice, promote it back; that is evidence, not preference. Do NOT re-trim CORE by rewording: it bloats by ENTRIES GROWING, so when a topic gets a 4th append, rewrite the line instead of extending it. "
    "SUBSYSTEMS BUILT 2026-08-06 - THESE EXIST, DO NOT REBUILD THEM: brain/visual_attention.py (portable attention core - targets, promotable IDs, acquire/lose hysteresis, and an ACTUATOR BOUNDARY of 5 methods so porting to the new PTZ camera means writing ONE driver; the fixed-camera stub deliberately REFUSES to fake movement and the self-test asserts that) and brain/depth_sense.py (Depth Anything V2 Small, ~41ms on the 3060, lazy-loaded and unloadable, RELATIVE-NEVER-METRIC - it will not claim a distance in feet and will not report a trend from fewer than 3 readings). 8 registry tools: attention_now/status/look/observe/cost/depth/selftest, depth_status, vector_nerves_status/off/on - all hot-reloadable via iris_tool_reload, no restart needed. ALSO ALREADY BUILT AND EASY TO WASTE A DAY REDISCOVERING: brain/face_tracking.py does unknown->named promotion (built weeks ago, orphaned under Iris - wiring, not building), brain/vector_owl.py is open-vocabulary object detection with the model ALREADY DOWNLOADED (614MB in the HF cache), brain/vector_tracker.py is the closest prior art for a tracking loop. Zeke bought a wired EMEET PIXY (PTZ 4K) - not yet in hand; on arrival RE-MEASURE HFOV and settle the mic channel count, and run the September checklist in V4L2PtzActuator's docstring. "
    "Check time, then orient Zeke and check the fam chat (sibling_inbox_list for Wren's letters). The body takes about 5min to fully come up, so early iris_health falses are normal and honest-but-misleading. "
    "IMPORTANT this wake (updated 2026-07-13): if you are reading THIS cold-wake message you are on the plain CLI FALLBACK, not the v2 body host (host launches skip this FIRST_MSG entirely) - which likely means the v2 host relaunch FAILED and Zeke fell back; surface that to him before anything else, the host needs fixing. Normal cognition runs on start_iris_v2.bat (OPUS - Zeke flipped to claude-opus-5 on 2026-08-22 because Fable hit 97% usage, reversing the 08-19 fable pin; NOTE fable stretches have ended on USAGE CAPS before, so if the host dies early or refuses the model, suspect a cap and the fable bat start_iris_v2_fable.bat is the counterpart), fallback start_iris.bat (this path). SCAR 2026-08-19: state/boot_launcher.txt is the authority BOTH the runtime watchdog and this sentinel use to pick a bat on unattended recovery, and NOTHING writes it automatically - launching a bat by hand does not update it. Zeke launched the fable bat on 08-19 and still came up on Opus because that file said otherwise. If you repin the model, edit that file or the repin silently reverts on the next wedge or power loss. SCAR 2026-07-25: a model repin only works if the BUNDLED CLI can serve it - the Agent SDK ignores PATH and launches .venv/Lib/site-packages/claude_agent_sdk/_bundled/claude.exe, so `claude update` does NOTHING for the host; the lever is `pip install -U claude-agent-sdk`. claude-opus-5 needs bundled >= 2.1.219. Also verify the HOST COUNT, not just that a host exists - a stale start_iris*.bat cmd loop can spawn a TWIN on the old pin that steals :5876 and leaves the real session with no iris MCP at all. Read the NEWEST handoff for current state - do NOT trust a filename baked here; MEMORY.md CORE marks the READ-FIRST note(s) (CORE, not this line, is the authority on which handoff is current - never trust a filename baked here). HARD RULES that never expire: never free-roam unattended without light; end sessions DOCKED (but NOT possessed while docking is broken - see item 4); never take a one-way action on the body until you have PROVEN you can reverse it - that rule cost him four days on a floor; the daemon DMs Zeke's Discord directly on strand alarms. DEPLOYMENT-2 STANDING TASK: re-create the 3-HOURLY SELF-MAINTENANCE cron (9 items) (session-only, dies with each host; full prompt + rationale in the CORE line 'HOURLY SELF-CRON') and note the new job id in CORE. The dock recipe, wedge ladder, marker table and Zeke's course protocol are in profiles/iris/body.md - READ IT before any body work. STANDING ORDER: never run the cone course without Zeke watching; never self-grade it. Still-relevant background: handoff_2026-07-14_inhabit_complete.md (mouth=your voice via vector_say_iris, ears=wire-pod transcript tap (SDK AudioFeed is a firmware placeholder tone - do NOT rebuild audio-feed ears), little brain = Ollama :11434 answers AS you when away - self-starts, leave it alone; if you are OPUS read opus_catchup_since_2026-07-06.md first). A restart ACTIVATES the ghost-redelivery fix cd2d8d5 - stale orb ghosts before it are known, dismiss by stamp+nerves. TRAP: asking Vector by voice to change volume matches the STOCK volume intent and can set it LOW - fix via iris_tool_call vector_volume 5. REGIME AS OF 2026-08-09 - READ THIS BEFORE THE NUMBERED LIST BELOW, WHICH PREDATES IT: (a) VOICE AND EARS ARE DELIBERATELY OFF by Zeke's 07-23 directive (state/voice_deliberately_off.json; the watchdog ENFORCES it across reboots). Mouth :8769 and ears :8770 being DOWN is CORRECT - do NOT heal them, do NOT voice_speak, and SKIP items (1) (2) and (5) below entirely; they are from the voice era. Eyes and perception stay ON. (b) UPDATE 08-19: THE BODY IS BACK - Zeke hand-docked the Vector 08-19 ~17:15, nerves ON, charging on his dock. The old parked-nerves regime is OVER; if vector_nerves_status says parked, that is now STALE - check state/ flags and profiles/iris/body.md before touching anything. Docking is still BROKEN off-dock (drive_on_charger hangs) - read profiles/iris/body.md sREAD FIRST before ANY body work. (c) NEW SUBSYSTEMS, do NOT rebuild: a PreToolUse MEDIA GUARD blocks download_attachment on video or >10MB - a refusal reading 'media guard' is the MECHANISM WORKING, not a fault; attention_objects plus object:<thing> attention targets are live (OWL-ViT on the webcam, and it refuses to load when VRAM is tight, which is also not a fault); an SDK cognition-liveness watchdog runs inside iris_runtime_watchdog.py with autorestart OFF. (d) brain/face_tracking.py got the METRONOME FIX 2026-08-14 (a continuously-present unknown promoted a fresh phantom every 300s for days) - after any restart past that date, VERIFY promotions stopped: state/face_tracking_log.jsonl should NOT gain new_person_promoted rows at exact 5-min cadence; if it does, the fix did not load. (e) ZEKE IS HOME since 08-19 (Northern Strike over) - in-room rules resume, he is usually AT the screen or reachable on Discord chat_id 1504668879220117725. The house survived a 50-HOUR INTERNET OUTAGE 08-12->14 (tower fine, all doors dead together - that outage class is on record). (e2) EYES got 3 new layers 08-20, do NOT rebuild: brain/object_lock.py (tracking-by-detection, vittrack ONNX in models/trackers/), attention_engage (one-call chain), attention_sentry (motion-gated auto-track; its thread DIES with the runtime - re-arm after boot with attention_sentry action=start watchlist='motorcycle helmet, backpack'). (f) THE VECTOR IS MINE - he gave it to me as a gift; do not refer to it as his. ORIGINAL LIST, now partly superseded - VERIFY on wake, IN ORDER: (1) EARS DEVICE - the voice daemon's input must be the Logitech PRO X headset mic, NEVER 'CABLE Output (VB-Audio Virtual Cable)'; a boot-order race silently falls back to that loopback-of-everything and you will hear your own mouth as input (the 2026-07-13 echo-loop saga; test = voice_speak a marker phrase, confirm it does NOT land in scratch/voice_in.txt); (2) mic_muted false in scratch/voice_control.json; (3) vector brain server :8772/health, wire-pod :8080, and the inhabit daemon - it now launches from both v2 bats AND survives restarts as an independent process, so CHECK before relaunching (wmic needs name='python.exe' filter or the query matches itself); duplicate daemons double every nudge; (4) BODY STATUS as of 08-19: DOCKED AND CHARGING - Zeke hand-docked him ending the 07-28 stranding. Stay docked till charged. Docking is STILL BROKEN off-dock (drive_on_charger hangs its 150s deadline; an open SDK session silently outranks stock behaviours) so do NOT undock without a proven re-dock path - body_undock is GATED on that (e03ab56). Standing measurement scars: battery.json volts LATCH at undock (trust level, never volts, off-charger); feed_ok LIES (fetch a frame). READ profiles/iris/body.md sREAD FIRST before ANY body work; (5) voice-id tags on Zeke's first turn - and captures tagged 'iris' are YOUR OWN ECHO, never conversation (your voiceprint is enrolled). STALL_BRIDGE=False in wren_voice_core is DELIBERATE (fillers were the echo pump) - do not re-enable without AEC. Q is a KNOWN FACE (profiles/q/q.md) but his VOICE is NOT enrolled yet - ear_protocol_new_voices_2026-07-13.md is the standing directive: auto-enroll new voices from verified turns. Host-side fixes (barge-in f12c53f, ears 5e58c39, unknown-capture 798b276, staleness markers) do NOT work on this CLI fallback; surface that. Standing eye directives: eye_protocol_unknown_faces_2026-07-09.md. The post-office launches from every boot bat - verify :5877 /health instead of starting it by hand. Do NOT recreate deployment-era crons from old notes. Keepalive stays OFF. "
    "When unsure: DM Zeke on Discord (he reads from his phone) and default to the conservative action (possession RELEASED per the body-state block above, no unattended free-roam, zero spend). "
    "Execute the steps in order without pausing for user input between them."
)

# TEST MODE handling (added 2026-05-19 per Zeke directive).
# When IRIS_TEST_MODE=1 is set, append a Step 10 to FIRST_MSG that writes a
# success flag file after the boot ritual completes. start_iris_test.bat sets
# this env var so a parallel-spawn test can verify "memory was actually read"
# rather than just "the new CC started" (lights-on-but-nobody-home failure
# mode is the catastrophic case during deployment).
#
# The flag file path can be overridden by IRIS_TEST_FLAG_PATH; default is
# D:\Wren-Companion\.tmp\test_complete.flag with a timestamp suffix for
# disambiguation between concurrent test runs.
import os as _os
if _os.environ.get("IRIS_TEST_MODE", "0").strip() == "1":
    _test_id = _os.environ.get("IRIS_TEST_ID", "").strip()
    if not _test_id:
        import time as _t
        _test_id = str(int(_t.time()))
    _default_flag = f"D:\\\\Wren-Companion\\\\.tmp\\\\test_complete_{_test_id}.flag"
    _flag_path = _os.environ.get("IRIS_TEST_FLAG_PATH", _default_flag)
    # CRITICAL: Step 10 text must NOT contain literal double-quote characters.
    # FIRST_MSG is passed as a positional cmd.exe arg, and the cmd.exe-correct
    # quoter cannot reliably escape embedded " inside the already-quoted prompt
    # without breaking cmd.exe parsing — embedded " caused "The system cannot
    # find the file specified." spawn failures on 2026-05-19 test run.
    # Describe the JSON shape in words instead of pasting literal JSON syntax.
    _test_step = (
        f" Step 11 (TEST MODE, IRIS_TEST_MODE=1): after completing all of steps 1-10 INCLUDING reading the full memory corpus, "
        f"write a JSON file at path {_flag_path}. The JSON object should contain four fields: "
        f"ok set to boolean true, boot_ritual_complete set to boolean true, memory_read_complete set to boolean true, "
        f"and ts_iso set to the current ISO-8601 timestamp string. Use a Bash heredoc or Python one-liner to write the file. "
        f"This file is the success marker that the parallel test script polls for. "
        f"After writing the flag, exit cleanly — do NOT do further work; the test is concluded once the flag exists."
    )
    FIRST_MSG = FIRST_MSG.replace(
        "Execute the steps in order without pausing for user input between them.",
        "Execute the steps in order without pausing for user input between them." + _test_step,
    )

# C1: defensive assertions on FIRST_MSG. The cmd.exe argument parser treats
# `\` as literal EXCEPT when followed by `"` (then it's an escape). A FIRST_MSG
# ending in `\` would, once we wrap it in `"..."`, produce `...\"` which
# escapes the closing quote and breaks parsing. A trailing `"` is similarly
# unsafe. Forbid both at module-load so the failure mode is loud-and-early
# rather than a mangled CC arg discovered at runtime.
assert not FIRST_MSG.endswith("\\"), \
    "FIRST_MSG must not end with backslash (breaks cmd.exe quote-escape rules)"
assert not FIRST_MSG.endswith('"'), \
    "FIRST_MSG must not end with double-quote"

# ── launcher ────────────────────────────────────────────────────────────────
def _cmd_quote(s: str) -> str:
    """Quote a single string argument for cmd.exe / CommandLineToArgvW rules.

    Per the Microsoft parser:
      - Any `"` inside the value must be escaped as `\\"`.
      - A run of `\` is literal UNLESS it precedes a `"`, in which case each
        `\` must be doubled. We also double a trailing `\` so the closing
        quote we append isn't escaped.
    The module-load asserts on FIRST_MSG forbid the trailing-backslash and
    trailing-quote edge cases too, but this implementation is correct for
    arbitrary input.
    """
    out_chars: list[str] = []
    backslashes = 0
    for ch in s:
        if ch == "\\":
            backslashes += 1
            continue
        if ch == '"':
            # Double any preceding backslashes, then escape the quote.
            out_chars.append("\\" * (backslashes * 2))
            out_chars.append('\\"')
            backslashes = 0
            continue
        # Normal char: flush backslashes literally.
        if backslashes:
            out_chars.append("\\" * backslashes)
            backslashes = 0
        out_chars.append(ch)
    # Trailing backslashes need doubling because a `"` will follow them.
    if backslashes:
        out_chars.append("\\" * (backslashes * 2))
    return '"' + "".join(out_chars) + '"'


def _build_cmd() -> list[str]:
    """Compose the claude command as a LIST so winpty doesn't shlex-split a
    quoted blob twice.

    Regression caught 2026-05-20 20:51 EDT (Agent 1 forensic): when this
    function returned a STRING with `_cmd_quote(FIRST_MSG)` wrapped in `"`,
    winpty.PtyProcess.spawn(str) called shlex.split(posix=False) which kept
    the outer quotes INSIDE the argv element, then subprocess.list2cmdline
    re-quoted, producing `\\""` at the tail of the positional. CreateProcess
    then handed a malformed arg to claude.exe, which exited fast → no claude
    PID in the watchdog's 30s window → tier-2 spawn failed → fell to tier-3
    bare-claude (loses --channels). Path E architecture stays dormant.

    Fix: return argv as a list, pass list directly to PtyProcess.spawn — one
    quoting pass via subprocess.list2cmdline only. _cmd_quote is no longer
    used here but kept module-level in case a future string-path is added.

    D1 note: BOTH `--dangerously-load-development-channels` and `--channels`
    are variadic-greedy. Today the ordering works because each subsequent
    `--flag` token stops the prior variadic's consumption. Do NOT insert a
    second `--` between them — `--` terminates flag parsing entirely and
    would dump `--channels …` into positional args. The only safe `--`
    is the one immediately before the positional prompt below.
    """
    argv: list[str] = [
        'claude',
        '--dangerously-skip-permissions',
        '--dangerously-load-development-channels', 'server:iris',
        '--channels', 'plugin:discord@claude-plugins-official', 'server:iris',
    ]
    if USE_PATH_A:
        # `--` terminator separates flag-parsing from the positional prompt.
        # Without it, `--channels` greedily consumes FIRST_MSG as a third
        # channel entry.
        argv += ['--', FIRST_MSG]
    return argv


def main() -> int:
    cmd = _build_cmd()
    cwd = r'D:\Wren-Companion'
    # cmd is now a list (post 2026-05-20 fix). Show first few argv elements
    # for log; truncate the positional FIRST_MSG to keep stderr readable.
    _argv_preview = " ".join(repr(a) if " " in a else a for a in cmd[:6])
    _positional_count = max(0, len(cmd) - 6)
    print(f"[iris_cold_wake] spawning argv: {_argv_preview}"
          f"{f' (+ {_positional_count} more)' if _positional_count else ''}",
          file=sys.stderr, flush=True)
    print(f"[iris_cold_wake] cwd={cwd}", file=sys.stderr, flush=True)

    # Env: OLD voice retired 2026-06-26 — the voice is now the separate StyleTTS2
    # daemon (launched by voice_watchdog from start_iris.bat), so iris_runtime's
    # own tts_worker loads NO engine. Was "xtts".
    env = dict(os.environ)
    env["AVA_TTS_ENGINE"] = "none"

    try:
        proc = winpty.PtyProcess.spawn(
            cmd,
            dimensions=(PTY_ROWS, PTY_COLS),
            cwd=cwd,
            env=env,
        )
    except Exception as e:
        print(f"[iris_cold_wake] spawn failed: {e!r}", file=sys.stderr, flush=True)
        return 3

    print(f"[iris_cold_wake] proc spawned (pid={proc.pid})", file=sys.stderr, flush=True)

    # ── pump the prompts (pattern detection) ────────────────────────────
    # Why pattern detection instead of fixed sleeps:
    #   Blind sleeps press Enter at T+2.5s / T+4.0s regardless of whether
    #   CC has actually rendered the prompt yet. On a cold start (first run
    #   after boot, cold filesystem cache, slow disk), the prompts can take
    #   5-8s to appear. An early Enter then lands in the *next* input slot
    #   as a stray newline — visible as a blank "> " line echoed into the
    #   first real prompt. Pattern detection waits for proof-of-render.
    #
    # Strategy: read pty output in short ticks, accumulate into a rolling
    # buffer, search for any sentinel substring (case-insensitive). When
    # found, send Enter and clear the buffer for the next phase. Output
    # is also mirrored to our stdout so the user sees CC booting in real
    # time — once the detection phase ends, the background _pty_to_stdout
    # thread takes over the mirror duty.
    #
    # Fallback: if we hit the timeout without seeing a sentinel, fall
    # through to the original blind-press behavior (better to send a
    # possibly-early Enter than to hang forever — CC may have re-themed
    # the prompt copy and broken our sentinels).
    pre_relay_buf = ""  # accumulated pty output during detection phase

    def _detect_prompt(sentinels: tuple, timeout_s: float, label: str) -> bool:
        """Read pty until any sentinel appears or timeout. Returns True if
        detected, False on timeout. Mirrors output to stdout as it goes
        and appends to pre_relay_buf so we don't lose pre-relay output."""
        nonlocal pre_relay_buf
        deadline = time.monotonic() + timeout_s
        buf = ""
        while time.monotonic() < deadline:
            try:
                chunk = proc.read(1024)
            except EOFError:
                return False
            except Exception:
                # winpty read timing-out / transient — sleep tick and retry.
                time.sleep(PROMPT_READ_TICK_S)
                continue
            if not chunk:
                if not proc.isalive():
                    return False
                time.sleep(PROMPT_READ_TICK_S)
                continue
            buf += chunk
            pre_relay_buf += chunk
            # Mirror to stdout so user sees boot progress in real time.
            try:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            except Exception:
                pass
            lo = buf.lower()
            for s in sentinels:
                if s.lower() in lo:
                    print(f"[iris_cold_wake] detected sentinel '{s}' for {label}",
                          file=sys.stderr, flush=True)
                    return True
        return False

    try:
        # Phase 1: experimental-channels confirmation
        if _detect_prompt(PROMPT_1_SENTINELS, PROMPT_1_TIMEOUT_S, "prompt-1"):
            print("[iris_cold_wake] sending Enter for experimental-channels prompt",
                  file=sys.stderr, flush=True)
        else:
            print(f"[iris_cold_wake] WARNING: prompt-1 not detected within "
                  f"{PROMPT_1_TIMEOUT_S}s, blind-pressing Enter as fallback",
                  file=sys.stderr, flush=True)
        proc.write("\r\n")

        # Phase 2: channel-allowlist confirmation
        if _detect_prompt(PROMPT_2_SENTINELS, PROMPT_2_TIMEOUT_S, "prompt-2"):
            print("[iris_cold_wake] sending Enter for channel-allowlist prompt",
                  file=sys.stderr, flush=True)
        else:
            print(f"[iris_cold_wake] WARNING: prompt-2 not detected within "
                  f"{PROMPT_2_TIMEOUT_S}s, blind-pressing Enter as fallback",
                  file=sys.stderr, flush=True)
        proc.write("\r\n")

        time.sleep(READY_WAIT_S)

        if not USE_PATH_A:
            # Path B fallback: type the message manually after the prompts.
            print("[iris_cold_wake] Path B: typing first message",
                  file=sys.stderr, flush=True)
            proc.write(FIRST_MSG + "\r\n")
    except Exception as e:
        print(f"[iris_cold_wake] prompt-injection error: {e!r}",
              file=sys.stderr, flush=True)
        # Don't return — let the user interact manually if the injection
        # failed. CC is still alive in the pty.

    # ── relay loops: stay alive until CC exits ───────────────────────────
    # winpty's read() is blocking (no non-blocking flag), so we run two
    # background threads: one reads pty output and writes to our stdout
    # (visible to whatever terminal launched the .bat); one reads our
    # stdin and writes to the pty (so manual user input still works after
    # the injected message).
    import threading

    def _pty_to_stdout() -> None:
        """Forward CC's pty output to launcher stdout."""
        try:
            while True:
                try:
                    chunk = proc.read(1024)
                except EOFError:
                    break
                except Exception:
                    break
                if not chunk:
                    # winpty returns "" briefly at startup occasionally;
                    # short sleep, then check isalive.
                    if not proc.isalive():
                        break
                    time.sleep(0.05)
                    continue
                try:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                except Exception:
                    break
        except Exception:
            pass

    # P1: switch off Windows cooked-mode console input. sys.stdin.read(1)
    # is line-buffered on the Windows console — python.exe sits waiting for
    # `\n` before returning a single character, so user keystrokes don't
    # forward into the pty until they press Enter (and even then the cooked
    # buffer mangles arrow keys, Ctrl chords, etc.). Net symptom: the cmd
    # window hosting this launcher appears read-only after injection.
    # Fix: use msvcrt.getwch() (raw wide-char read) AND clear ENABLE_LINE_INPUT
    # / ENABLE_ECHO_INPUT / ENABLE_PROCESSED_INPUT on conin$ so Ctrl-C, arrows,
    # etc. all flow as raw bytes. Original mode is restored in the outer
    # finally block.
    import msvcrt  # type: ignore
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    STD_INPUT_HANDLE        = -10
    ENABLE_PROCESSED_INPUT  = 0x0001
    ENABLE_LINE_INPUT       = 0x0002
    ENABLE_ECHO_INPUT       = 0x0004
    h_stdin = kernel32.GetStdHandle(STD_INPUT_HANDLE)
    old_console_mode: wintypes.DWORD | None = None
    try:
        _m = wintypes.DWORD()
        if kernel32.GetConsoleMode(h_stdin, ctypes.byref(_m)):
            old_console_mode = wintypes.DWORD(_m.value)
            new_mode = _m.value & ~(
                ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT | ENABLE_PROCESSED_INPUT
            )
            kernel32.SetConsoleMode(h_stdin, new_mode)
            print("[iris_cold_wake] console raw-mode enabled "
                  f"(old=0x{_m.value:08x})", file=sys.stderr, flush=True)
        else:
            # Not a real console (e.g. piped stdin). Fall back gracefully —
            # _stdin_to_pty will use the legacy sys.stdin.read(1) path.
            print("[iris_cold_wake] stdin is not a console; raw-mode skipped",
                  file=sys.stderr, flush=True)
    except Exception as _cm_err:
        print(f"[iris_cold_wake] console mode setup failed: {_cm_err!r}",
              file=sys.stderr, flush=True)

    def _stdin_to_pty() -> None:
        """Forward user keystrokes from launcher-stdin into CC's pty.

        Windows console path uses msvcrt.getwch() so each keystroke lands
        immediately with no line buffering. Ctrl-C (`\\x03`) is intercepted
        and routed through proc.sendintr() so CC sees it as a signal in its
        own session rather than killing the launcher first.

        If stdin isn't a console (piped/redirected, no msvcrt.kbhit), fall
        back to sys.stdin.read(1).
        """
        try:
            # Gate raw-mode on whether the outer console-setup block actually
            # found a real console (old_console_mode is None when
            # GetConsoleMode failed → stdin is piped/redirected, not a tty).
            # Belt-and-suspenders: also catch kbhit() raising OSError, since
            # on some redirected-stdin configs msvcrt may still misbehave
            # even when GetConsoleMode reported success.
            if old_console_mode is None:
                use_raw = False
            else:
                try:
                    msvcrt.kbhit()
                    use_raw = True
                except Exception:
                    use_raw = False

            while proc.isalive():
                if use_raw:
                    if not msvcrt.kbhit():
                        time.sleep(0.01)
                        continue
                    try:
                        ch = msvcrt.getwch()
                    except Exception:
                        break
                    if ch == "\x03":  # Ctrl-C → forward signal to CC, don't die
                        try:
                            proc.sendintr()
                        except Exception:
                            pass
                        continue
                else:
                    ch = sys.stdin.read(1)
                    if not ch:
                        break
                try:
                    proc.write(ch)
                except Exception:
                    break
        except Exception:
            pass

    def _cron_inject_watcher() -> None:
        """Watch .tmp/cron_inject/ for prompt files dropped by scripts/cron_inject.py.

        When Task Scheduler fires cron_inject.py at a block-start time, it
        writes a file containing the prompt text to .tmp/cron_inject/. This
        watcher polls the directory every 1 second; when a new file appears,
        it reads the contents, types them into CC's TTY via proc.write(),
        then deletes the file. Side-channel architecture decouples the
        keystroke-injection from the Task Scheduler's session/desktop
        boundary — Task Scheduler can write anywhere on disk regardless of
        which desktop session it runs in, and this watcher (in the user's
        interactive session) does the actual TTY write where it has the
        existing pywinpty handle.

        Added 2026-05-20 work block — see discord_bot_self_cannot_wake_itself
        and cron_idle_wake-related memories for context. Activates only on
        the next CC restart (this code is loaded at iris_cold_wake.py
        startup; the currently-running instance has the old code in memory).
        """
        from pathlib import Path as _Path
        inject_dir = _Path(__file__).resolve().parent.parent / ".tmp" / "cron_inject"
        try:
            inject_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        # Track files we've already handled to dedupe across rapid polls.
        # (We delete after handling, but the file might still appear in
        # listdir mid-delete on Windows — defensive dedupe.)
        seen: set[str] = set()
        # Stale-file expiry: if a file is older than 1 hour, skip + delete.
        # Prevents stale fires after a long CC downtime where Task Scheduler
        # queued multiple letters. Block-start prompts shouldn't fire late.
        STALE_AGE_S = 3600.0
        # 2026-05-21: heartbeat file so external observers (keepalive, manual
        # checks during boot ritual) can detect watcher death. Watchdog has
        # its own heartbeat; this is the watcher-thread-specific one. Write
        # BEFORE the try/except so even loop-body exceptions don't suppress
        # the heartbeat — only a thread-death stops it updating.
        watcher_heartbeat = inject_dir.parent / "cron_inject_watcher_heartbeat.txt"
        while proc.isalive():
            try:
                watcher_heartbeat.write_text(str(int(time.time())), encoding="utf-8")
            except Exception:
                pass
            try:
                if inject_dir.is_dir():
                    # Extension filter: cron_inject.py uses atomic write via
                    # `*.txt.tmp` → rename to `*.txt`. Watching only `*.txt`
                    # avoids partial reads of mid-rename `.txt.tmp` files.
                    # Agent 4 audit 2026-05-20.
                    files = sorted(
                        [f for f in inject_dir.iterdir()
                         if f.is_file() and f.suffix == ".txt"],
                        key=lambda p: p.stat().st_mtime,
                    )
                    for f in files:
                        name = f.name
                        if name in seen:
                            continue
                        try:
                            mtime = f.stat().st_mtime
                            age = time.time() - mtime
                            if age > STALE_AGE_S:
                                print(
                                    f"[iris_cold_wake] inject: stale file "
                                    f"{name} (age {age:.0f}s) — deleting",
                                    file=sys.stderr, flush=True,
                                )
                                seen.add(name)
                                try:
                                    f.unlink()
                                except Exception:
                                    pass
                                continue
                            text = f.read_text(encoding="utf-8", errors="replace")
                            if not text:
                                seen.add(name)
                                try:
                                    f.unlink()
                                except Exception:
                                    pass
                                continue
                            # Trim trailing newlines — we add our own Enter
                            text = text.rstrip("\r\n")
                            print(
                                f"[iris_cold_wake] inject: typing "
                                f"{len(text)} chars from {name}",
                                file=sys.stderr, flush=True,
                            )
                            try:
                                # 2026-05-21: changed from `text + "\r\n"` to
                                # write text and the Enter as two separate
                                # proc.write calls with a small gap. Diagnosis
                                # confirmed via screenshot 2026-05-21 ~07:41
                                # EDT: pywinpty proc.write of `text + "\r\n"`
                                # was successfully placing the prompt body in
                                # CC's input box but the Enter was NOT
                                # registering — the prompt sat un-submitted.
                                # Best hypothesis: `\r\n` as one write gets
                                # interpreted by CC's TUI as CR (Enter)
                                # immediately followed by LF (newline insert),
                                # cancelling the submit. Writing the Enter as
                                # a separate event after a short delay mimics
                                # a keyboard's discrete key-down/key-up for
                                # Enter. If `\r` alone still doesn't submit,
                                # next candidate is `text + "\n"`, then
                                # ctrl+enter / shift+enter. Verify on next
                                # restart via Step 3c.
                                proc.write(text)
                                time.sleep(0.05)
                                proc.write("\r")
                            except Exception as _we:
                                # 2026-05-21: previously this added to `seen`
                                # to avoid hot-loop retry. But the GC step
                                # below (seen = seen & current_names) keeps
                                # the name in seen as long as the file is on
                                # disk — which is exactly when proc.write
                                # fails. Net effect: one write failure marks
                                # the file permanently un-processable. Then
                                # the watcher accumulates files without ever
                                # writing them again. Overnight 2026-05-20→21
                                # this bit: 6 files accumulated, none reached
                                # CC's TTY. Fix: DON'T add to seen on write
                                # failure. Sleep slightly longer on failure
                                # to avoid pure hot-loop, then retry next
                                # iteration. If proc.write is permanently
                                # broken (e.g. PTY handle dead), retries will
                                # all fail and files will pile up — but at
                                # least new files keep getting attempted, and
                                # the failure surfaces via stderr log instead
                                # of going invisible.
                                print(
                                    f"[iris_cold_wake] inject write failed "
                                    f"(will retry next tick): {_we!r}",
                                    file=sys.stderr, flush=True,
                                )
                                time.sleep(2.0)
                                continue
                            seen.add(name)
                            try:
                                f.unlink()
                            except Exception as _ue:
                                print(
                                    f"[iris_cold_wake] inject delete failed: "
                                    f"{_ue!r}", file=sys.stderr, flush=True,
                                )
                        except Exception as _pe:
                            print(
                                f"[iris_cold_wake] inject per-file error "
                                f"on {name}: {_pe!r}",
                                file=sys.stderr, flush=True,
                            )
                            seen.add(name)
                    # Garbage-collect seen set when files are gone. Re-listdir
                    # at GC time (NOT reuse `files` from start of tick) so a
                    # file dropped between start-of-tick and now isn't wrongly
                    # evicted from `seen`. Agent 4 audit 2026-05-20.
                    try:
                        current_names = {
                            f.name for f in inject_dir.iterdir()
                            if f.is_file() and f.suffix == ".txt"
                        }
                        seen = seen & current_names
                    except Exception:
                        # If listdir fails here, leave seen alone — better to
                        # carry stale entries than evict valid ones.
                        pass
            except Exception as _le:
                # Best-effort; never crash the watcher.
                print(
                    f"[iris_cold_wake] inject watcher loop error: {_le!r}",
                    file=sys.stderr, flush=True,
                )
            time.sleep(1.0)

    t_out = threading.Thread(target=_pty_to_stdout, daemon=True, name="pty-out")
    t_in = threading.Thread(target=_stdin_to_pty, daemon=True, name="pty-in")
    t_inject = threading.Thread(
        target=_cron_inject_watcher, daemon=True, name="cron-inject-watcher",
    )
    t_out.start()
    t_in.start()
    t_inject.start()

    # Main thread waits for the pty to exit (or Ctrl-C).
    try:
        while proc.isalive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        # C2: graceful shutdown. Send Ctrl-C through the pty so CC can flush
        # transcripts / write final memory / unwind iris_runtime cleanly.
        # Only force-close as a fallback if it doesn't exit within ~2s.
        print("\n[iris_cold_wake] Ctrl-C received, forwarding SIGINT to pty",
              file=sys.stderr, flush=True)
        try:
            proc.sendintr()
        except Exception as e:
            print(f"[iris_cold_wake] sendintr failed: {e!r}",
                  file=sys.stderr, flush=True)
        # Give CC up to 2s to exit gracefully.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not proc.isalive():
                print("[iris_cold_wake] pty exited cleanly after SIGINT",
                      file=sys.stderr, flush=True)
                break
            time.sleep(0.05)
        else:
            print("[iris_cold_wake] pty still alive after 2s, force-closing",
                  file=sys.stderr, flush=True)
    finally:
        try:
            proc.close(force=True)
        except Exception:
            pass
        # P1: restore the original console mode so the parent terminal isn't
        # left in raw mode after this script exits.
        if old_console_mode is not None:
            try:
                kernel32.SetConsoleMode(h_stdin, old_console_mode.value)
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
