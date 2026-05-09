/**
 * Phase 48 — Desktop Widget Orb
 *
 * Minimal always-on-top floating orb window. Polls operator HTTP for
 * mood + voice state and renders OrbCanvas in a transparent 150×150 frame.
 * Position is persisted via the operator API.
 *
 * Bootstrap: Ava tracks where the widget gets moved and starts defaulting there.
 *
 * The widget orb mirrors the same state machine as the main orb so the two
 * always agree visually. Every state the main orb reacts to (thinking,
 * listening, speaking with live amplitude, offline) shows up here too.
 */
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import OrbCanvas from "./components/OrbCanvas";
import { API_BASE, getJson } from "./api";

const EMOTION_COLOR: Record<string, string> = {
  calmness: "#1a6cf5", joy: "#f5c518", happiness: "#f5c518", excitement: "#ff6b00",
  curiosity: "#00d4d4", interest: "#00d4d4", boredom: "#4a5568", frustration: "#e53e3e",
  sadness: "#553c9a", anger: "#c53030", fear: "#44337a", anxiety: "#44337a",
  surprise: "#d53f8c", trust: "#38a169", love: "#ed64a6", pride: "#6b46c1",
  confidence: "#ecc94b", awe: "#4299e1", confusion: "#9f7aea", loneliness: "#2c5282",
  contentment: "#68d391", relief: "#81e6d9", nostalgia: "#d4a574", hope: "#f6e05e",
  contempt: "#4a5568", shame: "#b7791f", guilt: "#2d3748", anticipation: "#d69e2e",
};

type OrbState = "idle" | "thinking" | "deep" | "speaking" | "bored" | "excited" | "offline" | "listening" | "attentive";

function getOrbState(snap: Record<string, unknown> | null, online: boolean): OrbState {
  if (!online || !snap) return "offline";
  const s = snap as Record<string, unknown>;

  // Backend run_ava thinking flag — overrides everything except offline.
  if (Boolean(s.thinking)) return "thinking";

  // Voice loop state takes priority over heartbeat.
  const voiceLoop = s.voice_loop as Record<string, unknown> | undefined;
  const voiceState = String(voiceLoop?.state ?? "passive");
  const voiceActive = Boolean(voiceLoop?.active);
  if (voiceActive) {
    if (voiceState === "speaking") return "speaking";
    if (voiceState === "thinking") return "thinking";
    if (voiceState === "listening") return "listening";
    if (voiceState === "attentive") return "attentive";
  }

  // TTS speaking outside the voice loop.
  const tts = s.tts as Record<string, unknown> | undefined;
  if (Boolean(tts?.tts_speaking)) return "speaking";

  // Heartbeat hint for idle visuals.
  const rb = s.ribbon as Record<string, unknown> | undefined;
  const hbMode = String(rb?.heartbeat_mode || "").toLowerCase();
  if (hbMode.includes("conversation")) return "speaking";
  if (hbMode.includes("maintenance") || hbMode.includes("learning")) return "thinking";
  return "idle";
}

function getEmotion(snap: Record<string, unknown> | null): [string, string] {
  if (!snap) return ["calmness", "#1a6cf5"];
  try {
    const p = snap.perception as Record<string, unknown> | undefined;
    const emo = String(p?.emotion_label || (snap as any)?.emotion_label || "calmness").toLowerCase();
    return [emo, EMOTION_COLOR[emo] || "#1a6cf5"];
  } catch {
    return ["calmness", "#1a6cf5"];
  }
}

function getTtsAmplitude(snap: Record<string, unknown> | null): number {
  if (!snap) return 0;
  const tts = snap.tts as Record<string, unknown> | undefined;
  return Number(tts?.tts_amplitude ?? 0);
}

function getEnergy(snap: Record<string, unknown> | null): number {
  if (!snap) return 0.5;
  const mood = snap.mood as Record<string, unknown> | undefined;
  const raw = mood?.raw_mood as Record<string, unknown> | undefined;
  const e = Number(raw?.energy ?? 0.5);
  return Math.max(0, Math.min(1, isFinite(e) ? e : 0.5));
}

// Save widget drag position back to the state file via operator API
function savePosition(x: number, y: number): void {
  fetch(`${API_BASE}/api/v1/widget/position`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ x, y }),
  }).catch(() => {});
}

export default function WidgetApp() {
  const [online, setOnline] = useState(false);
  const [snap, setSnap] = useState<Record<string, unknown> | null>(null);
  const dragRef = useRef<{ startX: number; startY: number; winX: number; winY: number } | null>(null);

  // Force transparent background on html/body/#root — overrides styles.css which sets --bg colour.
  // Must run synchronously before first paint so the dark background never flashes.
  useLayoutEffect(() => {
    const transparent = "transparent";
    const els = [
      document.documentElement,
      document.body,
      document.getElementById("root"),
    ];
    const prev = els.map((el) => el ? { bg: el.style.background, bgc: el.style.backgroundColor } : null);

    els.forEach((el) => {
      if (!el) return;
      el.style.setProperty("background", transparent, "important");
      el.style.setProperty("background-color", transparent, "important");
    });

    // Inject a <style> tag that also beats the cascade (stylesheet specificity)
    const tag = document.createElement("style");
    tag.id = "widget-transparent-override";
    tag.textContent = `
      html, body, #root {
        background: transparent !important;
        background-color: transparent !important;
        overflow: hidden !important;
      }
    `;
    document.head.appendChild(tag);

    return () => {
      tag.remove();
      els.forEach((el, i) => {
        if (!el || !prev[i]) return;
        el.style.background = prev[i]!.bg;
        el.style.backgroundColor = prev[i]!.bgc;
      });
    };
  }, []);

  // Poll snapshot — fast enough that amplitude updates feel live (every 500ms).
  // The operator snapshot reads live amplitude from the TTS worker each request.
  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const data = await getJson("/api/v1/snapshot");
        if (alive && data && typeof data === "object") {
          setSnap(data as Record<string, unknown>);
          setOnline(true);
        }
      } catch {
        if (alive) setOnline(false);
      }
    };
    poll();
    const iv = setInterval(poll, 500);
    return () => { alive = false; clearInterval(iv); };
  }, []);

  // Load saved position on mount and restore widget window position
  useEffect(() => {
    const restorePosition = async () => {
      try {
        const pos = await getJson("/api/v1/widget/position");
        if (pos && typeof pos === "object") {
          const p = pos as Record<string, unknown>;
          const x = Number(p.x) || 100;
          const y = Number(p.y) || 100;
          // Tauri v2: use proper window API to set position
          const { getCurrentWindow } = await import("@tauri-apps/api/window");
          const { LogicalPosition } = await import("@tauri-apps/api/dpi");
          const win = getCurrentWindow();
          await win.setPosition(new LogicalPosition(x, y));
        }
      } catch { /* position restore is best-effort */ }
    };
    void restorePosition();
  }, []);

  const [emotion, emotionColor] = getEmotion(snap);
  const orbState = getOrbState(snap, online);
  const ttsAmplitude = getTtsAmplitude(snap);
  const moodEnergy = getEnergy(snap);

  // Phase 49: pointer morph when Ava is pointing at something
  const widgetBlock = snap?.widget as Record<string, unknown> | undefined;
  const isPointing = Boolean(widgetBlock?.pointing);
  const shapeOverride = isPointing ? "pointer" : undefined;

  return (
    <div
      data-tauri-drag-region
      style={{
        width: "150px",
        height: "150px",
        background: "transparent",
        backgroundColor: "transparent",
        overflow: "hidden",
        cursor: "grab",
        userSelect: "none",
        position: "fixed",
        top: 0,
        left: 0,
      }}
    >
      <OrbCanvas
        emotion={emotion}
        emotionColor={emotionColor}
        state={orbState}
        size={150}
        shapeOverride={shapeOverride}
        amplitude={ttsAmplitude}
        energy={moodEnergy}
      />
    </div>
  );
}
