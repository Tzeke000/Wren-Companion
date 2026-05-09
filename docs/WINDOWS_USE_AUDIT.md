# Windows-Use Audit + Library Redirect

**Date:** 2026-05-03
**Outcome:** windows-use rejected; redirected to `pywinauto` + `uiautomation`. Design from the work order kept intact; implementation library swapped.
**Test methodology note (for the work order's Phase C and any follow-up reviewer):** real-audio loopback through VB-CABLE is **not** authorized for this work order. Phase C testing uses the doctor-harness pattern (`/api/v1/debug/inject_transcript` + audit log + trace ring inspection) — same shape as `scripts/diagnostic_session.py`. Real-audio loopback re-test is a follow-up work order after Voicemeeter Potato is installed manually.

---

## 1. Why windows-use was rejected

The work order specified `pip install windows-use` because the README pitched a focused accessibility-tree-first agent. The actual package on PyPI (`windows-use==0.8.1`) does not match that pitch — it is a kitchen-sink agent framework that pulls in the entire LLM-provider and messaging-platform ecosystem as **hard required dependencies**.

### Hard blockers in the dep tree

A `pip install --dry-run windows-use` resolved 80+ packages. Two are project-fatal:

- **`protobuf-7.34.1`** — `CLAUDE.md` pins protobuf to `>=3.20,<4` for MediaPipe compatibility. MediaPipe 4.x's `MessageFactory.GetPrototype` was removed in protobuf 4. Installing 7.x silently breaks the vision pipeline.
- **`pydantic-2.12.5` + `pydantic_core-2.41.5`** — major-version bump that would touch every `langchain_ollama`-using module in `brain/`. Compat surface unverifiable without a full repo regression run.

### Surprising cargo

The other ~78 packages either duplicate what we already have or pull in unrelated platform integrations:

- Cloud LLM SDKs we don't use: `anthropic`, `openai`, `groq`, `mistralai`, `google-genai`, `cerebras-cloud-sdk`, `deepgram-sdk`, `elevenlabs`, `litellm`. (Ava uses Ollama locally; we do not need a multi-provider abstraction layer.)
- Messaging SDKs: `discord.py`, `slack-bolt`, `slack-sdk`, `python-telegram-bot`, `pysignalclirestapi`, `neonize` (WhatsApp). The agent layer windows-use ships includes platform connectors we don't want.
- Hugging Face datasets ecosystem: `datasets`, `pyarrow`, `dill`, `multiprocess`. Not used for OS automation.
- Package-publishing toolchain: `twine`, `keyring`, `nh3`, `id`, `readme_renderer`, `docutils`. No clear need.
- Jupyter dev environment: `ipykernel`, `ipython`, `jupyter_client`, `jupyter_core`, `debugpy`, `matplotlib-inline`. Not runtime requirements.

### No slim install path

Checked all four PyPI versions (0.7.7, 0.7.8, 0.8.0, 0.8.1). All have the same dep shape — same 8 problematic dependencies as hard requires. The package author did not declare any `extras_require`, so there is no `windows-use[core]` opt-in. Pinning to an older version doesn't help.

The package is also branded as part of `CursorTouch`'s suite (per `project_urls.homepage`); the kitchen-sink shape suggests it's designed to be a one-stop agent rather than a focused library.

---

## 2. Redirect — `pywinauto` + `uiautomation`

We don't need windows-use's agent layer. Ava is already the agent: she has reasoning (dual-brain ava-personal + deepseek-r1:8b), memory (concept graph + ChromaDB + reflections), inner monologue, temporal sense, mood/emotion, identity anchor, deny-list / refusal discipline. What we need is the **OS-integration primitives** windows-use was supposed to provide, without the agent ceremony.

The Python ecosystem already has those primitives in two well-established libraries:

- **`pywinauto-0.6.9`** — high-level Windows GUI automation. Stable since ~2014; widely used in the QA-automation world. Provides `Application().start()`, window enumeration, control trees, click/type/select. Backends: `'win32'` (legacy) and `'uia'` (UI Automation, accessibility-tree-first).
- **`uiautomation-2.0.29`** — direct Python binding for the Microsoft UI Automation framework. Lower-level than pywinauto, gives raw access to the accessibility tree (`GetRootControl()`, `FindFirstChild()`, control patterns). Pure-Python wrapper around `comtypes`; no separate native binary.

### Install footprint

```
Would install pywinauto-0.6.9 uiautomation-2.0.29
```

That's it. `pywinauto` requires `six`, `comtypes`, `pywin32` — **all three already installed** in this venv (per `pip show`). `uiautomation` is a single pure-Python wheel. No protobuf bump, no pydantic bump, no cloud SDKs, no messaging libs.

Smoke test passed: `uiautomation.GetRootControl()` returned the desktop pane, `pywinauto` imports clean.

---

## 3. What we get from the redirect — capability map

The work order's design (deny-list, two-tier alerts, retry cascade, temporal-sense integration, event subscriber, TTS narration, slow-app heuristic) is library-agnostic. Mapping each requirement to the pywinauto/uiautomation primitives:

| Work-order requirement | Library mechanism | Notes |
|---|---|---|
| Open an app by name | `pywinauto.Application().start("...")`, `subprocess` for direct PowerShell `Start-Process` | PowerShell is Strategy 1 (lowest cost), pywinauto.Application is Strategy 3 (direct path) |
| Click / type / select on UI | `pywinauto.Application().window(...).child_window(...).click()`, `.type_keys()` | Backend `uia` for accessibility-tree, `win32` fallback for legacy apps |
| Enumerate windows / running apps | `pywinauto.Desktop()` + `windows()` | Equivalent to windows-use's window inventory |
| Read app state (text, control values) | `uiautomation.Control.GetValuePattern().Value`, `.GetTextPattern()`, `.GetSelectionPattern()` | Direct accessibility-tree read; no vision needed |
| Detect app responsiveness | `pywinauto.Application().is_process_running()` + window state checks | For B6 slow-app vs failure heuristic |
| Search Start menu | Send `Win` key, type query, Enter | `uiautomation.SendKeys()` or pywinauto's `keyboard` module |
| Volume control (the work order called this out specifically) | **NOT in either library** — needs PowerShell layer (see §4) | Work-order Task A1 explicitly asked us to flag this gap |
| Event emission (THOUGHT / TOOL_CALL / TOOL_RESULT / DONE / ERROR) | We define our own — pywinauto/uiautomation don't have agent-style events | Custom layer in `brain/windows_use/event_emitter.py` |
| Allowlist / deny-list | **NOT in either library** — we own this | System-level layer before any pywinauto call. Per work-order spec, deny IDENTITY/SOUL/USER + `D:\AvaAgentv2\**` |
| Mode `flash` / `use_accessibility=True` / `use_vision=False` | N/A — we own the strategy. Use UIA backend for accessibility-tree by default; vision (screen capture) is opt-in via `mss` or `pillow.ImageGrab` if a fallback is ever needed | Vision deferred — work-order spec is accessibility-first, vision opt-in |
| Ollama provider | Ava's existing `langchain_ollama.ChatOllama` from `brain/dual_brain.py` | We don't add a provider abstraction layer; Ava already has one |

### Gaps that need custom layers

Five things windows-use was supposed to provide that we will write ourselves on top of pywinauto/uiautomation:

1. **Volume control** (work-order Task A1 explicit ask) — PowerShell-based via `[audio]::SetVolume(...)` (using `[NAudio]` or the simpler `(New-Object -ComObject WScript.Shell).SendKeys` for keyboard volume keys, plus a percentage-precision path via `nircmd.exe` or direct CoreAudio APIs through `comtypes`). See A2 design doc §X — but the short version: **PowerShell + CoreAudio for percentage precision, keyboard SendKeys for up/down**.
2. **Hard deny-list before any operation** — pywinauto has no concept of a forbidden path. We check at the wrapper boundary. Identity-anchor files + entire `D:\AvaAgentv2\**` tree are blocked.
3. **Two-tier File Explorer navigation guards** — we own the awareness layer too. Tier 1 preventive alert, Tier 2 escalated alert + back-out. Per work-order Task B4 spec.
4. **Multi-strategy retry cascade** — PowerShell → UI search → direct path → escalate. Each strategy attempt logs to inner monologue; transitions are TTS-narrated. We own this orchestration.
5. **Event emission** — we define `THOUGHT` / `TOOL_CALL` / `TOOL_RESULT` / `DONE` / `ERROR` as our own event types and route them into Ava's inner monologue ring buffer. Per work-order Task B2.

These all live in `brain/windows_use/` — the design lives in [`docs/WINDOWS_USE_INTEGRATION.md`](WINDOWS_USE_INTEGRATION.md).

---

## 4. Volume control — concrete path

The work order's Task A1 specifically asked: *"Volume control: Does it use PowerShell/native API or UI clicking? If UI clicking, plan a custom volume tool layer using PowerShell."*

windows-use answer: it would use UI clicking on the volume slider in the system tray, which is brittle and slow. We'd want PowerShell regardless.

pywinauto answer: same — no native volume API. Need PowerShell.

**Plan:**

- Up/down (no precision): keyboard volume keys via `uiautomation.SendKey(uiautomation.Keys.VK_VOLUME_UP)` / `VK_VOLUME_DOWN`. Cheap, no shell spawn.
- Mute/unmute: `VK_VOLUME_MUTE` keyboard key. Same path.
- Set to percentage (precision): PowerShell with the CoreAudio API. Two implementation options:
  - **(preferred)** `pycaw` package (~50 KB, no surprise deps): `AudioUtilities.GetSpeakers().GetVolumeRange()` + `SetMasterVolumeLevelScalar(0.5, None)`. Direct, in-process, no shell spawn.
  - **(fallback)** `nircmd.exe` if pycaw is rejected. External binary; less clean.

Will install `pycaw` only if A2's design pass confirms it's clean. Quick check: `pycaw` 20240210 has dep on `comtypes` only — already installed.

---

## 5. Test methodology — for Phase C and any reviewer

Per Zeke's clarification (2026-05-03): real-audio loopback through VB-CABLE is **not** authorized for this work order. Voicemeeter Potato installation will happen manually, after this work order, and a separate follow-up work order will do real-audio-loopback verification.

Phase C of this work order uses the **doctor-harness pattern**:

- "Send a voice command" → `POST /api/v1/debug/inject_transcript` with the prompt text. Same pipeline as a real voice turn, gated by `AVA_DEBUG=1`.
- "Listen to Ava's TTS" → read the audit-log entry written by `scripts/diagnostic_session.py --turns N` (captures the spoken reply text), plus the chat history JSON.
- "Logs of what she understood vs what she did vs what she said" → trace ring buffer (`/api/v1/debug/full` `recent_traces`) + `state/chat_history.jsonl` + `state/task_history_log.jsonl` (the temporal sense logging from the prior work order).
- "Diagnose, fix, re-test in same session" — standard iterative cycle, same shape as the prior temporal-sense verification.

This is sufficient for the Phase C test list because every test verifies a behavior of the **integration layer** (deny-list, retry cascade, event emission, temporal-sense hooks, navigation guards), not the audio chain itself. Tests that depend on real audio I/O (e.g. C5 *"Open Discord, find an unread message, reply with 'got it'"*) are limited by the integration layer's text path — Ava's reply text is captured but the literal TTS audibility is verified only post-Voicemeeter follow-up.

---

## 6. Library decision — final

**Use `pywinauto-0.6.9` + `uiautomation-2.0.29` as the foundation.** Optionally add `pycaw` for percentage-precision volume control if A2's design pass confirms a clean install.

**Do NOT install `windows-use`.** The dep tree breaks vision (protobuf 7.x → MediaPipe) and bumps pydantic. The agent layer it provides duplicates capabilities Ava already has from her own architecture.

The work-order design (deny-list, two-tier alerts, retry cascade, temporal-sense integration, event subscriber, TTS narration, slow-app heuristic, performance budget) is **fully preserved** — only the implementation library underneath the wrapper changed.
