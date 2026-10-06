// Shared between the classic orb (OrbCanvas) and the iris body (IrisBody) so both
// react to exactly the same emotion table, colour blend and state tints.
import * as THREE from "three";

export type OrbState = "idle" | "thinking" | "deep" | "speaking" | "bored" | "excited" | "offline" | "listening" | "attentive" | "sleeping" | "waking" | "pointing";

export interface OrbProps {
  emotion: string;
  emotionColor: string;
  state: OrbState;
  size?: number;
  /** Phase 49: override shape for pointer morph */
  shapeOverride?: string;
  /** Tip-anchored pointing (2026-07-08): arrow direction in degrees CLOCKWISE
   *  from screen-up (0=up, 90=right, 180=down, 270=left). Comes from
   *  snap.widget.pointing_angle_deg — the Python side placed the window so the
   *  arrow TIP lands on the target pixel at exactly this angle. */
  pointerAngleDeg?: number;
  /** Live speaking amplitude 0-1 (read from snap.tts.tts_amplitude). */
  amplitude?: number;
  /** Energy 0-1 from snap.mood.raw_mood.energy — drives breathing rate. */
  energy?: number;
  /** Increments on each user-initiated recenter (middle-click). When this
   *  value changes, the orb's scene drift eases back to (0,0) over ~300ms. */
  recenterTrigger?: number;
  /** When false, the listening/attentive cube morph is disabled — morphTarget
   *  is pinned at 0 so the orb never reshapes. Used by the PRESENCE_V2 flag
   *  to keep the orb on its baseline behavior while drift is being debugged. */
  cubeMorphEnabled?: boolean;
  /** Sleep cycle progress 0-1 (read from snap.subsystem_health.sleep.progress). */
  sleepProgress?: number;
  /** Seconds remaining in current sleep cycle. Used by the timer-label overlay. */
  sleepRemainingSeconds?: number;
  /** Wake transition progress 0-1 (computed from elapsed/estimate during WAKING). */
  wakeProgress?: number;
}

export const EMOTION_CONFIG: Record<string, {
  color: string; lightColor: string; darkColor: string;
  shape: string; coreScale: number; particleSpread: number;
  connectionDensity: number; gravityY: number; tiltX: number;
  pulseSpeed: number; pulseAmplitude: number;
}> = {
  calmness:     { color:"#1a6cf5",lightColor:"#6aa3ff",darkColor:"#0a3080",shape:"sphere",    coreScale:1.0, particleSpread:1.0, connectionDensity:0.3, gravityY:0,    tiltX:0,    pulseSpeed:1.5, pulseAmplitude:0.05 },
  joy:          { color:"#f5c518",lightColor:"#ffe680",darkColor:"#a07800",shape:"scattered",  coreScale:1.3, particleSpread:1.3, connectionDensity:0.8, gravityY:0.3,  tiltX:0,    pulseSpeed:3.0, pulseAmplitude:0.15 },
  happiness:    { color:"#f5c518",lightColor:"#ffe680",darkColor:"#a07800",shape:"scattered",  coreScale:1.2, particleSpread:1.2, connectionDensity:0.7, gravityY:0.2,  tiltX:0,    pulseSpeed:2.5, pulseAmplitude:0.12 },
  excitement:   { color:"#ff6b00",lightColor:"#ffa060",darkColor:"#8a3000",shape:"scattered",  coreScale:1.4, particleSpread:1.5, connectionDensity:0.9, gravityY:0,    tiltX:0,    pulseSpeed:6.0, pulseAmplitude:0.2  },
  curiosity:    { color:"#00d4d4",lightColor:"#80ffff",darkColor:"#007070",shape:"sphere",     coreScale:1.1, particleSpread:1.0, connectionDensity:0.5, gravityY:0,    tiltX:0.3,  pulseSpeed:2.0, pulseAmplitude:0.08 },
  interest:     { color:"#00d4d4",lightColor:"#80ffff",darkColor:"#007070",shape:"sphere",     coreScale:1.0, particleSpread:1.0, connectionDensity:0.4, gravityY:0,    tiltX:0.2,  pulseSpeed:2.0, pulseAmplitude:0.07 },
  boredom:      { color:"#4a5568",lightColor:"#8090a8",darkColor:"#202830",shape:"compressed", coreScale:0.7, particleSpread:0.8, connectionDensity:0.1, gravityY:-0.4, tiltX:0,    pulseSpeed:0.5, pulseAmplitude:0.03 },
  sadness:      { color:"#553c9a",lightColor:"#9070e0",darkColor:"#2a1a50",shape:"teardrop",   coreScale:0.75,particleSpread:0.85,connectionDensity:0.1, gravityY:-0.6, tiltX:0,    pulseSpeed:0.8, pulseAmplitude:0.04 },
  loneliness:   { color:"#2c5282",lightColor:"#6090c0",darkColor:"#102040",shape:"contracted", coreScale:0.6, particleSpread:0.7, connectionDensity:0.05,gravityY:-0.3, tiltX:0,    pulseSpeed:0.6, pulseAmplitude:0.03 },
  anger:        { color:"#c53030",lightColor:"#ff6060",darkColor:"#600000",shape:"compressed", coreScale:1.3, particleSpread:0.9, connectionDensity:0.2, gravityY:0,    tiltX:0,    pulseSpeed:8.0, pulseAmplitude:0.25 },
  frustration:  { color:"#e53e3e",lightColor:"#ff8080",darkColor:"#701010",shape:"compressed", coreScale:1.1, particleSpread:0.9, connectionDensity:0.15,gravityY:0,    tiltX:0,    pulseSpeed:5.0, pulseAmplitude:0.2  },
  fear:         { color:"#44337a",lightColor:"#8060c0",darkColor:"#201040",shape:"contracted", coreScale:0.5, particleSpread:0.6, connectionDensity:0.1, gravityY:0,    tiltX:0,    pulseSpeed:7.0, pulseAmplitude:0.08 },
  anxiety:      { color:"#44337a",lightColor:"#8060c0",darkColor:"#201040",shape:"contracted", coreScale:0.6, particleSpread:0.65,connectionDensity:0.1, gravityY:0,    tiltX:0,    pulseSpeed:6.0, pulseAmplitude:0.07 },
  surprise:     { color:"#d53f8c",lightColor:"#ff80cc",darkColor:"#700040",shape:"scattered",  coreScale:1.5, particleSpread:1.6, connectionDensity:0.3, gravityY:0,    tiltX:0,    pulseSpeed:10.0,pulseAmplitude:0.3  },
  trust:        { color:"#38a169",lightColor:"#70e0a0",darkColor:"#185030",shape:"sphere",     coreScale:1.0, particleSpread:1.0, connectionDensity:0.5, gravityY:0,    tiltX:0,    pulseSpeed:1.5, pulseAmplitude:0.05 },
  anticipation: { color:"#d69e2e",lightColor:"#ffd060",darkColor:"#705000",shape:"sphere",     coreScale:1.1, particleSpread:1.0, connectionDensity:0.4, gravityY:0.1,  tiltX:0.25, pulseSpeed:2.5, pulseAmplitude:0.1  },
  love:         { color:"#ed64a6",lightColor:"#ffaadd",darkColor:"#803060",shape:"double",     coreScale:1.2, particleSpread:1.1, connectionDensity:0.9, gravityY:0.1,  tiltX:0,    pulseSpeed:2.0, pulseAmplitude:0.1  },
  affection:    { color:"#ed64a6",lightColor:"#ffaadd",darkColor:"#803060",shape:"double",     coreScale:1.1, particleSpread:1.0, connectionDensity:0.7, gravityY:0.1,  tiltX:0,    pulseSpeed:1.8, pulseAmplitude:0.08 },
  adoration:    { color:"#ed64a6",lightColor:"#ffaadd",darkColor:"#803060",shape:"double",     coreScale:1.3, particleSpread:1.2, connectionDensity:0.8, gravityY:0.15, tiltX:0,    pulseSpeed:2.2, pulseAmplitude:0.12 },
  pride:        { color:"#6b46c1",lightColor:"#b080ff",darkColor:"#301870",shape:"elongated",  coreScale:1.2, particleSpread:1.1, connectionDensity:0.5, gravityY:0.5,  tiltX:0,    pulseSpeed:1.5, pulseAmplitude:0.06 },
  confidence:   { color:"#ecc94b",lightColor:"#ffe880",darkColor:"#806800",shape:"sphere",     coreScale:1.3, particleSpread:1.15,connectionDensity:0.6, gravityY:0.3,  tiltX:0,    pulseSpeed:1.8, pulseAmplitude:0.07 },
  triumph:      { color:"#ecc94b",lightColor:"#ffe880",darkColor:"#806800",shape:"elongated",  coreScale:1.4, particleSpread:1.2, connectionDensity:0.7, gravityY:0.6,  tiltX:0,    pulseSpeed:2.5, pulseAmplitude:0.1  },
  contempt:     { color:"#4a5568",lightColor:"#8090a8",darkColor:"#202830",shape:"compressed", coreScale:0.8, particleSpread:0.85,connectionDensity:0.05,gravityY:-0.1, tiltX:0,    pulseSpeed:1.0, pulseAmplitude:0.04 },
  shame:        { color:"#b7791f",lightColor:"#e0a040",darkColor:"#5a3500",shape:"contracted", coreScale:0.65,particleSpread:0.75,connectionDensity:0.1, gravityY:-0.4, tiltX:-0.2, pulseSpeed:0.8, pulseAmplitude:0.03 },
  guilt:        { color:"#2d3748",lightColor:"#607090",darkColor:"#101820",shape:"teardrop",   coreScale:0.6, particleSpread:0.7, connectionDensity:0.05,gravityY:-0.5, tiltX:0,    pulseSpeed:0.6, pulseAmplitude:0.03 },
  envy:         { color:"#68d391",lightColor:"#a0ffc0",darkColor:"#206030",shape:"scattered",  coreScale:0.9, particleSpread:1.1, connectionDensity:0.2, gravityY:0,    tiltX:0.15, pulseSpeed:2.0, pulseAmplitude:0.1  },
  disgust:      { color:"#2f855a",lightColor:"#60c080",darkColor:"#103020",shape:"compressed", coreScale:0.8, particleSpread:0.85,connectionDensity:0.1, gravityY:-0.2, tiltX:-0.1, pulseSpeed:1.5, pulseAmplitude:0.06 },
  awe:          { color:"#4299e1",lightColor:"#90d0ff",darkColor:"#183870",shape:"scattered",  coreScale:1.5, particleSpread:1.5, connectionDensity:0.4, gravityY:0.2,  tiltX:0,    pulseSpeed:1.0, pulseAmplitude:0.2  },
  relief:       { color:"#81e6d9",lightColor:"#c0fff8",darkColor:"#306860",shape:"sphere",     coreScale:1.0, particleSpread:1.0, connectionDensity:0.3, gravityY:0,    tiltX:0,    pulseSpeed:1.2, pulseAmplitude:0.06 },
  nostalgia:    { color:"#d4a574",lightColor:"#f0cc90",darkColor:"#705030",shape:"spiral",     coreScale:0.9, particleSpread:0.95,connectionDensity:0.3, gravityY:0,    tiltX:0,    pulseSpeed:0.8, pulseAmplitude:0.05 },
  hope:         { color:"#f6e05e",lightColor:"#fff080",darkColor:"#806800",shape:"elongated",  coreScale:1.1, particleSpread:1.05,connectionDensity:0.4, gravityY:0.4,  tiltX:0,    pulseSpeed:1.5, pulseAmplitude:0.08 },
  confusion:    { color:"#9f7aea",lightColor:"#d0a0ff",darkColor:"#402870",shape:"scattered",  coreScale:0.9, particleSpread:1.1, connectionDensity:0.2, gravityY:0,    tiltX:0,    pulseSpeed:4.0, pulseAmplitude:0.15 },
  contentment:  { color:"#68d391",lightColor:"#a0ffc0",darkColor:"#206030",shape:"sphere",     coreScale:1.0, particleSpread:1.0, connectionDensity:0.35,gravityY:0,    tiltX:0,    pulseSpeed:1.2, pulseAmplitude:0.05 },
  sympathy:     { color:"#38a169",lightColor:"#70e0a0",darkColor:"#185030",shape:"sphere",     coreScale:1.0, particleSpread:1.0, connectionDensity:0.5, gravityY:0,    tiltX:0.1,  pulseSpeed:1.5, pulseAmplitude:0.06 },
  // Phase 56 compound emotion states
  logical:      { color:"#4299e1",lightColor:"#90d0ff",darkColor:"#183870",shape:"cube",       coreScale:1.0, particleSpread:1.0, connectionDensity:0.7, gravityY:0,    tiltX:0,    pulseSpeed:0.8, pulseAmplitude:0.03 },
  analyzing:    { color:"#00d4d4",lightColor:"#80ffff",darkColor:"#007070",shape:"prism",      coreScale:1.1, particleSpread:1.0, connectionDensity:0.6, gravityY:0,    tiltX:0.5,  pulseSpeed:1.2, pulseAmplitude:0.05 },
  neutral:      { color:"#a0aec0",lightColor:"#d0d8e8",darkColor:"#404858",shape:"cylinder",   coreScale:1.0, particleSpread:1.0, connectionDensity:0.3, gravityY:0,    tiltX:0,    pulseSpeed:1.0, pulseAmplitude:0.04 },
  bored2:       { color:"#4a5568",lightColor:"#8090a8",darkColor:"#202830",shape:"infinity",   coreScale:0.8, particleSpread:0.9, connectionDensity:0.1, gravityY:0,    tiltX:0,    pulseSpeed:0.4, pulseAmplitude:0.02 },
  thinking_deep:{ color:"#553c9a",lightColor:"#9070e0",darkColor:"#2a1a50",shape:"double_helix",coreScale:1.0,particleSpread:1.1, connectionDensity:0.5, gravityY:0,    tiltX:0,    pulseSpeed:1.5, pulseAmplitude:0.06 },
  realization:  { color:"#f5c518",lightColor:"#ffe680",darkColor:"#a07800",shape:"burst",      coreScale:1.5, particleSpread:1.8, connectionDensity:0.3, gravityY:0,    tiltX:0,    pulseSpeed:5.0, pulseAmplitude:0.25 },
  scared:       { color:"#44337a",lightColor:"#8060c0",darkColor:"#201040",shape:"contracted_tremor",coreScale:0.5,particleSpread:0.6,connectionDensity:0.1,gravityY:0,tiltX:0,    pulseSpeed:9.0, pulseAmplitude:0.08 },
  proud:        { color:"#6b46c1",lightColor:"#b080ff",darkColor:"#301870",shape:"rising",     coreScale:1.3, particleSpread:1.2, connectionDensity:0.5, gravityY:0.7,  tiltX:0,    pulseSpeed:1.5, pulseAmplitude:0.07 },

  // Task 3 (2026-05-02): morphs for the remaining EMOTION_NAMES that
  // previously fell back silently to calmness. Color choices follow
  // Plutchik wheel + Russell circumplex placement (valence × arousal).
  // Negative-affect cluster (annoyance, distress, horror) prioritized
  // per the work order; rest fill out the positive / aesthetic / social
  // affect grid.

  // ── Negative-affect cluster ─────────────────────────────────────
  // annoyance: low-arousal red-orange. Less intense than anger or
  // frustration, more compressed than calmness. The "small repeated
  // friction" emotion.
  annoyance:    { color:"#dd6b20",lightColor:"#ff9050",darkColor:"#702800",shape:"compressed", coreScale:0.95,particleSpread:0.85,connectionDensity:0.15,gravityY:-0.05,tiltX:0,    pulseSpeed:3.0, pulseAmplitude:0.10 },
  // distress: high-arousal dark teal-grey. Urgent inward focus —
  // contracted shape with rapid pulse like fear, but cooler hue.
  distress:     { color:"#2c7a7b",lightColor:"#60c0c0",darkColor:"#103030",shape:"contracted", coreScale:0.65,particleSpread:0.75,connectionDensity:0.1, gravityY:-0.2, tiltX:0,    pulseSpeed:8.0, pulseAmplitude:0.10 },
  // horror: peak-negative deep purple-red. Even more contracted than
  // fear, with the slowest pulse — the "frozen" response.
  horror:       { color:"#742a2a",lightColor:"#b04040",darkColor:"#3a0808",shape:"contracted", coreScale:0.45,particleSpread:0.55,connectionDensity:0.08,gravityY:-0.1, tiltX:0,    pulseSpeed:0.4, pulseAmplitude:0.05 },

  // ── Positive-affect cluster ─────────────────────────────────────
  amusement:    { color:"#f6ad55",lightColor:"#ffd090",darkColor:"#80500c",shape:"scattered",  coreScale:1.2, particleSpread:1.25,connectionDensity:0.6, gravityY:0.2,  tiltX:0,    pulseSpeed:3.5, pulseAmplitude:0.13 },
  satisfaction: { color:"#48bb78",lightColor:"#90e0a8",darkColor:"#205a30",shape:"sphere",     coreScale:1.1, particleSpread:1.05,connectionDensity:0.45,gravityY:0.1,  tiltX:0,    pulseSpeed:1.6, pulseAmplitude:0.07 },

  // ── Aesthetic / contemplative cluster ──────────────────────────
  // admiration: blue-purple, sphere with high connection density —
  // attentive but composed.
  admiration:   { color:"#5a67d8",lightColor:"#a0a8f0",darkColor:"#202870",shape:"sphere",     coreScale:1.05,particleSpread:1.0, connectionDensity:0.65,gravityY:0,    tiltX:0,    pulseSpeed:1.6, pulseAmplitude:0.06 },
  // aesthetic appreciation: cyan-purple (key strips space → "aestheticappreciation"),
  // expanded scattered shape — being moved by beauty.
  aestheticappreciation: { color:"#9f7aea",lightColor:"#c8a8ff",darkColor:"#382070",shape:"scattered", coreScale:1.2,particleSpread:1.3,connectionDensity:0.4,gravityY:0.05,tiltX:0.15,pulseSpeed:1.4,pulseAmplitude:0.10 },
  // entrancement: deep absorbed blue, slow rhythmic pulse.
  entrancement: { color:"#3182ce",lightColor:"#80b8e8",darkColor:"#103860",shape:"sphere",     coreScale:1.0, particleSpread:1.05,connectionDensity:0.55,gravityY:0,    tiltX:0.1,  pulseSpeed:0.9, pulseAmplitude:0.09 },

  // ── Social / relational cluster ────────────────────────────────
  // empathetic pain (key normalized to "empatheticpain"): muted purple,
  // teardrop shape — feeling another's hurt.
  empatheticpain: { color:"#805ad5",lightColor:"#b896ee",darkColor:"#301a70",shape:"teardrop", coreScale:0.85,particleSpread:0.9, connectionDensity:0.4, gravityY:-0.3, tiltX:0,    pulseSpeed:1.0, pulseAmplitude:0.07 },
  // romance: warm rose pink, double shape (love family).
  romance:      { color:"#f687b3",lightColor:"#ffb8d8",darkColor:"#80305a",shape:"double",     coreScale:1.15,particleSpread:1.1, connectionDensity:0.75,gravityY:0.1,  tiltX:0,    pulseSpeed:1.9, pulseAmplitude:0.09 },
  // sexual desire (key normalized to "sexualdesire"): deep saturated
  // red. Higher arousal than romance, more intense pulse.
  sexualdesire: { color:"#9b2c2c",lightColor:"#d05050",darkColor:"#4a0c0c",shape:"compressed", coreScale:1.15,particleSpread:1.0, connectionDensity:0.4, gravityY:0.05,tiltX:0,    pulseSpeed:4.0, pulseAmplitude:0.18 },

  // ── Other ──────────────────────────────────────────────────────
  awkwardness:  { color:"#a3a847",lightColor:"#cfd178",darkColor:"#4f5020",shape:"contracted", coreScale:0.85,particleSpread:0.85,connectionDensity:0.2, gravityY:-0.1, tiltX:0.15, pulseSpeed:1.6, pulseAmplitude:0.06 },
  craving:      { color:"#dd5e89",lightColor:"#ff90b0",darkColor:"#70203c",shape:"elongated",  coreScale:1.05,particleSpread:1.0, connectionDensity:0.35,gravityY:0.4,  tiltX:0,    pulseSpeed:3.5, pulseAmplitude:0.13 },
};

export function getCfg(emotion: string) {
  const key = emotion.toLowerCase().replace(/[^a-z]/g,"");
  return EMOTION_CONFIG[key] || EMOTION_CONFIG["calmness"];
}

// Derive base/light/dark from a blended emotion color (App sends primary+secondary
// mix). Empty color → fall back to the emotion table's own colors. THREE.Color
// parses both "#rrggbb" and "rgb(...)". light lifts toward white, dark sinks
// toward black — same ratios the whole orb was tuned against.
export function deriveBlendColors(
  emotionColor: string,
  fallback: { color: string; lightColor: string; darkColor: string },
): { color: string; lightColor: string; darkColor: string } {
  if (!emotionColor) {
    return { color: fallback.color, lightColor: fallback.lightColor, darkColor: fallback.darkColor };
  }
  const ecBase = new THREE.Color(emotionColor);
  return {
    color: `#${ecBase.getHexString()}`,
    lightColor: `#${ecBase.clone().lerp(new THREE.Color("#ffffff"), 0.42).getHexString()}`,
    darkColor: `#${ecBase.clone().lerp(new THREE.Color("#000000"), 0.55).getHexString()}`,
  };
}

// State-overlay colors. We blend the emotion color toward these by an
// override-strength factor so the orb still reads as "Iris in mood X" but
// also clearly signals what she's doing right now.
export const STATE_TINT = {
  thinking: new THREE.Color("#7a5dfc"),  // electric blue/purple
  listening: new THREE.Color("#3ee68f"), // calm green
  speaking: new THREE.Color("#ffb060"),  // warm amber
  attentive: new THREE.Color("#00ffcc"), // cyan — alert, ready
  offline: new THREE.Color("#404858"),
  sleeping: new THREE.Color("#0a1530"),  // deep midnight blue — emotion-agnostic during sleep
  waking: new THREE.Color("#5a7ad0"),    // dawn blue — brightening pulse during wake transition
  pointing: new THREE.Color("#ffeb3b"),  // bright yellow — Iris is targeting a desktop element (cu_click preview)
};
