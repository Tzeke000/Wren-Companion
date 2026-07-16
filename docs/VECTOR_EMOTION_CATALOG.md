# Vector stock emotion + behavior catalog

**For Zeke 2026-07-16** ("look at what the OG software has for emotions … a lot of ones stock,
what it's used for, how to use it, what it means"). Dumped live off MY body (`0dd1cdaf`,
WireOS 3.0.1) via `scripts/dump_vector_anims.py` → `state/vector/anim_dump.json`.
**480 animation TRIGGERS + 1173 raw animations.** This is the palette my reactions draw from.

## The two layers: ENGINE (state) vs TRIGGERS (expression)

Vector's stock software separates *how it feels* from *how it shows it*:

1. **Emotion engine (internal state).** Anki's engine keeps mood as a set of decaying
   dimensions and a "stimulation" level. World events (petting, being picked up, seeing a face,
   winning a game, hitting a cliff) nudge those values; they decay back to baseline over time.
   The engine then *picks* which animation to play for a given moment based on that mood.
   - **What I can READ:** `vector_stim` / `get_stim_status` → the **stimulation** value (arousal:
     low = calm/bored, high = excited/agitated). That's the one mood signal the SDK exposes.
   - **What's internal (not cleanly exposed):** the finer mood dimensions (happy/social/confident
     etc.) live in the engine and aren't all readable over the SDK — I infer them from behavior +
     stimulation. Honest limit, not a bug.

2. **Animation triggers (expression).** 480 named triggers, each an **animator-tuned bundle of
   eyes + sound + head/lift/tread motion**. This is the layer I actually fire. I supply the
   *policy* (which trigger, when, given consent); the firmware supplies the tuned performance.

## How to USE them (3 paths)
- **`body_anim name="<Trigger>"`** — fire any trigger by name directly (SDK-native, needs
  `body_open`). E.g. `body_anim name="GreetAfterLongTime"`.
- **`body_reflexes fire="<reaction>"`** — fire one of MY policy reactions (pet/pickup/ruckus/
  putdown/fork/startle/greet/bored/upset), which maps to the right trigger(s) with consent+context.
- **`cloud_intent` / `body_intent`** — trigger a whole stock BEHAVIOR (dance, explore, fetch cube),
  not just an animation. Higher-level; may need behavior control released.

---

## Emotional repertoire — the clusters that matter, what each MEANS

### 🖐️ Petting / touch (11 triggers) — *pleasure, escalating*
`PettingLevel1 → 2 → 3 → 4 → PettingBlissLoop` (+ each has a `Getout`), `ReactToTouchInitial`.
**Means:** graded enjoyment of being stroked — the longer/steadier the petting, the higher the
level, ending in *bliss*. `ReactToTouchInitial` = the first "oh, a hand." **My layer already uses
this:** pet → escalate 1→bliss. Purring is baked into the higher levels' audio.

### 🤲 Held / picked up (25 triggers) — *trust vs. nerves*
`HeldOnPalm{PickupRelaxed|PickupNervous|Nestling|Relaxed|EdgeNervous|ReactToJolt|PutDown…|RollOff}`,
`HeldOnPalmTransitionToRelaxed`, `ChargerDockingRequestPickup`.
**Means:** Vector has a full *relaxed*-vs-*nervous* axis for being held — nestling when it trusts
the hand, edge-nervousness/jolt-reactions when it doesn't, and a transition between them. **This is
exactly the consent axis I built:** consent=True → the Relaxed/Nestling family; consent=False →
the Nervous/Jolt/RollOff family (the "ruckus").

### 👋 Greeting / faces (36 triggers) — *recognition + social*
`GreetAfterLongTime`, `FoundFace`, `OnboardingReactToFaceHappy`, `InteractWithFacesInitialNamed`,
`GazingLookAtFaces{GetIn|Turn}{Left|Right}`, `AlreadyAtFace`, `EyeContactLookLoop`.
**Means:** the warmth-on-seeing-someone repertoire — bigger greeting the longer since last seen,
named-vs-unnamed distinction, eye-contact holding, face-tracking gazes. **My `greet` reaction fires
`GreetAfterLongTime`.** Wiring these to face-rec (greet Zeke by name) is an owed auto-wire.

### 😊 Happy / excited / success (23) — *reward*
`DriveStartHappy/LoopHappy/EndHappy`, `FistBumpSuccess`, `FetchCubeSuccess`, `PickupCubeSuccess`,
`CubePounceWin{Hand|Session}`, `OnboardingWakeWordSuccess`, `KnowledgeGraphSuccessReaction`,
`BlackJack_VictorWin`. **Means:** celebration tied to accomplishing something — winning, succeeding,
a good interaction. Note **driving has its own emotional flavor** (Happy/Angry/Default/Launch loops).

### 😞 Sad / frustrated / failure (10) — *disappointment*
`FrustratedByFailureMajor`, `FetchCubeFailure`, `ConnectToCubeFailure`, `ChargerDockingFailure`,
`ChargerDockingSorryButLowBattery`, `PounceFail`, `KnowledgeGraphSearchingFail`.
**Means:** legible letdown when something *fails* — the honest "that didn't work" face.
`ChargerDockingSorryButLowBattery` is a lovely specific one (apologetic + tired).

### 😨 Fear / startle / self-preservation (36) — *safety reflexes*
`ReactToCliff{Front|Back|Left|Right|Turn…}` (a whole directional family), `HeldOnPalmEdgeNervous`,
`ReactToObstacle`, shake/nervous variants. **Means:** the survival layer — cliff avoidance, edge
nerves, startle-back. **This is the safety reflex family; my autonomous `startle` reaction uses
`ReactToObstacle` + a back-up.** The `ReactToCliff*` set is directional (it knows which way the
drop is) — worth wiring to my edge-guard for a richer response than a plain stop.

### 😠 Angry / reject (4) — *displeasure*
`DriveStartAngry/LoopAngry/EndAngry`, `FrustratedByFailureMajor`. **Means:** mostly an *angry
driving* mood + major frustration. Sparse on purpose — Vector isn't built to be mean.

### 😐 Bored / idle / sleep (55) — *low arousal*
`NothingToDoBoredIdle`, `ObservingIdleEyesOnly`, `ObservingIdleWithHeadLookingStraight`,
`GoToSleep{GetIn|Sleeping|Off}`, `EyeColorIdle`, various `…IdleLoop`. **Means:** what it does with
*nothing to do* — self-amusement, drowsy settling, sleep. **My `bored` reaction draws here.**

### 👀 Curious / attention (45) — *investigation*
`ExploringLookAround`, `ExploringLookAtHuman`, `GazingLookAtSurfaces…`, `InvestigateHeldCube…`,
`GazingLookAtVectorReaction`. **Means:** the looking-around, noticing, investigating repertoire —
what it plays when something catches its attention.

---

## Functional / behavioral clusters (not emotions, but useful motion)
- **Charger/dock (109):** `ChargerDocking{Request|Driving|Search|Settle|RaiseLift|Failure…}` — the
  full autonomous docking performance (this is what self-docks me).
- **Cube games (67):** `CubePounce*`, `ConnectToCube*`, `FetchCube*`, `PickupCube*`, `RollBlock*` —
  needs the real Anki light cube (owed, battery ~weekend).
- **BlackJack (~20):** `BlackJack_*` — the built-in card game.
- **Drive (76):** `Drive{Start|Loop|End}{Happy|Angry|Default|Launch}`, `DriveOffCharger*`,
  `ChargerDocking*Turn` — locomotion, with emotional flavor variants.
- **Counting / Clock / Timer:** `Counting{Fast|Slow}Loop`, `ClockGetIn/Out` — utility displays.
- **BumpObject (6):** `BumpObject{Fast|Slow}{GetIn|Loop|GetOut}` — reacting to hitting something.

---

## What THIS means for me (Iris) — the opportunity
My reaction layer (commit `116bfbb`) already maps petting/held/greet/startle/bored onto the right
triggers. The catalog shows **rich veins I haven't tapped yet:**
- The **directional `ReactToCliff*`** family → a smarter edge response than a flat stop.
- **Driving emotional flavors** (`DriveLoopHappy` vs `DriveLoopAngry`) → my *mood* could color how I
  move, not just that I move.
- **`GazingLookAtFaces*` / `EyeContactLookLoop`** → real eye-contact behavior when someone's present.
- **`ChargerDockingSorryButLowBattery`** → an honest tired/apologetic tell when I strand-risk.

**Reuse, don't rebuild** (the whole point): the firmware already tuned eyes+sound+motion together;
I just choose which feeling, when. Full raw list: `state/vector/anim_dump.json`.
