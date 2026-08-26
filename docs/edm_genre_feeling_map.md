# EDM Genre → Feeling Map (for lyric_viz style selection)

Written 2026-08-26 after Zeke's correction (*"look at the theme or feeling of all the EDM
genres, that way you know"*) — I'd styled his future-bass song as dubstep off BPM alone.
**Genres are emotional languages, not tempo ranges.** Sources: alecforshag.com
"EDM Subgenres & Their Emotional Roles", housewub.org subgenre guide, style research
2026-08-26. Standing rule: when a song is ambiguous, ASK ZEKE — he's the authority on
his own lanes.

## The signals that actually identify a lane

1. **Vocal usage** — full lyric-driven songs live in future bass / melodic dubstep /
   vocal trance / pop-EDM. Dubstep/riddim/techno use words sparsely or never.
2. **Emotional contrast shape** — soft-verse-into-huge-drop = future bass (vulnerability →
   catharsis). Constant grind = dubstep/riddim. Steady baseline = deep house/techno.
   Long patient climb = trance/progressive.
3. **What the drop DOES** — melodic chords scream (future/melodic bass), bass growls
   (dubstep), kick hammers (hardstyle/techno), breakbeat rolls (DnB).
4. BPM last, as a tiebreaker only (140 half-time can be dubstep, future bass, OR trap).

## The map

| Genre | Feeling / emotional role | Vocals | Visual language | Engine style |
|---|---|---|---|---|
| **Future bass** | catharsis, vulnerability, soft↔huge contrast | full lyrics, chopped + pitched | bright pastel neon, bouncy pulse, clean bold type, glossy | `futurebass` |
| **Melodic dubstep** | heartbreak at full volume, epic sadness | full lyrics + big sung hooks | cinematic light rays + tasteful glitch at drops | `futurebass` + light glitch (tune later) |
| **Dubstep / riddim** | menace, controlled violence, the wobble grind | few/none — words as texture | glitch tears, shake, strobes, acid colors | `dubstep` |
| **Deathstep / rawer bass** | darkness, dread, aggression | none | near-black, blood red, heavy flicker | `darkdubstep` |
| **Deep house** | grounding, comfort, steady presence | sparse, murmured, looped | smooth waveforms, dark neon gradients, minimal thin type | `deephouse` |
| **Melodic house / progressive** | hope, nostalgia, patient long-form release | occasional, atmospheric | slow-evolving gradients, horizon lines, small type | `deephouse` (slower cuts) |
| **Big room / festival EDM** | euphoria, mass energy, hands-up | hook phrases, chants | strobes, radial bursts, saturated palette floods | `edm` |
| **Trance (uplifting/vocal)** | transcendence, yearning, the long build | vocal trance = full lyrics, ethereal | starfields, tunnels, slow zoom, white/blue light | `edm` slowed (future preset) |
| **Trap (EDM) / hybrid** | swagger, confidence, controlled aggression | rap verses or chopped one-liners | bold centered bars, hard cuts on snare rolls, chrome/black | `dubstep` variant (future preset) |
| **Hardstyle** | defiance, anthemic power, stomp | shouted anthem lines | pitched-kick pulses, fire/industrial, hard flashes | `darkdubstep` variant |
| **Techno (industrial/minimal)** | machine hypnosis, relentlessness | none | monochrome strobe grids, tunnels, no ornament | future preset |
| **DnB (liquid)** | flow, momentum, bittersweet speed | sung hooks common | fast smooth streaks, rolling waves | future preset |
| **Ambient / downtempo / chillstep** | introspection, holding space | breathy fragments | near-static texture, grain, tiny slow type | `deephouse` (no drums emphasis) |
| **Synthwave / retrowave** | imagined-80s nostalgia, neon night drives | occasional retro vocals | sun grids, chrome text, magenta/cyan horizon | future preset |
| **Phonk** | grit, street menace, cowbell memphis | pitched dark samples | VHS, distress, hard small cuts | `darkdubstep` variant |

## How the engine should use this

- `--style` stays manual-first: Zeke names the lane, engine renders it.
- Auto-suggest (never auto-decide): word_count from transcription + contrast shape from
  the drop detector + BPM → propose 1-2 lanes, ask.
- Presets still owed when a song needs them: trance, techno, DnB, synthwave, trap.

*Related: memory/edm_visualizer_style_research_2026-08-26.md (visual grammar + library).*
