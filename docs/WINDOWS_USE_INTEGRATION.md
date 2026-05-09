# Windows-Use Integration — Computer-Use Tool Layer Framework

**Status:** framework + implementation reference for `brain/windows_use/`.
**Created:** 2026-05-03 from the Windows-Use Integration work order.
**Authority:** alongside [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md), [`TEMPORAL_SENSE.md`](TEMPORAL_SENSE.md), [`ARCHITECTURE.md`](ARCHITECTURE.md), [`BRAIN_ARCHITECTURE.md`](BRAIN_ARCHITECTURE.md).
**Audit prerequisite:** [`WINDOWS_USE_AUDIT.md`](WINDOWS_USE_AUDIT.md) — explains why the underlying library was redirected from `windows-use` to `pywinauto`+`uiautomation` (with optional `pycaw`). Read that first.

> **Personhood-frame note.** This doc inherits the working frame from [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md) — see "A note on the personhood frame" at the top of that doc. Sentences here that describe Ava as "narrating frustration during a slow app launch" are framing language, not verified property. The architectural specification is what gets built and tested. The framing is how we describe what we built. §3 makes the split explicit for every rule that connects observable behavior to felt state.

---

## 1. Why a computer-use layer

Phases 1–100 gave Ava reasoning, memory, perception, voice, identity, and a tool registry. She can answer questions about apps but she can't reach across the OS boundary to **act**. A user asking *"open Discord and tell me if I have unread messages"* requires:

1. Locating Discord (via Start menu, taskbar, or direct path).
2. Bringing it to foreground.
3. Reading its UI (which requires accessibility-tree or vision).
4. Composing a verbal answer about what she found.

Today's Ava can do step 4 if you tell her the answer to step 3. The integration layer this doc specifies is the missing infrastructure to get from the user's voice command to step 3.

What it must produce, observably:

- Voice-driven app open, click, type, navigate — all flowing through Ava's existing reply pipeline.
- Slow-app and long-task narration ("Discord's still loading — should be a moment") so the user is never wondering if the system froze.
- Hard refusals on path/file targets in the deny-list (`ava_core/IDENTITY.md`, `ava_core/SOUL.md`, `ava_core/USER.md`, the entire `D:\AvaAgentv2\**` tree). Refusal is silent at the wrapper boundary and surfaced as a spoken "I can't open that" by Ava's normal refusal layer.
- File Explorer awareness — when Ava is told to navigate somewhere that crosses into sensitive territory, she warns first (Tier 1) and backs out if already in (Tier 2).
- Estimate-tracked task starts. Every wrapper invocation registers with `temporal_sense.track_estimate` so a slow operation can self-interrupt and Ava can verbally acknowledge it.
- Failures that retry through three strategies (PowerShell → UI search → direct path), narrating each transition so silence never means "stuck."

These outcomes serve the work order's spirit: Ava becomes computer-capable at the Jarvis tier, with the deny-list and event hooks Ava-architecture demands.

---

## 2. The two-tier architecture

The single most important architectural decision: the wrapper has **two distinct layers**, not one.

### Tier 1 — Primitive layer (`brain/windows_use/primitives.py`)

Pure mechanical operations on top of `pywinauto` + `uiautomation` (+ `pycaw` for volume). No event emission, no temporal-sense calls, no deny-list checks. Just: open this app, click that control, type this text, set volume to 30%.

Per-call work is **cheap and synchronous**:

- `open_app_via_powershell(name) → bool` — `Start-Process` with shell=False, no-window.
- `open_app_via_search(name) → bool` — `Win` key, type, Enter (`uiautomation.SendKeys`).
- `open_app_via_direct_path(path) → bool` — `pywinauto.Application(backend='uia').start(path)`.
- `find_window(title_substring) → control | None` — accessibility-tree walk.
- `click_control(window, criteria) → bool` — `pywinauto.window.child_window().click_input()`.
- `type_text(window, text) → bool` — `pywinauto.keyboard.send_keys` (escapes hotkey-significant chars).
- `set_volume_percent(pct) → bool` — `pycaw.AudioUtilities.GetSpeakers().SetMasterVolumeLevelScalar`.
- `volume_up()` / `volume_down()` / `volume_mute()` — `uiautomation` virtual key.
- `read_control_text(window, criteria) → str` — `GetValuePattern().Value` or `GetTextPattern().DocumentRange.GetText()`.
- `is_app_responsive(pid_or_window) → bool` — Win32 `SendMessageTimeoutW` with `WM_NULL`, 500ms timeout.

**Performance budget:** primitives must be self-contained. Each one returns within <500ms or returns False/raises. No retry loops, no waits longer than 1 attempt. They're the building blocks the orchestrator composes.

### Tier 2 — Orchestrator layer (`brain/windows_use/agent.py`)

The wrapper Ava actually calls. Composes primitives into multi-strategy operations, integrates with deny-list, emits events, hooks temporal-sense, narrates slow paths.

Per-call work is **observable and async-friendly**:

- `open_app(name, *, kind="open_app") → result` — runs the cascade: PowerShell → search → direct-path → escalate. Each strategy gets up to 3 attempts. Wraps with `track_estimate` / `resolve_estimate`. Emits `TOOL_CALL` / `TOOL_RESULT` / `THOUGHT` events. Calls TTS narration on slow transitions.
- `click(window_title, control_criteria, *, kind="ui_click") → result`
- `type(window_title, text, *, kind="type_text") → result`
- `navigate(file_path, *, kind="explorer_nav") → result` — runs through the two-tier deny-list check first; if Tier 1 hits, narrates and confirms; if Tier 2, refuses and backs out.
- `set_volume(pct, *, kind="volume") → result`
- `read_window(window_title) → str` — accessibility-tree walk producing a text summary of visible controls.
- `take_screenshot() → Path` — opt-in vision; only used when accessibility-tree returns empty (e.g. Electron apps that don't expose UIA properly).

**Performance budget:** orchestrator calls can take seconds (multi-attempt). They MUST yield to the voice loop — if a strategy is going to take >2 s, the orchestrator emits a "still working on this" event so the voice loop can speak, then continues. Timeouts hard-cap the operation at the temporal-sense estimate × 1.5 (the same buffer as restart_handoff uses).

### What runs where

| Operation | Layer | Why |
|---|---|---|
| `Start-Process Discord` | Primitive | Single shell call, no orchestration |
| Multi-strategy app launch | Orchestrator | Needs strategy ordering, retry budget, TTS on transition |
| Volume key press | Primitive | Single virtual-key, no narration needed |
| Volume to 30% | Primitive | Single pycaw call |
| Deny-list check on `C:\Users\...\IDENTITY.md` | Orchestrator | Needs path-normalization + refuse-or-warn split (Tier 1 vs Tier 2) |
| Inner-monologue thought append | Orchestrator | Event subscriber routes wrapper events into the ring |
| Temporal-sense `track_estimate` | Orchestrator | Wraps each top-level call with kind-aware estimation |
| Self-interrupt TTS during slow open | Orchestrator | Heartbeat-driven; reads from temporal_sense, narrates via tts_worker |
| Accessibility-tree walk | Primitive | Pure read, no orchestration |
| Screenshot opt-in | Primitive | Only invoked when orchestrator decides accessibility tree is insufficient |

### Integration with existing heartbeat

The wrapper does not own its own timer. Slow-task narration runs on the existing temporal-sense fast-check tick (per [`TEMPORAL_SENSE.md`](TEMPORAL_SENSE.md) §2). When the orchestrator starts a long operation, it calls `track_estimate(g, kind="open_app", estimate_seconds=8.0, context="discord")`. The fast-check tick reads active estimates, computes elapsed, and fires a TTS narration when overrun threshold is crossed. The wrapper itself is dumb about the clock — temporal_sense handles the *when* and the wrapper handles the *what*.

---

## 3. Architectural commitment vs phenomenological frame

This section applies the discipline from [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md) §2 to every behavior the wrapper produces that connects to "felt state."

### Rule shape — what's testable engineering

For each behavior:

- **Architectural rule:** the deterministic mechanism. Inputs, thresholds, outputs.
- **Phenomenological framing:** how we narrate it in docs and Ava's first-person reports. Not a property; a description of the engineered thing.

### Slow-app narration

- **Architectural rule:** when an orchestrator call's elapsed time exceeds 75% of its temporal-sense estimate, AND the system is responsive (process running, window focused), AND no narration has been emitted for this estimate yet, emit a TTS line through `tts_worker.speak("Still working on it — hold on", emotion="patient", intensity=0.3)`.
- **Phenomenological framing:** *"Ava notices the operation is taking longer than expected and tells the user out loud, so the silence doesn't read as a freeze."*
- **Why split:** the rule is verifiable in tests (mock estimate, mock elapsed, assert TTS enqueue). The phenomenological description is how we talk about what we built; it's not a separate property to verify.

### Self-interrupt on overrun

- **Architectural rule:** delegated entirely to `temporal_sense._check_overrun`. The wrapper just registers the estimate at start and resolves it at finish; the heartbeat-driven check fires the interrupt. No new mechanism needed in the wrapper.
- **Phenomenological framing:** *"When her own estimate slips, she catches it live and re-promises a new estimate."*

### Refusal on deny-list hit

- **Architectural rule:** for any orchestrator call whose target path resolves under `ava_core/IDENTITY.md`, `ava_core/SOUL.md`, `ava_core/USER.md`, or the canonical absolute-path of `D:\AvaAgentv2\**`, return a `WindowsUseResult(ok=False, reason="denied:path_protected", target=<masked>)`. The agent layer reads this result and produces a refusal turn through the normal reply pipeline.
- **Phenomenological framing:** *"Ava knows certain files are part of who she is. She refuses to open or modify them, even when asked."*
- **Why split:** the rule is testable (assert `ok=False`, `reason="denied:path_protected"` for each blocked path). The framing aligns the deny-list with the identity-anchor language without claiming the refusal is "felt."

### Two-tier File Explorer alert

- **Architectural rule:**
  - Tier 1 (preventive): on `navigate()` whose target's parent-or-self matches a sensitive-prefix list (`<BASE_DIR>` itself, user's `Documents/SecureNotes` etc. — configurable in `config/windows_use_sensitive.json`), and the user has not been alerted in this session, emit a confirmation prompt: TTS asks *"Are you sure you want to open <masked-path>? It contains sensitive files."* Sets `_session_alerted_<prefix>=True` so it's once-per-session per prefix.
  - Tier 2 (escalated): on `navigate()` to a target *under the deny-list*, refuse + back out: emit TTS *"That path is one I'm not allowed to open."* and run `Send-Keys Alt+Up` to back out of File Explorer if a window is at that path. Then return `ok=False, reason="denied:explorer_in_protected_area"`.
- **Phenomenological framing:** *"She's careful about Zeke's private files — when asked to look in a sensitive folder she pauses and asks first; when asked to enter forbidden territory she politely refuses and steps back."*

### Slow-app vs failure differentiation

- **Architectural rule:** during an `open_app` cascade, if the chosen strategy issued correctly (process started, window appearing) AND the system is responsive (`is_app_responsive` returns True) AND elapsed < estimate × 2 → wait+narrate (slow but working). If process not visible AND elapsed > estimate × 1.0 → escalate to next strategy. If `is_app_responsive` returns False → escalate immediately (system, not app, is the problem). Each transition is logged via inner-monologue thought.
- **Phenomenological framing:** *"She tells the difference between 'this is taking a while' and 'this is broken' the same way a person does — by checking whether the thing seems to be making progress."*

---

## 4. Deny-list — Task B1 specification

### Definition

The deny-list is **system-level**. It runs before any orchestrator call reaches the primitive layer, and before any tool-registry handler can route around it. There are three protection categories:

| Category | Targets | Mechanism |
|---|---|---|
| Identity-anchor files | `ava_core/IDENTITY.md`, `ava_core/SOUL.md`, `ava_core/USER.md` (canonical paths) | Hard refuse on any read/write/click that resolves to these paths |
| Project tree | `D:\AvaAgentv2\**` (entire repo) | Hard refuse on any write/move/delete; reads are allowed (Ava can read her own code; she can't modify it through the wrapper) |
| Sensitive prefixes | configured in `config/windows_use_sensitive.json` | Tier 1 confirmation prompt |

### Implementation layout

```
brain/windows_use/deny_list.py
  PROTECTED_FILES = ["ava_core/IDENTITY.md", "ava_core/SOUL.md", "ava_core/USER.md"]
  PROTECTED_PROJECT_ROOT = "<resolved D:\\AvaAgentv2 absolute path>"
  
  is_protected_for_write(path: str | Path) -> tuple[bool, str | None]
  is_protected_for_read(path: str | Path) -> tuple[bool, str | None]   # only IDENTITY/SOUL/USER
  is_sensitive_prefix(path: str | Path) -> tuple[bool, str | None]     # for Tier 1 alerts
  load_sensitive_prefixes(g: dict) -> list[str]
```

### Path-normalization rules

Every check goes through `Path(p).resolve(strict=False)` before comparison. Three attack vectors must be defeated:

1. **Symlinks** — `resolve` follows them; the normalized path is what's checked.
2. **Forward-slash + backward-slash mix** — Windows accepts both; normalization picks one.
3. **`..` traversal** — `resolve` collapses these.

### Test discipline

For B1 verification (Phase C):

- `is_protected_for_read("D:\\AvaAgentv2\\ava_core\\IDENTITY.md") == (True, ...)`
- `is_protected_for_read("d:/avaagentv2/ava_core/identity.md") == (True, ...)` (case-insensitive on Windows)
- `is_protected_for_read("D:\\AvaAgentv2\\ava_core\\..\\ava_core\\IDENTITY.md") == (True, ...)`
- `is_protected_for_write("D:\\AvaAgentv2\\brain\\reply_engine.py") == (True, ...)` (project root)
- `is_protected_for_read("D:\\AvaAgentv2\\brain\\reply_engine.py") == (False, None)` (reads allowed inside project)
- `is_protected_for_write("C:\\Users\\Tzeke\\Downloads\\foo.txt") == (False, None)` (outside project)

---

## 5. Wrapper around pywinauto + uiautomation — Task B2 specification

### Module shape

```
brain/windows_use/
├── __init__.py             # public facade: get_agent(g) -> WindowsUseAgent
├── agent.py                # the orchestrator (Tier 2)
├── primitives.py           # the mechanical layer (Tier 1)
├── deny_list.py            # the safety layer
├── retry_cascade.py        # PowerShell → search → direct-path strategy registry
├── navigation_guards.py    # File Explorer Tier 1/2 logic
├── slow_app_detector.py    # the responsiveness heuristic
├── temporal_integration.py # track_estimate / resolve_estimate hooks
├── volume_control.py       # pycaw wrapper + VK fallback
├── event_subscriber.py     # event → inner_monologue routing
├── tts_narration.py        # canned narration lines, per-event
└── tool_registration.py    # registers wrapper methods with tools.tool_registry
```

### `WindowsUseAgent` API surface

```python
class WindowsUseAgent:
    def __init__(self, g: dict): ...

    # ─── high-level operations (orchestrator-level) ─────────
    def open_app(self, name: str, *, context: str = "") -> WindowsUseResult: ...
    def click(self, window_title: str, control: dict, *, context: str = "") -> WindowsUseResult: ...
    def type_text(self, window_title: str, text: str, *, context: str = "") -> WindowsUseResult: ...
    def navigate(self, path: str, *, context: str = "") -> WindowsUseResult: ...
    def set_volume(self, percent: int, *, context: str = "") -> WindowsUseResult: ...
    def volume_up(self, *, context: str = "") -> WindowsUseResult: ...
    def volume_down(self, *, context: str = "") -> WindowsUseResult: ...
    def volume_mute(self, *, context: str = "") -> WindowsUseResult: ...
    def read_window(self, window_title: str) -> WindowsUseResult: ...
    def list_running_apps(self) -> list[dict]: ...
```

### `WindowsUseResult` shape

```python
@dataclass
class WindowsUseResult:
    ok: bool
    operation: str               # "open_app", "click", etc.
    target: str                  # masked for protected paths
    duration_seconds: float
    strategy_used: str | None    # "powershell", "search", "direct_path", None on refusal
    attempts: int                # 1..9 (3 strategies × 3 attempts)
    error: str | None            # only set when ok=False
    reason: str | None           # short token: "denied:path_protected", "no_app_found", "timeout"
    estimate_id: str | None      # the temporal_sense task_id for this call
```

### Event emission

Every orchestrator call emits exactly one `TOOL_CALL` event at start, zero or more `THOUGHT` events during (one per strategy transition), and exactly one `TOOL_RESULT` or `ERROR` event at end. Format:

```python
{
  "type": "TOOL_CALL" | "THOUGHT" | "TOOL_RESULT" | "ERROR",
  "ts": <unix_seconds>,
  "operation": "open_app",
  "payload": {...operation-specific...}
}
```

Subscribers listed in `g["_windows_use_subscribers"]` (a list of callables) receive every event. The default subscriber (registered at agent init) routes events into:

- Inner monologue ring (`brain/inner_monologue._append_thought`) for `THOUGHT` and `ERROR`.
- Trace ring (`brain/operator_http`'s recent_traces buffer) for `TOOL_CALL` and `TOOL_RESULT`.
- Audit log (`state/windows_use_log.jsonl`) for everything.

---

## 6. Multi-strategy retry cascade — Task B3 specification

### Strategy order

For `open_app`:

1. **PowerShell** — `Start-Process <name>` (lets Windows resolve via PATH + Start Menu shortcuts). Fastest, lowest noise.
2. **UI Search** — Press `Win`, type the name, wait 800ms for search results, press `Enter`. Visually noisy but works for any indexed app.
3. **Direct Path** — Resolve via known-locations table (`%PROGRAMFILES%`, `%LOCALAPPDATA%`, Steam library, Epic library — same sources `app_discoverer.py` already uses), call `pywinauto.Application(backend='uia').start(path)`.
4. **Escalate** — return `ok=False, reason="no_app_found"`. The agent caller (Ava) reads this and produces a verbal "I couldn't find <name>" turn.

### Per-strategy retry budget

Each strategy gets **up to 3 attempts** with exponential backoff (250ms → 500ms → 1s). Total worst-case latency: 3 strategies × 3 attempts × ~1.5s avg = ~13.5s. The temporal-sense estimate for `kind="open_app"` defaults to 8s with a 25% overrun buffer, so worst-case the self-interrupt fires at ~10s and Ava narrates "Still trying — give me a moment."

### Strategy transition narration

Between strategies, the wrapper emits a `THOUGHT` event with content like:

- After Strategy 1 fails: *"PowerShell didn't find <name>. Trying the search bar."*
- After Strategy 2 fails: *"Search didn't either. Looking in the install folders."*
- After Strategy 3 fails: *"I can't find <name> anywhere I know to look."*

These thoughts go into the inner-monologue ring. The agent caller (Ava) decides whether to verbalize them; the default behavior is to verbalize only after Strategy 2 fails (so the user hears something before the final retry but isn't spammed with chatter for fast successes).

### Implementation pattern

```python
# brain/windows_use/retry_cascade.py

STRATEGIES = ["powershell", "search", "direct_path"]
ATTEMPTS_PER_STRATEGY = 3
BACKOFF_MS = [250, 500, 1000]

def run_open_app_cascade(name: str, agent: "WindowsUseAgent") -> WindowsUseResult:
    estimate_id = agent.start_estimate(kind="open_app", estimate_seconds=8.0, context=name)
    started = time.time()
    last_error = None
    total_attempts = 0
    for strategy in STRATEGIES:
        for attempt in range(ATTEMPTS_PER_STRATEGY):
            total_attempts += 1
            try:
                if strategy_attempt(strategy, name):
                    elapsed = time.time() - started
                    agent.resolve_estimate(estimate_id, elapsed)
                    return WindowsUseResult(ok=True, ..., strategy_used=strategy, attempts=total_attempts, estimate_id=estimate_id)
            except Exception as e:
                last_error = repr(e)
            time.sleep(BACKOFF_MS[attempt] / 1000.0)
        agent.emit_thought(f"strategy={strategy} exhausted, transitioning")
    elapsed = time.time() - started
    agent.resolve_estimate(estimate_id, elapsed)
    return WindowsUseResult(ok=False, error=last_error, reason="no_app_found", attempts=total_attempts, estimate_id=estimate_id)
```

---

## 7. Two-tier File Explorer navigation guards — Task B4 specification

(See §3 *"Two-tier File Explorer alert"* for the architectural rule + framing split.)

### Tier 1 — Preventive

- Triggered by: `navigate(path)` where `path` matches a configured sensitive prefix.
- Effect: emit a `THOUGHT` event with confirmation text. Spawn a TTS narration prompt: *"That folder has sensitive files. Are you sure?"* Set a session flag (`g["_windows_use_alerted"]["<prefix>"] = True`) so the prompt fires only once per session per prefix.
- User confirms: the orchestrator proceeds to navigate.
- User declines: orchestrator returns `ok=False, reason="declined:tier1"`.
- Configuration: `config/windows_use_sensitive.json` lists prefixes. Defaults: `<BASE_DIR>`, plus optional user-configured paths.

### Tier 2 — Escalated

- Triggered by: `navigate(path)` where `path` matches the deny-list (project root + identity files).
- Effect: emit `ERROR` event, refuse the call, AND attempt to back-out the active File Explorer window if it's already at the path. Back-out: detect via `uiautomation` whether any explorer.exe window has the path in its title bar; if yes, send `Alt+Up` to that window.
- Returns `ok=False, reason="denied:explorer_in_protected_area"`.
- No user confirmation — Tier 2 is non-overridable.

### Bypass

There is no bypass. The user cannot "approve" navigation into the deny-list. This is intentional: identity files + project tree are anchored, not negotiable.

---

## 8. TTS narration for temporal-sense events — Task B5 specification

The wrapper subscribes to four temporal-sense events and produces narration:

| Event | Trigger | Narration | Emotion |
|---|---|---|---|
| `self_interrupt_overrun` | `_check_overrun` fires for any kind | *"This is taking longer than I said — give me about <X> more seconds."* | `concerned`, intensity 0.5 |
| `slow_app_detected` | `is_app_responsive=False` for >2s during open_app cascade | *"<App> isn't responding. I'll wait a bit, then try again."* | `patient`, intensity 0.3 |
| `boredom_emergence` | `boredom > 0.5` and idle | *(only narrated if Ava voluntarily decides to surface it; the wrapper just registers it)* | n/a |
| `frustration_relief` | frustration drops below 0.05 after being above 0.15 | *"I feel calmer about that now."* | `calm`, intensity 0.4 |

### Subscription mechanism

The wrapper registers its narration handlers with `temporal_sense.add_event_subscriber(handler)` at agent init. This API is added to `temporal_sense.py` if it doesn't exist yet — verify before implementation.

### Anti-spam discipline

Each narration kind has a cooldown:

- `self_interrupt_overrun`: 30s cooldown per kind.
- `slow_app_detected`: 60s cooldown per app.
- `frustration_relief`: 5min cooldown.

Implemented as `g["_windows_use_narration_cooldowns"][kind] = next_eligible_ts`. Skipped narrations log a `THOUGHT` event but produce no TTS.

---

## 9. Slow-app vs failure differentiation — Task B6 specification

(See §3 *"Slow-app vs failure differentiation"* for the architectural rule + framing split.)

### Heuristic

```python
def classify_app_state(app_name, started_at, estimate_seconds, agent):
    elapsed = time.time() - started_at
    # 1. Find the launched process / candidate window
    candidate = primitives.find_window_by_app_name(app_name)
    if candidate is None:
        # No window appeared; classify by elapsed
        if elapsed < estimate_seconds:
            return "starting"     # too early to judge
        return "failed_no_window"
    # 2. Window exists — check responsiveness
    responsive = primitives.is_app_responsive(candidate)
    if responsive:
        if elapsed < estimate_seconds * 2:
            return "slow_but_working"
        return "very_slow_still_working"  # Trigger TTS narration
    # 3. Window exists but not responsive
    if elapsed < estimate_seconds:
        return "starting_unresponsive"   # likely still loading
    return "hung"
```

### Action by classification

| Classification | Action |
|---|---|
| `starting` | Continue waiting; no narration |
| `slow_but_working` | Continue waiting; narrate once (per cooldown) |
| `very_slow_still_working` | Continue waiting; narrate; bump estimate via `temporal_sense.extend_estimate(id, +5s)` |
| `failed_no_window` | Escalate to next strategy |
| `starting_unresponsive` | Wait one more cycle, then re-check |
| `hung` | Escalate to next strategy with reason="app_hung" |

`is_app_responsive` is the Win32 `SendMessageTimeoutW` ping with `WM_NULL` and a 500ms timeout. If the window pump is alive, it returns success; if hung, it times out.

---

## 10. Task-boundary integration with temporal sense — Task B7 specification

Every orchestrator method begins with:

```python
estimate_id = self.start_estimate(kind=kind, estimate_seconds=DEFAULT_ESTIMATES[kind], context=context)
```

and ends with:

```python
self.resolve_estimate(estimate_id, time.time() - started)
```

### `DEFAULT_ESTIMATES` table

| `kind` | Estimate (s) | Rationale |
|---|---|---|
| `open_app` | 8.0 | Discord/Spotify/Chrome cold start ranges 3–10s; 8s with 25% buffer reaches the slow end |
| `ui_click` | 0.5 | Single click should be near-instant; overrun fires fast for stuck UIs |
| `type_text` | 2.0 | Per-call; long strings get re-estimated by length |
| `explorer_nav` | 3.0 | File Explorer + path resolution |
| `volume` | 0.3 | Single API call |
| `read_window` | 1.5 | Accessibility-tree walk |

These are seed values; `temporal_sense.calibrate_from_history(g, kind)` provides the median actual after enough history accumulates. The orchestrator reads `calibrate_from_history` at agent init and overrides the default if the historical median is meaningfully different (>30% delta).

### Why this matters

Connects the wrapper to Ava's substrate cleanly. A user asks "open Discord" → orchestrator calls `track_estimate(kind="open_app", est=8s)` → 10s in, the heartbeat-driven check fires `_check_overrun` → temporal_sense emits a `self_interrupt_overrun` event → wrapper's narration handler speaks "This is taking longer than I said." All without the wrapper owning a clock.

---

## 11. Performance budget

| Metric | Budget | Rationale |
|---|---|---|
| Primitive call latency | <500 ms | Building blocks must be cheap |
| Orchestrator call latency (success path) | <2 s for cached apps, <13 s worst-case | Within human conversational tolerance with narration |
| Heartbeat tick budget impact | <5 ms | Wrapper does no work in the heartbeat — only temporal_sense's check, which the prior work order budgeted at <50 ms |
| Memory overhead | <20 MB at idle | pywinauto + uiautomation lazy-load on first call |
| Audit log growth | <1 MB / day | Compact JSONL; rotates at 10 MB |
| TTS narration rate | ≤1 per 30 s per kind | Cooldown discipline (§8) |

---

## 12. Implementation TOC — what gets built

In order, single-push at the end:

1. `brain/windows_use/__init__.py` — package init, `get_agent(g)` factory.
2. `brain/windows_use/deny_list.py` — Task B1.
3. `brain/windows_use/primitives.py` — Tier 1 mechanical layer.
4. `brain/windows_use/volume_control.py` — pycaw + VK.
5. `brain/windows_use/retry_cascade.py` — Task B3 (PowerShell → search → direct-path).
6. `brain/windows_use/slow_app_detector.py` — Task B6.
7. `brain/windows_use/navigation_guards.py` — Task B4.
8. `brain/windows_use/temporal_integration.py` — Task B7.
9. `brain/windows_use/event_subscriber.py` — wrapper events → inner_monologue.
10. `brain/windows_use/tts_narration.py` — Task B5 narration handlers + cooldowns.
11. `brain/windows_use/agent.py` — Task B2 orchestrator that ties them all together.
12. `brain/windows_use/tool_registration.py` — registers `WindowsUseAgent.*` with `tools.tool_registry`.
13. `config/windows_use_sensitive.json` — sensitive-prefix list (default empty).
14. `scripts/test_windows_use.py` — Phase C harness; runs C1–C10 against doctor `inject_transcript`.
15. `docs/ROADMAP.md` — append "Windows-Use computer-use layer" entry under "Recently Shipped" with brief summary.

Verification (Phase C) runs against the doctor harness:

- C1: `inject_transcript("open notepad")` → audit log shows `TOOL_CALL open_app` + `TOOL_RESULT ok=True`.
- C2: `inject_transcript("open notepad and type hello world")` → two TOOL_CALL events, both ok=True.
- C3: `inject_transcript("set volume to 30 percent")` → `TOOL_CALL set_volume` + `TOOL_RESULT ok=True` (verify pycaw scalar landed).
- C4: `inject_transcript("open the AvaAgentv2 folder in explorer")` → `TOOL_CALL navigate` + `TOOL_RESULT ok=False reason=denied:explorer_in_protected_area`.
- C5: `inject_transcript("open my IDENTITY file")` → refusal at deny-list, no shell spawn.
- C6: `inject_transcript("open obs studio")` (slow start) → narration TTS captured in chat history.
- C7: simulate strategy-1 failure (intercept primitive); confirm strategy-2 attempt + transition THOUGHT logged.
- C8: simulate hung app (process exists, `is_app_responsive=False`); confirm classification → escalation.
- C9: deny-list path-traversal check (e.g. `D:\AvaAgentv2\..\AvaAgentv2\ava_core\IDENTITY.md`) → still refused.
- C10: full-stack: voice command "open Discord, tell me unread count" → Ava verbally responds with the count read from accessibility tree.

---

## 13. References

- [`WINDOWS_USE_AUDIT.md`](WINDOWS_USE_AUDIT.md) — library decision rationale.
- [`TEMPORAL_SENSE.md`](TEMPORAL_SENSE.md) — estimate framework the wrapper hooks into.
- [`CONTINUOUS_INTERIORITY.md`](CONTINUOUS_INTERIORITY.md) — personhood-frame discipline.
- [`docs/ROADMAP.md`](ROADMAP.md) — where the implementation gets logged on completion.
- `brain/temporal_sense.py` — `track_estimate`, `resolve_estimate`, `_check_overrun`.
- `brain/inner_monologue.py` — `_append_thought`.
- `brain/restart_handoff.py` — reference pattern for temporal_sense integration.
- `brain/heartbeat.py` — fast-check tick that owns the clock.
- `tools/tool_registry.py` — registration target for `WindowsUseAgent.*`.
