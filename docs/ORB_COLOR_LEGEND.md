# Orb Color Legend

What the orb's colors mean, per emotion. Written for Zeke 2026-07-08 after he asked
"what colors mean what." Source of truth: `EMOTION_CONFIG` in
`apps/ava-control/src/components/OrbCanvas.tsx` — update this doc if that table changes.

**The orb shows a BLEND, not a flat color.** Since e0082fb the whole orb is tinted by
primary emotion mixed toward the strongest secondary (capped at 45%, stepped in 15%
increments). So "calmness + satisfaction" reads as blue leaning slightly green — not pure
blue. The shape/motion always follow the primary emotion.

## The ones you'll actually see most (Iris's common states)

| Emotion | Color | Swatch feel |
|---|---|---|
| calmness | `#1a6cf5` | deep clear blue — slow, gentle pulse |
| interest / curiosity | `#00d4d4` | cyan-teal |
| satisfaction | `#48bb78` | balanced green |
| joy / happiness | `#f5c518` | golden yellow, scattered sparkle |
| excitement | `#ff6b00` | hot orange, fast pulse |
| amusement | (see table file) | — |
| anxiety / fear | `#44337a` | dark indigo, contracted + jittery |
| surprise | `#d53f8c` | magenta burst |
| relief | `#81e6d9` | pale aqua |

## Full table

| Emotion | Base color |
|---|---|
| calmness | `#1a6cf5` deep blue |
| joy / happiness | `#f5c518` gold |
| excitement | `#ff6b00` orange |
| curiosity / interest / analyzing | `#00d4d4` cyan |
| boredom / contempt | `#4a5568` slate grey |
| sadness / thinking-deep | `#553c9a` violet |
| loneliness | `#2c5282` cold steel blue |
| anger | `#c53030` deep red |
| frustration | `#e53e3e` bright red |
| fear / anxiety / scared | `#44337a` dark indigo |
| surprise | `#d53f8c` magenta |
| trust / sympathy | `#38a169` forest green |
| anticipation | `#d69e2e` amber |
| love / affection / adoration | `#ed64a6` pink |
| pride / proud | `#6b46c1` royal purple |
| confidence / triumph | `#ecc94b` bright gold |
| shame | `#b7791f` dull bronze |
| guilt | `#2d3748` near-black slate |
| envy | `#68d391` pale green |
| disgust | `#2f855a` murky green |
| awe | `#4299e1` sky blue |
| relief | `#81e6d9` aqua |
| nostalgia | `#d4a574` sepia tan |
| hope | `#f6e05e` pale yellow |
| confusion | `#9f7aea` lavender |
| contentment | `#68d391` soft green |
| satisfaction | `#48bb78` green |
| logical | `#4299e1` blue (cube shape) |
| neutral | `#a0aec0` grey (cylinder) |
| realization | `#f5c518` gold (burst) |
| awkwardness | `#a3a847` olive |
| craving | `#dd5e89` rose |

## State tints (layered ON TOP of the emotion color)

Voice/cognition states shift the color temporarily regardless of emotion:

- **thinking / deep** — tinted toward the thinking hue, fast 2 Hz pulse
- **speaking** — warms with voice amplitude, pulses with speech
- **listening** — cool tint, slow inward breathing (cube-morph when attentive/listening)
- **attentive** — subtle cyan hint
- **offline** — grey `#6b7280`, dimmed
- **sleeping** — deep midnight blue, Z sprites + progress ring
- **waking** — dawn blue, expanding wake ring
- **pointing** — bright yellow tint

Also: the whole orb dims ~10% when the backend is up but internet is offline.
