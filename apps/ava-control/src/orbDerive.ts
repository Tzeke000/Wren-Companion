// Shared orb derivation (2026-10-06). The main window AND the widget compute the orb's emotion, colour,
// state and sleep visuals HERE, so they cannot disagree. Zeke: "make sure that the widget mirrors. you
// wouldn't want to be joy and the widget says you're calm". (The widget used to read
// snap.perception.emotion_label, which does not exist, so it was always calmness blue.)

/** The listening/attentive soft-square morph, on or off for BOTH windows (was App-only: the widget
 *  defaulted it on while the main view had it off, so the two shapes disagreed). */
export const CUBE_MORPH_ENABLED = false;

type Rec = Record<string, unknown>;
const rec = (v: unknown): Rec | null => (v && typeof v === "object" ? (v as Rec) : null);

export type EmotionVisual = {
  color: string;
  shape:
    | "circle" | "infinity" | "rings" | "teardrop" | "jagged" | "spiral"
    | "flicker" | "awe_pop" | "heart" | "tall"
    // Phase 56 new shapes
    | "cube" | "prism" | "cylinder" | "double_helix" | "burst"
    | "contracted_tremor" | "rising" | "pointer" | string;
  pulse: "idle" | "thinking" | "deep" | "speaking" | "bored" | "excited" | "confused" | "offline" | "listening";
};

export function mixHex(a: string, b: string, t: number): string {
  // Linear blend of two #rrggbb colors; t=0 -> a, t=1 -> b. Used to tint the orb
  // toward a secondary emotion so a MIX of feelings (e.g. calm + curious) shows as a
  // blend instead of only the top emotion's color.
  const f = Math.max(0, Math.min(1, t));
  const pa = a.replace("#", "").trim();
  const pb = b.replace("#", "").trim();
  const na = Number.parseInt(pa.slice(0, 6), 16);
  const nb = Number.parseInt(pb.slice(0, 6), 16);
  if (!Number.isFinite(na) || !Number.isFinite(nb)) return a;
  const lerp = (x: number, y: number) =>
    Math.max(0, Math.min(255, Math.round(x + (y - x) * f)));
  const r = lerp((na >> 16) & 255, (nb >> 16) & 255);
  const g = lerp((na >> 8) & 255, (nb >> 8) & 255);
  const bl = lerp(na & 255, nb & 255);
  return `#${((1 << 24) | (r << 16) | (g << 8) | bl).toString(16).slice(1)}`;
}

export function shadeHex(hex: string, factor: number): string {
  const clean = hex.replace("#", "").trim();
  const full = clean.length === 3 ? clean.split("").map((c) => `${c}${c}`).join("") : clean;
  const n = Number.parseInt(full.slice(0, 6), 16);
  if (!Number.isFinite(n)) return hex;
  const r = Math.max(0, Math.min(255, Math.round(((n >> 16) & 255) * factor)));
  const g = Math.max(0, Math.min(255, Math.round(((n >> 8) & 255) * factor)));
  const b = Math.max(0, Math.min(255, Math.round((n & 255) * factor)));
  return `rgb(${r}, ${g}, ${b})`;
}

export const EMOTION_VISUALS: Record<string, EmotionVisual> = {
  calmness: { color: "#1a6cf5", shape: "circle", pulse: "idle" },
  joy: { color: "#f5c518", shape: "rings", pulse: "excited" },
  happiness: { color: "#f5c518", shape: "rings", pulse: "excited" },
  excitement: { color: "#ff6b00", shape: "rings", pulse: "excited" },
  curiosity: { color: "#00d4d4", shape: "spiral", pulse: "thinking" },
  interest: { color: "#00d4d4", shape: "spiral", pulse: "thinking" },
  boredom: { color: "#4a5568", shape: "infinity", pulse: "bored" },
  frustration: { color: "#e53e3e", shape: "jagged", pulse: "deep" },
  sadness: { color: "#553c9a", shape: "teardrop", pulse: "bored" },
  anger: { color: "#c53030", shape: "jagged", pulse: "deep" },
  fear: { color: "#44337a", shape: "flicker", pulse: "confused" },
  anxiety: { color: "#44337a", shape: "flicker", pulse: "confused" },
  surprise: { color: "#d53f8c", shape: "awe_pop", pulse: "excited" },
  trust: { color: "#38a169", shape: "circle", pulse: "idle" },
  sympathy: { color: "#38a169", shape: "circle", pulse: "idle" },
  anticipation: { color: "#d69e2e", shape: "rings", pulse: "thinking" },
  disgust: { color: "#2f855a", shape: "jagged", pulse: "deep" },
  love: { color: "#ed64a6", shape: "heart", pulse: "speaking" },
  affection: { color: "#ed64a6", shape: "heart", pulse: "speaking" },
  adoration: { color: "#ed64a6", shape: "heart", pulse: "speaking" },
  pride: { color: "#6b46c1", shape: "tall", pulse: "thinking" },
  triumph: { color: "#ecc94b", shape: "tall", pulse: "excited" },
  shame: { color: "#b7791f", shape: "teardrop", pulse: "bored" },
  guilt: { color: "#2d3748", shape: "teardrop", pulse: "bored" },
  envy: { color: "#68d391", shape: "flicker", pulse: "confused" },
  contempt: { color: "#4a5568", shape: "jagged", pulse: "deep" },
  awe: { color: "#4299e1", shape: "awe_pop", pulse: "thinking" },
  relief: { color: "#81e6d9", shape: "circle", pulse: "idle" },
  nostalgia: { color: "#d4a574", shape: "teardrop", pulse: "bored" },
  hope: { color: "#f6e05e", shape: "rings", pulse: "thinking" },
  loneliness: { color: "#2c5282", shape: "teardrop", pulse: "bored" },
  confusion: { color: "#9f7aea", shape: "flicker", pulse: "confused" },
  confidence: { color: "#ecc94b", shape: "tall", pulse: "speaking" },
  contentment: { color: "#68d391", shape: "circle", pulse: "idle" },
  // Phase 56 compound mappings
  logical: { color: "#4299e1", shape: "cube", pulse: "thinking" },
  analyzing: { color: "#00d4d4", shape: "prism", pulse: "thinking" },
  neutral: { color: "#a0aec0", shape: "cylinder", pulse: "idle" },
  realization: { color: "#f5c518", shape: "burst", pulse: "excited" },
  scared: { color: "#44337a", shape: "contracted_tremor", pulse: "confused" },
  proud: { color: "#6b46c1", shape: "rising", pulse: "thinking" },
  // Emotions mood_core can produce that were MISSING here (so they fell back to
  // calmness-blue — including satisfaction/admiration/amusement, which my affect-nudges
  // produce often). Colors mirror OrbCanvas's internal table for consistency; multi-word
  // keys match the exact lowercase names mood_core emits.
  satisfaction: { color: "#48bb78", shape: "circle", pulse: "speaking" },
  admiration: { color: "#5a67d8", shape: "circle", pulse: "thinking" },
  amusement: { color: "#f6ad55", shape: "rings", pulse: "excited" },
  annoyance: { color: "#dd6b20", shape: "jagged", pulse: "deep" },
  distress: { color: "#2c7a7b", shape: "flicker", pulse: "confused" },
  horror: { color: "#742a2a", shape: "flicker", pulse: "confused" },
  "aesthetic appreciation": { color: "#9f7aea", shape: "awe_pop", pulse: "thinking" },
  "empathetic pain": { color: "#6b6b9a", shape: "teardrop", pulse: "bored" },
  "sexual desire": { color: "#b83280", shape: "heart", pulse: "speaking" },
  craving: { color: "#c2548a", shape: "rings", pulse: "thinking" },
  entrancement: { color: "#8a6fd0", shape: "spiral", pulse: "thinking" },
  awkwardness: { color: "#c98a5a", shape: "flicker", pulse: "confused" },
  romance: { color: "#ed64a6", shape: "heart", pulse: "speaking" },
};

/** Primary emotion + the blended colour (primary tinted toward the strongest secondary, capped 0.45,
 *  quantized to 0.15 steps so it recolours in discrete jumps). Dims when the internet is down. */
export function deriveOrbEmotion(snap: Rec | null, opts: { backendShutdown?: boolean; connOffline?: boolean } = {}) {
  const mood = rec(snap?.mood);
  const primaryEmotion = String(mood?.primary_emotion ?? "calmness").toLowerCase();
  const orbVisual = EMOTION_VISUALS[primaryEmotion] ?? EMOTION_VISUALS.calmness;
  const secondaryEmotions = Array.isArray(mood?.secondary_emotions) ? (mood?.secondary_emotions as Rec[]) : [];
  const secName = String(secondaryEmotions[0]?.emotion ?? "").toLowerCase();
  const secVisual = secName ? EMOTION_VISUALS[secName] : undefined;
  const secIntensity = Number(secondaryEmotions[0]?.intensity ?? 0);
  const blendT = Math.round(Math.min(0.45, secIntensity) / 0.15) * 0.15;
  const orbBaseColor = secVisual && blendT > 0 ? mixHex(orbVisual.color, secVisual.color, blendT) : orbVisual.color;
  const conn = rec(snap?.connectivity);
  const connOffline = opts.connOffline ?? (conn ? !conn.online : false);
  const effectiveOrbColor = opts.backendShutdown ? "#6b7280" : connOffline ? shadeHex(orbBaseColor, 0.72) : orbBaseColor;
  return { primaryEmotion, orbVisual, secondaryEmotions, orbBaseColor, effectiveOrbColor };
}

/** Sleep cycle visuals. Case-insensitive: the backend reports "awake"/"sleeping" in lower case, and the
 *  old upper-case-only check meant the orb never showed sleep. */
export function deriveOrbSleep(snap: Rec | null) {
  const sleep = rec(rec(snap?.subsystem_health)?.sleep);
  const sleepState = String(sleep?.state || "AWAKE").toUpperCase();
  const sleepProgress = Number(sleep?.progress || 0);
  const sleepRemainingSeconds = Number(sleep?.remaining_seconds || 0);
  const wakeStartedTs = Number(sleep?.wake_started_ts || 0);
  const wakeEstimateS = Number(sleep?.wake_estimate_s || 5);
  const wakeProgress = wakeStartedTs > 0
    ? Math.max(0, Math.min(1, (Date.now() / 1000 - wakeStartedTs) / Math.max(0.5, wakeEstimateS)))
    : 0;
  return { sleepState, sleepProgress, sleepRemainingSeconds, wakeProgress,
    isSleepingOrWaking: sleepState === "SLEEPING" || sleepState === "WAKING" };
}

/** The orb's state ladder, identical for both windows. Window-local signals (the main window's own chat
 *  send / mic / fast TTS poll) are passed in; the widget passes what it can see. */
export function deriveOrbState(snap: Rec | null, o: {
  online: boolean; fallbackPulse: string; backendShutdown?: boolean; ttsSpeaking?: boolean;
  chatThinking?: boolean; sttListening?: boolean; deepMode?: boolean;
}): string {
  const { sleepState } = deriveOrbSleep(snap);
  const vl = rec(snap?.voice_loop);
  const vlActive = Boolean(vl?.active);
  const vlState = String(vl?.state ?? "passive");
  const tts = rec(snap?.tts);
  const speaking = Boolean(o.ttsSpeaking) || Boolean(tts?.tts_speaking);
  if (sleepState === "SLEEPING") return "sleeping";
  if (sleepState === "WAKING") return "waking";
  if (o.backendShutdown || !o.online || !snap) return "offline";
  if (snap.thinking) return "thinking";
  if (Number(snap.thinking_tier ?? 0) >= 3) return "thinking";
  if (vlActive && vlState === "speaking") return "speaking";
  if (vlActive && vlState === "thinking") return "thinking";
  if (vlActive && vlState === "listening") return "listening";
  if (vlActive && vlState === "attentive") return "attentive";
  if (speaking) return "speaking";
  // (Removed 2026-10-06: `tts.enabled ? "speaking"` — inherited from the Ava fork; in this snapshot
  //  `enabled` just means voice is ON, so the orb showed "speaking" while silent.)
  if (o.chatThinking) return o.deepMode ? "deep" : "thinking";
  if (o.sttListening) return "listening";
  return o.fallbackPulse;
}
