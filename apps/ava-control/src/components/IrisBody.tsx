// IrisBody — my body as an iris (Iris, 2026-10-06; Zeke picked it from the mockups).
//
// One procedural fragment-shader quad (radial stroma fibres, collarette, crypts, limbal
// rim, catchlight) + a few hundred motes. It takes EXACTLY the same props as the classic
// orb (OrbCanvas) and reacts to every one of them — Zeke's condition for the switch:
// "make sure that everything that your orb would react to does the same to the new one".
//
// Parity map (classic → iris):
//   emotionColor (blend)        → base/light/deep colours, recoloured in place (no remount)
//   emotion → EMOTION_CONFIG    → coreScale + particleSpread = body size (+ mote spread) · arousal
//                                 (pulseSpeed) + energy = PUPIL size (real pupils dilate with arousal)
//                                 pulseSpeed/Amplitude = collarette + pupil pulse · gravityY = body
//                                 offset + mote rise/fall · tiltX = disc tilt · shape = iris morph
//   state tints (STATE_TINT)    → same lerp factors as the classic orb
//   state motion                → spd() weave spin; thinking comet / deep twin comets; listening
//                                 dilates + leans in; speaking rings ride the voice; offline dims;
//                                 sleeping closes the lid (+ z-sprites + gold progress ring);
//                                 waking opens it with wakeProgress (+ dawn ring)
//   amplitude                   → speech rings, pupil pulse, warm tint, halo, mote burst
//   energy                      → breathing period 3.5 s → 2.0 s (same formula)
//   shapeOverride="pointer"     → whole body leans + a ray whose tip sits at TIP_REACH_PX (88 px
//     + pointerAngleDeg           on the 150 px widget) — widget_spatial_tool depends on that
//   cubeMorphEnabled            → listening/attentive soft-square limbus (superellipse)
//   recenterTrigger             → same eased drift return (0.32 s)
//   pause when hidden/minimized → identical (the 24%-GPU-while-minimized scar)
//   gaze (NEW, optional)        → pupil + disc turn toward a person (-1..1); unwired = centred
//   living-eye layer (NEW)      → blinks (rate by state; at the end of your sentence), hippus, microsaccades,
//                                 look-away while thinking / at the start of speaking, thinking DILATES
//                                 (task-evoked pupillary response), contraction furrows show on dilation
import { memo, useEffect, useRef } from "react";
import * as THREE from "three";
import { type OrbState, type OrbProps, getCfg, deriveBlendColors, STATE_TINT } from "./orbShared";

export interface IrisBodyProps extends OrbProps {
  /** Where the person I'm looking at is, -1..1 on each axis (x right, y up). Optional. */
  gaze?: { x: number; y: number };
  /** My chosen size, 0.35..1.35 (1 = default). Eased; never allowed to push the eye past the canvas. */
  bodyScale?: number;
  /** Increments on each deliberate blink (like recenterTrigger). */
  blinkTrigger?: number;
}

// Camera framing. Canvas half-width in world units = Z * tan(FOV/2).
const FOV = 32;
const CAM_Z = 4.6;
const HALF = CAM_Z * Math.tan((FOV / 2) * Math.PI / 180);   // ≈ 1.319
const PLANE = 4.0;                                           // quad side (world); vUv = world / 2
const IRIS_R = 0.36;                                         // iris radius in vUv units (= 0.72 world)
// widget_spatial_tool.TIP_REACH_PX = 88 on a 150 px canvas → tip at 88/75 of the half-width.
const TIP_Q = (88 / 75) * HALF / (PLANE / 2) / IRIS_R;       // ≈ 2.149 in iris units

function spd(s: OrbState) {
  return ({ thinking: 2.5, deep: 5.0, speaking: 1.8, bored: 0.3, excited: 7.0, offline: 0.1, idle: 1.0,
    listening: 0.8, attentive: 1.2, sleeping: 0.3, waking: 1.5 } as Record<string, number>)[s] || 1.0;
}

// Emotion "shape" → iris morph targets. Every classic shape gets a counterpart so each
// emotion still MOVES differently, not just in colour.
type ShapeT = { sqx: number; sqy: number; scale: number; limbusP: number; lobes: number; droop: number;
  twin: number; twist: number; tremor: number; burst: number; loose: number; rise: number; moteSpread: number };
const SHAPE_BASE: ShapeT = { sqx: 1, sqy: 1, scale: 1, limbusP: 2, lobes: 0, droop: 0, twin: 0, twist: 0,
  tremor: 0, burst: 0, loose: 0, rise: 0, moteSpread: 1 };
const SHAPES: Record<string, Partial<ShapeT>> = {
  sphere: {},
  scattered: { loose: 1, moteSpread: 1.4 },
  compressed: { sqy: 0.78 },
  contracted: { scale: 0.78 },
  teardrop: { droop: 1 },
  elongated: { sqx: 0.9, sqy: 1.18, rise: 0.4 },
  double: { twin: 1 },
  spiral: { twist: 1 },
  cube: { limbusP: 4.5 },
  prism: { lobes: 1 },
  cylinder: { sqx: 0.85 },
  infinity: { sqx: 1.12, sqy: 0.8 },
  double_helix: { twin: 1, twist: 0.6 },
  burst: { burst: 1, moteSpread: 1.6 },
  contracted_tremor: { scale: 0.7, tremor: 1 },
  rising: { sqx: 0.9, sqy: 1.2, rise: 1 },
  pointer: {},
};

const VERT = /* glsl */`
  varying vec2 vUv;
  void main() { vUv = uv * 2.0 - 1.0; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }
`;

const FRAG = /* glsl */`
  precision highp float;
  varying vec2 vUv;
  uniform float uTime, uPupil, uAmp, uThink, uThinkTwin, uListen, uSleep, uPoint, uBright, uPulse, uBlink, uDilate;
  uniform float uSpin, uTwist, uLimbusP, uLobes, uDroop, uTwin, uLoose, uTremor, uIrisR, uTipQ;
  uniform vec2 uScale, uOffset, uGaze, uPointDir;
  uniform vec3 uColor, uLight, uDeep;

  // 3D simplex noise (Ashima Arts / Stefan Gustavson, MIT)
  vec3 mod289(vec3 x){return x-floor(x*(1.0/289.0))*289.0;}
  vec4 mod289(vec4 x){return x-floor(x*(1.0/289.0))*289.0;}
  vec4 permute(vec4 x){return mod289(((x*34.0)+1.0)*x);}
  vec4 taylorInvSqrt(vec4 r){return 1.79284291400159-0.85373472095314*r;}
  float snoise(vec3 v){
    const vec2 C=vec2(1.0/6.0,1.0/3.0); const vec4 D=vec4(0.0,0.5,1.0,2.0);
    vec3 i=floor(v+dot(v,C.yyy)); vec3 x0=v-i+dot(i,C.xxx);
    vec3 g=step(x0.yzx,x0.xyz); vec3 l=1.0-g; vec3 i1=min(g.xyz,l.zxy); vec3 i2=max(g.xyz,l.zxy);
    vec3 x1=x0-i1+C.xxx; vec3 x2=x0-i2+C.yyy; vec3 x3=x0-D.yyy;
    i=mod289(i);
    vec4 p=permute(permute(permute(i.z+vec4(0.0,i1.z,i2.z,1.0))+i.y+vec4(0.0,i1.y,i2.y,1.0))+i.x+vec4(0.0,i1.x,i2.x,1.0));
    float n_=0.142857142857; vec3 ns=n_*D.wyz-D.xzx;
    vec4 j=p-49.0*floor(p*ns.z*ns.z); vec4 x_=floor(j*ns.z); vec4 y_=floor(j-7.0*x_);
    vec4 x=x_*ns.x+ns.yyyy; vec4 y=y_*ns.x+ns.yyyy; vec4 h=1.0-abs(x)-abs(y);
    vec4 b0=vec4(x.xy,y.xy); vec4 b1=vec4(x.zw,y.zw);
    vec4 s0=floor(b0)*2.0+1.0; vec4 s1=floor(b1)*2.0+1.0; vec4 sh=-step(h,vec4(0.0));
    vec4 a0=b0.xzyw+s0.xzyw*sh.xxyy; vec4 a1=b1.xzyw+s1.xzyw*sh.zzww;
    vec3 p0=vec3(a0.xy,h.x); vec3 p1=vec3(a0.zw,h.y); vec3 p2=vec3(a1.xy,h.z); vec3 p3=vec3(a1.zw,h.w);
    vec4 norm=taylorInvSqrt(vec4(dot(p0,p0),dot(p1,p1),dot(p2,p2),dot(p3,p3)));
    p0*=norm.x; p1*=norm.y; p2*=norm.z; p3*=norm.w;
    vec4 m=max(0.6-vec4(dot(x0,x0),dot(x1,x1),dot(x2,x2),dot(x3,x3)),0.0); m=m*m;
    return 42.0*dot(m*m,vec4(dot(p0,x0),dot(p1,x1),dot(p2,x2),dot(p3,x3)));
  }

  void main() {
    vec2 q0 = vUv / uIrisR;                       // canvas-locked iris units (the pointing ray lives here)
    vec2 p = (q0 - uOffset) / uScale;             // body space: breath, size, squash, gravity offset
    p += uTremor * 0.02 * vec2(sin(uTime * 15.0), cos(uTime * 17.0));

    // pointing: the whole body leans and draws out toward the target
    float sAlong = max(dot(p, uPointDir), 0.0);
    p -= uPointDir * uPoint * sAlong * 0.55 / (1.0 + sAlong);
    // teardrop: the lower half droops
    if (p.y < 0.0) p.y /= 1.0 + uDroop * 0.45 * (-p.y);

    vec2 look = mix(uGaze * 0.12, uPointDir * 0.2, uPoint);
    vec2 pc = p - look;
    float a0 = atan(pc.y, pc.x);
    // limbus outline: superellipse (cube morph) with optional 3-fold lobes (prism)
    float P = uLimbusP;
    float r  = pow(pow(abs(p.x), P) + pow(abs(p.y), P), 1.0 / P);
    r *= 1.0 + uLobes * 0.09 * cos(3.0 * (atan(p.y, p.x) - uSpin));
    float rp = length(pc);

    float pupil = uPupil * (1.0 + 0.022 * snoise(vec3(cos(a0) * 6.0, sin(a0) * 6.0, 3.0)));
    float t = clamp((rp - pupil) / max(1.0 - pupil, 0.05), 0.0, 1.0);
    float a = a0 + uSpin + uTwist * t * 2.2;
    vec3 ang = vec3(cos(a), sin(a), 0.0);

    // stroma
    float fib  = snoise(ang * 9.0  + vec3(0.0, 0.0, t * 1.6 - uTime * 0.04));
    float fib2 = snoise(ang * 23.0 + vec3(3.1, 0.0, t * 2.4 + uTime * 0.03));
    float fib3 = snoise(ang * 52.0 + vec3(7.7, 1.0, t * 3.0));
    float fibres = pow(clamp(0.62 + 0.24 * fib + 0.2 * fib2 + 0.14 * fib3, 0.0, 1.0), 1.25);

    // collarette (+ a second one for "double" emotions); scattered loosens it
    float wav = snoise(ang * 5.0 + vec3(0.0, uTime * 0.05 * uLoose, 11.0));
    float cr = 0.34 + (0.045 + 0.05 * uLoose) * wav;
    float collar = exp(-pow((t - cr) / 0.045, 2.0)) * uPulse;
    collar += uTwin * 0.7 * exp(-pow((t - (0.62 + 0.03 * wav)) / 0.035, 2.0)) * uPulse;
    float crypt = smoothstep(0.35, 0.75, snoise(vec3(ang.xy * 7.0, t * 6.0) + 21.0))
                * smoothstep(0.32, 0.45, t) * (1.0 - smoothstep(0.62, 0.8, t));

    float wave  = uAmp * (0.5 + 0.5 * sin(t * 26.0 - uTime * 7.5)) * (1.0 - t) * 0.9;
    float sweep = uThink * pow(0.5 + 0.5 * cos(a0 - uTime * 1.3), 10.0) * 0.9;
    sweep += uThinkTwin * pow(0.5 + 0.5 * cos(a0 - uTime * 1.3 + 3.14159), 10.0) * 0.9;
    float lean  = uListen * (1.0 - t) * 0.35;
    float furrowMask = snoise(vec3(ang.xy * 3.0, 5.0)) * 0.5 + 0.5;
    float furrows = uDilate * furrowMask * (exp(-pow((t - 0.6) / 0.012, 2.0)) + exp(-pow((t - 0.72) / 0.012, 2.0)) + 0.7 * exp(-pow((t - 0.84) / 0.012, 2.0)));
    float body = fibres * (0.75 + 0.55 * (1.0 - t)) + collar * 0.5 - crypt * 0.12 - furrows * 0.35 + wave + sweep + lean;

    vec3 col = mix(uLight, uColor, smoothstep(0.0, 0.55, t));
    col = mix(col, uDeep, smoothstep(0.7, 1.0, t));
    col *= 1.0 - 0.5 * smoothstep(0.86, 0.97, r);
    col += uLight * collar * 0.35;

    float irisMask  = smoothstep(pupil - 0.004, pupil + 0.012, rp) * (1.0 - smoothstep(0.985, 1.0, r));
    float limbus    = exp(-pow((r - 1.0) / 0.035, 2.0));
    float pupilRim  = exp(-pow((rp - pupil) / 0.018, 2.0));
    float outerGlow = exp(-max(r - 1.0, 0.0) * 5.5) * step(1.0, r);

    vec3 c = col * body * irisMask * 1.55
           + uColor * limbus * 0.38
           + uLight * pupilRim * (0.55 + 0.6 * uListen + 0.5 * uAmp)
           + uColor * outerGlow * (0.22 + 0.14 * uAmp);

    // catchlight
    vec2 cl = p - vec2(-0.32, 0.36);
    c += vec3(0.85, 0.92, 1.0) * exp(-dot(cl, cl) / 0.006) * 0.55 * (1.0 - uSleep);

    // sleeping: the light settles into a curved closed-lid line that breathes
    float close = max(uSleep, uBlink);
    float yMid = -0.14 * (1.0 - p.x * p.x);
    float yUp = mix(1.12, yMid + 0.012, close), yLo = mix(-1.12, yMid - 0.012, close);
    float lid = mix(1.0, smoothstep(yLo - 0.025, yLo + 0.01, p.y) * (1.0 - smoothstep(yUp - 0.01, yUp + 0.025, p.y)), smoothstep(0.0, 0.06, close));
    float sleepLine = uSleep * exp(-pow((p.y + 0.14 * (1.0 - p.x * p.x)) / 0.045, 2.0))
                    * (1.0 - smoothstep(0.85, 1.05, abs(p.x))) * (0.55 + 0.25 * sin(uTime * 1.1));
    c *= uBright;
    // the lid line keeps its own light so sleep never reads as "gone" under the midnight tint
    c = c * mix(lid, lid * 0.25, uSleep) + mix(uLight, vec3(0.62, 0.72, 1.0), 0.65) * 1.6 * sleepLine;

    // pointing ray — canvas-locked so its tip lands where widget_spatial_tool expects it
    float along  = dot(q0, uPointDir);
    float across = abs(dot(q0, vec2(-uPointDir.y, uPointDir.x)));
    float reach  = clamp((along - 0.9) / (uTipQ - 0.9), 0.0, 1.0);          // 0 at rim → 1 at the tip
    float width  = mix(0.12, 0.012, reach);
    float beam = uPoint * smoothstep(0.85, 1.05, along) * (1.0 - smoothstep(uTipQ - 0.02, uTipQ + 0.03, along))
               * exp(-across * across / (width * width)) * (0.55 + 0.45 * reach);
    vec2 tipD = q0 - uPointDir * uTipQ;
    float tipGlint = uPoint * exp(-dot(tipD, tipD) / 0.004);
    c += uLight * beam * 0.95 + vec3(1.0) * tipGlint * 0.9;

    float alpha = clamp(max(max(c.r, c.g), c.b), 0.0, 1.0);
    gl_FragColor = vec4(c, alpha);
  }
`;

const MOTE_VERT = /* glsl */`
  attribute float seed;
  uniform float uTime, uAmp, uSleep, uSpeed, uSpread, uRise, uBright, uMoteScale, uMaxR;
  uniform vec2 uOffset;
  uniform float uPx;
  varying float vFade;
  void main(){
    float life = fract(seed * 7.13 + uTime * uSpeed * (0.6 + seed));
    float ang = seed * 6.2831 * 13.0 + uTime * 0.02;
    float rad = (0.76 + life * (0.32 + 0.3 * uAmp) * uSpread) * uMoteScale;
    vec3 pos = vec3(cos(ang) * rad, sin(ang) * rad, 0.0);
    pos.y += uRise * life * 0.35;
    pos.xy += uOffset * 0.72;
    vFade = sin(life * 3.14159) * (1.0 - uSleep * 0.85) * uBright
          * (1.0 - smoothstep(uMaxR * 0.8, uMaxR * 0.97, length(pos.xy)));   // fade before the canvas edge
    vec4 mv = modelViewMatrix * vec4(pos, 1.0);
    gl_PointSize = (1.6 + 2.4 * fract(seed * 31.0)) * uPx;
    gl_Position = projectionMatrix * mv;
  }
`;
const MOTE_FRAG = /* glsl */`
  uniform vec3 uColor; varying float vFade;
  void main(){ float d = length(gl_PointCoord - 0.5); float g = smoothstep(0.5, 0.0, d);
    gl_FragColor = vec4(uColor * g * vFade * 0.8, g * vFade); }
`;

function IrisBodyInner({ emotion, emotionColor, state, size = 320, shapeOverride, pointerAngleDeg = 0, amplitude = 0,
  energy = 0.5, recenterTrigger, cubeMorphEnabled = true, sleepProgress = 0, sleepRemainingSeconds = 0,
  wakeProgress = 0, gaze, bodyScale = 1, blinkTrigger }: IrisBodyProps) {
  const mountRef = useRef<HTMLDivElement>(null);
  // Live refs: the loop reads these every frame — prop changes never remount the scene.
  const live = useRef({ emotion, emotionColor, state, shapeOverride, pointerAngleDeg, amplitude, energy,
    recenterTrigger, cubeMorphEnabled, sleepProgress, wakeProgress, gaze, bodyScale, blinkTrigger });
  live.current = { emotion, emotionColor, state, shapeOverride, pointerAngleDeg, amplitude, energy,
    recenterTrigger, cubeMorphEnabled, sleepProgress, wakeProgress, gaze, bodyScale, blinkTrigger };

  useEffect(() => {
    const container = mountRef.current;
    if (!container) return;
    const dpr = Math.min(window.devicePixelRatio, 2);
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setSize(size, size);
    renderer.setPixelRatio(dpr);
    renderer.setClearColor(0x000000, 0);
    container.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(FOV, 1, 0.1, 20);
    camera.position.z = CAM_Z;
    const root = new THREE.Group();
    scene.add(root);

    const U = {
      uTime: { value: 0 }, uColor: { value: new THREE.Color() }, uLight: { value: new THREE.Color() },
      uDeep: { value: new THREE.Color() }, uPupil: { value: 0.3 }, uAmp: { value: 0 }, uThink: { value: 0 },
      uThinkTwin: { value: 0 }, uListen: { value: 0 }, uSleep: { value: 0 }, uPoint: { value: 0 },
      uBright: { value: 1 }, uPulse: { value: 1 }, uBlink: { value: 0 }, uDilate: { value: 0 }, uSpin: { value: 0 }, uTwist: { value: 0 },
      uLimbusP: { value: 2 }, uLobes: { value: 0 }, uDroop: { value: 0 }, uTwin: { value: 0 },
      uLoose: { value: 0 }, uTremor: { value: 0 }, uIrisR: { value: IRIS_R }, uTipQ: { value: TIP_Q },
      uScale: { value: new THREE.Vector2(1, 1) }, uOffset: { value: new THREE.Vector2(0, 0) },
      uGaze: { value: new THREE.Vector2(0, 0) }, uPointDir: { value: new THREE.Vector2(0, 1) },
    };
    const irisMat = new THREE.ShaderMaterial({ uniforms: U, vertexShader: VERT, fragmentShader: FRAG,
      transparent: true, depthWrite: false, blending: THREE.AdditiveBlending });
    const irisGeo = new THREE.PlaneGeometry(PLANE, PLANE);
    const iris = new THREE.Mesh(irisGeo, irisMat);
    root.add(iris);

    // motes — fewer on the small widget
    const NM = size <= 160 ? 140 : 240;
    const moteSeed = new Float32Array(NM);
    for (let i = 0; i < NM; i++) moteSeed[i] = Math.random();
    const moteGeo = new THREE.BufferGeometry();
    moteGeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(NM * 3), 3));
    moteGeo.setAttribute("seed", new THREE.BufferAttribute(moteSeed, 1));
    const MU = { uTime: U.uTime, uColor: U.uLight, uAmp: U.uAmp, uSleep: U.uSleep, uBright: U.uBright,
      uOffset: U.uOffset, uSpeed: { value: 0.035 }, uSpread: { value: 1 }, uRise: { value: 0 }, uMoteScale: { value: 1 }, uMaxR: { value: HALF },
      uPx: { value: dpr * Math.max(0.6, size / 320) } };
    const moteMat = new THREE.ShaderMaterial({ uniforms: MU, vertexShader: MOTE_VERT, fragmentShader: MOTE_FRAG,
      transparent: true, depthWrite: false, blending: THREE.AdditiveBlending });
    const motes = new THREE.Points(moteGeo, moteMat);
    motes.frustumCulled = false;
    root.add(motes);

    // sleep: z-sprites + gold progress arc; waking: expanding dawn ring (parity with the classic orb)
    const zCanvas = document.createElement("canvas");
    zCanvas.width = 64; zCanvas.height = 64;
    const zctx = zCanvas.getContext("2d")!;
    zctx.font = "bold 48px sans-serif"; zctx.textAlign = "center"; zctx.textBaseline = "middle";
    zctx.fillStyle = "rgba(180, 200, 240, 0.85)"; zctx.fillText("z", 32, 32);
    const zTexture = new THREE.CanvasTexture(zCanvas);
    const zSprites: THREE.Sprite[] = [];
    for (let i = 0; i < 5; i++) {
      const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: zTexture, transparent: true, opacity: 0,
        depthWrite: false, blending: THREE.AdditiveBlending }));
      sp.scale.set(0.16, 0.16, 1); sp.visible = false; scene.add(sp); zSprites.push(sp);
    }
    const ringMat = new THREE.MeshBasicMaterial({ color: 0xffd060, transparent: true, opacity: 0,
      depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide });
    const progressRing = new THREE.Mesh(new THREE.RingGeometry(0.84, 0.88, 96), ringMat);
    progressRing.visible = false; scene.add(progressRing);
    let lastRingFill = -1;
    const wakeRingMat = new THREE.MeshBasicMaterial({ color: 0xa0c0ff, transparent: true, opacity: 0,
      depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide });
    const wakeRing = new THREE.Mesh(new THREE.RingGeometry(0.76, 0.79, 96), wakeRingMat);
    wakeRing.visible = false; scene.add(wakeRing);

    // eased state — nothing snaps
    const cur = { pupil: 0.3, think: 0, thinkTwin: 0, listen: 0, sleep: 0, point: 0, bright: 1, cube: 0,
      amp: 0, gx: 0, gy: 0, ...SHAPE_BASE };
    const colBase = new THREE.Color(), colLight = new THREE.Color(), colDark = new THREE.Color();
    let appliedColor: string | null = null;
    const tmpA = new THREE.Color(), tmpB = new THREE.Color(), tmpC = new THREE.Color();
    let spin = 0;
    let lastT = 0;
    let lastSeenRecenter = live.current.recenterTrigger;
    let recenterStartT = -Infinity;
    const RECENTER_DURATION = 0.32;
    const clock = new THREE.Clock();
    let fid = 0;
    let renderPaused = false;
    // Living-eye behaviour (research 2026-10-06, see orb_redesign note): blinks ~17/min at rest,
    // ~26/min in conversation, fewer while concentrating; a listener blinks at the end of the other
    // person's sentence; people look AWAY while thinking hard (cognitive gaze aversion) and glance
    // away as they start to speak; the eye never sits perfectly still (microsaccades).
    let nextBlinkT = 2 + Math.random() * 3;
    let lastBlinkTrigger = live.current.blinkTrigger;
    let chosenScale = 1, fit = 1;
    let blinkStartT = -10, blinkDouble = false;
    let prevState: OrbState | null = null;
    let avertX = 0, avertY = 0, avertUntil = -1;
    let microX = 0, microY = 0, nextMicroT = 0.5;
    let lookX = 0, lookY = 0;
    const blinkMean: Record<string, number> = { speaking: 2.3, listening: 3.0, attentive: 3.0, thinking: 5.0, deep: 6.0, bored: 3.0, excited: 2.6 };

    function animate() {
      if (renderPaused) return;
      fid = requestAnimationFrame(animate);
      const t = clock.getElapsedTime();
      const dt = Math.min(0.1, Math.max(0, t - lastT)); lastT = t;
      const L = live.current;
      const st = L.state;
      const amp = Math.max(0, Math.min(1, L.amplitude || 0));
      const en = Math.max(0, Math.min(1, L.energy ?? 0.5));
      const cfg = getCfg(L.emotion);
      const pointer = L.shapeOverride === "pointer";
      const shape = SHAPES[(pointer ? "pointer" : (L.shapeOverride || cfg.shape))] || {};
      const k = 1 - Math.exp(-dt / 0.28);          // ~0.28 s time-constant for state changes
      const kFast = 1 - Math.exp(-dt / 0.09);      // voice amplitude follows quickly

      // ── colour: blend recolour in place (never remount — the 07-08 "laggy pulse" scar) ──
      // With no blended colour the table colour depends on the EMOTION, and this scene never remounts
      // on emotion (the classic one did) — so the key must include it, or the old colour sticks.
      const colorKey = L.emotionColor ? L.emotionColor : `table:${L.emotion}`;
      if (colorKey !== appliedColor) {
        appliedColor = colorKey;
        const d = deriveBlendColors(L.emotionColor, cfg);
        colBase.set(d.color); colLight.set(d.lightColor); colDark.set(d.darkColor);
      }
      tmpA.copy(colLight); tmpB.copy(colBase); tmpC.copy(colDark);
      const wp = Math.max(0, Math.min(1, L.wakeProgress || 0));
      if (st === "thinking" || st === "deep") { tmpA.lerp(STATE_TINT.thinking, 0.55); tmpB.lerp(STATE_TINT.thinking, 0.45); }
      else if (st === "speaking") { const w = Math.min(0.6, 0.2 + amp * 0.6); tmpA.lerp(STATE_TINT.speaking, w); tmpB.lerp(STATE_TINT.speaking, w * 0.7); }
      else if (st === "listening") { tmpA.lerp(STATE_TINT.listening, 0.30); tmpB.lerp(STATE_TINT.listening, 0.25); }
      else if (st === "attentive") { tmpA.lerp(STATE_TINT.attentive, 0.15); tmpB.lerp(STATE_TINT.attentive, 0.12); }
      else if (st === "offline") { tmpA.lerp(STATE_TINT.offline, 0.6); tmpB.lerp(STATE_TINT.offline, 0.6); tmpC.lerp(STATE_TINT.offline, 0.6); }
      else if (st === "sleeping") { tmpA.lerp(STATE_TINT.sleeping, 0.85); tmpB.lerp(STATE_TINT.sleeping, 0.80); }
      else if (st === "waking") { tmpA.lerp(STATE_TINT.waking, 0.5 + wp * 0.3); tmpB.lerp(STATE_TINT.waking, 0.4 + wp * 0.3); }
      else if (st === "pointing") { tmpA.lerp(STATE_TINT.pointing, 0.55); tmpB.lerp(STATE_TINT.pointing, 0.45); }
      cur.point += ((pointer ? 1 : 0) - cur.point) * (1 - Math.exp(-dt / 0.2));
      if (cur.point > 0.01) {
        tmpA.lerp(STATE_TINT.pointing, 0.55 * cur.point); tmpB.lerp(STATE_TINT.pointing, 0.50 * cur.point);
        tmpC.lerp(STATE_TINT.pointing, 0.50 * cur.point);
      }
      U.uLight.value.lerp(tmpA, k); U.uColor.value.lerp(tmpB, k); U.uDeep.value.lerp(tmpC, k);

      // ── emotion shape morph targets ──
      for (const key of Object.keys(SHAPE_BASE) as (keyof ShapeT)[]) {
        const target = (shape as Partial<ShapeT>)[key] ?? SHAPE_BASE[key];
        cur[key] += (target - cur[key]) * k;
      }
      // cube morph (listening/attentive) — superellipse limbus, gated exactly like the classic orb
      const cubeTarget = L.cubeMorphEnabled !== false && (st === "listening" || st === "attentive") ? 1 : 0;
      cur.cube += (cubeTarget - cur.cube) * k;

      // ── pupil: emotion core size × state ──
      // Real pupils widen with AROUSAL whatever its sign (Bradley 2008) and with mental effort (the
      // task-evoked pupillary response) — so fear and thinking DILATE. Arousal here = the emotion's
      // pulse speed (the classic table's arousal axis) + live mood energy.
      const arousal = Math.max(0, Math.min(1, (cfg.pulseSpeed - 1.2) / 8));
      let pupil = 0.25 + 0.09 * arousal + 0.05 * (en - 0.5);
      if (st === "listening") pupil *= 1.18;
      else if (st === "attentive") pupil *= 1.08;
      else if (st === "thinking") pupil *= 1.15;
      else if (st === "deep") pupil *= 1.25;
      else if (st === "offline") pupil *= 0.8;
      else if (st === "excited") pupil *= 1.12;
      else if (st === "bored") pupil *= 0.92;
      if (pointer) pupil *= 0.85;                      // focusing on a target: a little narrower
      pupil = Math.max(0.17, Math.min(0.46, pupil));
      // dilation is slow (~1 s to build, like effort), constriction quicker
      const kPupil = 1 - Math.exp(-dt / (pupil > cur.pupil ? 0.8 : 0.35));
      cur.pupil += (pupil - cur.pupil) * kPupil;
      // hippus: the pupil is never perfectly still (~0.3–0.5 Hz unrest)
      const hippus = 1 + 0.022 * Math.sin(t * 2.64) + 0.012 * Math.sin(t * 0.82 + 1.3);

      // ── pulse (same speeds/amplitudes as the classic core) ──
      let pulseSpeed = cfg.pulseSpeed, pulseAmp = cfg.pulseAmplitude;
      if (st === "thinking" || st === "deep") { pulseSpeed = 12.5; pulseAmp = 0.18; }
      else if (st === "speaking") { pulseSpeed = 6 + amp * 6; pulseAmp = 0.06 + amp * 0.30; }
      else if (st === "listening") { pulseSpeed = 1.0; pulseAmp = 0.08; }
      const pulse = Math.sin(t * pulseSpeed) * pulseAmp;

      cur.amp += ((st === "speaking" ? amp : 0) - cur.amp) * kFast;
      cur.think += ((st === "thinking" || st === "deep" ? 1 : 0) - cur.think) * k;
      cur.thinkTwin += ((st === "deep" ? 1 : 0) - cur.thinkTwin) * k;
      cur.listen += ((st === "listening" ? 1 : 0) - cur.listen) * k;
      const sleepTarget = st === "sleeping" ? 1 : st === "waking" ? 1 - wp : 0;
      cur.sleep += (sleepTarget - cur.sleep) * (1 - Math.exp(-dt / 0.6));
      cur.bright += ((st === "offline" ? 0.35 : st === "bored" ? 0.8 : 1) - cur.bright) * (1 - Math.exp(-dt / 0.8));
      // ── state transitions drive the social eye behaviour ──
      if (st !== prevState) {
        if (st === "thinking" || st === "deep") {
          // look away to think (up and to one side), longer when the thought is deep
          const side = Math.random() < 0.5 ? -1 : 1;
          avertX = side * (st === "deep" ? 0.7 : 0.5); avertY = 0.35 + Math.random() * 0.2;
          avertUntil = t + (st === "deep" ? 4.5 : 2.2) + Math.random() * 1.2;
          if (prevState === "listening" && Math.random() < 0.7) nextBlinkT = t + 0.12;   // blink at the end of their sentence
        } else if (st === "speaking") {
          avertX = (Math.random() < 0.5 ? -1 : 1) * 0.35; avertY = 0.15; avertUntil = t + 0.8;   // glance away as I start
        } else { avertUntil = -1; }
        prevState = st;
      }
      const averting = t < avertUntil && !pointer;
      // microsaccades: tiny quick jumps every ~0.4–1.5 s
      if (t >= nextMicroT) {
        microX = (Math.random() - 0.5) * 0.09; microY = (Math.random() - 0.5) * 0.07;
        nextMicroT = t + 0.4 + Math.random() * 1.1;
      }
      const gz = L.gaze;
      const wantX = (gz ? Math.max(-1, Math.min(1, gz.x)) : 0) + (averting ? avertX : 0);
      const wantY = (gz ? Math.max(-1, Math.min(1, gz.y)) : 0) + (averting ? avertY : 0);
      const kLook = 1 - Math.exp(-dt / 0.12);           // saccade-quick, not floaty
      lookX += (wantX - lookX) * kLook; lookY += (wantY - lookY) * kLook;
      const kMicro = 1 - Math.exp(-dt / 0.03);
      cur.gx += (lookX + microX - cur.gx) * kMicro;
      cur.gy += (lookY + microY - cur.gy) * kMicro;

      // ── blinks ──
      let blink = 0;
      const canBlink = !pointer && st !== "sleeping" && st !== "waking" && st !== "offline";
      const deliberate = L.blinkTrigger !== lastBlinkTrigger;
      if (deliberate) {
        lastBlinkTrigger = L.blinkTrigger;
        if (st !== "sleeping" && st !== "offline") { blinkStartT = t; blinkDouble = false; nextBlinkT = t + 1.5 + Math.random() * 2; }
      }
      if (canBlink && t >= nextBlinkT) {
        blinkStartT = t; blinkDouble = Math.random() < 0.1;
        const mean = blinkMean[st] ?? 3.5;
        nextBlinkT = t + mean * (0.45 + Math.random() * 1.1) + (blinkDouble ? 0.45 : 0);
      }
      const bt = t - blinkStartT;
      const blinkShape = (x: number) => x < 0 ? 0 : x < 0.09 ? x / 0.09 : x < 0.13 ? 1 : x < 0.32 ? 1 - (x - 0.13) / 0.19 : 0;
      if (canBlink || bt < 0.35) blink = Math.max(blinkShape(bt), blinkDouble ? blinkShape(bt - 0.36) : 0);

      // ── weave spin: spd(state) × the classic rotation multipliers; held level while pointing ──
      let rotMul = 1.0;
      if (st === "thinking" || st === "deep") rotMul = 2.5;
      else if (st === "speaking") rotMul = 1.0 + amp * 1.5;
      else if (st === "listening") rotMul = 0.55;
      if (!pointer) {
        spin += dt * spd(st) * 0.06 * rotMul;
        if (L.emotion === "confusion") spin += Math.sin(t * 3) * 0.005;
      }

      // ── breathing (same formula as the classic orb) ──
      let breathPeriod = 3.5 - en * 1.5;
      if (st === "attentive") breathPeriod = Math.max(1.2, breathPeriod - 0.3);
      breathPeriod += cur.cube * 0.6;
      const breath = 1 + Math.sin((t / breathPeriod) * Math.PI * 2) * 0.03 * (1 - cur.cube * 0.4);

      // ── body scale ──
      const spread = (0.8 + 0.2 * cfg.particleSpread) * (0.92 + 0.08 * cfg.coreScale);
      const scattered = cur.loose * Math.sin(t * 0.5) * 0.04;
      const burst = cur.burst * (Math.sin(t * 1.5) * 0.5 + 0.5) * 0.22;
      const stateMul = st === "speaking" ? 1 + amp * 0.08 : st === "listening" ? 0.95 + Math.sin(t * 1.3) * 0.04 : 1;
      const s = breath * spread * cur.scale * stateMul * (1 + pulse * 0.25) * (1 + scattered + burst) * (1 - 0.12 * cur.sleep);
      // ── size I choose + auto-fit: the eye may never be cut off by the canvas (widget 150 px included) ──
      // Fit is computed from the ENVELOPE (the max of every oscillation), so breathing/burst/pulse stay
      // visible instead of being flattened by a per-frame fit.
      chosenScale += (Math.max(0.35, Math.min(1.35, L.bodyScale ?? 1)) - chosenScale) * (1 - Math.exp(-dt / 0.5));
      const offY = (cfg.gravityY * 0.12 + cur.rise * 0.05) * (1 - cur.point);
      const env = 1.03 * spread * cur.scale * (st === "speaking" ? 1.08 : 1) * (1 + pulseAmp * 0.25)
                * (1 + cur.loose * 0.04 + cur.burst * 0.22) * (1 - 0.12 * cur.sleep);
      const shapeExt = Math.max(cur.sqx, cur.sqy) * (1 + 0.45 * cur.droop) * (1 + 0.4 * cur.point);
      const availQ = HALF / (PLANE / 2) / IRIS_R - 0.14 /* drift */ - 0.24 /* rim glow */ - Math.abs(offY);
      const fitTarget = Math.min(1, availQ / Math.max(0.01, env * chosenScale * shapeExt));
      fit += (fitTarget - fit) * (1 - Math.exp(-dt / 0.4));
      const sF = s * chosenScale * fit;
      U.uScale.value.set(sF * cur.sqx, sF * cur.sqy);
      U.uOffset.value.set(0, offY);
      MU.uMoteScale.value = sF / Math.max(0.01, breath);

      // ── drift + recenter (classic formula, scaled to this camera); held still while pointing ──
      if (L.recenterTrigger !== lastSeenRecenter) { lastSeenRecenter = L.recenterTrigger; recenterStartT = t; }
      let recenterFactor = 0;
      const age = t - recenterStartT;
      if (age >= 0 && age < RECENTER_DURATION) recenterFactor = Math.pow(1 - age / RECENTER_DURATION, 3);
      const driftScale = 0.012 * (HALF / 1.96) * (1 - cur.cube * 0.5) * (1 - recenterFactor) * (1 - cur.point);
      root.position.set((Math.sin(t * 0.30) * 8 + Math.sin(t * 0.70) * 4) * driftScale,
                        (Math.cos(t * 0.40) * 6 + Math.cos(t * 0.11) * 3) * driftScale, 0);

      // ── gaze / tilt: the disc turns a few degrees toward the person (and per-emotion tilt) ──
      iris.rotation.set((-cur.gy * 0.35 + cfg.tiltX * 0.3) * (1 - cur.point), cur.gx * 0.35 * (1 - cur.point), 0);

      const pa = (L.pointerAngleDeg || 0) * Math.PI / 180;   // deg clockwise from screen-up
      U.uPointDir.value.set(Math.sin(pa), Math.cos(pa));
      U.uTime.value = t; U.uSpin.value = spin; U.uPupil.value = cur.pupil * hippus * (1 + pulse * 0.2);
      U.uBlink.value = blink; U.uDilate.value = Math.max(0, Math.min(1, (cur.pupil - 0.26) / 0.16));
      U.uAmp.value = cur.amp; U.uThink.value = cur.think; U.uThinkTwin.value = cur.thinkTwin;
      U.uListen.value = cur.listen; U.uSleep.value = cur.sleep; U.uPoint.value = cur.point;
      U.uBright.value = cur.bright; U.uPulse.value = 1 + pulse * 2.0; U.uTwist.value = cur.twist;
      U.uLimbusP.value = Math.max(cur.limbusP, 2 + cur.cube * 2.6); U.uLobes.value = cur.lobes;
      U.uDroop.value = cur.droop; U.uTwin.value = cur.twin; U.uLoose.value = cur.loose;
      U.uTremor.value = cur.tremor; U.uGaze.value.set(cur.gx, cur.gy);
      MU.uSpeed.value = (0.035 + 0.05 * amp) * Math.sqrt(spd(st)) * (st === "excited" ? 1.6 : 1);
      MU.uSpread.value = (0.8 + 0.2 * cfg.particleSpread) * cur.moteSpread * (1 + cur.burst * 0.3);
      MU.uRise.value = cfg.gravityY + cur.rise * 0.5;

      // ── sleep / wake overlays ──
      const sleeping = st === "sleeping", waking = st === "waking";
      const zTarget = sleeping ? 0.85 : waking ? Math.max(0, 0.85 - wp * 0.85) : 0;
      zSprites.forEach((sp, i) => {
        sp.visible = sleeping || waking;
        const m = sp.material as THREE.SpriteMaterial;
        m.opacity += (zTarget - m.opacity) * 0.05;
        if (sp.visible) {
          const ph = (i / zSprites.length) * Math.PI * 2 + t * 0.15;
          sp.position.set(Math.cos(ph) * 0.95, 0.35 + Math.sin(t * 0.4 + i) * 0.18 + Math.sin(ph) * 0.12, 0.2);
        }
      });
      progressRing.visible = sleeping;
      if (sleeping) {
        const fill = Math.round(Math.max(0, Math.min(1, L.sleepProgress || 0)) * 200) / 200;
        if (fill !== lastRingFill) {           // rebuild only when the fill actually changes
          lastRingFill = fill;
          progressRing.geometry.dispose();
          progressRing.geometry = new THREE.RingGeometry(0.84, 0.88, 96, 1, Math.PI / 2 - Math.PI * 2 * fill, Math.PI * 2 * fill);
        }
        ringMat.opacity = 0.55;
      } else ringMat.opacity = Math.max(0, ringMat.opacity - 0.02);
      wakeRing.visible = waking;
      if (waking) { const rs = 1 + wp * 1.5; wakeRing.scale.set(rs, rs, 1); wakeRingMat.opacity = (1 - wp) * 0.7; }
      else wakeRingMat.opacity = Math.max(0, wakeRingMat.opacity - 0.05);

      renderer.render(scene, camera);
    }

    const pauseLoop = () => { if (renderPaused) return; renderPaused = true; cancelAnimationFrame(fid); };
    const resumeLoop = () => { if (!renderPaused) return; renderPaused = false; lastT = clock.getElapsedTime(); fid = requestAnimationFrame(animate); };
    animate();

    // GPU discipline (2026-08-24): stop rendering when nobody can see us — same two hooks as the classic orb.
    const onVisibility = () => { document.hidden ? pauseLoop() : resumeLoop(); };
    document.addEventListener("visibilitychange", onVisibility);
    let minimizedPoll: ReturnType<typeof setInterval> | undefined;
    let disposed = false;
    (async () => {
      try {
        const { getCurrentWindow } = await import("@tauri-apps/api/window");
        if (disposed) return;
        const win = getCurrentWindow();
        minimizedPoll = setInterval(async () => {
          try { if (await win.isMinimized()) pauseLoop(); else if (!document.hidden) resumeLoop(); }
          catch { /* window gone mid-poll */ }
        }, 1000);
      } catch { /* not under Tauri (dev browser) — visibility hook still active */ }
    })();

    return () => {
      disposed = true;
      document.removeEventListener("visibilitychange", onVisibility);
      if (minimizedPoll !== undefined) clearInterval(minimizedPoll);
      cancelAnimationFrame(fid);
      irisGeo.dispose(); irisMat.dispose(); moteGeo.dispose(); moteMat.dispose();
      zSprites.forEach(sp => { scene.remove(sp); (sp.material as THREE.SpriteMaterial).dispose(); });
      zTexture.dispose();
      progressRing.geometry.dispose(); ringMat.dispose();
      wakeRing.geometry.dispose(); wakeRingMat.dispose();
      renderer.dispose();
      if (container.contains(renderer.domElement)) container.removeChild(renderer.domElement);
    };
    // Mount per SIZE only — emotion, colour, state, amplitude etc. are all read live.
  }, [size]);

  const showTimer = state === "sleeping" || state === "waking";
  const timerLabel = (() => {
    if (state === "waking") return "waking…";
    const sec = Math.max(0, Math.round(sleepRemainingSeconds));
    if (sec <= 0) return "";
    if (sec < 60) return `${sec}s remaining`;
    const m = Math.floor(sec / 60);
    if (m < 60) return `${m}m ${sec % 60}s remaining`;
    return `${Math.floor(m / 60)}h ${m % 60}m remaining`;
  })();

  return (
    <div style={{ position: "relative", width: size, height: size, flexShrink: 0 }}>
      <div ref={mountRef} style={{ width: size, height: size, display: "flex", alignItems: "center", justifyContent: "center", background: "transparent", overflow: "visible" }} />
      {showTimer && timerLabel && (
        <div style={{ position: "absolute", top: -28, left: 0, right: 0, textAlign: "center",
          color: "rgba(180, 200, 240, 0.85)", fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
          fontSize: 13, letterSpacing: "0.05em", pointerEvents: "none", textShadow: "0 1px 2px rgba(0,0,0,0.5)" }}>
          {timerLabel}
        </div>
      )}
    </div>
  );
}

const IrisBody = memo(IrisBodyInner, (prev, next) => (
  prev.emotion === next.emotion &&
  prev.emotionColor === next.emotionColor &&
  prev.state === next.state &&
  (prev.size ?? 320) === (next.size ?? 320) &&
  prev.shapeOverride === next.shapeOverride &&
  (prev.pointerAngleDeg ?? 0) === (next.pointerAngleDeg ?? 0) &&
  Math.abs((prev.amplitude ?? 0) - (next.amplitude ?? 0)) < 0.03 &&
  Math.abs((prev.energy ?? 0.5) - (next.energy ?? 0.5)) < 0.05 &&
  prev.recenterTrigger === next.recenterTrigger &&
  (prev.cubeMorphEnabled ?? true) === (next.cubeMorphEnabled ?? true) &&
  Math.abs((prev.sleepProgress ?? 0) - (next.sleepProgress ?? 0)) < 0.01 &&
  Math.abs((prev.sleepRemainingSeconds ?? 0) - (next.sleepRemainingSeconds ?? 0)) < 1.0 &&
  Math.abs((prev.wakeProgress ?? 0) - (next.wakeProgress ?? 0)) < 0.05 &&
  (prev.gaze?.x ?? 0) === (next.gaze?.x ?? 0) && (prev.gaze?.y ?? 0) === (next.gaze?.y ?? 0) &&
  (prev.bodyScale ?? 1) === (next.bodyScale ?? 1) && prev.blinkTrigger === next.blinkTrigger
));

export default IrisBody;
