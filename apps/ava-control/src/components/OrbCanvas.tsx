import { memo, useEffect, useRef } from "react";
import * as THREE from "three";

type OrbState = "idle" | "thinking" | "deep" | "speaking" | "bored" | "excited" | "offline" | "listening" | "attentive" | "sleeping" | "waking" | "pointing";

interface OrbProps {
  emotion: string;
  emotionColor: string;
  state: OrbState;
  size?: number;
  /** Phase 49: override shape for pointer morph */
  shapeOverride?: string;
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

const EMOTION_CONFIG: Record<string, {
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

function getCfg(emotion: string) {
  const key = emotion.toLowerCase().replace(/[^a-z]/g,"");
  return EMOTION_CONFIG[key] || EMOTION_CONFIG["calmness"];
}

function createGlowTex(color: string): THREE.Texture {
  const c = document.createElement("canvas");
  c.width=128; c.height=128;
  const ctx = c.getContext("2d")!;
  const col = new THREE.Color(color);
  const g = ctx.createRadialGradient(64,64,0,64,64,64);
  g.addColorStop(0,`rgba(${Math.round(col.r*255)},${Math.round(col.g*255)},${Math.round(col.b*255)},1)`);
  g.addColorStop(0.4,`rgba(${Math.round(col.r*255)},${Math.round(col.g*255)},${Math.round(col.b*255)},0.4)`);
  g.addColorStop(1,"rgba(0,0,0,0)");
  ctx.fillStyle=g; ctx.fillRect(0,0,128,128);
  return new THREE.CanvasTexture(c);
}

// State-overlay colors. We blend the emotion color toward these by an
// override-strength factor so the orb still reads as "Ava in mood X" but
// also clearly signals what she's doing right now.
const STATE_TINT = {
  thinking: new THREE.Color("#7a5dfc"),  // electric blue/purple
  listening: new THREE.Color("#3ee68f"), // calm green
  speaking: new THREE.Color("#ffb060"),  // warm amber
  attentive: new THREE.Color("#00ffcc"), // cyan — alert, ready
  offline: new THREE.Color("#404858"),
  sleeping: new THREE.Color("#0a1530"),  // deep midnight blue — emotion-agnostic during sleep
  waking: new THREE.Color("#5a7ad0"),    // dawn blue — brightening pulse during wake transition
  pointing: new THREE.Color("#ffeb3b"),  // bright yellow — Ava is targeting a desktop element (cu_click preview)
};

function OrbCanvasInner({ emotion, state, size = 320, shapeOverride, amplitude = 0, energy = 0.5, recenterTrigger, cubeMorphEnabled = true, sleepProgress = 0, sleepRemainingSeconds = 0, wakeProgress = 0 }: OrbProps) {
  const mountRef = useRef<HTMLDivElement>(null);
  const disposeRef = useRef<()=>void>(()=>{});

  // Live refs — update on every render so the animation loop reads the latest
  // value without remounting the whole Three.js scene.
  const stateRef = useRef<OrbState>(state);
  const amplitudeRef = useRef<number>(amplitude);
  const emotionRef = useRef<string>(emotion);
  const shapeOverrideRef = useRef<string | undefined>(shapeOverride);
  const energyRef = useRef<number>(energy);
  // listening/attentive cube morph: target 1.0 when listening, 0.0 otherwise.
  // The animate loop eases this toward the target so the morph is smooth.
  const morphRef = useRef<number>(0);
  // Recenter signal: animate loop reads `recenterTriggerRef`, compares with
  // its own last-seen value, and on change captures a clock-time start point
  // to drive the eased return to (0,0). This avoids piping the trigger
  // through React effects (which would tear down/rebuild the scene).
  const recenterTriggerRef = useRef<number | undefined>(recenterTrigger);
  const cubeMorphEnabledRef = useRef<boolean>(cubeMorphEnabled);
  // Sleep state refs — driven by parent snapshot polling.
  const sleepProgressRef = useRef<number>(sleepProgress);
  const sleepRemainingRef = useRef<number>(sleepRemainingSeconds);
  const wakeProgressRef = useRef<number>(wakeProgress);
  stateRef.current = state;
  amplitudeRef.current = amplitude;
  emotionRef.current = emotion;
  shapeOverrideRef.current = shapeOverride;
  energyRef.current = energy;
  recenterTriggerRef.current = recenterTrigger;
  cubeMorphEnabledRef.current = cubeMorphEnabled;
  sleepProgressRef.current = sleepProgress;
  sleepRemainingRef.current = sleepRemainingSeconds;
  wakeProgressRef.current = wakeProgress;

  useEffect(() => {
    if (!mountRef.current) return;
    disposeRef.current();
    const container = mountRef.current;
    const cfgInit = { ...getCfg(emotion), ...(shapeOverride ? { shape: shapeOverride } : {}) };

    const renderer = new THREE.WebGLRenderer({ antialias:true, alpha:true });
    renderer.setSize(size,size);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio,2));
    renderer.setClearColor(0x000000,0);
    container.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(70,1,0.01,100);
    camera.position.z = 2.8;

    // rootGroup holds all orb sub-groups so we can apply breathing scale +
    // gentle drift in one place without affecting per-frame morph maths.
    const rootGroup = new THREE.Group();
    scene.add(rootGroup);
    const innerGroup = new THREE.Group();
    const outerGroup = new THREE.Group();
    const streamGroup = new THREE.Group();
    rootGroup.add(innerGroup,outerGroup,streamGroup);

    // Core
    const coreGeo = new THREE.SphereGeometry(0.15,32,32);
    const coreMat = new THREE.MeshBasicMaterial({ color:new THREE.Color(cfgInit.lightColor), transparent:true, opacity:0.95, blending:THREE.AdditiveBlending, depthWrite:false });
    const core = new THREE.Mesh(coreGeo,coreMat);
    innerGroup.add(core);

    const igGeo = new THREE.SphereGeometry(0.3,16,16);
    const igMat = new THREE.MeshBasicMaterial({ color:new THREE.Color(cfgInit.color), transparent:true, opacity:0.3, blending:THREE.AdditiveBlending, depthWrite:false });
    const innerGlow = new THREE.Mesh(igGeo,igMat);
    innerGroup.add(innerGlow);

    // Streams
    const streamPhases: number[] = [];
    const streams: THREE.Line[] = [];
    for(let i=0;i<16;i++){
      const pts: THREE.Vector3[] = [];
      const cp = [
        new THREE.Vector3(0,0,0),
        new THREE.Vector3((Math.random()-.5)*1.2,(Math.random()-.5)*1.2,(Math.random()-.5)*1.2),
        new THREE.Vector3((Math.random()-.5)*1.4,(Math.random()-.5)*1.4,(Math.random()-.5)*1.4),
        new THREE.Vector3((Math.random()-.5)*1.0,(Math.random()-.5)*1.0,(Math.random()-.5)*1.0),
        new THREE.Vector3(0,0,0),
      ];
      const curve = new THREE.CatmullRomCurve3(cp);
      for(let j=0;j<=60;j++) pts.push(curve.getPoint(j/60));
      const geo = new THREE.BufferGeometry().setFromPoints(pts);
      const mat = new THREE.LineBasicMaterial({ color:new THREE.Color(i<8?cfgInit.lightColor:cfgInit.color), transparent:true, opacity:0.4+Math.random()*0.4, blending:THREE.AdditiveBlending, depthWrite:false });
      const line = new THREE.Line(geo,mat);
      line.rotation.set(Math.random()*Math.PI*2,Math.random()*Math.PI*2,Math.random()*Math.PI*2);
      streamGroup.add(line); streams.push(line); streamPhases.push(Math.random()*Math.PI*2);
    }

    // Particles
    const N = 1500;
    const pos = new Float32Array(N*3);
    const orig = new Float32Array(N*3);
    const vel = new Float32Array(N*3);
    const pcol = new Float32Array(N*3);
    const baseC = new THREE.Color(cfgInit.color);
    const lightC = new THREE.Color(cfgInit.lightColor);
    const darkC = new THREE.Color(cfgInit.darkColor);

    for(let i=0;i<N;i++){
      const tier = Math.random();
      const r = tier<0.25 ? 0.05+Math.random()*0.35 : tier<0.65 ? 0.35+Math.random()*0.35 : 0.7+Math.random()*0.3;
      const theta = Math.random()*Math.PI*2;
      const phi = Math.acos(2*Math.random()-1);
      const x=r*Math.sin(phi)*Math.cos(theta), y=r*Math.sin(phi)*Math.sin(theta), z=r*Math.cos(phi);
      pos[i*3]=x; pos[i*3+1]=y; pos[i*3+2]=z;
      orig[i*3]=x; orig[i*3+1]=y; orig[i*3+2]=z;
      vel[i*3]=(Math.random()-.5)*0.001; vel[i*3+1]=(Math.random()-.5)*0.001; vel[i*3+2]=(Math.random()-.5)*0.001;
      const c = tier<0.25?lightC:tier<0.65?baseC:darkC;
      pcol[i*3]=c.r; pcol[i*3+1]=c.g; pcol[i*3+2]=c.b;
    }

    const pGeo = new THREE.BufferGeometry();
    pGeo.setAttribute("position",new THREE.BufferAttribute(pos,3));
    pGeo.setAttribute("color",new THREE.BufferAttribute(pcol,3));
    const pMat = new THREE.PointsMaterial({ size:0.025, vertexColors:true, transparent:true, opacity:0.85, blending:THREE.AdditiveBlending, depthWrite:false, sizeAttenuation:true });
    innerGroup.add(new THREE.Points(pGeo,pMat));

    // Shell
    const shellGeo = new THREE.SphereGeometry(1.0,16,12);
    const shellMat = new THREE.MeshBasicMaterial({ color:new THREE.Color(cfgInit.darkColor), wireframe:true, transparent:true, opacity:0.08, blending:THREE.AdditiveBlending, depthWrite:false });
    outerGroup.add(new THREE.Mesh(shellGeo,shellMat));

    // Halo
    const haloTex = createGlowTex(cfgInit.color);
    const haloMat = new THREE.SpriteMaterial({ map:haloTex, transparent:true, opacity:0.2, blending:THREE.AdditiveBlending, depthWrite:false });
    const halo = new THREE.Sprite(haloMat);
    halo.scale.set(3.5,3.5,1);
    rootGroup.add(halo);

    const pLight = new THREE.PointLight(new THREE.Color(cfgInit.color),2.0,5);
    rootGroup.add(pLight);

    const clock = new THREE.Clock();
    let fid = 0;
    // Recenter animation state — captured here so the animate loop can read
    // it without going through React. `lastSeenRecenterTrigger` is the
    // trigger value we last reacted to; `recenterStartT` is the clock time
    // when we kicked off the easing.
    let lastSeenRecenterTrigger: number | undefined = recenterTriggerRef.current;
    let recenterStartT = -Infinity;
    const RECENTER_DURATION = 0.32;  // seconds — matches the CSS pulse

    // Reusable color scratch — avoid allocating per frame.
    const _coreScratch = new THREE.Color();
    const _glowScratch = new THREE.Color();
    const _lightScratch = new THREE.Color();

    function morph(t: number, currentState: OrbState, currentAmp: number) {
      const c = getCfg(emotionRef.current);
      const sp = c.particleSpread, gy = c.gravityY, tx = c.tiltX;
      const shape = shapeOverrideRef.current || c.shape;
      const pa = pGeo.attributes.position as THREE.BufferAttribute;
      // Listening cube morph factor (0..1). Eased toward target in animate().
      const cubeMorph = morphRef.current;

      // Per-state amplitude/scale envelopes — read live each frame.
      const speakingScale = currentState === "speaking" ? (1 + currentAmp * 0.35) : 1;
      const listeningInBreath = currentState === "listening" ? (0.92 + Math.sin(t * 1.3) * 0.04) : 1;
      const thinkingScale = currentState === "thinking" || currentState === "deep" ? (0.96 + Math.sin(t * 6) * 0.04) : 1;

      for(let i=0;i<N;i++){
        const ox=orig[i*3], oy=orig[i*3+1], oz=orig[i*3+2];
        const dist=Math.sqrt(ox*ox+oy*oy+oz*oz);
        let mx=ox*sp, my=oy*sp, mz=oz*sp;
        if(shape==="teardrop"){ my-=Math.max(0,-oy)*0.4; my+=gy*dist*0.5; }
        else if(shape==="elongated"){ my*=1.4; my+=gy*0.3; }
        else if(shape==="compressed"){ my*=0.7; }
        else if(shape==="contracted"){ mx*=0.6; my*=0.6; mz*=0.6; }
        else if(shape==="scattered"){ const f=1+Math.sin(t*0.5+dist*3)*0.15; mx*=f; my*=f; mz*=f; }
        else if(shape==="double"){ if(oy>0){mx+=0.15;my+=0.1;}else{mx-=0.15;my-=0.05;} }
        else if(shape==="spiral"){ const a=t*0.3+dist*2; const nx=mx*Math.cos(a)-mz*Math.sin(a); mz=mx*Math.sin(a)+mz*Math.cos(a); mx=nx; }
        else if(shape==="pointer"){
          if(oy>0){ my*=2.2; mx*=Math.max(0.1, 1.0-oy*1.2); mz*=Math.max(0.1, 1.0-oy*1.2); }
          else{ mx*=0.5; my*=0.4; mz*=0.5; }
        }
        else if(shape==="cube"){
          mx = Math.sign(mx)*(Math.abs(mx)<0.5?Math.abs(mx):Math.abs(mx)*0.9+0.1*Math.sign(mx));
          my = Math.sign(my)*(Math.abs(my)<0.5?Math.abs(my):Math.abs(my)*0.9+0.1*Math.sign(my));
          mz = Math.sign(mz)*(Math.abs(mz)<0.5?Math.abs(mz):Math.abs(mz)*0.9+0.1*Math.sign(mz));
        }
        else if(shape==="prism"){
          const ang = Math.floor(Math.atan2(ox,oz)/(Math.PI*2/3))*Math.PI*2/3;
          const r2 = Math.sqrt(ox*ox+oz*oz); mx=r2*Math.sin(ang+t*0.5)*sp; mz=r2*Math.cos(ang+t*0.5)*sp;
        }
        else if(shape==="cylinder"){
          const r3 = Math.sqrt(ox*ox+oz*oz); mx=r3*Math.cos(Math.atan2(oz,ox))*sp; mz=r3*Math.sin(Math.atan2(oz,ox))*sp;
        }
        else if(shape==="infinity"){
          const u = Math.atan2(oy,ox); mx=sp*Math.cos(u)/(1+Math.sin(u)*Math.sin(u)); my=sp*Math.sin(u)*Math.cos(u)/(1+Math.sin(u)*Math.sin(u)); mz*=0.3;
        }
        else if(shape==="double_helix"){
          const strand = i%2===0?1:-1; const ang2=t*0.5+dist*4+strand*Math.PI; mx=0.5*Math.cos(ang2)*sp; mz=0.5*Math.sin(ang2)*sp;
        }
        else if(shape==="burst"){
          const phase = (Math.sin(t*1.5)*0.5+0.5); const scale = 1+phase*1.5; mx*=scale; my*=scale; mz*=scale;
        }
        else if(shape==="contracted_tremor"){
          mx*=0.4; my*=0.4; mz*=0.4;
          mx+=Math.sin(t*15+i*0.3)*0.05; my+=Math.cos(t*17+i*0.5)*0.05;
        }
        else if(shape==="rising"){
          my*=1.6; my+=gy*0.5; mx*=0.7; mz*=0.7;
        }
        my+=gy*0.2;
        mz+=tx*oy*0.3;
        mx+=vel[i*3]*30; my+=vel[i*3+1]*30; mz+=vel[i*3+2]*30;

        // ── Voice-state overlays ────────────────────────────────────────────
        if(currentState==="speaking"){
          // Amplitude-driven outward burst wave per-particle
          const wave = Math.sin(t * (4 + currentAmp * 8) + dist * 6) * currentAmp * 0.35;
          const wScale = 1 + wave;
          mx *= wScale * speakingScale;
          my *= wScale * speakingScale;
          mz *= wScale * speakingScale;
        } else if(currentState==="listening"){
          // Particles drift inward toward center, gentle breathing scale.
          const inward = 0.85 + Math.sin(t * 1.5 + dist * 4) * 0.06;
          mx *= inward * listeningInBreath;
          my *= inward * listeningInBreath;
          mz *= inward * listeningInBreath;
        } else if(currentState==="thinking" || currentState==="deep"){
          // Faster orbital motion + tight rapid pulse
          const fast = 1 + Math.sin(t * 8 + dist * 5) * 0.04;
          mx *= fast * thinkingScale;
          my *= fast * thinkingScale;
          mz *= fast * thinkingScale;
        }

        // ── Listening soft-cube morph ─────────────────────────────────────
        // Blend each particle toward a rounded-cube surface using an Lp-norm
        // projection (p=8 ≈ soft cube). At cubeMorph=1 the cloud reads as a
        // gentle cube; at 0 it stays a sphere. All other shape/state work
        // above is preserved so breathing/drift/color logic continue to apply.
        if (cubeMorph > 0.001) {
          const r = Math.sqrt(mx*mx + my*my + mz*mz);
          if (r > 1e-5) {
            const ux = mx / r, uy = my / r, uz = mz / r;
            const p = 8;
            const lp = Math.pow(
              Math.pow(Math.abs(ux), p) +
              Math.pow(Math.abs(uy), p) +
              Math.pow(Math.abs(uz), p),
              1 / p
            ) || 1e-6;
            const scf = 1 / lp;
            const cx = ux * scf * r;
            const cy = uy * scf * r;
            const cz = uz * scf * r;
            mx = mx + (cx - mx) * cubeMorph;
            my = my + (cy - my) * cubeMorph;
            mz = mz + (cz - mz) * cubeMorph;
          }
        }

        pa.setXYZ(i,mx,my,mz);
      }
      pa.needsUpdate=true;
    }

    function spd(s: OrbState) {
      return ({thinking:2.5,deep:5.0,speaking:1.8,bored:0.3,excited:7.0,offline:0.1,idle:1.0,listening:0.8,attentive:1.2,sleeping:0.3,waking:1.5} as Record<string, number>)[s] || 1.0;
    }

    // ── Sleep visuals (initially hidden; toggle on state) ────────────────
    // 4-6 floating "z" sprites orbiting the orb during SLEEPING.
    function buildZTexture(): THREE.CanvasTexture {
      const cnv = document.createElement("canvas");
      cnv.width = 64; cnv.height = 64;
      const ctx = cnv.getContext("2d")!;
      ctx.fillStyle = "rgba(0,0,0,0)";
      ctx.fillRect(0, 0, 64, 64);
      ctx.font = "bold 48px sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = "rgba(180, 200, 240, 0.85)";
      ctx.fillText("z", 32, 32);
      return new THREE.CanvasTexture(cnv);
    }
    const zTexture = buildZTexture();
    const zSprites: THREE.Sprite[] = [];
    const Z_COUNT = 5;
    for (let i = 0; i < Z_COUNT; i++) {
      const mat = new THREE.SpriteMaterial({
        map: zTexture,
        transparent: true,
        opacity: 0,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      });
      const sp = new THREE.Sprite(mat);
      const phase = (i / Z_COUNT) * Math.PI * 2;
      sp.position.set(Math.cos(phase) * 0.9, 0.3 + (i % 2) * 0.2, Math.sin(phase) * 0.9);
      sp.scale.set(0.18, 0.18, 1);
      sp.visible = false;
      scene.add(sp);
      zSprites.push(sp);
    }

    // Progress ring around the orb showing sleep cycle progression 0→1.
    const ringGeo = new THREE.RingGeometry(1.05, 1.15, 64, 1, 0, Math.PI * 2);
    const ringMat = new THREE.MeshBasicMaterial({
      color: 0xffd060,  // muted gold
      side: THREE.DoubleSide,
      transparent: true,
      opacity: 0,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    const progressRing = new THREE.Mesh(ringGeo, ringMat);
    progressRing.rotation.x = -Math.PI / 2;
    progressRing.visible = false;
    scene.add(progressRing);

    // Wake glow ring — expands and fades during WAKING.
    const wakeRingGeo = new THREE.RingGeometry(1.0, 1.05, 64, 1);
    const wakeRingMat = new THREE.MeshBasicMaterial({
      color: 0xa0c0ff,
      side: THREE.DoubleSide,
      transparent: true,
      opacity: 0,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    const wakeRing = new THREE.Mesh(wakeRingGeo, wakeRingMat);
    wakeRing.rotation.x = -Math.PI / 2;
    wakeRing.visible = false;
    scene.add(wakeRing);

    function animate(){
      fid=requestAnimationFrame(animate);
      const t=clock.getElapsedTime();
      const liveState = stateRef.current;
      const liveAmp = amplitudeRef.current;
      const liveEmotion = emotionRef.current;
      const liveEnergy = Math.max(0, Math.min(1, energyRef.current));
      const c=getCfg(liveEmotion);
      const s=spd(liveState);
      const r=s*0.001;

      // ── Cube-morph easing (listening/attentive only) ─────────────────────
      // Target = 1 while listening/attentive, 0 otherwise. Per-frame lerp at
      // ~0.05 reaches ~95% in ~700ms at 60fps which matches the spec.
      // When the cube-morph is disabled (PRESENCE_V2 off), pin target to 0
      // so morphRef eases to 0 and stays there — orb keeps its baseline
      // shape regardless of state.
      const morphTarget = cubeMorphEnabledRef.current && (liveState === "listening" || liveState === "attentive") ? 1.0 : 0.0;
      morphRef.current += (morphTarget - morphRef.current) * 0.05;
      if (morphRef.current < 0.001) morphRef.current = 0;
      else if (morphRef.current > 0.999) morphRef.current = 1;
      const cubeFactor = morphRef.current;

      // ── Breathing (always-on) ─────────────────────────────────────────────
      // Period shrinks with energy (0 → 3.5s, 1 → 2.0s). Attentive shaves 0.3s
      // off the period so the orb feels slightly more alert without being busy.
      let breathPeriod = 3.5 - liveEnergy * 1.5;
      if (liveState === "attentive") breathPeriod = Math.max(1.2, breathPeriod - 0.3);
      // When the cube is fully formed, slow breathing slightly — she feels
      // attentive and steady, not jittery.
      breathPeriod = breathPeriod + cubeFactor * 0.6;
      const breathAmp = 0.03 * (1 - cubeFactor * 0.4);
      const breath = 1 + Math.sin((t / breathPeriod) * Math.PI * 2) * breathAmp;

      // ── Recenter signal (middle-click) ───────────────────────────────────
      // Detect a fresh middle-click from the parent component. On change,
      // capture clock-time so we can ease drift back to zero over ~320ms.
      const liveTrigger = recenterTriggerRef.current;
      if (liveTrigger !== lastSeenRecenterTrigger) {
        lastSeenRecenterTrigger = liveTrigger;
        recenterStartT = t;
      }
      // recenterFactor: 1.0 at start of pulse, 0.0 after RECENTER_DURATION.
      // While > 0 we scale drift down toward zero so the orb snaps to center.
      let recenterFactor = 0;
      const recenterAge = t - recenterStartT;
      if (recenterAge >= 0 && recenterAge < RECENTER_DURATION) {
        const k = recenterAge / RECENTER_DURATION;             // 0..1
        const eased = 1 - Math.pow(1 - k, 3);                   // easeOutCubic
        recenterFactor = 1 - eased;                              // 1..0
      }

      // ── Drift (always-on) ─────────────────────────────────────────────────
      // Two superimposed sines on each axis so the motion never repeats cleanly.
      // Amplitude is in scene units (camera at z=2.8); these translate to ~6-12
      // pixels at the canvas size, matching the user's spec of ~8/6 px.
      // Drift amplitude is reduced when the cube is formed so it feels steady.
      // During a recenter pulse, drift is scaled toward 0 so the orb returns
      // visibly to the center of its row.
      const driftScale = 0.012 * (1 - cubeFactor * 0.5) * (1 - recenterFactor);
      const dx = (Math.sin(t * 0.30) * 8 + Math.sin(t * 0.70) * 4) * driftScale;
      const dy = (Math.cos(t * 0.40) * 6 + Math.cos(t * 0.11) * 3) * driftScale;
      rootGroup.position.set(dx, dy, 0);
      rootGroup.scale.setScalar(breath);

      // Rotation rates — thinking/listening/speaking each rotate differently.
      let rotMul = 1.0;
      if (liveState === "thinking" || liveState === "deep") rotMul = 2.5;
      else if (liveState === "speaking") rotMul = 1.0 + liveAmp * 1.5;
      else if (liveState === "listening") rotMul = 0.55;
      innerGroup.rotation.y += r * rotMul;
      innerGroup.rotation.x += r * 0.4 * rotMul;
      outerGroup.rotation.y -= r * 0.7 * rotMul;
      outerGroup.rotation.z += r * 0.3 * rotMul;
      streamGroup.rotation.y += r * 1.2 * rotMul;
      streamGroup.rotation.x -= r * 0.5 * rotMul;

      // Pulse — thinking pulses fast (2Hz), speaking pulses with amplitude.
      let pulseSpeed = c.pulseSpeed;
      let pulseAmp = c.pulseAmplitude;
      if (liveState === "thinking" || liveState === "deep") {
        pulseSpeed = 12.5;  // fast 2Hz
        pulseAmp = 0.18;
      } else if (liveState === "speaking") {
        pulseSpeed = 6 + liveAmp * 6;
        pulseAmp = 0.06 + liveAmp * 0.30;
      } else if (liveState === "listening") {
        pulseSpeed = 1.0;  // slow breathing
        pulseAmp = 0.08;
      }
      const pulse = 1 + Math.sin(t * pulseSpeed) * pulseAmp;
      const stateScaleMul = liveState === "speaking" ? (1 + liveAmp * 0.25)
        : liveState === "listening" ? (0.95 + Math.sin(t * 1.3) * 0.04)
        : 1;
      core.scale.setScalar(pulse * c.coreScale * stateScaleMul);
      innerGlow.scale.setScalar(pulse * c.coreScale * 0.9 * stateScaleMul);
      pLight.intensity = 1.5 + Math.sin(t * pulseSpeed) * 0.5;

      // ── Color overlays per state ──────────────────────────────────────────
      // Blend the emotion's light/base color toward the state tint.
      const baseLight = new THREE.Color(c.lightColor);
      const baseBase = new THREE.Color(c.color);
      _coreScratch.copy(baseLight);
      _glowScratch.copy(baseBase);
      _lightScratch.copy(baseBase);

      if (liveState === "thinking" || liveState === "deep") {
        _coreScratch.lerp(STATE_TINT.thinking, 0.55);
        _glowScratch.lerp(STATE_TINT.thinking, 0.45);
        _lightScratch.lerp(STATE_TINT.thinking, 0.45);
      } else if (liveState === "speaking") {
        // Color temperature shifts warmer with amplitude.
        const warmFactor = Math.min(0.6, 0.20 + liveAmp * 0.6);
        _coreScratch.lerp(STATE_TINT.speaking, warmFactor);
        _glowScratch.lerp(STATE_TINT.speaking, warmFactor * 0.7);
        _lightScratch.lerp(STATE_TINT.speaking, warmFactor * 0.6);
      } else if (liveState === "listening") {
        _coreScratch.lerp(STATE_TINT.listening, 0.30);
        _glowScratch.lerp(STATE_TINT.listening, 0.25);
        _lightScratch.lerp(STATE_TINT.listening, 0.25);
      } else if (liveState === "attentive") {
        // Subtle cyan hint — alert without being busy. Core slightly brighter.
        _coreScratch.lerp(STATE_TINT.attentive, 0.15);
        _glowScratch.lerp(STATE_TINT.attentive, 0.12);
        _lightScratch.lerp(STATE_TINT.attentive, 0.12);
      } else if (liveState === "offline") {
        _coreScratch.lerp(STATE_TINT.offline, 0.6);
        _glowScratch.lerp(STATE_TINT.offline, 0.6);
        _lightScratch.lerp(STATE_TINT.offline, 0.6);
      } else if (liveState === "sleeping") {
        // Sleep: emotion-agnostic deep midnight blue; dim, slow pulse handled below.
        _coreScratch.lerp(STATE_TINT.sleeping, 0.85);
        _glowScratch.lerp(STATE_TINT.sleeping, 0.80);
        _lightScratch.lerp(STATE_TINT.sleeping, 0.75);
      } else if (liveState === "waking") {
        // Wake: blend toward dawn-blue, intensifying with wakeProgress.
        const wp = Math.max(0, Math.min(1, wakeProgressRef.current));
        _coreScratch.lerp(STATE_TINT.waking, 0.5 + wp * 0.3);
        _glowScratch.lerp(STATE_TINT.waking, 0.4 + wp * 0.3);
        _lightScratch.lerp(STATE_TINT.waking, 0.4 + wp * 0.3);
      } else if (liveState === "pointing") {
        // Pointing: bright yellow tint — Ava is targeting a specific desktop
        // element (cu_click target preview). Strong, unmissable. Full Arrow
        // shape geometry deferred to future visual polish; tint signals the
        // state for now. See vault: designs/orb-state-shapes.md.
        _coreScratch.lerp(STATE_TINT.pointing, 0.55);
        _glowScratch.lerp(STATE_TINT.pointing, 0.45);
        _lightScratch.lerp(STATE_TINT.pointing, 0.45);
      }

      // ── Sleep / wake visuals ─────────────────────────────────────────────
      const isSleeping = liveState === "sleeping";
      const isWaking = liveState === "waking";

      // Z-sprites: visible during SLEEPING, fade out during WAKING, hidden otherwise.
      let zTargetOpacity = 0;
      if (isSleeping) zTargetOpacity = 0.85;
      else if (isWaking) zTargetOpacity = Math.max(0, 0.85 - wakeProgressRef.current * 0.85);
      for (let i = 0; i < zSprites.length; i++) {
        const sp = zSprites[i];
        sp.visible = isSleeping || isWaking;
        const mat = sp.material as THREE.SpriteMaterial;
        // Ease toward target opacity at ~0.05 per frame.
        mat.opacity += (zTargetOpacity - mat.opacity) * 0.05;
        if (sp.visible) {
          // Slow orbit + gentle vertical drift.
          const phase = (i / zSprites.length) * Math.PI * 2 + t * 0.15;
          sp.position.x = Math.cos(phase) * (0.9 + Math.sin(t * 0.2 + i) * 0.05);
          sp.position.z = Math.sin(phase) * (0.9 + Math.sin(t * 0.2 + i) * 0.05);
          sp.position.y = 0.3 + Math.sin(t * 0.4 + i) * 0.15;
        }
      }

      // Progress ring: shows during SLEEPING. Fills clockwise based on sleepProgress.
      progressRing.visible = isSleeping;
      if (isSleeping) {
        const sp = Math.max(0, Math.min(1, sleepProgressRef.current));
        // Re-build ring geometry to reflect partial fill.
        // (This is cheap — RingGeometry creates a small index/buffer.)
        progressRing.geometry.dispose();
        progressRing.geometry = new THREE.RingGeometry(1.05, 1.15, 64, 1, -Math.PI / 2, Math.PI * 2 * sp);
        ringMat.opacity = 0.55;
      } else {
        ringMat.opacity = Math.max(0, ringMat.opacity - 0.02);
      }

      // Wake ring: brief expanding-glow during WAKING.
      wakeRing.visible = isWaking;
      if (isWaking) {
        const wp = Math.max(0, Math.min(1, wakeProgressRef.current));
        const ringScale = 1 + wp * 1.5;
        wakeRing.scale.set(ringScale, ringScale, 1);
        wakeRingMat.opacity = (1 - wp) * 0.7;
      } else {
        wakeRingMat.opacity = Math.max(0, wakeRingMat.opacity - 0.05);
      }

      coreMat.color.copy(_coreScratch);
      igMat.color.copy(_glowScratch);
      pLight.color.copy(_lightScratch);

      morph(t, liveState, liveAmp);

      for(let i=0;i<N;i++){
        vel[i*3]+=(Math.random()-.5)*0.00002; vel[i*3+1]+=(Math.random()-.5)*0.00002; vel[i*3+2]+=(Math.random()-.5)*0.00002;
        vel[i*3]*=0.99; vel[i*3+1]*=0.99; vel[i*3+2]*=0.99;
      }
      streams.forEach((l,i)=>{
        const mat=l.material as THREE.LineBasicMaterial;
        mat.opacity=Math.sin(t*s*2+streamPhases[i])>0.7?0.9:0.2+Math.sin(t*s+streamPhases[i])*0.2;
        l.rotation.y+=r*0.5*Math.sin(streamPhases[i]);
      });
      const haloAmpBoost = liveState === "speaking" ? liveAmp * 0.5 : 0;
      halo.scale.set(3.5+Math.sin(t*0.7)*0.3 + haloAmpBoost, 3.5+Math.sin(t*0.7)*0.3 + haloAmpBoost, 1);
      haloMat.opacity = liveState === "offline"
        ? 0.06
        : 0.15 + Math.sin(t*0.5)*0.05 + (liveState === "speaking" ? liveAmp * 0.15 : 0);
      if(liveState==="offline"){ coreMat.opacity=Math.max(0.20,coreMat.opacity-0.001); pMat.opacity=Math.max(0.30,pMat.opacity-0.0005); }
      else { coreMat.opacity = 0.95; pMat.opacity = 0.85; }
      if(liveEmotion==="confusion") innerGroup.rotation.z+=Math.sin(t*3)*0.005;
      renderer.render(scene,camera);
    }
    animate();

    disposeRef.current=()=>{
      cancelAnimationFrame(fid);
      renderer.dispose();
      [coreGeo,igGeo,shellGeo,pGeo].forEach(g=>g.dispose());
      [coreMat,igMat,shellMat,pMat,haloMat].forEach(m=>m.dispose());
      streams.forEach(l=>{ l.geometry.dispose(); (l.material as THREE.Material).dispose(); });
      haloTex.dispose();
      // Sleep visuals.
      zSprites.forEach(sp => {
        scene.remove(sp);
        (sp.material as THREE.SpriteMaterial).dispose();
      });
      zTexture.dispose();
      scene.remove(progressRing);
      progressRing.geometry.dispose();
      ringMat.dispose();
      scene.remove(wakeRing);
      wakeRing.geometry.dispose();
      wakeRingMat.dispose();
      if(container.contains(renderer.domElement)) container.removeChild(renderer.domElement);
    };

    return () => disposeRef.current();
    // Mount per emotion/size — state and amplitude are read via refs so they
    // update live without remounting the scene.
  }, [emotion, size]);

  // Format remaining-seconds for the sleep timer label.
  const showTimer = state === "sleeping" || state === "waking";
  const timerLabel = (() => {
    if (state === "waking") {
      // During WAKING, show "waking…" with rough remaining estimate.
      // wakeProgress reaches 1.0 at the estimate boundary; remaining = (1-wp)*est is unknown
      // here, so just show "waking..." without a hard countdown.
      return "waking…";
    }
    const s = Math.max(0, Math.round(sleepRemainingSeconds));
    if (s <= 0) return "";
    if (s < 60) return `${s}s remaining`;
    const m = Math.floor(s / 60);
    const sec = s % 60;
    if (m < 60) return `${m}m ${sec}s remaining`;
    const h = Math.floor(m / 60);
    const min = m % 60;
    return `${h}h ${min}m remaining`;
  })();

  return (
    <div style={{ position: "relative", width: size, height: size, flexShrink: 0 }}>
      <div ref={mountRef} style={{ width:size, height:size, display:"flex", alignItems:"center", justifyContent:"center", background:"transparent", overflow:"visible" }} />
      {showTimer && timerLabel && (
        <div
          style={{
            position: "absolute",
            top: -28,
            left: 0,
            right: 0,
            textAlign: "center",
            color: "rgba(180, 200, 240, 0.85)",
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
            fontSize: 13,
            letterSpacing: "0.05em",
            pointerEvents: "none",
            textShadow: "0 1px 2px rgba(0,0,0,0.5)",
          }}
        >
          {timerLabel}
        </div>
      )}
    </div>
  );
}

// React.memo: bail out only on micro-changes. Allow re-render on state OR
// amplitude change so the inner component re-runs and refreshes its refs;
// the useEffect deps (`[emotion, size]`) ensure the Three.js scene only
// rebuilds on emotion/size changes — state and amplitude updates feed the
// animation loop via refs without remounting.
const OrbCanvas = memo(OrbCanvasInner, (prev, next) => (
  prev.emotion === next.emotion &&
  prev.emotionColor === next.emotionColor &&
  prev.state === next.state &&
  (prev.size ?? 320) === (next.size ?? 320) &&
  prev.shapeOverride === next.shapeOverride &&
  Math.abs((prev.amplitude ?? 0) - (next.amplitude ?? 0)) < 0.03 &&
  Math.abs((prev.energy ?? 0.5) - (next.energy ?? 0.5)) < 0.05 &&
  prev.recenterTrigger === next.recenterTrigger &&
  (prev.cubeMorphEnabled ?? true) === (next.cubeMorphEnabled ?? true) &&
  // Sleep state — re-render on every change so the orb reflects new
  // timer / progress / wake-progress promptly. Tolerance kept tight.
  Math.abs((prev.sleepProgress ?? 0) - (next.sleepProgress ?? 0)) < 0.01 &&
  Math.abs((prev.sleepRemainingSeconds ?? 0) - (next.sleepRemainingSeconds ?? 0)) < 1.0 &&
  Math.abs((prev.wakeProgress ?? 0) - (next.wakeProgress ?? 0)) < 0.05
));

export default OrbCanvas;
