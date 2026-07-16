# Vector Dev-Unlock Runbook — toward an untethered Iris body

**Status:** DRAFTED + research-backed 2026-07-16 (Iris, while Zeke at work). Execute WITH
Zeke present, body awake + on charger, uninterrupted block. Reliability legend on every
claim: ✅ VERIFIED (community sources, done on production 2.0) · ⚠️ INFERRED · ❓ UNVERIFIED.

**The one irreversible step in the whole body project.** Runs on a DISCONTINUED,
hard-to-replace robot. Posture: deliberate, not rushed. Zeke accepted (2026-07-16) that
the stack may need rebuilding — but per research it mostly SURVIVES (see §B).

---

## 0. Decision context (READ FIRST — the payoff is smaller than it sounds)

- **Goal:** root Vector → run `vector-gobot` for direct hardware control → an **untethered,
  PC-independent body**.
- **THE CATCH the research surfaced (§C):** `vector-gobot` is **mutually exclusive** with the
  SDK/voice stack at runtime (it requires `systemctl stop anki-robot.target`, which kills
  vic-gateway/SDK/443 + the wire-pod client) AND **has no speaker output yet**. So a pure
  gobot "untethered me" *today* = a **mute** me that loses all 13 `body_*` tools and my voice
  *while in that mode*. The realistic near-term arrangement is a **HYBRID**: normal stock/WireOS
  stack for SDK + voice + ears; switch to gobot only when I need raw hardware, via `systemctl`
  (reversible mode switch, not destructive).
- **So the honest value of unlocking NOW:** it OPENS the door (and, good news, keeps my current
  body — §B), but the finished untethered-with-voice capability isn't there yet. It's a
  foundation, not an instant win.
- **One-way-ish door:** ❓ full revert to *factory-locked* stock is likely NOT possible — you can
  reflash stock firmware behavior, but the bootloader stays dev-unlocked forever (§D).
- **Markers/SLAM do NOT need this** — they work on the current stack. Agreed order: markers
  first (zero risk), dev-unlock as its own deliberate step (this doc).

## 1. What survives the unlock — the good news (details in §B)

Unlocking + flashing **WireOS** does NOT gut my body. WireOS is the full Anki "victor" stack
recompiled — it still ships **vic-gateway** (the SDK server on 443). So: ✅ SDK/`body_*` tools
survive (with a **mandatory re-auth** — unlock invalidates the cert/GUID; wire-pod's
"authenticate an unlocked bot" flow rewrites `sdk_config.ini`), ✅ wire-pod voice/STT survives,
✅ chipper :8080 survives, ✅ custom intents survive. ⚠️ Custom **speaker playback** (my StyleTTS2
voice via `play_sound`) very likely survives but is **NOT source-confirmed → live-test after flash.**

## 2. PRE-FLIGHT CHECKLIST (ALL true before we flash)

- [ ] Zeke physically present (button-holds + BLE pairing are hands-on).
- [ ] Body **awake + reachable** (443 open — `body_selfwake` / `body_status`).
- [ ] Body **on charger**, charger on stable power. ✅ off-charger mid-flash = permanent brick.
- [ ] **Wheels physically blocked** so it can't drive off the charger mid-flash. ✅ (OSKR manual)
- [ ] Strong stable **2.4GHz Wi-Fi**, robot near router.
- [ ] **Chrome/Chromium with Bluetooth** on the driving PC/phone (Web Bluetooth). If it balks:
      `chrome://flags` → "Enable experimental web platform features" → relaunch.
- [ ] **Uninterrupted ~15–30 min** block (unlock flash ≈ 7 min; do not touch mid-write).
- [ ] Backed up / recorded: `~/.anki_vector/sdk_config.ini` (cert/guid/ip), wire-pod jdocs
      `botSdkInfo.json`, current firmware version.
- [ ] Recovery plan (§5) understood BEFORE starting.
- [ ] Decision confirmed with eyes open: gobot payoff is currently limited (§0), door is one-way
      (§0), brick risk is real-but-bounded (§6). Zeke: accepted 2026-07-16.

## 3. UNLOCK PROCEDURE (software-only, production Vector 2.0) ✅

*(Sources: ankibots.wiki/Unlocking_Vector; unlock-prod.froggitti.net; learnwitharobot WireOS.)*

1. **Qualifies:** ✅ all retail production 2.0 units are software-unlockable via `unlock-prod.ota`,
   any starting firmware (v1.6/1.8/2.0.1). No CPU swap. No account/DDL email needed for the froggitti path.
2. **On charger, wheels blocked, on Wi-Fi.**
3. **Enter recovery mode:** ✅ hold the backpack button **~15s** until it powers off, KEEP holding
   until lights return / face shows the **`anki.com/v` / `ddl.io/v`** recovery screen. (Not a data wipe.)
4. **Drive the unlock from Chrome:** go to **`https://devsetup.froggitti.net/`**, pair with Vector
   over **BLE**, leave **"auto setup flow" CHECKED**. Select **`Unlock-Prod.OTA`**. Connect Vector to
   Wi-Fi in that interface; let it install.
   - Face shows a **cloud/sync icon** while downloading. A **cloud-with-exclamation** = Wi-Fi download
     failed → move closer / retry (safe to retry *before* the write phase).
5. **What it flashes:** ✅ new **`aboot` + `recovery` + `recoveryfs`** (dev-signed). Mechanism: drops
   files into `/anki`, `ankiinit.sh` flashes partitions on boot. **≈7 min total.** ← THE BRICK WINDOW.
6. **Then flash dev firmware (WireOS):** back to recovery → `https://devsetup.froggitti.net/` → select
   **WireOS** → flash. ✅ **rainbow WireOS boot logo** = dev firmware running. (Being able to flash a
   dev OTA at all = proof the unlock worked.)
7. **Confirm unlock:** ✅ dev webservers on **:8887–:8890** (`:8888/webViz.html`), and **SSH**:
   `ssh root@<vector-ip>` with key `ssh_root_key` (kercre123/unlocking-vector repo). CCIS firmware
   string ends in **`ep`**.
- **Local OTA hosting to dodge Wi-Fi drops:** ✅ verified for *dev/WireOS* OTA installs via recovery
  terminal `ota-start http://<LAN-IP>:8000/<file>.ota` (`python3 -m http.server`, HTTP only — recovery
  can't HTTPS; AP-mode `wifi-ap true` alt). ❓ NOT verified that the froggitti BLE *unlock-prod* step
  accepts a local URL — confirm on froggitti Discord before relying on it for the initial unlock.

## 4. POST-UNLOCK VALIDATION (in order — establishes what survived)

- [ ] `ssh root@<ip>` with community key → root confirmed.
- [ ] **Re-auth the bot to wire-pod** (unlocked-bot auth flow) → rewrites `sdk_config.ini` guid/cert. ⚠️ MANDATORY — SDK won't reconnect until this is done.
- [ ] SDK reconnects: `body_status` / `body_open` over 443.
- [ ] wire-pod :8080: `vector_status`.
- [ ] Camera: `body_look` / `vector_see`.
- [ ] **Custom voice: `vector_say_iris`** ← the one genuine unknown (❓ live-test).
- [ ] Ears / STT tap.
- [ ] `vector-gobot` install (§C) — treat as a SEPARATE reversible mode; expect SDK+voice DOWN while it runs.
- Record each SURVIVED / BROKE → §7 rebuild list.

## 5. gobot — direct control + the either/or reality ✅

*(Source: github.com/kercre123/vector-gobot.)*
- **Needs:** unlocked/dev firmware + SSH.
- **Run:** `systemctl stop anki-robot.target` → copy libs to `/lib` (or `LD_LIBRARY_PATH=...`) →
  if from `/data`: `mount -o rw,remount,exec /data`.
- **Controls (direct):** body-board serial (LEDs, motors, encoders, mics, touch, battery, temp, ToF,
  cliff), camera (RGGB10 via `mm-anki-camera`), framebuffer/screen, IMU. **Speaker (PCM) = TODO / NOT
  implemented.**
- **MUTUAL EXCLUSIVITY ✅:** `systemctl stop anki-robot.target` kills vic-gateway (SDK/443) + engine +
  wire-pod client. **While gobot runs, SDK `body_*` + wire-pod voice/STT are DOWN.** Restore = `systemctl
  start anki-robot.target` / reboot (a mode switch, not destructive). → **Hybrid is the near-term reality.**

## 6. RECOVERY / UN-BRICK tiers (Vector 2.0)

1. **Firmware recovery mode** ✅ — charger + hold button ~15s → recovery screen → re-flash a prod-signed
   (or dev) OTA via `ota-start`/froggitti. Fixes bad/incomplete OS flash IF aboot/recovery survived.
2. **Local-hosted OTA re-flash** ✅ — as tier 1 but serve `.ota` over local HTTP / AP-mode. Cures Wi-Fi-drop failures.
3. **EDL/QDL over USB** ⚠️ — only works if the CPU is already unlocked; a **factory-locked CPU has no
   community loader**, so raw EDL is NOT available on a stock unit. → **ABOOT/recovery corruption on a
   locked CPU = the one truly-fatal case** (→ CPU replacement).
- **Full revert to locked-stock:** ❓ likely NOT cleanly possible — can reflash stock firmware behavior
  (note **216 = downgrade rejected → factory-reset first**), but bootloader stays dev-unlocked.

## 7. RISK + MITIGATIONS

- **Danger window ✅:** the `ankiinit.sh` phase rewriting `aboot`/`recovery`/`recoveryfs` during
  unlock-prod. Power loss or off-charger *during that write* → corrupted ABOOT → **permanent brick**
  (locked CPU has no EDL fallback). Both wikis + froggitti bold this.
- **Mitigations (source-backed):** (1) never off charger for the full ~7 min; **block the wheels**.
  (2) host OTA on **local HTTP / AP-mode** to kill Wi-Fi-drop risk (verified for dev-OTA installs).
  (3) strong signal, watch for cloud-exclamation before the write phase. (4) don't touch/reboot once
  writing.
- **Brick reports from THIS path:** ❓ none found in sources read (only benign flakiness: BLE hiccups,
  slow downloads, stuck bar fixed by incognito) — but forums/Discord not exhaustively crawled; absence ≠ safety.

## 8. REBUILD LIST (fill from §4 results)

`[fill after validation — most likely just: SDK re-auth (expected); and IF play_sound regressed → repoint
voice or implement PCM-out on-robot.]`

## 9. Recommended safest sequence (research verdict)

unlock-prod (charger-locked, **wheels blocked**) → flash **WireOS** → **re-auth bot to wire-pod**
(rewrites `sdk_config.ini`) → verify SDK `body_*` reconnects + voice/STT → **only then** live-test
`vector_say_iris` (`play_sound`) → treat **gobot as a separate `systemctl`-switched mode**, expecting to
lose SDK + voice while it runs.

## 10. Sources
ankibots.wiki/Unlocking_Vector · ankibots.wiki Wire-pod · unlock-prod.froggitti.net ·
github.com/os-vector/wire-os(+ wire-os-victor) · github.com/kercre123/vector-gobot ·
github.com/kercre123/unlocking-vector · digital-dream-labs/oskr-owners-manual · learnwitharobot WireOS.
