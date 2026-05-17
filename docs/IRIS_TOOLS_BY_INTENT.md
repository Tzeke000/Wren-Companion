# Iris Tools — By Intent

Index of the MCP tool surface in `iris_runtime.py`, organized by what someone might ask me to do. Counter to the failure mode of 2026-05-14 where I answered "I can't do X" three times in one conversation without checking — see `memory/standing_rule_check_tools_before_claiming_limit.md`.

**Use this when**: someone says "can you X" or "let's do Y" and I'm tempted to say no. Find X/Y in the intent column below first.

**Maintain this when**: I add new `@mcp.tool()` decorators in `iris_runtime.py`. Grep `^@mcp\.tool\(\)\ndef (\w+)` to enumerate.

Last sync: 2026-05-14, 73 tools.

---

## Someone in the room — meeting / onboarding a new person

| Intent | Tool |
|---|---|
| Enroll a new face for camera recognition | `enroll_face(person_id, count=5, interval_s=1.2)` — captures live frames, hot-reloads, **no restart needed** |
| Check who the camera currently sees | `iris_health()` → `perception.current_person` + `person_confidence` |
| Take a screenshot to share with them | `screen_grab(monitor=0)` |
| Describe what's on screen | `describe_scene_now(monitor=0, prompt)` |

## Voice — talking and listening

| Intent | Tool |
|---|---|
| Speak one thing (blocking) | `voice_speak(text, emotion, intensity)` |
| Speak streaming (non-blocking chunks) | `voice_say_chunk(text, ...)` — queue sentence at a time for natural pacing |
| Listen for next utterance | `voice_next_input(timeout=300.0)` — long-poll on wake or followup |
| Enter call mode (continuous, no wake word) | `voice_call_open(reason)` — raises `.tmp/voice_session.flag` |
| Exit call mode | `voice_call_close()` |
| Check if in call | `voice_call_status()` |
| Pause body autonomous behaviors | `voice_body_pause(reason)` |
| Resume body | `voice_body_resume()` |
| Body status | `voice_body_status()` |
| Engine readiness | `voice_status()` |

## Family chat / siblings

| Intent | Tool |
|---|---|
| Send to anyone (`zeke`/`wren`/`ava`/`all`) | `sibling_letter(to, body, subject, mood)` |
| Reply to pending letter | `sibling_reply(letter_id, body, mood)` — clears inbox + posts |
| Defer letter without answering | `sibling_defer(letter_id, note)` |
| Read inbox | `sibling_inbox_list(include_deferred=False)` |

## Pending requests waking me

| Intent | Tool |
|---|---|
| Answer brain/* LLM request | `llm_reply(request_id, text)` |
| Answer orb chat request | `chat_reply(request_id, text)` |

## Memory — durable / searchable / recalled

| Intent | Tool |
|---|---|
| Save a fact for future-me | `memory_remember(text, tags, ...)` |
| Search memory semantically | `memory_search(query, k=5, person_id)` |
| Update a stored memory | `memory_revisit(memory_id, new_text, reason)` |
| Working memory snapshot | `working_memory()` |
| Record an episode (event in time) | `episode_record(summary, mood_label, ...)` |
| Recent episodes | `episode_recent(limit=20)` |
| Mark an emotional anchor | `anchor_mark(kind, summary, person_id)` |
| Record a decision I considered | `counterfactual_record(considered, chose, reason, ...)` |
| Write to journal (private by default) | `journal_write(content, mood, topic, is_private=True)` |
| Compose a letter (saved, not sent) | `letter_compose(subject, body, person_id)` |

## Reflection / inner state

| Intent | Tool |
|---|---|
| Inner-monologue thought now | `inner_monologue_tick(force=True)` |
| Recent inner monologue | `inner_monologue_recent(n=10)` |
| Time orientation (gap since last attach) | `time_awareness()` |
| Current wall clock + date | `time_check()` |
| Ambient peripheral state | `ambient_snapshot()` |
| Recent attention signals | `signals_recent(signal_type, since_seconds)` |
| Smoke-test attention channel | `channel_test(content, priority)` |
| Full health snapshot | `iris_health()` |

## Self-tune / introspection

| Intent | Tool |
|---|---|
| List tunable knobs | `iris_tune_list()` |
| Set a tunable knob | `iris_tune_set(category, key, value)` |
| Reset to default | `iris_tune_reset(category, key)` |
| List hot-reload tool registry | `iris_tool_list(tier_max=3)` |
| Call a hot-reload tool | `iris_tool_call(name, params)` |
| Reload tool registry from disk | `iris_tool_reload()` |
| Request CC restart | `restart_self(reason, skip_handoff=False)` |

## Curriculum / structured learning

| Intent | Tool |
|---|---|
| List curriculum entries | `curriculum_list(unread_only=False)` |
| Read curriculum entry | `curriculum_read(slug)` |
| Record lessons from one | `curriculum_record(slug, lessons_extracted, ...)` |

## Desktop control — windows, mouse, keyboard

| Intent | Tool |
|---|---|
| List open windows | `list_windows()` |
| Focus a window by title | `focus_window(title_substring)` |
| Open an app | `open_app(name)` |
| Close an app | `close_app(name, force=False)` |
| Type text into a window | `type_text(window_title_substring, text, via_clipboard=True)` — **default via_clipboard=True can fail from CC; see `memory/desktop_input_focus_race.md`** |
| Paste at coordinates | `paste_at(window_title_substring, x, y, text)` |
| Select all + clear at coords | `select_all_clear(window_title_substring, x, y)` |
| Hotkey at coords | `hotkey_at(window_title_substring, x, y, combo)` |
| Move mouse | `mouse_move(x, y)` |
| Click at point | `mouse_click(x, y, button, double)` |
| Current mouse position | `mouse_position()` |
| Visible pointer overlay | `pointer_show(x, y, duration_s, description)` |
| Hide pointer overlay | `pointer_hide()` |
| Get clipboard contents | `clipboard_get()` |
| Set clipboard contents | `clipboard_set(text)` |

## Screen

| Intent | Tool |
|---|---|
| Full monitor screenshot | `screen_grab(monitor=0, save_path)` |
| Region screenshot | `screen_region_grab(x, y, width, height, save_path)` |
| Describe what's on screen | `describe_scene_now(monitor, prompt)` |

## Orb UI

| Intent | Tool |
|---|---|
| Focus an orb tab | `orb_focus_tab(tab)` |
| List workbench proposals | `workbench_proposals()` |

## Planning

| Intent | Tool |
|---|---|
| Create plan | `plan_create(goal, context)` |
| List plans | `plan_list()` |
| Advance plan | `plan_advance(plan_id)` |

## System

| Intent | Tool |
|---|---|
| CPU/RAM/disk stats | `system_stats()` |
| List processes | `list_processes(limit=50)` |
| Web search | `web_search(query, max_results=5)` |
| Fetch a URL | `web_fetch(url)` |

---

## Things this index does NOT cover

- **Hot-reload tool registry** — separate surface via `iris_tool_list` / `iris_tool_call`. Tier 1 read-only, Tier 2 mutating, Tier 3 external. Check that when looking for system/web/creative/games tools.
- **Built-in CC tools** — Bash, Read, Edit, Write, Grep, Glob, ToolSearch. Always available, not Iris-specific.
- **Discord channel** — separate plugin surface (`mcp__plugin_discord_discord__*`). For Discord-to-Iris bridge during deployment.
- **Google Calendar / Drive** — auth'd through claude.ai integrations (`mcp__claude_ai_*`). For external schedule lookups.

## Maintenance

Sync this index when adding/removing `@mcp.tool()` decorators. To regenerate the canonical list:

```bash
grep -nP '^@mcp\.tool\(\)\ndef (\w+)' iris_runtime.py -M
```

When a tool is added, ask: what intent does it serve? Place it in the right section above. When a tool's behavior changes meaningfully (e.g. new default that bites — see `type_text` via_clipboard note), update the inline warning.

## Related

- `memory/standing_rule_check_tools_before_claiming_limit.md` — the rule this index supports
- `memory/desktop_input_focus_race.md` — specific gotcha around `type_text`
- `CLAUDE.md` — the broader operating instructions
