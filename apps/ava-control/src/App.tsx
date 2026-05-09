import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as d3 from "d3";
import ForceGraph3D from "3d-force-graph";
import { API_BASE, ApiLogEntry, getJson, getText, postJson, registerApiLogger } from "./api";
import { JsonBlock, Kv, Section } from "./components/Ui";
import OrbCanvas from "./components/OrbCanvas";
import { listen } from "@tauri-apps/api/event";

/** Operator HTTP API aggregate (brain/operator_server.py — started from avaagent.py). */
type Snapshot = Record<string, unknown>;
type ChatMessage = {
  role?: string;
  content?: string;
  source?: string;            // zeke | claude_code | ava_response | ava_initiative | unknown_user | <person_id>
  meta?: Record<string, unknown>;
};

// ── Brain-tab performance caps ──────────────────────────────────────────
// The 3D force-graph renders sluggishly with large graphs. The lunch run
// had 654 nodes / 6122 edges and produced ~15fps. Caps below keep the
// scene under what an RTX 5060 can render at 60fps with the OrbCanvas
// also active. Nodes are kept by weight (top by activation_count *
// last_activated freshness); edges are filtered to those connecting two
// surviving nodes, then capped by strength.
const BRAIN_GRAPH_MAX_NODES = 200;
const BRAIN_GRAPH_MAX_EDGES = 500;


// ── Ava-centric brain layout ────────────────────────────────────────────
// Per docs/BRAIN_ARCHITECTURE.md: Ava's identity sits at (0,0,0) as the
// gravitational center. Concentric rings around it carry identity anchors
// closest, then people by trust score, then active concerns, then
// memories by recency, with regional clusters at the outer edge.
//
// Tier 0:  AVA SELF (the center node, pinned)
// Tier 1:  Identity anchors — IDENTITY, SOUL, USER nodes from ava_core/
// Tier 2:  People (trust-weighted; high trust = closer)
// Tier 3:  Active concerns / threads / current curiosity
// Tier 4:  Memory nodes (decay-weighted; recent = closer)
// Tier 5:  Outer / unclassified
const AVA_BRAIN_TIER_RADII = [0, 80, 180, 300, 450, 600] as const;
const AVA_BRAIN_TIER_COLORS = {
  0: "#a78bfa",  // ava self — warm violet
  1: "#f5c518",  // identity anchors — gold
  2: "#4299e1",  // people — blue (intensity scaled by trust below)
  3: "#ed64a6",  // active threads / curiosity — pink
  4: "#68d391",  // memories — green
  5: "#888888",  // outer/misc — grey
} as const;

function classifyAvaBrainTier(node: any): number {
  // Identity anchors have explicit `_tier` we set when injecting.
  if (typeof node?._tier === "number") return Number(node._tier);
  const t = String(node?.type || "").toLowerCase();
  const id = String(node?.id || "");
  if (id === "ava-self" || id === "ava_self" || t === "ava-self") return 0;
  if (id.startsWith("identity-") || id.startsWith("soul-") || id.startsWith("user-anchor")) return 1;
  if (t === "person") return 2;
  if (t === "curiosity" || t === "concern" || t === "thread") return 3;
  if (t === "memory" || t === "topic" || t === "event" || t === "opinion" || t === "emotion") return 4;
  return 5;
}

function trustScoreOf(node: any): number {
  // trust is sometimes nested in notes or carried as a top-level number.
  // Default 0.4 — middle of the trust band.
  const direct = Number(node?.trust_score ?? node?.trust ?? NaN);
  if (Number.isFinite(direct)) return Math.max(0, Math.min(1, direct));
  return 0.4;
}

function ageDecayOf(node: any): number {
  // 0..1 where 1 = freshly activated, 0 = ancient. Used to pull recent
  // memories closer to center within the memory ring.
  const last = Number(node?.last_activated || 0);
  if (!last) return 0.0;
  const ageDays = Math.max(0, (Date.now() / 1000 - last) / 86400);
  // 0d=1, 7d=0.5, 30d=0.1
  return Math.max(0, Math.exp(-ageDays / 10));
}

// ── Presence-v2 feature flags ───────────────────────────────────────────
// When PRESENCE_V2_ENABLED is true, the streaming speaking-text and
// inner-state-line regions render. The earlier drift symptom (orb falling
// off-screen on snapshot ticks) was traced to two interacting patterns:
//
//   (a) `key={text || "empty"}` on the text divs forced React to unmount
//       and remount the element on every content change.
//   (b) The `from { transform: translateY(2px) }` entry keyframe in
//       speakingFadeIn fires fresh on each remount. Per-tick remounts
//       caused per-tick transform shifts that didn't fully settle before
//       the next remount, producing cumulative drift.
//
// Fix: removed `key=` from both divs so React reuses the same DOM node
// across content changes. The empty→live class transition still drives
// a one-shot fade-in on each speaking session start; subsequent word
// updates within a session no longer trigger animation.
//
// PRESENCE_V2_CUBE_MORPH_ENABLED gates the OrbCanvas cube-morph
// independently so the user can verify text+inner-state stable, then
// flip the cube-morph flag separately if drift returns.
const PRESENCE_V2_ENABLED = true;
const PRESENCE_V2_CUBE_MORPH_ENABLED = false;

const TABS = [
  { id: "voice" as const, label: "Voice" },
  { id: "chat" as const, label: "Chat" },
  { id: "brain" as const, label: "Brain" },
  { id: "status" as const, label: "Status / Heartbeat" },
  { id: "memory" as const, label: "Memory" },
  { id: "tools" as const, label: "Tools" },
  { id: "models" as const, label: "Models / Brains" },
  { id: "creative" as const, label: "Creative" },
  { id: "finetune" as const, label: "Finetune" },
  { id: "workbench" as const, label: "Workbench" },
  { id: "plans" as const, label: "Plans" },
  { id: "journal" as const, label: "Journal" },
  { id: "learning" as const, label: "Learning" },
  { id: "people" as const, label: "People" },
  { id: "emil" as const, label: "Emil" },
  { id: "proposals" as const, label: "Proposals" },
  { id: "identity" as const, label: "Identity" },
  { id: "debug" as const, label: "Debug" },
];
type TabId = (typeof TABS)[number]["id"];

type EmotionVisual = {
  color: string;
  shape:
    | "circle" | "infinity" | "rings" | "teardrop" | "jagged" | "spiral"
    | "flicker" | "awe_pop" | "heart" | "tall"
    // Phase 56 new shapes
    | "cube" | "prism" | "cylinder" | "double_helix" | "burst"
    | "contracted_tremor" | "rising" | "pointer" | string;
  pulse: "idle" | "thinking" | "deep" | "speaking" | "bored" | "excited" | "confused" | "offline" | "listening";
};

function hexToRgbTriplet(hex: string): string {
  const clean = hex.replace("#", "").trim();
  const full = clean.length === 3 ? clean.split("").map((c) => `${c}${c}`).join("") : clean;
  const n = Number.parseInt(full.slice(0, 6), 16);
  if (!Number.isFinite(n)) return "26 108 245";
  return `${(n >> 16) & 255} ${(n >> 8) & 255} ${n & 255}`;
}

function shadeHex(hex: string, factor: number): string {
  const clean = hex.replace("#", "").trim();
  const full = clean.length === 3 ? clean.split("").map((c) => `${c}${c}`).join("") : clean;
  const n = Number.parseInt(full.slice(0, 6), 16);
  if (!Number.isFinite(n)) return hex;
  const r = Math.max(0, Math.min(255, Math.round(((n >> 16) & 255) * factor)));
  const g = Math.max(0, Math.min(255, Math.round(((n >> 8) & 255) * factor)));
  const b = Math.max(0, Math.min(255, Math.round((n & 255) * factor)));
  return `rgb(${r}, ${g}, ${b})`;
}

type BrainNode = {
  id: string;
  label: string;
  type: string;
  weight: number;
  last_activated: number;
  activation_count: number;
  color: string;
  notes: string;
};

type BrainEdge = {
  source: string;
  target: string;
  relationship: string;
  strength: number;
  last_fired: number;
};

type BrainRenderNode = BrainNode & {
  x?: number;
  y?: number;
  fx?: number | null;
  fy?: number | null;
};

type BrainRenderEdge = BrainEdge & {
  source: string | BrainRenderNode;
  target: string | BrainRenderNode;
};

type BrainGraphRender = {
  g: d3.Selection<SVGGElement, unknown, null, undefined>;
  link: d3.Selection<SVGLineElement, BrainRenderEdge, SVGGElement, unknown>;
  node: d3.Selection<SVGCircleElement, BrainRenderNode, SVGGElement, unknown>;
  ring: d3.Selection<SVGCircleElement, BrainRenderNode, SVGGElement, unknown>;
  labels: d3.Selection<SVGTextElement, BrainRenderNode, SVGGElement, unknown>;
  nodes: BrainRenderNode[];
  links: BrainRenderEdge[];
};

const BRAIN_NODE_TYPES: Array<{ type: string; color: string; description: string }> = [
  { type: "person", color: "#ed64a6", description: "People Ava knows" },
  { type: "topic", color: "#4299e1", description: "Subjects and concepts" },
  { type: "emotion", color: "#f5c518", description: "Emotional states" },
  { type: "memory", color: "#9f7aea", description: "Stored memories" },
  { type: "opinion", color: "#ecc94b", description: "Ava's formed opinions" },
  { type: "curiosity", color: "#00d4d4", description: "Things Ava wonders about" },
  { type: "self", color: "#68d391", description: "Ava's self-concept" },
  { type: "event", color: "#ff6b00", description: "Events that happened" },
];

function brainEdgeKey(edge: Pick<BrainEdge, "source" | "target" | "relationship">): string {
  return `${String(edge.source)}->${String(edge.target)}::${String(edge.relationship || "")}`;
}

type FinetunePrereq = {
  ready?: boolean;
  issues?: string[];
  checks?: Record<string, boolean>;
  free_gb?: number;
};

const EMOTION_VISUALS: Record<string, EmotionVisual> = {
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
};

function asRecord(v: unknown): Record<string, unknown> | undefined {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : undefined;
}

// ── Custom tab renderer ──────────────────────────────────────────────────────
// Tabs created at runtime via brain/command_builder. Read from
// /api/v1/ui/custom_tabs. The config.content_type drives the rendering.

type CustomTabConfig = {
  id: string;
  name: string;
  content_type: string;
  data_source?: string;
  config?: Record<string, unknown>;
};

function CustomTabRenderer({ config, snap }: { config: CustomTabConfig; snap: Snapshot | null }) {
  const t = config.content_type;
  if (t === "web_embed") {
    const url = String(config.data_source || "");
    return (
      <div className="op-pane">
        <h1 className="op-h1">{config.name}</h1>
        {url ? (
          <iframe
            src={url}
            title={config.name}
            style={{ width: "100%", height: "70vh", border: "none", borderRadius: 6, background: "#0b0f1a" }}
          />
        ) : (
          <p className="op-muted">No URL configured.</p>
        )}
      </div>
    );
  }
  if (t === "journal_view") {
    return (
      <div className="op-pane">
        <h1 className="op-h1">{config.name}</h1>
        <p className="op-muted">Pulls from <code>{String(config.data_source || "/api/v1/journal/shared")}</code>.</p>
        <div style={{ marginTop: 12, opacity: 0.85 }}>
          {/* Lightweight inline viewer — full impl reads journal entries on focus. */}
          <iframe
            src={`${API_BASE}${String(config.data_source || "/api/v1/journal/shared")}`}
            title={`${config.name}-journal`}
            style={{ width: "100%", height: "60vh", border: "none", borderRadius: 6, background: "#0b0f1a" }}
          />
        </div>
      </div>
    );
  }
  if (t === "custom_stats") {
    const fields = (config.config?.stats_fields as string[] | undefined) || [];
    return (
      <div className="op-pane">
        <h1 className="op-h1">{config.name}</h1>
        <pre className="debug-pre" style={{ maxHeight: "70vh", overflow: "auto" }}>
          {fields.length === 0
            ? JSON.stringify(snap, null, 2)
            : JSON.stringify(
                fields.reduce<Record<string, unknown>>((acc, f) => {
                  acc[f] = (snap as Record<string, unknown> | null)?.[f];
                  return acc;
                }, {}),
                null,
                2,
              )}
        </pre>
      </div>
    );
  }
  if (t === "image_gallery") {
    return (
      <div className="op-pane">
        <h1 className="op-h1">{config.name}</h1>
        <p className="op-muted">Image gallery — fetch from <code>{String(config.data_source || "/api/v1/images/list")}</code>.</p>
      </div>
    );
  }
  if (t === "data_display") {
    return (
      <div className="op-pane">
        <h1 className="op-h1">{config.name}</h1>
        <p className="op-muted">Data from <code>{String(config.data_source || "")}</code>.</p>
      </div>
    );
  }
  if (t === "chat_log") {
    return (
      <div className="op-pane">
        <h1 className="op-h1">{config.name}</h1>
        <p className="op-muted">Chat log filter — UI TBD.</p>
      </div>
    );
  }
  return (
    <div className="op-pane">
      <h1 className="op-h1">{config.name}</h1>
      <p className="op-muted">Unknown content type: <code>{t}</code></p>
    </div>
  );
}

type CustomTab = {
  id: string;
  name: string;
  content_type: string;
  data_source?: string;
  config?: Record<string, unknown>;
};

export default function App() {
  // Tab id can be a built-in TabId OR a custom tab id created at runtime.
  const [tab, setTab] = useState<string>("voice");
  const [customTabs, setCustomTabs] = useState<CustomTab[]>([]);
  const [online, setOnline] = useState(false);
  const [connecting, setConnecting] = useState(true);
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [pollErr, setPollErr] = useState<string>("");
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);

  const [chatInput, setChatInput] = useState("");
  const [chatBusy, setChatBusy] = useState(false);
  const [chatHist, setChatHist] = useState<ChatMessage[]>([]);
  const [lastChatErr, setLastChatErr] = useState<string>("");
  const [wbActionMsg, setWbActionMsg] = useState<string>("");
  // Phase 55: drag-drop file input
  const [dropHover, setDropHover] = useState(false);
  const [dropProcessing, setDropProcessing] = useState(false);
  const [chatThinking, setChatThinking] = useState(false);
  const chatScrollRef = useRef<HTMLDivElement | null>(null);
  const [cameraTick, setCameraTick] = useState(() => Date.now());
  const [sttListening, setSttListening] = useState(false);
  const [sttProcessing, setSttProcessing] = useState(false);
  const [cameraFrameOk, setCameraFrameOk] = useState(false);
  const [presenceCameraOk, setPresenceCameraOk] = useState(false);
  const [brainGraph, setBrainGraph] = useState<{ nodes: BrainNode[]; edges: BrainEdge[]; stats?: Record<string, unknown> }>({
    nodes: [],
    edges: [],
  });
  const [brainLoading, setBrainLoading] = useState(false);
  const [brainGraphError, setBrainGraphError] = useState<string>("");
  const [brainActive, setBrainActive] = useState<{ active_nodes: BrainNode[]; firing_paths: BrainEdge[] }>({
    active_nodes: [],
    firing_paths: [],
  });
  const [selectedBrainNode, setSelectedBrainNode] = useState<BrainNode | null>(null);
  // Middle-click → recenter orb. orbRecenterCounter increments on each
  // middle-click; OrbCanvas watches the prop and snaps drift back to (0,0).
  // orbRecenterPulse drives a brief scale-up CSS animation so the user gets
  // immediate visual feedback even before the Three.js drift settles.
  const [orbRecenterCounter, setOrbRecenterCounter] = useState(0);
  const [orbRecenterPulse, setOrbRecenterPulse] = useState(false);
  // 3D brain graph (3d-force-graph). The container div is the mount point;
  // fgRef holds the constructed ForceGraph3D instance so we can update data
  // without rebuilding the WebGL scene.
  const brainGraph3DContainerRef = useRef<HTMLDivElement | null>(null);
  const brainGraph3DInstanceRef = useRef<any>(null);
  const [brainGraphSyncing, setBrainGraphSyncing] = useState<boolean>(false);
  // Legacy 2D refs kept for typing — unused now but referenced in stale effects.
  const brainSvgRef = useRef<SVGSVGElement | null>(null);
  const brainZoomTransformRef = useRef(d3.zoomIdentity);
  const brainRenderRef = useRef<BrainGraphRender | null>(null);
  const brainSimulationRef = useRef<d3.Simulation<BrainRenderNode, undefined> | null>(null);
  const brainInitDoneRef = useRef(false);
  const lastBrainInitCountRef = useRef(0);
  const brainInitSvgRef = useRef<SVGSVGElement | null>(null);
  const firedEdgesRef = useRef<Map<string, number>>(new Map());
  const [brainStatsBar, setBrainStatsBar] = useState({ nodes: 0, edges: 0, active: 0, mostConnected: "—" });
  const [finetuneStatus, setFinetuneStatus] = useState<Record<string, unknown>>({});
  const [finetunePrep, setFinetunePrep] = useState<Record<string, unknown>>({});
  const [finetunePrereq, setFinetunePrereq] = useState<FinetunePrereq>({});
  const [finetuneLog, setFinetuneLog] = useState<string[]>([]);
  const [finetuneBusy, setFinetuneBusy] = useState(false);

  const [overrideModel, setOverrideModel] = useState("");
  const [overrideMode, setOverrideMode] = useState("");
  const [routeMsg, setRouteMsg] = useState<string>("");

  const [identity, setIdentity] = useState<{ identity: string; soul: string; user: string; err?: string }>({
    identity: "",
    soul: "",
    user: "",
  });

  const [plans, setPlans] = useState<{ plans: Record<string, unknown>[]; active_count: number } | null>(null);
  const [plansBusy, setPlansBusy] = useState(false);
  const [planGoalInput, setPlanGoalInput] = useState("");
  const [planMsg, setPlanMsg] = useState("");

  // Phase 86: journal state
  const [journalEntries, setJournalEntries] = useState<Record<string, unknown>[] | null>(null);
  const [journalBusy, setJournalBusy] = useState(false);
  const [journalTotal, setJournalTotal] = useState(0);
  const [journalSharedCount, setJournalSharedCount] = useState(0);

  // Phase 94: learning tab state
  const [learningLog, setLearningLog] = useState<Record<string, unknown>[] | null>(null);
  const [learningGaps, setLearningGaps] = useState<string[]>([]);
  const [learningWeekSummary, setLearningWeekSummary] = useState<string>("");
  const [learningBusy, setLearningBusy] = useState(false);

  // Phase 94: people tab state
  const [profiles, setProfiles] = useState<Record<string, unknown>[] | null>(null);
  const [peopleBusy, setPeopleBusy] = useState(false);

  const [emilStatus, setEmilStatus] = useState<Record<string, unknown> | null>(null);
  const [emilSendMsg, setEmilSendMsg] = useState("");
  const [emilSendInput, setEmilSendInput] = useState("");
  const [emilBusy, setEmilBusy] = useState(false);

  const [proposals, setProposals] = useState<Record<string, unknown>[] | null>(null);
  const [proposalMsg, setProposalMsg] = useState("");
  const [proposalsBusy, setProposalsBusy] = useState(false);

  const [apiCallLog, setApiCallLog] = useState<ApiLogEntry[]>([]);
  const [lastChatResponse, setLastChatResponse] = useState<Record<string, unknown> | null>(null);
  const [lastSnapshotRaw, setLastSnapshotRaw] = useState<Snapshot | null>(null);
  const [debugExportText, setDebugExportText] = useState<string>("");
  const [debugExportBusy, setDebugExportBusy] = useState(false);
  const [appEventLog, setAppEventLog] = useState<{ ts: string; message: string }[]>([]);
  const [shutdownConfirmOpen, setShutdownConfirmOpen] = useState(false);
  const [shutdownInProgress, setShutdownInProgress] = useState(false);
  const [shutdownGoodbye, setShutdownGoodbye] = useState("");
  const [shutdownDone, setShutdownDone] = useState(false);
  const [shutdownError, setShutdownError] = useState<string>("");
  const [shutdownWindowCloseHint, setShutdownWindowCloseHint] = useState(false);
  const [inputMuted, setInputMuted] = useState(false);
  const [backendShutdownDetected, setBackendShutdownDetected] = useState(false);
  const [operatorOpen, setOperatorOpen] = useState(false);
  const [cameraOverlayOpen, setCameraOverlayOpen] = useState(false);

  // Connectivity state
  const [connOnline, setConnOnline] = useState(false);
  const [connQuality, setConnQuality] = useState<string>("offline");
  const [connCloudAvailable, setConnCloudAvailable] = useState(false);

  // Creative / image generation state
  const [imagePrompt, setImagePrompt] = useState("");
  const [imageStyle, setImageStyle] = useState("");
  const [imageBusy, setImageBusy] = useState(false);
  const [imageResult, setImageResult] = useState<string | null>(null);
  const [imageList, setImageList] = useState<Record<string, unknown>[]>([]);
  const [imageListBusy, setImageListBusy] = useState(false);
  const [comfyuiOnline, setComfyuiOnline] = useState(false);

  // Phase 79: onboarding overlay
  const [onboardingActive, setOnboardingActive] = useState(false);
  const [onboardingStage, setOnboardingStage] = useState<string | null>(null);
  const [onboardingReply, setOnboardingReply] = useState<string>("");
  const [onboardingInput, setOnboardingInput] = useState("");
  const [onboardingBusy, setOnboardingBusy] = useState(false);
  const [onboardingStageIndex, setOnboardingStageIndex] = useState(0);
  const [onboardingStageCount, setOnboardingStageCount] = useState(13);
  const PHOTO_STAGES_UI = ["photo_front", "photo_left", "photo_right", "photo_up", "photo_down"];
  const [liveFrameSrc, setLiveFrameSrc] = useState<string | null>(null);
  // mem0 memory state — refreshed every 30s while Memory tab is active.
  const [mem0Entries, setMem0Entries] = useState<Array<Record<string, unknown>>>([]);
  const [mem0Searching, setMem0Searching] = useState<boolean>(false);
  const [mem0SearchQuery, setMem0SearchQuery] = useState<string>("");
  const [mem0SearchResults, setMem0SearchResults] = useState<Array<Record<string, unknown>> | null>(null);
  // Fast TTS amplitude poll — drives the orb's real-time speech reaction.
  // Pulled separately from the slow snapshot poll so the orb pulses with each
  // syllable instead of every 5 seconds.
  const [liveTtsAmp, setLiveTtsAmp] = useState<number>(0);
  const [liveTtsSpeaking, setLiveTtsSpeaking] = useState<boolean>(false);
  const liveTtsSpeakingRef = useRef<boolean>(false);
  liveTtsSpeakingRef.current = liveTtsSpeaking;
  // Inner thought cross-fade: hold the displayed thought, fade in new ones.
  const [displayedThought, setDisplayedThought] = useState<string>("");
  const [thoughtVisible, setThoughtVisible] = useState<boolean>(false);
  const prevOnlineRef = useRef<boolean | null>(null);
  const appStartedAtRef = useRef(Date.now());
  const connectStartRef = useRef(Date.now());
  const pollFailureCountRef = useRef(0);
  const sttPollRef = useRef<number | null>(null);

  const pushEvent = useCallback((message: string) => {
    const row = { ts: new Date().toISOString(), message };
    setAppEventLog((prev) => [row, ...prev].slice(0, 400));
  }, [shutdownDone, shutdownInProgress]);

  useEffect(() => {
    registerApiLogger((entry) => {
      setApiCallLog((prev) => [entry, ...prev].slice(0, 50));
    });
    return () => registerApiLogger(null);
  }, []);

  // Widget orb: show when main window minimizes, hide when restored
  useEffect(() => {
    let unlistenFocus: (() => void) | null = null;
    let unlistenBlur: (() => void) | null = null;
    let pollId: ReturnType<typeof setInterval> | null = null;
    let wasMinimized = false;

    const setupWidgetListeners = async () => {
      try {
        const { getCurrentWindow } = await import("@tauri-apps/api/window");
        const { WebviewWindow } = await import("@tauri-apps/api/webviewWindow");
        const mainWin = getCurrentWindow();

        const showWidget = async () => {
          try {
            const widget = await WebviewWindow.getByLabel("widget");
            if (widget) await widget.show();
          } catch { /* widget window may not exist */ }
        };

        const hideWidget = async () => {
          try {
            const widget = await WebviewWindow.getByLabel("widget");
            if (widget) await widget.hide();
          } catch { /* widget window may not exist */ }
        };

        // Event-based: blur fires when minimize button is clicked on Windows
        unlistenBlur = await mainWin.listen("tauri://blur", async () => {
          await new Promise((r) => setTimeout(r, 200));
          try {
            const minimized = await mainWin.isMinimized();
            if (minimized && !wasMinimized) {
              wasMinimized = true;
              await showWidget();
            }
          } catch { /* silent — no fallback; polling handles it */ }
        });

        unlistenFocus = await mainWin.listen("tauri://focus", async () => {
          if (wasMinimized) {
            wasMinimized = false;
            await hideWidget();
          }
        });

        // Polling backup: catches cases where blur event doesn't fire (e.g. Win+D)
        pollId = setInterval(async () => {
          try {
            const minimized = await mainWin.isMinimized();
            if (minimized !== wasMinimized) {
              wasMinimized = minimized;
              if (minimized) await showWidget();
              else await hideWidget();
            }
          } catch { /* Tauri not available in browser */ }
        }, 500);
      } catch {
        // Not running inside Tauri (dev browser) — no-op
      }
    };

    void setupWidgetListeners();
    return () => {
      unlistenFocus?.();
      unlistenBlur?.();
      if (pollId !== null) clearInterval(pollId);
    };
  }, []);

  const refreshSnapshotOnly = useCallback(async () => {
    setPollErr("");
    try {
      await getJson<{ ok?: boolean }>("/api/v1/health");
      const s = await getJson<Snapshot>("/api/v1/snapshot");
      setSnap(s);
      setLastSnapshotRaw(s);
      pollFailureCountRef.current = 0;
      setOnline(true);
      setConnecting(false);
      setBackendShutdownDetected(false);
      setLastUpdated(typeof s.ts === "number" ? s.ts * 1000 : Date.now());
    } catch (e) {
      pollFailureCountRef.current += 1;
      const elapsedMs = Date.now() - connectStartRef.current;
      const stillConnecting = elapsedMs < 120_000;
      if (stillConnecting) {
        // In connecting window — stay completely silent, keep retrying
        return;
      }
      // Past connecting window: require 3 consecutive failures before going offline.
      // This prevents a single slow/failed poll from causing an offline flash.
      if (pollFailureCountRef.current >= 3) {
        setConnecting(false);
        if (prevOnlineRef.current === true || shutdownInProgress || shutdownDone) {
          setBackendShutdownDetected(true);
        }
        setOnline(false);
        setSnap(null);
        setLastSnapshotRaw(null);
        setPollErr(e instanceof Error ? e.message : String(e));
      }
    }
  }, []);

  const pollChatHistory = useCallback(async (): Promise<ChatMessage[] | null> => {
    try {
      const histRes = await getJson<{ ok?: boolean; messages?: ChatMessage[] }>("/api/v1/chat/history");
      if (Array.isArray(histRes.messages)) {
        setChatHist(histRes.messages);
        return histRes.messages;
      }
    } catch {
      /* optional */
    }
    return null;
  }, []);

  const poll = useCallback(async () => {
    await refreshSnapshotOnly();
    await pollChatHistory();
  }, [refreshSnapshotOnly, pollChatHistory]);

  useEffect(() => {
    if (prevOnlineRef.current === null) {
      prevOnlineRef.current = online;
      return;
    }
    if (prevOnlineRef.current !== online) {
      pushEvent(`Backend ${online ? "online" : "offline"}`);
      prevOnlineRef.current = online;
    }
  }, [online, pushEvent]);

  useEffect(() => {
    void poll();
    const id = window.setInterval(() => void poll(), 5000);
    return () => window.clearInterval(id);
  }, [poll]);

  // ── Drift diagnostic ────────────────────────────────────────────────────
  // Measurement-only — DO NOT add fixes here. Three rounds of fix→regression
  // on the orb-drift bug means we don't actually know what's growing or
  // where the scroll is happening. This effect samples DOM measurements
  // every 5s on the same cadence as the snapshot poll, tagged with a tick
  // counter, so the user can paste 12+ ticks of console output and we can
  // diff tick=1 vs tick=12 to find the actual culprit.
  useEffect(() => {
    let tick = 0;
    const measure = () => {
      const sample = (sel: string) => {
        const el = document.querySelector(sel) as HTMLElement | null;
        if (!el) return { found: false } as const;
        const cs = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return {
          found: true,
          clientHeight: el.clientHeight,
          scrollHeight: el.scrollHeight,
          scrollTop: el.scrollTop,
          offsetHeight: el.offsetHeight,
          computedHeight: cs.height,
          computedMinHeight: cs.minHeight,
          rectTop: Math.round(rect.top * 100) / 100,
          rectBottom: Math.round(rect.bottom * 100) / 100,
        } as const;
      };
      const docEl = document.documentElement;
      const body = document.body;
      const data = {
        tick,
        time: new Date().toISOString().slice(11, 23),
        windowInner: { w: window.innerWidth, h: window.innerHeight },
        documentElement: {
          scrollTop: docEl.scrollTop,
          scrollHeight: docEl.scrollHeight,
          clientHeight: docEl.clientHeight,
        },
        body: {
          scrollTop: body.scrollTop,
          scrollHeight: body.scrollHeight,
          clientHeight: body.clientHeight,
        },
        presenceRoot: sample(".presence-root"),
        presenceStage: sample(".presence-stage"),
        presenceHudRow: sample(".presence-hud-row"),
        presenceSpeakingText: sample(".presence-speaking-text"),
        presenceOrbWrap: sample(".presence-orb-wrap"),
        orbCanvasShell: sample(".orb-canvas-shell"),
        presenceInnerStateLine: sample(".presence-inner-state-line"),
        presenceHudRowBottom: sample(".presence-hud-row-bottom"),
        presenceInputRow: sample(".presence-input-row"),
      };
      // One line per tick, structured — copy/paste-friendly for diffing.
      // eslint-disable-next-line no-console
      console.log(`[drift-debug tick=${tick}]`, JSON.stringify(data));
      tick += 1;
    };
    measure(); // tick=0 baseline at mount
    const id = window.setInterval(measure, 5000);
    return () => window.clearInterval(id);
  }, []);

  // Live TTS amplitude — polls /api/v1/tts/state at 100ms while online so the
  // orb reacts to the actual audio Kokoro is producing in real time.
  useEffect(() => {
    if (!online) {
      setLiveTtsAmp(0);
      setLiveTtsSpeaking(false);
      return;
    }
    let active = true;
    let timeoutId: number;
    const tick = async () => {
      try {
        const r = await getJson<{ ok: boolean; speaking?: boolean; amplitude?: number }>("/api/v1/tts/state");
        if (active && r) {
          setLiveTtsAmp(Number(r.amplitude ?? 0));
          setLiveTtsSpeaking(Boolean(r.speaking));
        }
      } catch { /* ignore */ }
      if (active) {
        // Poll fast (100ms) while speaking, slower (500ms) when idle.
        const next = liveTtsSpeakingRef.current ? 100 : 500;
        timeoutId = window.setTimeout(tick, next);
      }
    };
    timeoutId = window.setTimeout(tick, 0);
    return () => { active = false; window.clearTimeout(timeoutId); };
  }, [online]);

  // ── UI tab routing — polls /api/v1/ui/tab every 1s; switches tab when set
  useEffect(() => {
    if (!online) return;
    let active = true;
    const tick = async () => {
      try {
        const r = await getJson<{ tab?: string | null }>("/api/v1/ui/tab");
        if (active && r && typeof r.tab === "string" && r.tab) {
          setTab(r.tab);
        }
      } catch { /* ignore */ }
    };
    const id = window.setInterval(tick, 1000);
    return () => { active = false; window.clearInterval(id); };
  }, [online]);

  // ── Mem0 memories — polled when Memory tab is open. Lightweight list.
  useEffect(() => {
    if (!online || tab !== "memory") return;
    let active = true;
    const fetchMem = async () => {
      try {
        const r = await getJson<{ ok?: boolean; entries?: Array<Record<string, unknown>> }>("/api/v1/memory/mem0");
        if (active && Array.isArray(r?.entries)) setMem0Entries(r.entries);
      } catch { /* ignore */ }
    };
    void fetchMem();
    const id = window.setInterval(fetchMem, 30000);
    return () => { active = false; window.clearInterval(id); };
  }, [online, tab]);

  const runMem0Search = useCallback(async () => {
    const q = mem0SearchQuery.trim();
    if (!q) return;
    setMem0Searching(true);
    try {
      const r = await postJson<{ ok?: boolean; results?: Array<Record<string, unknown>> }>(
        "/api/v1/memory/mem0/search", { query: q, limit: 10 }
      );
      setMem0SearchResults(Array.isArray(r?.results) ? r.results : []);
    } catch {
      setMem0SearchResults([]);
    } finally {
      setMem0Searching(false);
    }
  }, [mem0SearchQuery]);

  const deleteMem0Entry = useCallback(async (id: string) => {
    try {
      await fetch(`${API_BASE}/api/v1/memory/mem0/${id}`, { method: "DELETE" });
      setMem0Entries((prev) => prev.filter((e) => String(e.id) !== id));
      setMem0SearchResults((prev) => prev ? prev.filter((e) => String(e.id) !== id) : prev);
    } catch { /* ignore */ }
  }, []);

  // ── Custom tabs — polled at mount and every 30s. Tab list is small.
  useEffect(() => {
    if (!online) return;
    let active = true;
    const fetchTabs = async () => {
      try {
        const r = await getJson<{ ok?: boolean; tabs?: CustomTab[] }>("/api/v1/ui/custom_tabs");
        if (active && Array.isArray(r?.tabs)) setCustomTabs(r.tabs);
      } catch { /* ignore */ }
    };
    void fetchTabs();
    const id = window.setInterval(fetchTabs, 30000);
    return () => { active = false; window.clearInterval(id); };
  }, [online]);

  // Inner-thought cross-fade animation. When the snapshot reports a new
  // current_thought, fade out the old one over 1s, swap, then fade in over 1s
  // and hold for 8s. Idempotent if the thought string doesn't change.
  // Read inline so this effect doesn't depend on a downstream declaration.
  const _innerLifeThoughtFromSnap = String(
    ((snap as Record<string, unknown> | null)?.inner_life as Record<string, unknown> | undefined)
      ?.current_thought ?? ""
  ).trim();
  useEffect(() => {
    if (!_innerLifeThoughtFromSnap) {
      setThoughtVisible(false);
      return;
    }
    if (_innerLifeThoughtFromSnap === displayedThought) {
      setThoughtVisible(true);
      return;
    }
    setThoughtVisible(false);
    const swap = window.setTimeout(() => {
      setDisplayedThought(_innerLifeThoughtFromSnap);
      setThoughtVisible(true);
    }, 1000);
    const fadeOut = window.setTimeout(() => {
      setThoughtVisible(false);
    }, 9000);
    return () => {
      window.clearTimeout(swap);
      window.clearTimeout(fadeOut);
    };
  }, [_innerLifeThoughtFromSnap]); // eslint-disable-line react-hooks/exhaustive-deps

  // Live camera feed — polls /api/v1/camera/live_frame at ~5fps. Always running,
  // not gated by active tab, so any view that displays the camera always has a
  // fresh frame ready.
  useEffect(() => {
    if (!online) {
      setLiveFrameSrc(null);
      return;
    }
    let active = true;
    let timeoutId: number;
    const fetchFrame = async () => {
      try {
        const r = await getJson<{ ok: boolean; b64?: string; age_sec?: number }>("/api/v1/camera/live_frame");
        if (active) {
          if (r.ok && r.b64) {
            setLiveFrameSrc(`data:image/jpeg;base64,${r.b64}`);
          } else {
            setLiveFrameSrc(null);
          }
        }
      } catch { /* ignore */ }
      if (active) timeoutId = window.setTimeout(fetchFrame, 200);
    };
    timeoutId = window.setTimeout(fetchFrame, 0);
    return () => { active = false; window.clearTimeout(timeoutId); };
  }, [online]);

  // Phase 63: WebSocket real-time transport (with REST fallback)
  useEffect(() => {
    let ws: WebSocket | null = null;
    let reconnectId: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      try {
        const wsUrl = API_BASE.replace(/^http/, "ws") + "/ws";
        ws = new WebSocket(wsUrl);
        ws.onopen = () => { /* connected */ };
        ws.onmessage = (ev) => {
          try {
            const msg = JSON.parse(ev.data as string);
            if (msg.type === "snapshot" && msg.data) {
              setSnap(msg.data as Snapshot);
              // online state is authoritative from REST poll only — WS never sets it
            } else if (msg.type === "delta") {
              setSnap((prev) => prev ? { ...prev, ...Object.fromEntries(Object.entries(msg).filter(([k]) => k !== "type")) } : prev);
            }
          } catch { /* ignore parse errors */ }
        };
        ws.onerror = () => { /* silent — REST poll is authoritative for online state */ };
        ws.onclose = () => {
          ws = null;
          if (reconnectId) clearTimeout(reconnectId);
          reconnectId = setTimeout(connect, 5000);
        };
      } catch { /* WebSocket unavailable */ }
    };

    connect();
    return () => {
      if (reconnectId) clearTimeout(reconnectId);
      ws?.close();
    };
  }, []);

  // Phase 55: drag-drop file input via Tauri event
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    let unlistenHover: (() => void) | undefined;
    let unlistenLeave: (() => void) | undefined;

    const setup = async () => {
      try {
        unlistenHover = await listen("tauri://drag-over", () => setDropHover(true)) as unknown as () => void;
        unlistenLeave = await listen("tauri://drag-leave", () => setDropHover(false)) as unknown as () => void;
        unlisten = await listen<{ paths: string[] }>("tauri://drop", async (event) => {
          setDropHover(false);
          const paths: string[] = event.payload?.paths ?? [];
          if (!paths.length) return;
          setDropProcessing(true);
          try {
            for (const p of paths.slice(0, 3)) {
              await postJson("/api/v1/chat", { message: `[Dropped file: ${p}]` });
            }
            await poll();
          } catch { /* ok */ } finally {
            setDropProcessing(false);
          }
        }) as unknown as () => void;
      } catch { /* Tauri API unavailable in browser */ }
    };
    void setup();
    return () => { unlisten?.(); unlistenHover?.(); unlistenLeave?.(); };
  }, [poll]);

  useEffect(() => {
    if (tab !== "identity") return;
    let cancelled = false;
    (async () => {
      try {
        const [identityMd, soul, user] = await Promise.all([
          getText("/api/v1/identity/IDENTITY").catch(() => ""),
          getText("/api/v1/identity/SOUL").catch(() => ""),
          getText("/api/v1/identity/USER").catch(() => ""),
        ]);
        if (!cancelled) setIdentity({ identity: identityMd, soul, user });
      } catch (e) {
        if (!cancelled)
          setIdentity((x) => ({
            ...x,
            err: e instanceof Error ? e.message : String(e),
          }));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [tab]);

  useEffect(() => {
    const el = chatScrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [chatHist, chatThinking]);

  const fetchPlans = useCallback(async () => {
    setPlansBusy(true);
    try {
      const data = await getJson("/api/v1/plans");
      setPlans(data as { plans: Record<string, unknown>[]; active_count: number });
    } catch {
      // keep stale
    } finally {
      setPlansBusy(false);
    }
  }, []);

  useEffect(() => {
    if (tab !== "plans") return;
    void fetchPlans();
  }, [tab, fetchPlans]);

  useEffect(() => {
    if (tab !== "journal") return;
    setJournalBusy(true);
    getJson("/api/v1/journal/entries")
      .then((d) => {
        const r = d as Record<string, unknown>;
        setJournalEntries((r.entries as Record<string, unknown>[]) ?? []);
        setJournalTotal(typeof r.total === "number" ? r.total : 0);
        setJournalSharedCount(typeof r.shared_count === "number" ? r.shared_count : 0);
      })
      .catch(() => {})
      .finally(() => setJournalBusy(false));
  }, [tab]);

  useEffect(() => {
    if (tab !== "learning") return;
    setLearningBusy(true);
    Promise.all([
      getJson("/api/v1/learning/log"),
      getJson("/api/v1/learning/gaps"),
      getJson("/api/v1/learning/week"),
    ]).then(([log, gaps, week]) => {
      setLearningLog((log as Record<string, unknown>).entries as Record<string, unknown>[]);
      setLearningGaps((gaps as Record<string, unknown>).gaps as string[] ?? []);
      setLearningWeekSummary(String((week as Record<string, unknown>).summary ?? ""));
    }).catch(() => {}).finally(() => setLearningBusy(false));
  }, [tab]);

  useEffect(() => {
    if (tab !== "people") return;
    setPeopleBusy(true);
    getJson("/api/v1/profiles/list")
      .then((d) => setProfiles((d as Record<string, unknown>).profiles as Record<string, unknown>[]))
      .catch(() => {})
      .finally(() => setPeopleBusy(false));
  }, [tab]);

  useEffect(() => {
    if (tab !== "emil") return;
    getJson("/api/v1/emil/status").then((d) => setEmilStatus(d as Record<string, unknown>)).catch(() => {});
  }, [tab]);

  const fetchProposals = useCallback(async () => {
    setProposalsBusy(true);
    try {
      const d = await getJson("/api/v1/identity/proposals");
      const r = d as Record<string, unknown>;
      setProposals((r.proposals as Record<string, unknown>[]) ?? []);
    } catch {
      // keep stale
    } finally {
      setProposalsBusy(false);
    }
  }, []);

  useEffect(() => {
    if (tab !== "proposals") return;
    void fetchProposals();
  }, [tab, fetchProposals]);

  useEffect(() => {
    const id = window.setInterval(() => {
      setCameraTick(Date.now());
    }, 2000);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    return () => {
      if (sttPollRef.current !== null) {
        window.clearInterval(sttPollRef.current);
      }
    };
  }, []);

  // Sync connectivity from snapshot
  useEffect(() => {
    if (!snap) return;
    const conn = asRecord(snap.connectivity);
    if (!conn) return;
    setConnOnline(Boolean(conn.online));
    setConnQuality(typeof conn.quality === "string" ? conn.quality : "offline");
    setConnCloudAvailable(Boolean(conn.cloud_models_available));
  }, [snap]);

  // Creative tab: load image list
  useEffect(() => {
    if (tab !== "creative") return;
    setImageListBusy(true);
    Promise.all([
      getJson("/api/v1/images/list"),
      getJson("/api/v1/connectivity"),
    ]).then(([imgList, conn]) => {
      const il = imgList as Record<string, unknown>;
      setImageList((il.images as Record<string, unknown>[]) ?? []);
      const c = conn as Record<string, unknown>;
      setComfyuiOnline(false); // will be set by separate check
      setConnOnline(Boolean(c.online));
      setConnCloudAvailable(Boolean(c.cloud_reachable));
    }).catch(() => {}).finally(() => setImageListBusy(false));
  }, [tab]);

  // Phase 79: sync onboarding state from snapshot
  useEffect(() => {
    if (!snap) return;
    const ob = asRecord(snap.onboarding);
    if (!ob) return;
    const active = Boolean(ob.active);
    setOnboardingActive(active);
    if (active) {
      setOnboardingStage(typeof ob.stage === "string" ? ob.stage : null);
      setOnboardingStageIndex(typeof ob.stage_index === "number" ? ob.stage_index : 0);
      setOnboardingStageCount(typeof ob.stage_count === "number" ? ob.stage_count : 13);
    }
  }, [snap]);

  const pollBrainGraph = useCallback(async (reason = "interval") => {
    setBrainLoading(true);
    setBrainGraphError("");
    pushEvent(`Brain graph fetch start (${reason})`);
    try {
      const res = await getJson<{ nodes?: BrainNode[]; edges?: BrainEdge[]; stats?: Record<string, unknown> }>(
        "/api/v1/brain/graph"
      );
      const nodes = Array.isArray(res.nodes) ? res.nodes : [];
      const edges = Array.isArray(res.edges) ? res.edges : [];
      pushEvent(`Brain graph fetch success: nodes=${nodes.length}, edges=${edges.length}`);
      // Stabilize: keep the previous reference if data is effectively unchanged.
      // This stops the D3 init effect from firing on every poll.
      setBrainGraph((prev) => {
        if (prev.nodes.length === nodes.length && prev.edges.length === edges.length) {
          let same = true;
          for (let i = 0; i < nodes.length; i++) {
            const a = nodes[i];
            const b = prev.nodes[i];
            if (
              !b ||
              a.id !== b.id ||
              a.label !== b.label ||
              Math.abs(Number(a.weight || 0) - Number(b.weight || 0)) > 0.05
            ) {
              same = false;
              break;
            }
          }
          if (same) return prev;
        }
        return { nodes, edges, stats: res.stats ?? {} };
      });
      console.log("[brain] /api/v1/brain/graph", { nodes: nodes.length, edges: edges.length });
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setBrainGraphError(msg);
      pushEvent(`Brain graph fetch failed: ${msg}`);
      console.log("[brain] /api/v1/brain/graph failed", msg);
      setBrainGraph({
        nodes: [],
        edges: [],
        stats: {},
      });
    } finally {
      setBrainLoading(false);
    }
  }, [pushEvent]);

  const startSttListen = async () => {
    if (sttListening || sttProcessing || inputMuted) return;
    setSttListening(true);
    setSttProcessing(false);
    try {
      await postJson<{ ok?: boolean; listening?: boolean }>("/api/v1/stt/listen", {});
      if (sttPollRef.current !== null) {
        window.clearInterval(sttPollRef.current);
      }
      sttPollRef.current = window.setInterval(() => {
        void (async () => {
          try {
            const res = await getJson<{ text?: string; ready?: boolean; processing?: boolean; error?: string }>(
              "/api/v1/stt/result"
            );
            setSttProcessing(Boolean(res.processing));
            if (!res.ready) return;
            if (sttPollRef.current !== null) {
              window.clearInterval(sttPollRef.current);
              sttPollRef.current = null;
            }
            setSttListening(false);
            setSttProcessing(false);
            const text = String(res.text ?? "").trim();
            if (text) {
              setChatInput(text);
              await sendChatText(text);
            } else if (res.error) {
              setLastChatErr(String(res.error));
            }
          } catch {
            // keep polling until ready
          }
        })();
      }, 500);
    } catch (e) {
      setSttListening(false);
      setSttProcessing(false);
      setLastChatErr(e instanceof Error ? e.message : String(e));
    }
  };

  const pollBrainActive = useCallback(async () => {
    try {
      const res = await getJson<{ active_nodes?: BrainNode[]; firing_paths?: BrainEdge[] }>("/api/v1/brain/active");
      const now = Date.now();
      const recent = firedEdgesRef.current;
      for (const e of Array.isArray(res.firing_paths) ? res.firing_paths : []) {
        recent.set(brainEdgeKey(e), now);
      }
      setBrainActive({
        active_nodes: Array.isArray(res.active_nodes) ? res.active_nodes : [],
        firing_paths: Array.isArray(res.firing_paths) ? res.firing_paths : [],
      });
    } catch {
      // optional endpoint
    }
  }, []);

  useEffect(() => {
    if (tab !== "brain") return;
    void pollBrainGraph("tab-open");
    void pollBrainActive();
    const id = window.setInterval(() => {
      void pollBrainGraph("interval");
      void pollBrainActive();
    }, 3000);
    return () => window.clearInterval(id);
  }, [pollBrainGraph, pollBrainActive, tab]);

  const fetchFinetuneStatus = useCallback(async () => {
    try {
      const res = await getJson<Record<string, unknown>>("/api/v1/finetune/status");
      setFinetuneStatus(res);
    } catch {
      // optional endpoint
    }
  }, []);

  const fetchFinetuneLog = useCallback(async () => {
    try {
      const res = await getJson<{ ok?: boolean; lines?: string[] }>("/api/v1/finetune/log");
      setFinetuneLog(Array.isArray(res.lines) ? res.lines.slice(-20) : []);
    } catch {
      // optional endpoint
    }
  }, []);

  const prepareFinetuneDataset = async () => {
    setFinetuneBusy(true);
    try {
      const res = await postJson<Record<string, unknown>>("/api/v1/finetune/prepare", {});
      setFinetunePrep(res);
      const validation = asRecord(res.validation);
      const checks = asRecord(res.checks) as Record<string, boolean> | undefined;
      setFinetunePrereq({
        ready: Boolean(res.ok),
        issues: Array.isArray(validation?.issues) ? (validation?.issues as string[]) : [],
        checks: checks,
      });
      await fetchFinetuneStatus();
      await fetchFinetuneLog();
    } catch (e) {
      setFinetunePrep({ ok: false, error: e instanceof Error ? e.message : String(e) });
    } finally {
      setFinetuneBusy(false);
    }
  };

  const startFinetune = async () => {
    setFinetuneBusy(true);
    try {
      const res = await postJson<Record<string, unknown>>("/api/v1/finetune/start", {});
      const checks = asRecord(res.checks) as Record<string, boolean> | undefined;
      setFinetunePrereq({
        ready: Boolean(res.ok),
        issues: Array.isArray(res.issues) ? (res.issues as string[]) : [],
        checks,
      });
      setFinetunePrep(res);
      await fetchFinetuneStatus();
      await fetchFinetuneLog();
    } catch (e) {
      setFinetunePrep({ ok: false, error: e instanceof Error ? e.message : String(e) });
    } finally {
      setFinetuneBusy(false);
    }
  };

  useEffect(() => {
    void fetchFinetuneStatus();
    void fetchFinetuneLog();
    const id = window.setInterval(() => {
      void fetchFinetuneStatus();
      void fetchFinetuneLog();
    }, 5000);
    return () => window.clearInterval(id);
  }, [fetchFinetuneStatus, fetchFinetuneLog]);

  const ribbon = asRecord(snap?.ribbon);
  const hb = asRecord(snap?.heartbeat_runtime);
  const models = asRecord(snap?.models);
  const memory = asRecord(snap?.memory_continuity);
  const wb = asRecord(snap?.workbench);
  const tts = asRecord(snap?.tts);
  const mood = asRecord(snap?.mood);
  const style = asRecord(snap?.style);
  const snapshotBrainGraph = asRecord(snap?.brain_graph);
  const snapshotNodesByType = asRecord(snapshotBrainGraph?.nodes_by_type);
  const toolsBlock = asRecord(snap?.tools);

  // Dual-brain status from snapshot
  const dualBrain = asRecord((snap as Record<string, unknown> | null)?.dual_brain);
  const dbStreamA = asRecord(dualBrain?.stream_a);
  const dbStreamB = asRecord(dualBrain?.stream_b);
  const dbBusy = Boolean(dbStreamB?.busy);
  const dbCurrentTask = typeof dbStreamB?.current_task === "string" ? dbStreamB.current_task : null;
  const dbLiveThinking = Boolean(dbStreamB?.live_thinking);
  const dbPendingInsight = Boolean(dualBrain?.pending_insight);
  const dbQueueDepth = typeof dbStreamB?.queue_depth === "number" ? dbStreamB.queue_depth : 0;
  const dbTasksToday = typeof dbStreamB?.tasks_today === "number" ? dbStreamB.tasks_today : 0;
  const dbStreamABusy = Boolean(dbStreamA?.busy);
  const toolsRegistry = asRecord(toolsBlock?.tools_registry);

  const sendChatText = async (rawText: string) => {
    if (inputMuted) return;
    const t = rawText.trim();
    if (!t) return;
    setChatBusy(true);
    setChatThinking(true);
    setLastChatErr("");
    pushEvent(`Chat: sending user message (${t.length} chars)`);
    setChatHist((prev) => [...prev, { role: "user", content: t }]);
    setChatInput("");
    try {
      const response = await postJson<Record<string, unknown>>("/api/v1/chat", { message: t });
      setLastChatResponse({
        ...response,
        debug_reply_source:
          typeof response.debug_reply_source === "string" ? response.debug_reply_source : "empty",
      });

      const reply =
        (typeof response.reply === "string" && response.reply.trim()) ||
        (typeof response.assistant_reply === "string" && response.assistant_reply.trim()) ||
        (typeof response.message === "string" && response.message.trim()) ||
        (typeof response.text === "string" && response.text.trim()) ||
        "";

      if (reply) {
        setChatHist((prev) => [...prev, { role: "assistant", content: reply }]);
        pushEvent(`Chat: received reply (${reply.length} chars)`);
        void refreshSnapshotOnly();
        return;
      }

      // 2) Empty / missing reply — sync from canonical history (Gradio path may have updated it)
      pushEvent("Chat: empty reply field — refreshing /api/v1/chat/history");
      const msgs = await pollChatHistory();

      let recovered = false;
      if (msgs && msgs.length > 0) {
        let userIdx = -1;
        for (let i = msgs.length - 1; i >= 0; i--) {
          const m = msgs[i];
          if (m.role === "user" && String(m.content ?? "").trim() === t) {
            userIdx = i;
            break;
          }
        }
        const next = userIdx >= 0 ? msgs[userIdx + 1] : undefined;
        if (next?.role === "assistant" && String(next.content ?? "").trim()) {
          recovered = true;
        }
      }

      if (!recovered) {
        const errMsg =
          "No reply from Ava: the \"reply\" field was empty and chat history did not show an assistant message after your text.";
        setLastChatErr(errMsg);
        setChatHist((prev) => [
          ...prev,
          {
            role: "system",
            content:
              "No reply received (empty \"reply\" from server and nothing in history yet). Open the Debug tab or try again.",
          },
          {
            role: "system",
            content: `Raw /api/v1/chat response:\n${JSON.stringify(response, null, 2)}`,
          },
        ]);
        pushEvent("Chat: no reply after POST and history poll");
      } else {
        setLastChatErr("");
        pushEvent("Chat: recovered assistant turn from history");
      }

      void refreshSnapshotOnly();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      pushEvent(`Chat send error: ${msg}`);
      if (msg.includes("404")) {
        setLastChatErr("Chat endpoint not yet wired. Expected path: /api/v1/chat");
      } else {
        setLastChatErr(msg);
      }
    } finally {
      setChatBusy(false);
      setChatThinking(false);
    }
  };

  const sendChat = async () => {
    await sendChatText(chatInput);
    setChatInput("");
  };

  const applyModelOverride = async () => {
    setRouteMsg("");
    try {
      const r = await postJson<Record<string, unknown>>("/api/v1/routing/override", {
        model: overrideModel.trim() || null,
        cognitive_mode: overrideMode.trim() || null,
      });
      setRouteMsg(`Override applied: ${JSON.stringify(r)}`);
      pushEvent(`Model routing override applied (${JSON.stringify(r)})`);
      setOverrideModel("");
      setOverrideMode("");
      await poll();
    } catch (e) {
      const m = e instanceof Error ? e.message : String(e);
      pushEvent(`Model override failed: ${m}`);
      setRouteMsg(m);
    }
  };

  const displayMessages = useMemo(() => chatHist.slice(-200), [chatHist]);

  const memThreads = memory?.active_threads;
  const threadList = Array.isArray(memThreads) ? memThreads : [];
  const memoryCount = threadList.length;

  const availModels = models?.available_models;
  const modelTags = Array.isArray(availModels) ? (availModels as string[]) : [];
  const wbMeta = asRecord(wb?.workbench_meta);
  const wbProposals = Array.isArray(wbMeta?.proposals) ? (wbMeta?.proposals as Record<string, unknown>[]) : [];
  const selectedProposalId = wbProposals.length ? String(wbProposals[0].proposal_id ?? "") : "";
  const vision = asRecord(snap?.vision);
  const perception = asRecord(vision?.perception);
  // Live person info — prefer the snap.current_person block populated by
  // runtime_presence (InsightFace cache → fallback face_recognition). Falls
  // back to the legacy perception fields only if current_person is empty.
  const currentPersonBlock = asRecord(snap?.current_person);
  const currentPersonName = String(currentPersonBlock?.display_name ?? "").trim();
  const currentPersonConf = Number(currentPersonBlock?.confidence ?? 0);
  const currentPersonIsZeke = Boolean(currentPersonBlock?.is_zeke);
  const currentPersonTimeAtMachineRaw = Number(currentPersonBlock?.time_at_machine ?? 0);
  const personIdentity = currentPersonName ||
    String(perception?.resolved_face_identity ?? "").trim() ||
    String(perception?.stable_face_identity ?? "").trim() ||
    String(perception?.recognized_text ?? "").trim() ||
    "Unknown";
  const personConfidenceRaw = currentPersonName
    ? currentPersonConf
    : Number(perception?.interpretation_confidence ?? 0);
  const personConfidencePct = Number.isFinite(personConfidenceRaw)
    ? Math.max(0, Math.min(100, Math.round(personConfidenceRaw * 100)))
    : 0;
  const personConfidencePctOneDp = Number.isFinite(personConfidenceRaw)
    ? (Math.max(0, Math.min(1, personConfidenceRaw)) * 100).toFixed(1)
    : "0.0";
  const personTimeAtMachineLabel = (() => {
    const s = Math.max(0, Math.round(currentPersonTimeAtMachineRaw));
    const m = Math.floor(s / 60);
    const r = s % 60;
    return m > 0 ? `${m}m ${r}s at machine` : `${r}s at machine`;
  })();
  const rawSceneSummary = String(perception?.scene_compact_summary ?? "").trim();
  const sceneSummary = rawSceneSummary || "Observing…";
  const sceneIsObserving = !rawSceneSummary;
  // Mood line — read from snap.mood.mood_label (always fresh on snapshot).
  const moodLabelLive = String(mood?.mood_label ?? "").trim();
  const moodLine = moodLabelLive ||
    String(perception?.face_status ?? "").trim() ||
    String(ribbon?.nuance_tone ?? "").trim() ||
    "Neutral / steady";

  // Inner thought — fades through the chat with cross-fade animation.
  const innerLifeBlock = asRecord(snap?.inner_life);
  const currentInnerThought = String(innerLifeBlock?.current_thought ?? "").trim();
  const primaryEmotion = String(mood?.primary_emotion ?? "calmness").toLowerCase();
  const orbVisual = EMOTION_VISUALS[primaryEmotion] ?? EMOTION_VISUALS.calmness;
  // Connectivity-aware orb color: dims and cools when offline
  const connOffline = !connOnline && online; // backend up but no internet
  const effectiveOrbColor = backendShutdownDetected
    ? "#6b7280"
    : connOffline
      ? shadeHex(orbVisual.color, 0.72)  // 10% dimmer when internet offline
      : orbVisual.color;
  const styleGlow = Number(style?.orb_glow_intensity ?? 0.8);
  const orbMidColor = shadeHex(effectiveOrbColor, 1.08);
  const orbDarkColor = shadeHex(effectiveOrbColor, 0.52);
  const orbDeepColor = shadeHex(effectiveOrbColor, 0.32);
  const secondaryEmotions = Array.isArray(mood?.secondary_emotions)
    ? (mood?.secondary_emotions as Array<Record<string, unknown>>)
    : [];
  const primaryIntensity = Number(mood?.primary_intensity ?? 0);
  const lastAssistantMessage = [...chatHist]
    .reverse()
    .find((m) => String(m.role ?? "") === "assistant")?.content ?? "";
  // Combine the slow-snapshot tts.tts_speaking with the fast 100ms live poll
  // so the orb reacts immediately when Kokoro starts/stops audio.
  const ttsSpeaking = Boolean(tts?.tts_speaking) || liveTtsSpeaking;
  const ttsAmplitude = Math.max(Number(tts?.tts_amplitude ?? 0), liveTtsAmp);
  // Energy from raw_mood drives orb breathing rate.
  const moodEnergy = Math.max(0, Math.min(1, Number(asRecord(mood?.raw_mood)?.energy ?? 0.5)));
  // Phase 74: voice_loop state drives orb when active
  const voiceLoopState = String((snap as Record<string, unknown>)?.voice_loop
    ? ((snap as Record<string, unknown>).voice_loop as Record<string, unknown>)?.state ?? "passive"
    : "passive");
  const voiceLoopActive = Boolean((snap as Record<string, unknown>)?.voice_loop
    ? ((snap as Record<string, unknown>).voice_loop as Record<string, unknown>)?.active
    : false);
  // Backend run_ava thinking flag — overrides everything except offline
  const avaThinking = Boolean((snap as Record<string, unknown> | null)?.thinking);
  // Component 10 of conversational naturalness: thinking_tier from snapshot
  // drives a stronger thinking-state signal when Ava's coordinator decides
  // the turn is taking long enough to need a metacognitive cue (Tier 3+).
  // See brain/thinking_tier.py and docs/CONVERSATIONAL_DESIGN.md.
  const thinkingTier = Number((snap as Record<string, unknown> | null)?.thinking_tier ?? 0);
  const tierForcesThinking = thinkingTier >= 3;
  // Sleep state (from /api/v1/debug/full subsystem_health.sleep). When SLEEPING
  // or WAKING, override the orb's normal pulse mode with the sleep visual state.
  const sleepInfo = (snap as Record<string, unknown> | null)?.subsystem_health
    ? ((snap as Record<string, unknown>).subsystem_health as Record<string, unknown>)?.sleep
    : null;
  const sleepState = String((sleepInfo as Record<string, unknown> | null)?.state || "AWAKE");
  const sleepProgress = Number((sleepInfo as Record<string, unknown> | null)?.progress || 0);
  const sleepRemainingSeconds = Number((sleepInfo as Record<string, unknown> | null)?.remaining_seconds || 0);
  const wakeStartedTs = Number((sleepInfo as Record<string, unknown> | null)?.wake_started_ts || 0);
  const wakeEstimateS = Number((sleepInfo as Record<string, unknown> | null)?.wake_estimate_s || 5);
  const wakeProgress = wakeStartedTs > 0
    ? Math.max(0, Math.min(1, (Date.now() / 1000 - wakeStartedTs) / Math.max(0.5, wakeEstimateS)))
    : 0;
  const isSleepingOrWaking = sleepState === "SLEEPING" || sleepState === "WAKING";
  const orbPulseMode = sleepState === "SLEEPING"
    ? "sleeping"
    : sleepState === "WAKING"
      ? "waking"
    : backendShutdownDetected || !online
    ? "offline"
    : avaThinking
      ? "thinking"  // Ava is processing a chat turn — fast blue pulse
    : tierForcesThinking
      ? "thinking"  // Tier 3+ from the metacognitive coordinator — sustained thinking visual
    : voiceLoopActive && voiceLoopState === "speaking"
      ? "speaking"
    : voiceLoopActive && voiceLoopState === "thinking"
      ? "thinking"
    : voiceLoopActive && voiceLoopState === "listening"
      ? "listening"
    : voiceLoopActive && voiceLoopState === "attentive"
      ? "attentive"
    : ttsSpeaking
      ? "speaking"
      : Boolean(tts?.enabled)
        ? "speaking"
        : chatThinking
          ? String(models?.cognitive_mode ?? "").includes("deep")
            ? "deep"
            : "thinking"
          : sttListening
            ? "listening"
            : orbVisual.pulse;
  // Live speech text (above the orb) and inner-state line (below the orb).
  // Speech text streams word-by-word from /api/v1/snapshot speech.* fields
  // when Kokoro is playing; falls back to the last assistant message if TTS
  // is disabled/muted but Ava has produced a reply.
  const speechBlock = (snap as Record<string, unknown> | null)?.speech as
    Record<string, unknown> | undefined;
  const speechSpeaking = Boolean(speechBlock?.speaking);
  const speechSpokenSoFar = String(speechBlock?.spoken_so_far ?? "");
  const speechFullReply = String(speechBlock?.full_reply ?? "");
  const ttsEnabled = Boolean(tts?.enabled);
  const speakingTextForUI = backendShutdownDetected
    ? "Ava has shut down."
    : speechSpeaking
      ? speechSpokenSoFar
      : speechFullReply
        ? speechFullReply
        : !ttsEnabled && lastAssistantMessage
          ? String(lastAssistantMessage)
          : "";
  const innerStateLine = backendShutdownDetected
    ? ""
    : String((snap as Record<string, unknown> | null)?.inner_state_line ?? "");
  const uptimeMs = Math.max(0, Date.now() - appStartedAtRef.current);
  const uptimeLabel = `${Math.floor(uptimeMs / 3600000)}h ${Math.floor((uptimeMs % 3600000) / 60000)}m`;

  const updatedLabel = lastUpdated ? new Date(lastUpdated).toLocaleString() : "—";
  // frameUrl reaches /api/v1/vision/latest_frame for the few places we still
  // need a fresh HTTP-driven frame (e.g. brain visualizations that want a
  // specific moment, or the camera tab's "expanded" view fallback). For
  // everywhere ELSE that displays the live camera, prefer `liveFrameSrc`
  // (the shared state set by the single 5fps poll at line 885+). This
  // matches the orb pattern: one source, mirrored to all tabs. Multiple
  // <img src={frameUrl}> references fire independent HTTP fetches per tab
  // even though Ava only captures the camera once on the backend — wasteful
  // bandwidth + browser load. Vault: see 2026-05 work order Phase A2.
  const frameUrl = `${API_BASE}/api/v1/vision/latest_frame?t=${cameraTick}`;
  // Prefer shared state for camera display. Falls back to frameUrl only when
  // shared state hasn't loaded yet (cold start or paused stream).
  const sharedCameraSrc = liveFrameSrc || frameUrl;
  const activeBrainIdSet = useMemo(
    () => new Set((brainActive.active_nodes || []).map((n) => String(n.id))),
    [brainActive.active_nodes]
  );
  const selectedBrainNeighbors = useMemo(() => {
    if (!selectedBrainNode) return [] as BrainNode[];
    const id = String(selectedBrainNode.id);
    const linked = new Set<string>();
    for (const e of brainGraph.edges) {
      if (String(e.source) === id) linked.add(String(e.target));
      if (String(e.target) === id) linked.add(String(e.source));
    }
    return brainGraph.nodes.filter((n) => linked.has(String(n.id))).slice(0, 20);
  }, [selectedBrainNode, brainGraph.edges, brainGraph.nodes]);

  useEffect(() => {
    if (tab !== "brain") return;
    const update = () => {
      const degree = new Map<string, number>();
      for (const e of brainGraph.edges) {
        const source = String(e.source);
        const target = String(e.target);
        degree.set(source, (degree.get(source) ?? 0) + 1);
        degree.set(target, (degree.get(target) ?? 0) + 1);
      }
      let mostConnected = "—";
      let maxDegree = -1;
      for (const n of brainGraph.nodes) {
        const d = degree.get(String(n.id)) ?? 0;
        if (d > maxDegree) {
          maxDegree = d;
          mostConnected = n.label || String(n.id);
        }
      }
      setBrainStatsBar({
        nodes: brainGraph.nodes.length,
        edges: brainGraph.edges.length,
        active: brainActive.active_nodes.length,
        mostConnected,
      });
    };
    update();
    const id = window.setInterval(update, 5000);
    return () => window.clearInterval(id);
  }, [tab, brainGraph.nodes, brainGraph.edges, brainActive.active_nodes.length]);

  // ── 3D brain graph (3d-force-graph) ──────────────────────────────────────
  // Initialize ONCE when the brain tab first becomes visible. Data updates
  // happen via .graphData() in a separate effect — we never tear down the
  // WebGL scene on data changes, so the graph never dims or "loads in".
  useEffect(() => {
    if (tab !== "brain") return;
    if (!brainGraph3DContainerRef.current) return;
    if (brainGraph3DInstanceRef.current) return;  // already initialized

    const colors: Record<string, string> = {
      person: "#ed64a6",
      memory: "#9f7aea",
      topic: "#4299e1",
      emotion: "#f5c518",
      curiosity: "#00d4d4",
      self: "#68d391",
      opinion: "#ecc94b",
      event: "#ff6b00",
    };

    try {
      // 3d-force-graph's typings declare the constructor result as non-callable
      // on the instance; the documented runtime API is `ForceGraph3D()(domEl)`.
      // Cast via a local any to suppress the TS check.
      const FG: any = ForceGraph3D as any;
      const container = brainGraph3DContainerRef.current as HTMLElement;
      const w0 = container.clientWidth || 0;
      const h0 = container.clientHeight || 0;
      console.info("[brain-3d] init container dims", { width: w0, height: h0 });
      const fg = FG()(container)
        .backgroundColor("rgba(0,0,0,0)")
        .width(w0 || 800)
        .height(h0 || 620)
        .nodeLabel((node: any) => {
          const t = String(node.type || "node");
          const lbl = String(node.label || node.id || "");
          const w = Number(node.weight || 0).toFixed(2);
          if (node._tier === 0) return `Ava — center of mind`;
          if (node._tier === 1) return `Identity anchor: ${lbl}`;
          return `${t}: ${lbl} (w=${w})`;
        })
        .nodeColor((node: any) => {
          // Ava-centric color scheme — see AVA_BRAIN_TIER_COLORS.
          const tier = classifyAvaBrainTier(node);
          if (tier === 0) return AVA_BRAIN_TIER_COLORS[0];   // ava self — violet
          if (tier === 1) return AVA_BRAIN_TIER_COLORS[1];   // anchors — gold
          if (tier === 2) {
            // people — blue intensity scaled by trust
            const trust = trustScoreOf(node);
            // map trust 0..1 to lightness range — high trust = bright blue
            const v = Math.round(80 + 175 * trust);
            return `rgb(${Math.round(v * 0.4)}, ${Math.round(v * 0.7)}, ${v})`;
          }
          if (tier === 3) return AVA_BRAIN_TIER_COLORS[3];   // active — pink
          if (tier === 4) {
            // memories fade with age
            const recency = ageDecayOf(node);
            const v = Math.round(80 + 120 * recency);
            return `rgb(${Math.round(v * 0.5)}, ${v}, ${Math.round(v * 0.6)})`;
          }
          return AVA_BRAIN_TIER_COLORS[5];
        })
        .nodeVal((node: any) => {
          if (node._tier === 0) return 24;            // ava self — biggest
          if (node._tier === 1) return 9;             // anchors — large
          return 1 + Number(node.weight || 0) * 4;
        })
        .linkColor(() => "rgba(255,255,255,0.18)")
        .linkWidth(0.5)
        .linkDirectionalParticles(1)
        .linkDirectionalParticleSpeed(0.005)
        .linkOpacity(0.4)
        .onNodeClick((node: any) => {
          setSelectedBrainNode(node as BrainNode);
        });
      brainGraph3DInstanceRef.current = fg;
      // Apply Ava-centric radial force layout. 3d-force-graph exposes a
      // d3Force(name, force) hook; we add a custom radial pull that
      // targets each node's tier-specific ring radius. Strength scales
      // with how well-classified the node is — anchors (tier 1) get a
      // strong pull, generic memories (tier 4) get a softer pull so
      // they have room to cluster naturally.
      try {
        const tierStrength = [0, 0.6, 0.35, 0.25, 0.15, 0.1];
        // d3-force-3d's forceRadial; 3d-force-graph re-exports under d3.
        // Custom force lambda that nudges each node toward its target ring.
        fg.d3Force("avaRadial", (alpha: number) => {
          // The graph exposes the live nodes via fg.graphData().nodes.
          const data = fg.graphData();
          const nodes = data?.nodes || [];
          for (const n of nodes) {
            const tier = classifyAvaBrainTier(n);
            if (tier === 0) continue; // pinned at origin via fx/fy/fz
            const target = AVA_BRAIN_TIER_RADII[tier] ?? AVA_BRAIN_TIER_RADII[5];
            // For people (tier 2), pull closer if trust is high.
            let r = target;
            if (tier === 2) r = AVA_BRAIN_TIER_RADII[1] + (1 - trustScoreOf(n)) *
                                (AVA_BRAIN_TIER_RADII[2] - AVA_BRAIN_TIER_RADII[1] + 80);
            else if (tier === 4) r = AVA_BRAIN_TIER_RADII[3] + (1 - ageDecayOf(n)) *
                                (AVA_BRAIN_TIER_RADII[4] - AVA_BRAIN_TIER_RADII[3] + 80);
            const x = Number(n.x || 0), y = Number(n.y || 0), z = Number(n.z || 0);
            const cur = Math.sqrt(x * x + y * y + z * z) || 1;
            const k = (tierStrength[tier] || 0.1) * alpha;
            const factor = (r - cur) * k / cur;
            n.vx = (n.vx || 0) + x * factor;
            n.vy = (n.vy || 0) + y * factor;
            n.vz = (n.vz || 0) + z * factor;
          }
        });
      } catch (fErr) {
        console.warn("[brain-3d] radial force setup failed", fErr);
      }
      // After mount, wait a frame and re-apply size — the container may have
      // been zero-sized when init ran (flex/grid layout settling), in which
      // case the WebGL canvas is invisible until we resize it.
      requestAnimationFrame(() => {
        try {
          const cw = container.clientWidth || 0;
          const ch = container.clientHeight || 0;
          if (cw > 0 && ch > 0) {
            fg.width(cw).height(ch);
            console.info("[brain-3d] post-init resize", { width: cw, height: ch });
          } else {
            console.warn("[brain-3d] container still zero-sized after init", { cw, ch });
          }
        } catch (re) {
          console.error("[brain-3d] post-init resize failed", re);
        }
      });
      // Keep the renderer aligned with container resizes — the previous
      // window-resize listener only fired on tab change, missing layout
      // shifts from the side panel or DevTools docking.
      try {
        const ro = new ResizeObserver((entries) => {
          for (const entry of entries) {
            const cr = entry.contentRect;
            if (cr.width > 0 && cr.height > 0) {
              try {
                fg.width(cr.width).height(cr.height);
              } catch { /* ignore */ }
            }
          }
        });
        ro.observe(container);
        // Stash on the instance so we can disconnect on unmount.
        (fg as any)._avaResizeObserver = ro;
      } catch (roErr) {
        console.warn("[brain-3d] ResizeObserver unavailable", roErr);
      }
      console.info("[brain-3d] init succeeded");
    } catch (e) {
      console.error("[brain-3d] init failed", e);
    }
  }, [tab]);

  // Push graph data into the 3D scene whenever it changes. Never blanks the
  // canvas — the graph just morphs to the new node/edge set.
  // Optimization: the raw graph can have 600+ nodes and 6000+ edges, which
  // tanks the renderer to ~15fps. Cap to BRAIN_GRAPH_MAX_NODES (top by
  // weight) and BRAIN_GRAPH_MAX_EDGES (highest strength among surviving
  // node pairs). Also skip the update entirely when the brain tab isn't
  // visible — pointless work that can starve the orb's frame budget.
  useEffect(() => {
    const fg = brainGraph3DInstanceRef.current;
    if (!fg) return;
    if (tab !== "brain") return;  // skip when tab not focused
    setBrainGraphSyncing(true);
    try {
      const rawNodes = brainGraph.nodes || [];
      const rawEdges = brainGraph.edges || [];
      // Sort nodes by weight descending; keep the top N.
      let topNodes = rawNodes;
      if (rawNodes.length > BRAIN_GRAPH_MAX_NODES) {
        topNodes = [...rawNodes]
          .sort((a, b) => Number(b.weight || 0) - Number(a.weight || 0))
          .slice(0, BRAIN_GRAPH_MAX_NODES);
      }
      const survivingIds = new Set(topNodes.map((n) => n.id));
      // Keep edges where both endpoints survived; sort by strength descending.
      const survivingEdges = rawEdges.filter(
        (e) => survivingIds.has((e as any).source) && survivingIds.has((e as any).target)
      );
      let topEdges = survivingEdges;
      if (survivingEdges.length > BRAIN_GRAPH_MAX_EDGES) {
        topEdges = [...survivingEdges]
          .sort((a, b) => Number((b as any).strength || 0) - Number((a as any).strength || 0))
          .slice(0, BRAIN_GRAPH_MAX_EDGES);
      }
      if (rawNodes.length !== topNodes.length || rawEdges.length !== topEdges.length) {
        console.info("[brain-3d] capped", {
          nodes: `${rawNodes.length}->${topNodes.length}`,
          edges: `${rawEdges.length}->${topEdges.length}`,
        });
      }
      // Inject AVA SELF + identity anchors at the center. Per
      // docs/BRAIN_ARCHITECTURE.md the self anchors are the gravitational
      // center; concept-graph nodes orbit around them. Tag each node with
      // `_tier` for the radial force layout below.
      const enrichedNodes = topNodes.map((n) => {
        const out: any = { ...n };
        out._tier = classifyAvaBrainTier(out);
        return out;
      });
      const avaSelfNode: any = {
        id: "ava-self",
        label: "AVA",
        type: "ava-self",
        weight: 1.0,
        _tier: 0,
        // Pin to origin so the force layout treats it as the absolute center.
        fx: 0, fy: 0, fz: 0,
      };
      // Identity anchors — pinned in a small ring around ava-self.
      const anchorRadius = AVA_BRAIN_TIER_RADII[1];
      const identityAnchors: any[] = [
        { id: "identity-anchor", label: "IDENTITY", type: "identity_anchor", weight: 0.95, _tier: 1,
          fx: anchorRadius, fy: 0, fz: 0 },
        { id: "soul-anchor", label: "SOUL", type: "identity_anchor", weight: 0.95, _tier: 1,
          fx: -anchorRadius / 2, fy: anchorRadius * Math.sqrt(3) / 2, fz: 0 },
        { id: "user-anchor", label: "USER", type: "identity_anchor", weight: 0.95, _tier: 1,
          fx: -anchorRadius / 2, fy: -anchorRadius * Math.sqrt(3) / 2, fz: 0 },
      ];
      const finalNodes = [avaSelfNode, ...identityAnchors, ...enrichedNodes];
      fg.graphData({
        nodes: finalNodes,
        links: topEdges.map((e) => ({ ...e })),
      });
    } catch (e) {
      console.error("[brain-3d] graphData failed", e);
    } finally {
      // Quick clear — the sync indicator only flashes briefly.
      window.setTimeout(() => setBrainGraphSyncing(false), 250);
    }
  }, [brainGraph, tab]);

  // Pause/resume the force simulation based on tab focus. 3d-force-graph
  // runs the physics every frame even when the canvas isn't visible —
  // pausing reclaims those cycles for the orb (the only thing the user
  // is watching when the brain tab isn't active).
  useEffect(() => {
    const fg = brainGraph3DInstanceRef.current;
    if (!fg) return;
    try {
      if (tab === "brain") {
        if (typeof fg.resumeAnimation === "function") fg.resumeAnimation();
      } else {
        if (typeof fg.pauseAnimation === "function") fg.pauseAnimation();
      }
    } catch (e) {
      console.warn("[brain-3d] pause/resume failed", e);
    }
  }, [tab]);

  // Active node pulse highlighting on the 3D graph.
  useEffect(() => {
    const fg = brainGraph3DInstanceRef.current;
    if (!fg) return;
    try {
      fg.nodeColor((node: any) => {
        const t = String(node.type || "");
        const baseColors: Record<string, string> = {
          person: "#ed64a6", memory: "#9f7aea", topic: "#4299e1",
          emotion: "#f5c518", curiosity: "#00d4d4", self: "#68d391",
          opinion: "#ecc94b", event: "#ff6b00",
        };
        const isActive = activeBrainIdSet.has(String(node.id));
        if (isActive) return "#ffffff";
        return node.color || baseColors[t] || "#888888";
      });
    } catch { /* ignore */ }
  }, [activeBrainIdSet]);

  // Resize the 3D graph when the window or container changes size.
  useEffect(() => {
    if (tab !== "brain") return;
    const fg = brainGraph3DInstanceRef.current;
    const container = brainGraph3DContainerRef.current;
    if (!fg || !container) return;
    const resize = () => {
      try {
        fg.width(container.clientWidth);
        fg.height(container.clientHeight);
      } catch { /* ignore */ }
    };
    resize();
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, [tab]);

  useEffect(() => {
    if (tab !== "brain") return;
    if (brainLoading) return;
    if (brainGraph.nodes.length > 0) return;
    const retry = window.setTimeout(() => {
      void pollBrainGraph("empty-retry");
    }, 1200);
    return () => window.clearTimeout(retry);
  }, [tab, brainGraph.nodes.length, pollBrainGraph, brainLoading]);

  const toggleTts = async () => {
    try {
      const res = await postJson<Record<string, unknown>>("/api/v1/tts/toggle", {});
      pushEvent(`TTS toggle -> enabled=${String(res.enabled ?? false)} engine=${String(res.engine ?? "none")}`);
      await poll();
    } catch (e) {
      pushEvent(`TTS toggle failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const runShutdown = async () => {
    setShutdownConfirmOpen(false);
    setShutdownInProgress(true);
    setShutdownError("");
    setShutdownGoodbye("");
    setShutdownWindowCloseHint(false);
    try {
      const res = await postJson<Record<string, unknown>>("/api/v1/shutdown", {});
      const goodbye = String(res.goodbye ?? "Goodnight, Zeke.");
      setShutdownGoodbye(goodbye);
      pushEvent(`Shutdown endpoint returned note_saved=${String(res.note_saved ?? false)}`);
      window.setTimeout(async () => {
        setShutdownDone(true);
        setBackendShutdownDetected(true);
        try {
          const { getCurrentWindow } = await import("@tauri-apps/api/window");
          await getCurrentWindow().close();
        } catch {
          setShutdownWindowCloseHint(true);
          try {
            window.close();
          } catch {
            // Browser fallback.
          }
        }
      }, 3000);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setShutdownError(msg);
      setShutdownInProgress(false);
      pushEvent(`Shutdown failed: ${msg}`);
    }
  };

  const workbenchAction = async (action: "approve" | "reject", proposalId?: string) => {
    setWbActionMsg("");
    try {
      const path = action === "approve" ? "/api/v1/workbench/approve" : "/api/v1/workbench/reject";
      const payload = { proposal_id: proposalId || selectedProposalId || null };
      pushEvent(`Workbench: ${action} proposal ${payload.proposal_id ?? "(none)"}`);
      const res = await postJson<Record<string, unknown>>(path, payload);
      setWbActionMsg(String(res.message ?? `${action} request sent`));
      await poll();
    } catch (e) {
      const m = e instanceof Error ? e.message : String(e);
      pushEvent(`Workbench ${action} error: ${m}`);
      setWbActionMsg(m);
    }
  };

  const fetchDebugExport = async () => {
    setDebugExportBusy(true);
    try {
      const text = await getText("/api/v1/debug/export");
      setDebugExportText(text);
      pushEvent("Fetched GET /api/v1/debug/export");
    } catch (e) {
      const m = e instanceof Error ? e.message : String(e);
      setDebugExportText(`(error) ${m}`);
      pushEvent(`Debug export failed: ${m}`);
    } finally {
      setDebugExportBusy(false);
    }
  };

  const copyToClipboard = async (text: string, label: string) => {
    try {
      await navigator.clipboard.writeText(text);
      pushEvent(`Copied ${label} to clipboard`);
    } catch (e) {
      pushEvent(`Clipboard error (${label}): ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const lastReplyPresent =
    lastChatResponse &&
    (
      (typeof lastChatResponse.reply === "string" && lastChatResponse.reply.trim() !== "") ||
      (typeof lastChatResponse.assistant_reply === "string" &&
        lastChatResponse.assistant_reply.trim() !== "") ||
      (typeof lastChatResponse.message === "string" && lastChatResponse.message.trim() !== "") ||
      (typeof lastChatResponse.text === "string" && lastChatResponse.text.trim() !== "")
    );

  const lastReplyEmpty = lastChatResponse !== null && !lastReplyPresent;
  const prereqChecks = finetunePrereq.checks ?? {};
  const canStartFinetune = Boolean(finetunePrereq.ready);

  return (
    <div
      className="app operator-app presence-root"
      style={
        {
          "--orb-color": effectiveOrbColor,
          "--orb-rgb": hexToRgbTriplet(effectiveOrbColor),
          "--orb-mid": orbMidColor,
          "--orb-dark": orbDarkColor,
          "--orb-deep": orbDeepColor,
          "--orb-glow": String(styleGlow),
        } as any
      }
    >
      <header className="op-header">
        <div className="op-brand">
          <span className="op-title">Ava</span>
          <span className={`op-status-dot ${online ? "on" : "off"}`} aria-hidden="true" />
          <span className="op-status-text">{online ? "Live" : "Offline"}</span>
        </div>
        <div className="op-meta">
          <span className="op-meta-item">Brain {String(models?.selected_model ?? "—")}</span>
          <span className="op-meta-item">Heartbeat {String(hb?.heartbeat_mode ?? "—")}</span>
          <span className="op-meta-item op-meta-issue">Issue {String(hb?.runtime_active_issue_summary ?? "none")}</span>
          <span className="op-meta-item">Updated {updatedLabel}</span>
          {inputMuted ? <span className="input-muted-pill">Input muted</span> : null}
          <button
            type="button"
            className="btn ghost op-header-btn"
            onClick={() => void toggleTts()}
            disabled={shutdownInProgress}
            title={`TTS engine: ${String(tts?.engine ?? "none")}`}
          >
            TTS {Boolean(tts?.enabled) ? "On" : "Off"} ({String(tts?.engine ?? "none")})
          </button>
          <button
            type="button"
            className="btn op-shutdown-btn"
            onClick={() => setShutdownConfirmOpen(true)}
            disabled={shutdownInProgress}
          >
            Shut Down
          </button>
        </div>
      </header>

      {!online && (
        <div className="op-banner">
          {connecting ? (
            "Connecting to Ava..."
          ) : backendShutdownDetected ? (
            "Ava has shut down."
          ) : (
            <>
              Backend not responding on :5876. Start <code>avaagent.py</code> (operator HTTP must be enabled).{" "}
              {pollErr ? `(${pollErr})` : ""}
            </>
          )}
        </div>
      )}

      <section
        className="presence-stage"
        style={{ position: "relative" }}
        onMouseDown={(ev) => {
          // Middle mouse button (button === 1) → snap orb back to center.
          // No preventDefault — we don't want to suppress autoscroll cursor or
          // any other browser default behavior elsewhere.
          if (ev.button === 1) {
            setOrbRecenterCounter((n) => n + 1);
            setOrbRecenterPulse(true);
            window.setTimeout(() => setOrbRecenterPulse(false), 320);
          }
        }}
      >
        {dropHover && (
          <div style={{
            position: "absolute", inset: 0, zIndex: 100, background: "rgba(0,212,212,0.12)",
            border: "2px dashed #00d4d4", display: "flex", alignItems: "center",
            justifyContent: "center", pointerEvents: "none", borderRadius: "8px",
          }}>
            <span style={{ color: "#00d4d4", fontSize: "1.2rem", fontWeight: 600 }}>Drop file for Ava</span>
          </div>
        )}
        {dropProcessing && (
          <div style={{
            position: "absolute", inset: 0, zIndex: 100, background: "rgba(0,0,0,0.5)",
            display: "flex", alignItems: "center", justifyContent: "center",
          }}>
            <span style={{ color: "#00d4d4" }}>Processing…</span>
          </div>
        )}
        {/* Connectivity status bar */}
        <div style={{
          display: "flex", alignItems: "center", gap: 8,
          padding: "4px 12px", fontSize: "0.72rem",
          background: "rgba(0,0,0,0.3)", borderRadius: 6, marginBottom: 4,
        }}>
          <span style={{
            width: 8, height: 8, borderRadius: "50%", display: "inline-block",
            background: !online ? "#6b7280" : connOnline ? "#4ade80" : "#f59e0b",
            boxShadow: connOnline && online ? "0 0 6px #4ade80" : "none",
            flexShrink: 0,
          }} />
          <span style={{ color: !online ? "#6b7280" : connOnline ? "#4ade80" : "#f59e0b", fontFamily: "monospace" }}>
            {!online ? "AVA OFFLINE" : connOnline ? (connCloudAvailable ? "Cloud active" : "Local only") : "Local only"}
          </span>
          {connOnline && connQuality === "online_fast" && (
            <span style={{ color: "#4a5568", marginLeft: 4 }}>fast</span>
          )}
          {connOnline && connQuality === "online_slow" && (
            <span style={{ color: "#d69e2e", marginLeft: 4 }}>slow</span>
          )}
        </div>
        <div className="presence-hud-row">
          <div className="presence-hud" style={{ color: effectiveOrbColor }}>
            EMOTION: {primaryEmotion}
          </div>
          <div className="presence-hud">
            HEARTBEAT: {String(hb?.heartbeat_mode ?? "idle")}
          </div>
        </div>
        {PRESENCE_V2_ENABLED && (
          // No `key=` — React reuses this DOM node across content changes.
          // The empty→live class transition triggers the one-shot fade-in;
          // subsequent text updates within the same session don't remount
          // and don't re-fire the entry animation.
          <div className={`presence-speaking-text ${speakingTextForUI ? "live" : "empty"}`}>
            {speakingTextForUI}
          </div>
        )}
        <div className="presence-orb-wrap">
          <div className={`orb-canvas-shell${orbRecenterPulse ? " recenter-pulse" : ""}`}>
            <OrbCanvas
              emotion={primaryEmotion}
              emotionColor={effectiveOrbColor}
              state={shutdownInProgress ? "offline" : (orbPulseMode as any)}
              size={320}
              amplitude={ttsAmplitude}
              energy={moodEnergy}
              recenterTrigger={orbRecenterCounter}
              cubeMorphEnabled={PRESENCE_V2_CUBE_MORPH_ENABLED}
              sleepProgress={sleepProgress}
              sleepRemainingSeconds={sleepRemainingSeconds}
              wakeProgress={wakeProgress}
            />
            {/* Offline overlay text */}
            {connOffline && (
              <div style={{
                position: "absolute", bottom: 12, left: "50%", transform: "translateX(-50%)",
                color: "rgba(156,163,175,0.5)", fontSize: "0.6rem", fontFamily: "monospace",
                letterSpacing: "0.15em", pointerEvents: "none", userSelect: "none",
              }}>LOCAL</div>
            )}
          </div>
        </div>
        <div className="presence-orb-line" aria-hidden="true" />
        <div className="presence-hud-row presence-hud-row-bottom">
          <div className="presence-hud" style={{
            color: dbBusy ? "#60a5fa" : dbPendingInsight ? "#a78bfa" : dbLiveThinking ? "#2dd4bf" : undefined,
          }}>
            {dbPendingInsight
              ? "READY TO SHARE"
              : dbBusy && dbCurrentTask
                ? `THINKING: ${dbCurrentTask}`
                : dbLiveThinking
                  ? "PROCESSING..."
                  : `NEURAL ACTIVITY: ${Number(snapshotBrainGraph?.total_nodes ?? brainGraph.nodes.length)}`}
          </div>
          <div className="presence-hud">
            UPTIME: {uptimeLabel}
          </div>
        </div>
        {avaThinking && (
          <div style={{
            textAlign: "center", color: "#3b82f6", fontFamily: "monospace",
            fontSize: "0.95rem", letterSpacing: "0.06em", marginTop: "0.4rem",
            animation: "pulse 1.2s ease-in-out infinite",
          }}>
            Ava is thinking…
          </div>
        )}
        {PRESENCE_V2_ENABLED && (
          // No `key=` — same rationale as presence-speaking-text above.
          <div className={`presence-inner-state-line ${innerStateLine ? "live" : "empty"}`}>
            {innerStateLine}
          </div>
        )}
        {/* Inner monologue — Ava's current thought, rendered under the orb.
            Distinct from the spoken reply: this is what she's thinking
            *about*, not what she's saying. Pulled from
            snapshot.inner_life.current_thought (refreshed each tick). */}
        <div className={`presence-inner-thought ${currentInnerThought ? "live" : "empty"}`}>
          {currentInnerThought ? `💭 ${currentInnerThought}` : ""}
        </div>
        <div className="presence-input-row">
          <input
            type="text"
            value={chatInput}
            onChange={(e) => setChatInput(e.target.value)}
            placeholder={inputMuted ? "YOUR INPUT IS MUTED" : "SPEAK TO AVA..."}
            onKeyDown={(e) => {
              if (inputMuted) return;
              if (e.key === "Enter") {
                e.preventDefault();
                void sendChat();
              }
            }}
            disabled={chatBusy || shutdownInProgress || inputMuted}
          />
        </div>
        <button
          type="button"
          className={`mute-input-btn ${inputMuted ? "muted" : ""}`}
          onClick={() => setInputMuted((v) => !v)}
          aria-label={inputMuted ? "Unmute your input" : "Mute your input"}
          title={inputMuted ? "Input muted" : "Input on"}
        >
          <span className="mute-input-icon" aria-hidden="true">{inputMuted ? "🎙️✕" : "🎙️"}</span>
          <span className="mute-input-label">{inputMuted ? "Input muted — Ava can't hear you" : "Input on"}</span>
        </button>
        <button className="presence-gear" type="button" onClick={() => setOperatorOpen(true)} aria-label="Open operator panel">
          ⚙
        </button>
        <button className="presence-camera-thumb" type="button" onClick={() => setCameraOverlayOpen(true)} aria-label="Expand camera">
          {liveFrameSrc ? (
            <img src={liveFrameSrc} alt="camera live thumb" />
          ) : presenceCameraOk ? (
            <img
              src={frameUrl}
              alt="camera thumb"
              onLoad={() => setPresenceCameraOk(true)}
              onError={() => setPresenceCameraOk(false)}
            />
          ) : (
            <span className="presence-camera-empty">No camera</span>
          )}
          <img
            src={frameUrl}
            alt=""
            aria-hidden="true"
            className="presence-camera-probe"
            onLoad={() => setPresenceCameraOk(true)}
            onError={() => setPresenceCameraOk(false)}
          />
          <span className="presence-camera-scene">{sceneSummary}</span>
        </button>
      </section>
      {cameraOverlayOpen && (
        <div className="camera-overlay" onClick={() => setCameraOverlayOpen(false)}>
          {/* Prefer live frame (refreshes every 200ms) — fall back to cached annotated frame */}
          <img src={liveFrameSrc || frameUrl} alt="camera expanded" />
        </div>
      )}

      {/* Phase 79: onboarding overlay */}
      {onboardingActive && (
        <div style={{
          position: "fixed", inset: 0, zIndex: 900,
          background: "rgba(0,0,0,0.85)", display: "flex", alignItems: "center", justifyContent: "center",
        }}>
          <div style={{
            background: "#0d1117", border: "1px solid #1e40af", borderRadius: 16,
            padding: "2rem", maxWidth: 520, width: "90%", boxShadow: "0 0 40px rgba(26,108,245,0.4)",
          }}>
            <div style={{ marginBottom: "1rem" }}>
              <div style={{ fontSize: "0.75rem", color: "#4a90d9", marginBottom: 6 }}>
                ONBOARDING — Stage {onboardingStageIndex + 1} / {onboardingStageCount}
              </div>
              <div style={{
                height: 4, background: "#1e293b", borderRadius: 2, overflow: "hidden", marginBottom: "1rem",
              }}>
                <div style={{
                  height: "100%", borderRadius: 2, background: "#1a6cf5",
                  width: `${Math.round(((onboardingStageIndex) / Math.max(1, onboardingStageCount - 1)) * 100)}%`,
                  transition: "width 0.4s ease",
                }} />
              </div>
              <div style={{ color: "#e2e8f0", fontSize: "0.95rem", lineHeight: 1.6, marginBottom: "1.25rem" }}>
                {onboardingReply || "Starting onboarding…"}
              </div>
              {PHOTO_STAGES_UI.includes(onboardingStage || "") && (
                <div style={{ marginBottom: "0.75rem" }}>
                  {/* Onboarding photo stage uses the same shared camera
                      stream as the rest of the UI (no separate fetch). */}
                  <img src={sharedCameraSrc}
                    alt="camera" style={{ width: "100%", borderRadius: 8, border: "1px solid #1e40af" }}
                    onError={() => {}} />
                </div>
              )}
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <input
                type="text"
                value={onboardingInput}
                onChange={(e) => setOnboardingInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !onboardingBusy) {
                    const inp = onboardingInput;
                    setOnboardingInput("");
                    setOnboardingBusy(true);
                    postJson("/api/v1/onboarding/step", { input: inp })
                      .then((d) => {
                        const r = d as Record<string, unknown>;
                        if (typeof r.reply === "string") setOnboardingReply(r.reply);
                        if (r.done) { setOnboardingActive(false); setOnboardingReply(""); }
                      })
                      .catch(() => {})
                      .finally(() => setOnboardingBusy(false));
                  }
                }}
                placeholder="Type your response…"
                disabled={onboardingBusy}
                style={{
                  flex: 1, background: "#1e293b", border: "1px solid #2d3748",
                  borderRadius: 8, padding: "0.6rem 0.9rem", color: "#e2e8f0", fontSize: "0.9rem",
                }}
                autoFocus
              />
              <button
                type="button"
                disabled={onboardingBusy}
                onClick={() => {
                  const inp = onboardingInput;
                  setOnboardingInput("");
                  setOnboardingBusy(true);
                  postJson("/api/v1/onboarding/step", { input: inp })
                    .then((d) => {
                      const r = d as Record<string, unknown>;
                      if (typeof r.reply === "string") setOnboardingReply(r.reply);
                      if (r.done) { setOnboardingActive(false); setOnboardingReply(""); }
                    })
                    .catch(() => {})
                    .finally(() => setOnboardingBusy(false));
                }}
                style={{
                  background: "#1a6cf5", border: "none", borderRadius: 8,
                  padding: "0.6rem 1.2rem", color: "#fff", cursor: "pointer", fontSize: "0.9rem",
                }}
              >
                {onboardingBusy ? "…" : "Send"}
              </button>
            </div>
          </div>
        </div>
      )}

      <div className={`operator-backdrop ${operatorOpen ? "open" : ""}`} onClick={() => setOperatorOpen(false)} />
      <div className={`op-body operator-drawer ${operatorOpen ? "open" : ""} ${shutdownInProgress ? "shutdown-locked" : ""}`}>
        <button className="operator-close" type="button" onClick={() => setOperatorOpen(false)}>
          ×
        </button>
        <nav className="op-nav" aria-label="Primary">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              className={tab === t.id ? "active" : ""}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
          {customTabs.map((ct) => (
            <button
              key={`custom_${ct.id}`}
              type="button"
              className={tab === ct.id ? "active" : ""}
              onClick={() => setTab(ct.id)}
              title={`Custom tab: ${ct.content_type}`}
            >
              {ct.name}
            </button>
          ))}
        </nav>

        <main className="op-main">
          {tab === "chat" && (
            <div className="op-pane op-pane-chat">
              <div className="chat-two-panel">
                <aside className="chat-camera-panel">
                  <h2 className="op-h1">Camera</h2>
                  <div className="camera-frame-shell">
                    {cameraFrameOk ? null : (
                      <div className="camera-placeholder">No camera feed</div>
                    )}
                    <img
                      className="camera-frame"
                      src={sharedCameraSrc}
                      alt="Ava camera feed"
                      onLoad={() => setCameraFrameOk(true)}
                      onError={() => setCameraFrameOk(false)}
                      style={{ display: cameraFrameOk ? "block" : "none" }}
                    />
                  </div>
                  <div className="chat-orb-wrap">
                    <div className="chat-orb">
                      <OrbCanvas
                        emotion={primaryEmotion}
                        emotionColor={effectiveOrbColor}
                        state={shutdownInProgress ? "offline" : (orbPulseMode as any)}
                        size={120}
                        amplitude={ttsAmplitude}
                        energy={moodEnergy}
                        sleepProgress={sleepProgress}
                        sleepRemainingSeconds={sleepRemainingSeconds}
                        wakeProgress={wakeProgress}
                      />
                    </div>
                    <div className="chat-orb-label">{primaryEmotion}</div>
                  </div>
                  <Section title="Awareness">
                    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <span className="op-muted" style={{ minWidth: 96 }}>Seen person</span>
                        <span style={{ color: "#dbe6f5" }}>{personIdentity}</span>
                        {currentPersonIsZeke && (
                          <span style={{
                            padding: "1px 8px", borderRadius: 999,
                            background: "rgba(56, 161, 105, 0.18)",
                            color: "#38d390", fontSize: 11, fontWeight: 600,
                            letterSpacing: "0.05em",
                          }}>ZEKE</span>
                        )}
                      </div>
                      <div style={{ display: "flex", gap: 8 }}>
                        <span className="op-muted" style={{ minWidth: 96 }}>Confidence</span>
                        <span style={{ color: "#dbe6f5" }}>{personConfidencePctOneDp}%</span>
                      </div>
                      {currentPersonName && (
                        <div style={{ display: "flex", gap: 8 }}>
                          <span className="op-muted" style={{ minWidth: 96 }}>Time</span>
                          <span style={{ color: "#a3b1c0" }}>{personTimeAtMachineLabel}</span>
                        </div>
                      )}
                      <div style={{ display: "flex", gap: 8 }}>
                        <span className="op-muted" style={{ minWidth: 96 }}>Scene</span>
                        <span style={{ color: sceneIsObserving ? "#7a8aa3" : "#dbe6f5", fontStyle: sceneIsObserving ? "italic" : "normal" }}>
                          {sceneSummary}
                          {sceneIsObserving && (
                            <span className="thought-cursor" style={{
                              display: "inline-block", marginLeft: 4,
                              animation: "thoughtBlink 1s steps(2) infinite",
                            }}>▌</span>
                          )}
                        </span>
                      </div>
                      <div style={{ display: "flex", gap: 8 }}>
                        <span className="op-muted" style={{ minWidth: 96 }}>Emotion</span>
                        <span style={{ color: "#dbe6f5" }}>{moodLine}</span>
                      </div>
                    </div>
                  </Section>
                </aside>
                <section className="chat-main-panel">
                  <h1 className="op-h1">Chat</h1>
                  <p className="op-lead">
                    Uses <code>POST /api/v1/chat</code> and <code>GET /api/v1/chat/history</code>.
                  </p>
                  <div className="chat-scroll chat-scroll-full" ref={chatScrollRef}>
                    {displayMessages.length === 0 && (
                      <p className="op-muted">No messages yet. Send text below — history syncs from the canonical log.</p>
                    )}
                    {displayMessages.map((m, i) => {
                      const rk =
                        m.role === "assistant"
                          ? "assistant"
                          : m.role === "system"
                            ? "system"
                            : "user";
                      // Prefer the `source` tag (zeke / claude_code /
                      // ava_response / ava_initiative / unknown_user) over
                      // the bare `role` so the chat log surfaces who
                      // actually spoke. Source-attribution tagging lands at
                      // brain/avaagent.py:_resolve_chat_source.
                      const sourceLabel = (() => {
                        const s = (m.source ?? "").toString();
                        if (s === "zeke") return "zeke";
                        if (s === "claude_code") return "claude code";
                        if (s === "ava_response") return "ava";
                        if (s === "ava_initiative") return "ava (initiated)";
                        if (s === "unknown_user") return "unknown user";
                        if (s) return s;  // passthrough for third-party profiles
                        return m.role ?? "?";
                      })();
                      return (
                        <div key={i} className={`chat-bubble chat-${rk}`}>
                          <span className="chat-role">{sourceLabel}</span>
                          <div className="chat-text">{String(m.content ?? "")}</div>
                        </div>
                      );
                    })}
                    {chatThinking && (
                      <div className="typing-line">
                        <span className="typing-dot" />
                        <span className="typing-dot" />
                        <span className="typing-dot" />
                        <span className="typing-text">Ava is thinking...</span>
                      </div>
                    )}
                    {/* Inner thought — fades in, holds 8s, fades out. */}
                    {displayedThought && (
                      <div
                        className="inner-thought"
                        style={{
                          marginTop: 12,
                          padding: "8px 12px",
                          fontStyle: "italic",
                          color: "#7a8aa3",
                          fontSize: 13,
                          opacity: thoughtVisible ? 0.85 : 0,
                          transition: "opacity 1s ease-in-out",
                          letterSpacing: "0.01em",
                        }}
                      >
                        💭 {displayedThought}
                      </div>
                    )}
                  </div>
                  <div className="chat-compose chat-compose-stick">
                    <textarea
                      rows={3}
                      value={chatInput}
                      onChange={(e) => setChatInput(e.target.value)}
                      placeholder={inputMuted ? "Your input is muted" : "Message Ava…"}
                      disabled={chatBusy || inputMuted}
                      onKeyDown={(e) => {
                        if (inputMuted) return;
                        if (e.key === "Enter" && !e.shiftKey) {
                          e.preventDefault();
                          void sendChat();
                        }
                      }}
                    />
                    <div className="chat-actions">
                      <button type="button" className="btn primary" disabled={chatBusy || !online || inputMuted} onClick={() => void sendChat()}>
                        {chatBusy ? "Sending…" : "Send"}
                      </button>
                      <button type="button" className="btn ghost" disabled title="Voice capture not wired in operator API yet">
                        Voice — coming soon
                      </button>
                    </div>
                  </div>
                  {lastChatErr ? <p className="op-error">{lastChatErr}</p> : null}
                </section>
              </div>
            </div>
          )}

          {tab === "brain" && (
            <div className="op-pane op-pane-brain">
              <div className="brain-layout">
                <div className="brain-canvas-wrap" style={{ position: "relative" }}>
                  <div className="brain-stats-bar">
                    NODES: {brainStatsBar.nodes} EDGES: {brainStatsBar.edges} ACTIVE: {brainStatsBar.active} MOST CONNECTED:{" "}
                    {brainStatsBar.mostConnected}
                  </div>
                  {/* Sync indicator — small text top-right; never dims the graph. */}
                  {(brainGraphSyncing || brainLoading) && (
                    <div style={{
                      position: "absolute", top: 8, right: 12, zIndex: 5,
                      fontSize: 11, color: "#7eb6ff", letterSpacing: "0.05em",
                      pointerEvents: "none",
                    }}>
                      Syncing…
                    </div>
                  )}
                  {brainGraphError && brainGraph.nodes.length === 0 && (
                    <div style={{
                      position: "absolute", top: "40%", left: 0, right: 0,
                      textAlign: "center", color: "#a3b1c0", zIndex: 4,
                      pointerEvents: "none",
                    }}>
                      Graph fetch failed: {brainGraphError}
                    </div>
                  )}
                  {/* 3D WebGL graph mount. Drag = rotate, right-drag = pan,
                       scroll = zoom, middle-click = recenter on AVA SELF. */}
                  <div
                    ref={brainGraph3DContainerRef}
                    className="brain-canvas"
                    style={{ width: "100%", height: 620, background: "transparent", borderRadius: 6 }}
                    onMouseDown={(ev) => {
                      // Middle button — recenter the camera on the AVA SELF
                      // node and reset zoom. Reachable from anywhere on the
                      // canvas because it bubbles from inside the ForceGraph
                      // child elements.
                      if (ev.button !== 1) return;
                      ev.preventDefault();
                      const fg = brainGraph3DInstanceRef.current;
                      if (!fg) return;
                      try {
                        // 800 distance is the d3-force-3d default camera
                        // distance after init; use it as our "reset zoom".
                        if (typeof fg.cameraPosition === "function") {
                          fg.cameraPosition({ x: 0, y: 0, z: 800 }, { x: 0, y: 0, z: 0 }, 600);
                        }
                      } catch (re) {
                        console.warn("[brain-3d] middle-click recenter failed", re);
                      }
                    }}
                  />
                  <div className="brain-legend">
                    <h4>NODE TYPES</h4>
                    {BRAIN_NODE_TYPES.map((entry) => (
                      <div key={entry.type} className="brain-legend-row">
                        <span className="brain-legend-dot" style={{ backgroundColor: entry.color, color: entry.color }} />
                        <span className="brain-legend-type">{entry.type}</span>
                        <span className="brain-legend-desc">{entry.description}</span>
                      </div>
                    ))}
                  </div>
                </div>
                <aside className="brain-side-panel">
                  <h3>Brain Activity</h3>
                  <p className="op-muted">Active nodes: {brainActive.active_nodes.length}</p>
                  <div className="brain-active-list">
                    {(brainActive.active_nodes || []).slice(0, 12).map((n) => (
                      <div key={n.id} className="brain-active-item">
                        <span style={{ color: n.color }}>{n.label}</span>
                      </div>
                    ))}
                  </div>
                  <h3>Selected Node</h3>
                  {selectedBrainNode ? (
                    <div className="brain-node-details">
                      <p>{selectedBrainNode.label}</p>
                      <p>Type: {selectedBrainNode.type}</p>
                      <p>Weight: {Number(selectedBrainNode.weight || 0).toFixed(2)}</p>
                      <p>Activations: {selectedBrainNode.activation_count}</p>
                      <p>Last: {selectedBrainNode.last_activated ? new Date(selectedBrainNode.last_activated * 1000).toLocaleString() : "—"}</p>
                      <p>{selectedBrainNode.notes || "No notes"}</p>
                      <p>Connected: {selectedBrainNeighbors.map((n) => n.label).join(", ") || "None"}</p>
                    </div>
                  ) : (
                    <p className="op-muted">Click a node to inspect details.</p>
                  )}
                  <h3>Stats</h3>
                  <p>Total nodes: {brainGraph.nodes.length}</p>
                  <p>Total edges: {brainGraph.edges.length}</p>
                  <p>Active (30s): {brainActive.active_nodes.length}</p>
                  <p>Most activated: {String(snapshotBrainGraph?.most_activated ?? "—")}</p>
                  <p>
                    Last bootstrap:{" "}
                    {Number(snapshotBrainGraph?.last_bootstrap ?? 0) > 0
                      ? new Date(Number(snapshotBrainGraph?.last_bootstrap) * 1000).toLocaleString()
                      : "—"}
                  </p>
                  <p>
                    Types: person {Number(snapshotNodesByType?.person ?? 0)}, topic {Number(snapshotNodesByType?.topic ?? 0)},
                    emotion {Number(snapshotNodesByType?.emotion ?? 0)}, memory {Number(snapshotNodesByType?.memory ?? 0)},
                    opinion {Number(snapshotNodesByType?.opinion ?? 0)}, curiosity {Number(snapshotNodesByType?.curiosity ?? 0)},
                    self {Number(snapshotNodesByType?.self ?? 0)}, event {Number(snapshotNodesByType?.event ?? 0)}
                  </p>
                </aside>
              </div>
            </div>
          )}

          {tab === "status" && (
            <div className="op-pane">
              <h1 className="op-h1">Status</h1>
              <p className="op-lead">Polled every 5s via <code>GET /api/v1/snapshot</code> (no separate <code>/status</code> route).</p>
              {!online ? (
                <p className="op-banner-inline">Offline — snapshot unavailable.</p>
              ) : (
                <>
                  <Section title="Runtime">
                    <Kv
                      items={[
                        { label: "Heartbeat mode", value: hb?.heartbeat_mode },
                        { label: "Brain / model", value: models?.selected_model },
                        { label: "Cognitive mode", value: models?.cognitive_mode },
                        { label: "Runtime readiness", value: hb?.runtime_ready_state },
                        { label: "Active issue", value: hb?.runtime_active_issue_summary },
                        { label: "Camera / vision", value: ribbon?.vision_status },
                        { label: "Snapshot ts", value: snap?.ts },
                      ]}
                    />
                  </Section>
                  <Section title="Vision / Gaze / Expression">
                    {liveFrameSrc ? (
                      <div style={{ marginBottom: "0.75rem" }}>
                        <img
                          src={liveFrameSrc}
                          alt="Live camera feed"
                          style={{
                            width: 320, height: 240, objectFit: "cover",
                            borderRadius: 6, border: "1px solid #333",
                            display: "block",
                          }}
                        />
                        <p style={{ fontSize: "0.75rem", color: "#666", marginTop: 4 }}>Live feed ~5fps</p>
                      </div>
                    ) : online ? (
                      <p style={{ fontSize: "0.8rem", color: "#555", marginBottom: "0.75rem" }}>
                        Camera unavailable
                      </p>
                    ) : null}
                    {(() => {
                      const attn = asRecord((snap as Record<string, unknown> | null)?.attention);
                      const gazeRegion = String(attn?.gaze_region ?? "unknown");
                      const attnState = String(attn?.attention_state ?? "unknown");
                      const expr = String(attn?.expression ?? "neutral");
                      const calibrated = Boolean(attn?.gaze_calibrated);
                      const gazeTarget = String(attn?.gaze_target ?? "");
                      return (
                        <>
                          <Kv items={[
                            { label: "Gaze region", value: gazeRegion },
                            { label: "Attention state", value: attnState },
                            { label: "Expression", value: expr },
                            { label: "Gaze calibrated", value: calibrated ? "yes" : "no" },
                            { label: "Gaze target", value: gazeTarget || "—" },
                          ]} />
                          <div style={{ marginTop: "0.75rem", display: "flex", gap: 8, flexWrap: "wrap" }}>
                            <button type="button" className="op-btn" onClick={() => {
                              postJson("/api/v1/camera/calibrate_gaze", {})
                                .then((d) => { const r = d as Record<string, unknown>; alert(r.ok ? "Calibration complete!" : `Failed: ${r.error}`); })
                                .catch(() => {});
                            }}>Calibrate Gaze</button>
                          </div>
                        </>
                      );
                    })()}
                  </Section>
                  <Section title="Onboarding">
                    <p className="op-muted" style={{ marginBottom: "0.75rem" }}>
                      Start a new person onboarding flow. Ava will greet them, take photos, and build a profile.
                    </p>
                    <button
                      type="button"
                      className="op-btn"
                      onClick={() => {
                        postJson("/api/v1/onboarding/start", {})
                          .then((d) => {
                            const r = d as Record<string, unknown>;
                            if (typeof r.reply === "string") setOnboardingReply(r.reply);
                            setOnboardingActive(true);
                            setOnboardingStage("greeting");
                            setOnboardingStageIndex(0);
                          })
                          .catch(() => {});
                      }}
                    >
                      Start Onboarding
                    </button>
                    {Boolean(asRecord(snap?.onboarding)?.active) && (
                      <p style={{ color: "#f5c518", marginTop: "0.5rem", fontSize: "0.85rem" }}>
                        Onboarding active — stage: {String(asRecord(snap?.onboarding)?.stage ?? "")}
                      </p>
                    )}
                  </Section>
                </>
              )}
            </div>
          )}

          {tab === "memory" && (
            <div className="op-pane">
              <h1 className="op-h1">Memory</h1>
              <p className="op-lead">
                From snapshot <code>memory_continuity</code>. There is no dedicated memory list API yet.
              </p>
              {!online ? (
                <p className="op-muted">Not connected.</p>
              ) : (
                <>
                  <Section title="Summary">
                    <Kv
                      items={[
                        { label: "Strategic continuity", value: memory?.strategic_continuity_summary },
                        { label: "Relationship carryover", value: memory?.relationship_carryover },
                        { label: "Thread-like entries (count)", value: memoryCount },
                        { label: "Unfinished thread?", value: memory?.unfinished_thread_present },
                      ]}
                    />
                  </Section>
                  <Section title="Active threads (snapshot)">
                    {threadList.length === 0 ? (
                      <p className="op-muted">No structured threads in snapshot.</p>
                    ) : (
                      <JsonBlock data={threadList} />
                    )}
                  </Section>

                  <Section title={`Mem0 — what Ava knows about you (${mem0Entries.length})`}>
                    <p className="op-muted" style={{ marginTop: 0 }}>
                      Long-term semantic memory. Entries are extracted from conversations by{" "}
                      <code>ava-gemma4</code>. Backed by ChromaDB at <code>memory/mem0_chroma/</code>.
                    </p>
                    <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
                      <input
                        type="text"
                        value={mem0SearchQuery}
                        onChange={(e) => setMem0SearchQuery(e.target.value)}
                        onKeyDown={(e) => { if (e.key === "Enter") void runMem0Search(); }}
                        placeholder="Search memories…"
                        style={{
                          flex: 1, padding: "6px 10px", background: "#0b0f1a",
                          border: "1px solid #2a3548", color: "#dbe6f5", borderRadius: 4,
                        }}
                      />
                      <button
                        type="button" className="btn"
                        disabled={mem0Searching || !mem0SearchQuery.trim()}
                        onClick={() => void runMem0Search()}
                      >
                        {mem0Searching ? "Searching…" : "Search"}
                      </button>
                      {mem0SearchResults !== null && (
                        <button type="button" className="btn ghost" onClick={() => { setMem0SearchResults(null); setMem0SearchQuery(""); }}>
                          Clear
                        </button>
                      )}
                    </div>
                    {mem0Entries.length === 0 && mem0SearchResults === null ? (
                      <p className="op-muted">No memories yet — Ava hasn't been told (or learned) anything to remember.</p>
                    ) : (
                      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                        {(mem0SearchResults ?? mem0Entries).map((entry, i) => {
                          const id = String(entry.id ?? i);
                          const text = String(entry.memory ?? "");
                          const created = String(entry.created_at ?? "").slice(0, 19).replace("T", " ");
                          const score = typeof entry.score === "number" ? entry.score : null;
                          return (
                            <div key={id} style={{
                              padding: "10px 12px", background: "#0b0f1a",
                              border: "1px solid #1f2a3d", borderRadius: 6,
                              display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12,
                            }}>
                              <div style={{ flex: 1 }}>
                                <div style={{ color: "#dbe6f5" }}>{text}</div>
                                <div style={{ fontSize: 11, color: "#7a8aa3", marginTop: 4 }}>
                                  {created || "—"}
                                  {score !== null && <span style={{ marginLeft: 12 }}>score {score.toFixed(2)}</span>}
                                </div>
                              </div>
                              <button
                                type="button" className="btn ghost"
                                onClick={() => void deleteMem0Entry(id)}
                                title="Delete this memory"
                                style={{ flexShrink: 0 }}
                              >
                                Forget
                              </button>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </Section>
                </>
              )}
            </div>
          )}

          {tab === "voice" && (
            <div className="op-pane voice-pane">
              <h1 className="op-h1">Voice</h1>
              <Section title="Mood display">
                <div className="voice-mood-card" style={{ borderColor: orbVisual.color }}>
                  <div className="voice-mood-primary" style={{ color: orbVisual.color }}>
                    {primaryEmotion}
                  </div>
                  <div className="voice-mood-secondary">
                    {secondaryEmotions.map((e, idx) => (
                      <div key={idx} className="voice-mood-row">
                        <span>{String(e.emotion ?? "")}</span>
                        <div className="voice-bar">
                          <div style={{ width: `${Math.round(Number(e.intensity ?? 0) * 100)}%` }} />
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              </Section>
              <div className="voice-orb-center">
                <OrbCanvas
                  emotion={primaryEmotion}
                  emotionColor={effectiveOrbColor}
                  state={shutdownInProgress ? "offline" : (orbPulseMode as any)}
                  size={220}
                  amplitude={ttsAmplitude}
                  energy={moodEnergy}
                  sleepProgress={sleepProgress}
                  sleepRemainingSeconds={sleepRemainingSeconds}
                  wakeProgress={wakeProgress}
                />
              </div>
              <Section title="What Ava sees">
                <div className="camera-frame-shell">
                  <img className="camera-frame" src={sharedCameraSrc} alt="Ava camera feed voice tab" />
                </div>
                <p className="op-muted">{sceneSummary}</p>
                <p className="op-muted">
                  Seen person: {personIdentity} ({personConfidencePct}%)
                </p>
              </Section>
              <Section title="Voice loop status">
                {(() => {
                  const vl = asRecord(snap?.voice_loop);
                  const vlState = String(vl?.state ?? "passive").toLowerCase();
                  const vlActive = Boolean(vl?.active);
                  const stateColor: Record<string, string> = {
                    passive: "#6b7280",
                    listening: "#4ade80",
                    thinking: "#3b82f6",
                    speaking: "#a855f7",
                  };
                  const color = stateColor[vlState] ?? "#6b7280";
                  return (
                    <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "0.5rem 0" }}>
                      <span style={{
                        display: "inline-block", width: 14, height: 14, borderRadius: "50%",
                        background: color, boxShadow: `0 0 12px ${color}`,
                      }} />
                      <span style={{ fontFamily: "monospace", fontWeight: 600, fontSize: "1rem", color }}>
                        {vlState.toUpperCase()}
                      </span>
                      <span className="op-muted" style={{ marginLeft: 8 }}>
                        {vlActive ? "loop active — always listening for wake word" : "loop inactive"}
                      </span>
                    </div>
                  );
                })()}
              </Section>
              <Section title="Wake word & clap detector">
                <Kv items={[
                  { label: "Wake word", value: String(asRecord(snap?.voice)?.wake_word_active ?? "—") },
                  { label: "Clap detector", value: "active (always-on)" },
                  { label: "TTS engine", value: String(tts?.engine ?? "none") },
                ]} />
                <div style={{ marginTop: "0.6rem" }}>
                  <button
                    type="button"
                    className="op-btn"
                    onClick={() => {
                      postJson("/api/v1/clap/calibrate", {})
                        .then((r) => {
                          const rec = r as Record<string, unknown>;
                          alert(rec.ok ? `Recalibrated. Threshold=${rec.threshold}` : `Failed: ${rec.error}`);
                        })
                        .catch(() => {});
                    }}
                  >
                    Recalibrate Clap Detector
                  </button>
                </div>
              </Section>
              <Section title="TTS output controls">
                <p className="op-muted" style={{ marginBottom: "0.5rem" }}>
                  The mic is always listening. Only Ava's voice output can be muted.
                </p>
                <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "center" }}>
                  <button type="button" className="op-btn" onClick={() => void toggleTts()}>
                    {Boolean(tts?.enabled) ? "Mute Ava's voice" : "Unmute Ava's voice"}
                  </button>
                  <button
                    type="button"
                    className="op-btn"
                    onClick={() => {
                      postJson("/api/v1/tts/speak", { text: "Hello. This is a voice test." })
                        .catch(() => {});
                    }}
                  >
                    Test Voice
                  </button>
                  <span className="op-muted">
                    Voice output is {Boolean(tts?.enabled) ? "ON" : "MUTED"}
                  </span>
                </div>
              </Section>
            </div>
          )}

          {tab === "tools" && (
            <div className="op-pane">
              <h1 className="op-h1">Tools</h1>
              <p className="op-lead">Ava can use these tools autonomously (Tier 1) or with verbal check-in (Tier 2).</p>
              <Section title="Registry">
                <Kv
                  items={[
                    { label: "Tool count", value: Number(toolsRegistry?.tool_count ?? 0) },
                    { label: "Last tool used", value: toolsBlock?.last_tool_used ?? "—" },
                    { label: "Last tool result", value: toolsBlock?.last_tool_result ?? "—" },
                    { label: "Execution count", value: Number(toolsBlock?.tool_execution_count ?? 0) },
                  ]}
                />
              </Section>
              <Section title="Available tools">
                {Array.isArray(toolsRegistry?.available_tools) && toolsRegistry.available_tools.length ? (
                  <JsonBlock data={toolsRegistry.available_tools} />
                ) : (
                  <p className="op-muted">No tools published in snapshot yet.</p>
                )}
              </Section>
            </div>
          )}

          {tab === "models" && (
            <div className="op-pane">
              <h1 className="op-h1">Models / Brains</h1>
              <p className="op-lead">
                Discovery + routing. Cloud models available when internet connected.
              </p>

              {/* Dual Brain Status — always shown */}
              <Section title="Dual Brain Status">
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {/* Stream A */}
                  <div style={{
                    background: "#0d1117", border: `1px solid ${dbStreamABusy ? "#4ade80" : "#1e293b"}`,
                    borderRadius: 8, padding: "0.75rem",
                    boxShadow: dbStreamABusy ? "0 0 8px rgba(74,222,128,0.2)" : "none",
                  }}>
                    <div style={{ fontSize: "0.72rem", color: "#4a5568", marginBottom: 4, letterSpacing: "0.1em" }}>
                      STREAM A — FOREGROUND
                    </div>
                    <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                      <span style={{
                        width: 10, height: 10, borderRadius: "50%", flexShrink: 0,
                        background: dbStreamABusy ? "#4ade80" : "#2d3748",
                        boxShadow: dbStreamABusy ? "0 0 6px #4ade80" : "none",
                      }} />
                      <div>
                        <div style={{ color: "#e2e8f0", fontSize: "0.85rem" }}>
                          {String(dbStreamA?.model ?? "ava-personal:latest")}
                        </div>
                        <div style={{ color: dbStreamABusy ? "#4ade80" : "#6b7280", fontSize: "0.75rem" }}>
                          {dbStreamABusy ? "ACTIVE — speaking" : "IDLE"}
                        </div>
                        {Boolean(dbStreamA?.last_active) && Number(dbStreamA?.last_active) > 0 && (
                          <div style={{ color: "#4a5568", fontSize: "0.72rem" }}>
                            Last active {Math.round(Date.now() / 1000 - Number(dbStreamA?.last_active ?? 0))}s ago
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                  {/* Stream B */}
                  <div style={{
                    background: "#0d1117",
                    border: `1px solid ${dbBusy ? "#3b82f6" : dbLiveThinking ? "#0d9488" : "#1e293b"}`,
                    borderRadius: 8, padding: "0.75rem",
                    boxShadow: dbBusy ? "0 0 8px rgba(59,130,246,0.2)" : dbLiveThinking ? "0 0 8px rgba(13,148,136,0.2)" : "none",
                  }}>
                    <div style={{ fontSize: "0.72rem", color: "#4a5568", marginBottom: 4, letterSpacing: "0.1em" }}>
                      STREAM B — BACKGROUND
                    </div>
                    <div style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
                      <span style={{
                        width: 10, height: 10, borderRadius: "50%", flexShrink: 0, marginTop: 3,
                        background: dbBusy ? "#3b82f6" : dbLiveThinking ? "#2dd4bf" : "#2d3748",
                        boxShadow: dbBusy ? "0 0 6px #3b82f6" : dbLiveThinking ? "0 0 6px #2dd4bf" : "none",
                      }} />
                      <div style={{ flex: 1 }}>
                        <div style={{ color: "#93c5fd", fontSize: "0.85rem" }}>
                          {String(dbStreamB?.model ?? "qwen2.5:14b")}
                        </div>
                        <div style={{ color: dbBusy ? "#3b82f6" : dbLiveThinking ? "#2dd4bf" : "#6b7280", fontSize: "0.75rem" }}>
                          {dbBusy && dbCurrentTask
                            ? `thinking: ${dbCurrentTask}`
                            : dbLiveThinking
                              ? "💭 Live thinking about current topic"
                              : dbPendingInsight
                                ? "✨ Has something to share"
                                : "—"}
                        </div>
                        <div style={{ color: "#4a5568", fontSize: "0.72rem", marginTop: 2 }}>
                          Queue: {dbQueueDepth} pending · Completed today: {dbTasksToday}
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </Section>

              {!online ? (
                <p className="op-muted">Not connected.</p>
              ) : (
                <>
                  <Section title="Current routing">
                    <Kv
                      items={[
                        { label: "Selected model", value: models?.selected_model },
                        { label: "Fallback", value: models?.fallback_model },
                        { label: "Reason", value: models?.routing_reason },
                        { label: "Internet", value: connOnline ? `online (${connQuality})` : "offline — cloud disabled" },
                        { label: "Override (host)", value: models?.override_model },
                      ]}
                    />
                  </Section>
                  <Section title="Local Models">
                    <div style={{ fontSize: "0.85rem", color: "#9ca3af" }}>
                      {modelTags.filter(m => !m.includes(":cloud")).map(m => (
                        <div key={m} style={{
                          display: "flex", alignItems: "center", gap: 8, padding: "4px 0",
                          borderBottom: "1px solid #1e293b",
                        }}>
                          <span style={{
                            width: 8, height: 8, borderRadius: "50%", flexShrink: 0,
                            background: models?.selected_model === m ? "#4ade80" : "#2d3748",
                          }} />
                          <span style={{ color: models?.selected_model === m ? "#e2e8f0" : "#9ca3af" }}>{m}</span>
                        </div>
                      ))}
                      {modelTags.filter(m => !m.includes(":cloud")).length === 0 && (
                        <p className="op-muted">No local models discovered.</p>
                      )}
                    </div>
                  </Section>
                  <Section title={`Cloud Models ${connOnline ? "(available)" : "(offline — locked)"}`}>
                    {["kimi-k2.6:cloud", "qwen3.5:cloud", "glm-5.1:cloud", "minimax-m2.7:cloud"].map(m => (
                      <div key={m} style={{
                        display: "flex", alignItems: "center", gap: 8, padding: "4px 0",
                        borderBottom: "1px solid #1e293b", opacity: connOnline ? 1 : 0.4,
                      }}>
                        <span style={{
                          width: 8, height: 8, borderRadius: "50%", flexShrink: 0,
                          background: !connOnline ? "#374151" : models?.selected_model === m ? "#4ade80" : "#1a6cf5",
                        }} />
                        <span style={{ color: connOnline ? "#93c5fd" : "#4b5563", fontSize: "0.85rem" }}>{m}</span>
                        {!connOnline && <span style={{ color: "#374151", fontSize: "0.7rem" }}>🔒 offline</span>}
                      </div>
                    ))}
                  </Section>
                  <Section title="Switch model">
                    <label className="op-label">Model tag</label>
                    <input
                      className="op-input"
                      value={overrideModel}
                      onChange={(e) => setOverrideModel(e.target.value)}
                      placeholder="e.g. llama3:latest"
                      list="model-tags"
                    />
                    <datalist id="model-tags">
                      {modelTags.map((m) => (
                        <option key={m} value={m} />
                      ))}
                    </datalist>
                    <label className="op-label">Cognitive mode (optional)</label>
                    <input
                      className="op-input"
                      value={overrideMode}
                      onChange={(e) => setOverrideMode(e.target.value)}
                      placeholder="Clear override: leave blank and apply"
                    />
                    <button type="button" className="btn primary" onClick={() => void applyModelOverride()}>
                      Apply override
                    </button>
                    {routeMsg ? <p className="op-note">{routeMsg}</p> : null}
                  </Section>
                </>
              )}
            </div>
          )}

          {tab === "creative" && (
            <div className="op-pane">
              <h1 className="op-h1">Creative</h1>
              <p className="op-lead">Image generation via ComfyUI (local FLUX) or Pollinations.ai (cloud).</p>
              <Section title="Generation Status">
                <Kv items={[
                  { label: "ComfyUI :8188", value: comfyuiOnline ? "online" : "not detected" },
                  { label: "Cloud (Pollinations)", value: connOnline ? "available" : "offline" },
                  { label: "Last image", value: String(asRecord(snap)?.latest_image ?? "none") },
                ]} />
              </Section>
              <Section title="Generate Image">
                <label className="op-label">Prompt</label>
                <input
                  className="op-input"
                  value={imagePrompt}
                  onChange={(e) => setImagePrompt(e.target.value)}
                  placeholder="a cat in a forest, cinematic lighting"
                />
                <label className="op-label">Style (optional)</label>
                <input
                  className="op-input"
                  value={imageStyle}
                  onChange={(e) => setImageStyle(e.target.value)}
                  placeholder="oil painting, watercolor, cyberpunk…"
                />
                <button
                  type="button" className="op-btn" disabled={imageBusy || !imagePrompt.trim()}
                  onClick={() => {
                    if (!imagePrompt.trim()) return;
                    setImageBusy(true); setImageResult(null);
                    postJson("/api/v1/images/generate", { prompt: imagePrompt, style: imageStyle })
                      .then((d) => {
                        const r = d as Record<string, unknown>;
                        if (r.ok && typeof r.path === "string") {
                          setImageResult(r.path);
                          // Refresh list
                          getJson("/api/v1/images/list").then((il) => {
                            setImageList(((il as Record<string, unknown>).images as Record<string, unknown>[]) ?? []);
                          }).catch(() => {});
                        }
                      })
                      .catch(() => {})
                      .finally(() => setImageBusy(false));
                  }}
                >
                  {imageBusy ? "Generating…" : "Generate"}
                </button>
                {imageResult && (
                  <p style={{ color: "#4ade80", fontSize: "0.82rem", marginTop: 6 }}>
                    Saved: {imageResult.split("/").pop()}
                  </p>
                )}
              </Section>
              <Section title="Image Gallery">
                {imageListBusy ? <p className="op-muted">Loading…</p> : (
                  imageList.length === 0 ? (
                    <p className="op-muted">No images generated yet.</p>
                  ) : (
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                      {imageList.map((img, i) => {
                        const im = img as Record<string, unknown>;
                        return (
                          <div key={i} style={{
                            border: "1px solid #1e293b", borderRadius: 8, padding: 6,
                            background: "#0d1117", fontSize: "0.72rem", color: "#6b7280",
                            maxWidth: 160,
                          }}>
                            <div style={{ fontFamily: "monospace", wordBreak: "break-all" }}>
                              {String(im.filename ?? "")}
                            </div>
                            <div>{String(im.size_kb ?? "")} KB</div>
                            <button
                              type="button"
                              style={{ marginTop: 4, fontSize: "0.7rem", color: "#ef4444", background: "none", border: "none", cursor: "pointer", padding: 0 }}
                              onClick={() => {
                                fetch(`${API_BASE}/api/v1/images/${String(im.filename ?? "")}`, { method: "DELETE" })
                                  .then(() => setImageList(prev => prev.filter((_, j) => j !== i)))
                                  .catch(() => {});
                              }}
                            >Delete</button>
                          </div>
                        );
                      })}
                    </div>
                  )
                )}
              </Section>
            </div>
          )}

          {tab === "finetune" && (
            <div className="op-pane">
              <h1 className="op-h1">Finetune</h1>
              <p className="op-lead">
                Fine-tuning takes 30-60 minutes and will use significant CPU/GPU. Ava will continue running during this process.
              </p>
              <Section title="Status">
                <Kv
                  items={[
                    { label: "Status", value: finetuneStatus.status ?? "idle" },
                    { label: "Started", value: finetuneStatus.started_at ? new Date(Number(finetuneStatus.started_at) * 1000).toLocaleString() : "—" },
                    { label: "Completed", value: finetuneStatus.completed_at ? new Date(Number(finetuneStatus.completed_at) * 1000).toLocaleString() : "—" },
                    { label: "Examples used", value: finetuneStatus.examples_used ?? finetuneStatus.dataset_count ?? "—" },
                    { label: "Output model", value: finetuneStatus.output_model ?? "ava-personal:latest" },
                  ]}
                />
              </Section>
              <Section title="Dataset + Prerequisites">
                <p className="op-muted">Dataset examples: {String((asRecord(finetunePrep.validation)?.count ?? finetuneStatus.dataset_count ?? 0) as number)}</p>
                <div className="finetune-checks">
                  {Object.keys(prereqChecks).length === 0 ? (
                    <p className="op-muted">Run Prepare Dataset or Start Fine-tune to evaluate prerequisites.</p>
                  ) : (
                    Object.entries(prereqChecks).map(([k, v]) => (
                      <div key={k} className={`finetune-check ${v ? "ok" : "bad"}`}>
                        {v ? "✓" : "✕"} {k}
                      </div>
                    ))
                  )}
                </div>
                {Array.isArray(finetunePrereq.issues) && finetunePrereq.issues.length > 0 ? (
                  <pre className="mono-block">{finetunePrereq.issues.join("\n")}</pre>
                ) : null}
              </Section>
              <Section title="Actions">
                <div className="row-gap">
                  <button type="button" className="btn" onClick={() => void prepareFinetuneDataset()} disabled={finetuneBusy}>
                    Prepare Dataset
                  </button>
                  <button type="button" className="btn primary" onClick={() => void startFinetune()} disabled={finetuneBusy || !canStartFinetune}>
                    Start Fine-tune
                  </button>
                  <button type="button" className="btn ghost" onClick={() => void fetchFinetuneStatus()} disabled={finetuneBusy}>
                    Check Status
                  </button>
                </div>
                {String(finetuneStatus.status ?? "") === "complete" ? (
                  <p className="op-note">ava-personal:latest is ready. Switch to it in Models/Brains tab.</p>
                ) : null}
              </Section>
              <Section title="Live Log (last 20 lines)">
                <pre className="debug-pre tall">{finetuneLog.length ? finetuneLog.join("\n") : "(no finetune log yet)"}</pre>
              </Section>
            </div>
          )}

          {tab === "workbench" && (
            <div className="op-pane">
              <h1 className="op-h1">Workbench</h1>
              <p className="op-lead">
                Snapshot fields + index text; approve/reject now call operator HTTP and refresh state.
              </p>
              {!online ? (
                <p className="op-muted">Not connected.</p>
              ) : (
                <>
                  <Section title="State">
                    <Kv
                      items={[
                        { label: "Has proposal", value: wb?.workbench_has_proposal },
                        { label: "Top title", value: wb?.workbench_top_proposal_title },
                        { label: "Summary", value: wb?.workbench_summary },
                        { label: "Execution ready", value: wb?.workbench_execution_ready },
                      ]}
                    />
                  </Section>
                  {typeof wb?.workbench_index_text === "string" && wb.workbench_index_text.trim() ? (
                    <Section title="Index preview">
                      <pre className="mono-block">{wb.workbench_index_text.slice(0, 12000)}</pre>
                    </Section>
                  ) : (
                    <p className="op-muted">No workbench index text on snapshot.</p>
                  )}
                  <Section title="Actions">
                    <div className="row-gap">
                      <button
                        type="button"
                        className="btn"
                        onClick={() => void workbenchAction("approve", selectedProposalId)}
                        disabled={!selectedProposalId}
                      >
                        Approve top proposal
                      </button>
                      <button
                        type="button"
                        className="btn"
                        onClick={() => void workbenchAction("reject", selectedProposalId)}
                        disabled={!selectedProposalId}
                      >
                        Reject top proposal
                      </button>
                    </div>
                    {wbActionMsg ? <p className="op-note">{wbActionMsg}</p> : null}
                  </Section>
                  <Section title="workbench_meta">
                    <JsonBlock data={wb?.workbench_meta ?? {}} maxHeight={240} />
                  </Section>
                </>
              )}
            </div>
          )}

          {tab === "plans" && (
            <div className="op-pane">
              <h1 className="op-h1">Plans</h1>
              <p className="op-lead">
                Ava's long-horizon plans — created from her own goals and curiosity.
                She decides priority, approach, and timing.
              </p>
              <Section title="Create a plan (Ava-initiated)">
                <p className="op-note">
                  Ava creates plans from her own goals. You can seed one here as a suggestion.
                </p>
                <div style={{ display: "flex", gap: "8px", marginBottom: "8px" }}>
                  <input
                    className="op-input"
                    placeholder="Goal description…"
                    value={planGoalInput}
                    onChange={(e) => setPlanGoalInput(e.target.value)}
                    style={{ flex: 1 }}
                  />
                  <button
                    type="button"
                    className="btn primary"
                    disabled={plansBusy || !planGoalInput.trim()}
                    onClick={async () => {
                      setPlansBusy(true);
                      setPlanMsg("");
                      try {
                        const res = await postJson("/api/v1/plans/create", { goal: planGoalInput.trim() });
                        const r = res as Record<string, unknown>;
                        if (r.ok) {
                          setPlanMsg(`Created plan: ${String((r.plan as Record<string, unknown>)?.id ?? "")}`);
                          setPlanGoalInput("");
                          void fetchPlans();
                        } else {
                          setPlanMsg(`Error: ${String(r.error ?? "unknown")}`);
                        }
                      } catch (e) {
                        setPlanMsg(e instanceof Error ? e.message : String(e));
                      } finally {
                        setPlansBusy(false);
                      }
                    }}
                  >
                    Create
                  </button>
                </div>
                {planMsg && <p className="op-note">{planMsg}</p>}
              </Section>
              <Section title={`Active plans (${plans?.active_count ?? 0})`}>
                <button type="button" className="btn ghost" style={{ marginBottom: "8px" }} onClick={() => void fetchPlans()} disabled={plansBusy}>
                  Refresh
                </button>
                {!plans ? (
                  <p className="op-muted">Loading…</p>
                ) : plans.plans.length === 0 ? (
                  <p className="op-muted">No plans yet. Ava will create them from her goals and curiosity.</p>
                ) : (
                  plans.plans.map((plan) => {
                    const p = plan as Record<string, unknown>;
                    const steps = (p.steps as Record<string, unknown>[]) ?? [];
                    const done = steps.filter((s) => String(s.status) === "completed" || String(s.status) === "skipped").length;
                    const pct = steps.length ? Math.round((done / steps.length) * 100) : 0;
                    return (
                      <div key={String(p.id)} style={{ border: "1px solid #2a2a3a", borderRadius: "6px", padding: "12px", marginBottom: "10px" }}>
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "6px" }}>
                          <strong style={{ color: "#a78bfa" }}>[{String(p.id)}] {String(p.goal ?? "").slice(0, 80)}</strong>
                          <span style={{
                            background: String(p.status) === "active" ? "#22c55e22" : String(p.status) === "paused" ? "#eab30822" : "#6b728022",
                            color: String(p.status) === "active" ? "#4ade80" : String(p.status) === "paused" ? "#fbbf24" : "#9ca3af",
                            borderRadius: "4px", padding: "2px 8px", fontSize: "0.8em",
                          }}>{String(p.status)}</span>
                        </div>
                        <div style={{ fontSize: "0.85em", color: "#9ca3af", marginBottom: "6px" }}>
                          {done}/{steps.length} steps · {pct}%
                          <div style={{ background: "#1e1e2e", borderRadius: "3px", height: "4px", marginTop: "4px" }}>
                            <div style={{ background: "#a78bfa", width: `${pct}%`, height: "4px", borderRadius: "3px", transition: "width 0.3s" }} />
                          </div>
                        </div>
                        {((p.progress_notes as string[]) ?? []).slice(-2).map((n, i) => (
                          <p key={i} style={{ fontSize: "0.8em", color: "#6b7280", margin: "2px 0" }}>· {n}</p>
                        ))}
                        <div style={{ display: "flex", gap: "6px", marginTop: "8px" }}>
                          {String(p.status) === "active" && (
                            <button type="button" className="btn ghost" style={{ fontSize: "0.8em", padding: "2px 8px" }}
                              onClick={async () => {
                                await postJson(`/api/v1/plans/${String(p.id)}/pause`, {});
                                void fetchPlans();
                              }}>Pause</button>
                          )}
                          {String(p.status) === "paused" && (
                            <button type="button" className="btn ghost" style={{ fontSize: "0.8em", padding: "2px 8px" }}
                              onClick={async () => {
                                await postJson(`/api/v1/plans/${String(p.id)}/resume`, {});
                                void fetchPlans();
                              }}>Resume</button>
                          )}
                        </div>
                      </div>
                    );
                  })
                )}
              </Section>
            </div>
          )}

          {tab === "journal" && (
            <div className="op-pane">
              <h1 className="op-h1">Journal</h1>
              <p className="op-lead">Ava's private journal. She decides what to write and what to share.</p>
              {journalBusy ? (
                <p className="op-muted">Loading…</p>
              ) : (
                <>
                  <Section title="Summary">
                    <Kv items={[
                      { label: "Total entries", value: journalTotal },
                      { label: "Shared with you", value: journalSharedCount },
                    ]} />
                  </Section>
                  <Section title="Entries">
                    {(journalEntries ?? []).length === 0 ? (
                      <p className="op-muted">No entries yet.</p>
                    ) : (
                      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                        {[...(journalEntries ?? [])].reverse().map((entry, i) => {
                          const e = entry as Record<string, unknown>;
                          const isPrivate = Boolean(e.is_private) && !Boolean(e.shared);
                          return (
                            <div key={String(e.id ?? i)} style={{
                              background: "#0d1117", border: "1px solid #1e293b",
                              borderRadius: 8, padding: "0.75rem", fontSize: "0.85rem",
                            }}>
                              <div style={{ color: "#4a90d9", marginBottom: 4, fontSize: "0.75rem" }}>
                                {String(e.date ?? "")} · {String(e.topic ?? "")}
                                {Boolean(e.shared) && <span style={{ color: "#4ade80", marginLeft: 8 }}>shared</span>}
                              </div>
                              {isPrivate ? (
                                <p style={{ color: "#4a5568", fontStyle: "italic" }}>
                                  [private entry — {String(e.date ?? "")}]
                                </p>
                              ) : (
                                <p style={{ color: "#e2e8f0", lineHeight: 1.5 }}>{String(e.content ?? "")}</p>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </Section>
                </>
              )}
            </div>
          )}

          {tab === "learning" && (
            <div className="op-pane">
              <h1 className="op-h1">Learning</h1>
              <p className="op-lead">What Ava has learned, from where, and what she's still curious about.</p>
              {learningBusy ? <p className="op-muted">Loading…</p> : (
                <>
                  <Section title="This Week">
                    <p style={{ color: "#e2e8f0", fontSize: "0.85rem", lineHeight: 1.6, whiteSpace: "pre-wrap" }}>
                      {learningWeekSummary || "No learnings recorded yet."}
                    </p>
                  </Section>
                  <Section title="Knowledge Gaps">
                    {learningGaps.length === 0 ? <p className="op-muted">No gaps identified.</p> : (
                      <ul style={{ color: "#9ca3af", fontSize: "0.85rem", paddingLeft: "1.2rem" }}>
                        {learningGaps.map((g, i) => <li key={i}>{g}</li>)}
                      </ul>
                    )}
                  </Section>
                  <Section title="Recent Learnings">
                    {(learningLog ?? []).length === 0 ? <p className="op-muted">Nothing yet.</p> : (
                      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                        {[...(learningLog ?? [])].reverse().slice(0, 20).map((e, i) => {
                          const entry = e as Record<string, unknown>;
                          return (
                            <div key={i} style={{
                              background: "#0d1117", border: "1px solid #1e293b",
                              borderRadius: 8, padding: "0.6rem", fontSize: "0.82rem",
                            }}>
                              <div style={{ color: "#4a90d9", marginBottom: 3 }}>
                                {String(entry.date ?? "")} · {String(entry.topic ?? "")} · <em>{String(entry.source ?? "")}</em>
                                <span style={{ color: "#4ade80", marginLeft: 8 }}>
                                  {Math.round(Number(entry.confidence ?? 0) * 100)}% confidence
                                </span>
                              </div>
                              <p style={{ color: "#9ca3af", margin: 0 }}>{String(entry.knowledge ?? "")}</p>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </Section>
                </>
              )}
            </div>
          )}

          {tab === "people" && (
            <div className="op-pane">
              <h1 className="op-h1">People</h1>
              <p className="op-lead">Everyone Ava knows. Recognition confidence and profile status.</p>
              <Section title="Current at Machine">
                <Kv items={[
                  { label: "Person", value: String(asRecord(snap?.current_person)?.display_name ?? "Unknown") },
                  { label: "Confidence", value: `${Math.round(Number(asRecord(snap?.current_person)?.confidence ?? 0) * 100)}%` },
                  { label: "Time at machine", value: `${Math.round(Number(asRecord(snap?.current_person)?.time_at_machine ?? 0))}s` },
                  { label: "Is Zeke", value: Boolean(asRecord(snap?.current_person)?.is_zeke) ? "yes" : "no" },
                ]} />
              </Section>
              <Section title="Start Onboarding">
                <button type="button" className="op-btn" onClick={() => {
                  postJson("/api/v1/onboarding/start", {})
                    .then((d) => {
                      const r = d as Record<string, unknown>;
                      if (typeof r.reply === "string") setOnboardingReply(r.reply);
                      setOnboardingActive(true);
                    })
                    .catch(() => {});
                }}>Start New Onboarding</button>
              </Section>
              {peopleBusy ? <p className="op-muted">Loading…</p> : (
                <Section title="Known Profiles">
                  {(profiles ?? []).length === 0 ? <p className="op-muted">No profiles.</p> : (
                    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                      {(profiles ?? []).map((p, i) => {
                        const prof = p as Record<string, unknown>;
                        return (
                          <div key={i} style={{
                            background: "#0d1117", border: "1px solid #1e293b",
                            borderRadius: 8, padding: "0.75rem", fontSize: "0.85rem",
                          }}>
                            <div style={{ color: "#e2e8f0", fontWeight: 600 }}>{String(prof.name ?? prof.person_id ?? "")}</div>
                            <div style={{ color: "#4a5568", fontSize: "0.75rem" }}>
                              {String(prof.person_id ?? "")} · {String(prof.relationship_to_zeke ?? "")}
                              {Boolean(prof.onboarding_complete) && <span style={{ color: "#4ade80", marginLeft: 8 }}>onboarded</span>}
                            </div>
                            <button type="button" style={{
                              marginTop: 6, fontSize: "0.75rem", background: "#1e293b",
                              border: "1px solid #2d3748", borderRadius: 6, padding: "0.2rem 0.6rem",
                              color: "#9ca3af", cursor: "pointer",
                            }} onClick={() => {
                              postJson(`/api/v1/profile/${String(prof.person_id ?? "")}/refresh`, {})
                                .then(() => setPeopleBusy(false))
                                .catch(() => {});
                            }}>Refresh Profile</button>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </Section>
              )}
            </div>
          )}

          {tab === "emil" && (
            <div className="op-pane">
              <h1 className="op-h1">Emil</h1>
              <p className="op-lead">Emil is Ava's sibling AI on port 5877. They share knowledge, not identity.</p>
              <Section title="Status">
                <button type="button" className="btn ghost" style={{ marginBottom: "8px" }}
                  onClick={() => {
                    setEmilBusy(true);
                    postJson("/api/v1/emil/ping", {})
                      .then((d) => { setEmilStatus(d as Record<string, unknown>); setEmilBusy(false); })
                      .catch(() => setEmilBusy(false));
                  }} disabled={emilBusy}>Ping Emil</button>
                {emilStatus ? (
                  <div style={{ fontSize: "0.9em" }}>
                    <p><strong>Online:</strong> <span style={{ color: emilStatus.online ? "#4ade80" : "#f87171" }}>{emilStatus.online ? "yes" : "no"}</span></p>
                    <p><strong>Last contact:</strong> {emilStatus.last_contact ? new Date(Number(emilStatus.last_contact) * 1000).toLocaleString() : "never"}</p>
                    <p><strong>Shared topics:</strong> {Array.isArray(emilStatus.shared_topics) && emilStatus.shared_topics.length > 0 ? (emilStatus.shared_topics as string[]).join(", ") : "(none yet)"}</p>
                  </div>
                ) : <p className="op-muted">Not loaded — click Ping Emil.</p>}
              </Section>
              <Section title="Send message to Emil">
                <div style={{ display: "flex", gap: "8px", marginBottom: "8px" }}>
                  <input className="op-input" placeholder="Message…" value={emilSendInput}
                    onChange={(e) => setEmilSendInput(e.target.value)} style={{ flex: 1 }} />
                  <button type="button" className="btn primary" disabled={emilBusy || !emilSendInput.trim()}
                    onClick={async () => {
                      setEmilBusy(true); setEmilSendMsg("");
                      try {
                        const r = await postJson("/api/v1/emil/send", { message: emilSendInput.trim() }) as Record<string, unknown>;
                        setEmilSendMsg(r.ok ? `Emil replied: ${String(r.reply || "(no reply)")}` : `Error: ${String(r.error || "unknown")}`);
                        setEmilSendInput("");
                      } catch (e) { setEmilSendMsg(e instanceof Error ? e.message : String(e)); }
                      finally { setEmilBusy(false); }
                    }}>Send</button>
                </div>
                {emilSendMsg && <p className="op-note">{emilSendMsg}</p>}
              </Section>
            </div>
          )}

          {tab === "proposals" && (
            <div className="op-pane">
              <h1 className="op-h1">Identity Proposals</h1>
              <p className="op-lead">Ava proposes additions to her own identity. You review and approve or ignore.</p>
              <Section title={`Pending proposals (${proposals?.length ?? 0})`}>
                <button type="button" className="btn ghost" style={{ marginBottom: "8px" }}
                  onClick={() => void fetchProposals()} disabled={proposalsBusy}>Refresh</button>
                {proposalMsg && <p className="op-note">{proposalMsg}</p>}
                {!proposals ? <p className="op-muted">Loading…</p>
                  : proposals.length === 0 ? <p className="op-muted">No pending proposals yet. Ava will propose identity additions as she learns.</p>
                  : proposals.map((prop, idx) => {
                    const p = prop as Record<string, unknown>;
                    const ts = Number(p.ts || 0);
                    return (
                      <div key={idx} style={{ border: "1px solid #2a2a3a", borderRadius: "6px", padding: "12px", marginBottom: "8px" }}>
                        <p style={{ fontSize: "0.85em", color: "#a78bfa", marginBottom: "4px" }}>
                          {ts > 0 ? new Date(ts * 1000).toLocaleString() : ""}
                          {" · "}
                          <span style={{ color: String(p.status) === "pending" ? "#fbbf24" : "#4ade80" }}>{String(p.status)}</span>
                        </p>
                        <p style={{ fontSize: "0.9em", color: "#d1d5db", marginBottom: "8px" }}>{String(p.text || "")}</p>
                        {String(p.status) === "pending" && (
                          <button type="button" className="btn primary" style={{ fontSize: "0.8em", padding: "3px 10px" }}
                            onClick={async () => {
                              setProposalsBusy(true); setProposalMsg("");
                              try {
                                const r = await postJson("/api/v1/identity/proposals/approve", { text: p.text }) as Record<string, unknown>;
                                setProposalMsg(r.ok ? "Approved and applied to identity." : `Error: ${String(r.error || "unknown")}`);
                                void fetchProposals();
                              } catch (e) { setProposalMsg(e instanceof Error ? e.message : String(e)); }
                              finally { setProposalsBusy(false); }
                            }}>Approve</button>
                        )}
                      </div>
                    );
                  })
                }
              </Section>
              <Section title="Active identity extensions">
                <p className="op-note">Extensions loaded from <code>state/identity_extensions.md</code> and injected into all prompts.</p>
                <pre className="identity-ro" style={{ fontSize: "0.8em" }}>
                  {String((snap as Record<string, unknown>)?.identity_extensions || "(none yet)")}
                </pre>
              </Section>
            </div>
          )}

          {tab === "identity" && (
            <div className="op-pane">
              <h1 className="op-h1">Identity</h1>
              <p className="op-lead">
                Read-only via <code>GET /api/v1/identity/IDENTITY</code> (and SOUL, USER). No editing.
              </p>
              {identity.err ? <p className="op-error">{identity.err}</p> : null}
              {!online && !identity.identity && !identity.soul && !identity.user ? (
                <p className="op-muted">Offline — identity files not loaded.</p>
              ) : (
                <>
                  <Section title="IDENTITY.md">
                    <pre className="identity-ro">{identity.identity || "(empty or unavailable)"}</pre>
                  </Section>
                  <Section title="SOUL.md">
                    <pre className="identity-ro">{identity.soul || "(empty or unavailable)"}</pre>
                  </Section>
                  <Section title="USER.md">
                    <pre className="identity-ro">{identity.user || "(empty or unavailable)"}</pre>
                  </Section>
                </>
              )}
            </div>
          )}

          {tab === "debug" && (
            <div className="op-pane op-pane-debug">
              <h1 className="op-h1">Debug</h1>
              <p className="op-lead">
                Operator HTTP instrumentation. Paste <strong>Backend Debug Export</strong> for a full Ava handoff.
              </p>

              <Section title="1 — Live API response log">
                <div className="debug-toolbar">
                  <button type="button" className="btn ghost" onClick={() => setApiCallLog([])}>
                    Clear log
                  </button>
                  <span className="op-muted">{apiCallLog.length}/50 entries · newest first</span>
                </div>
                <div className="debug-log-scroll">
                  {apiCallLog.length === 0 ? (
                    <p className="op-muted">No API calls recorded yet.</p>
                  ) : (
                    apiCallLog.map((e, idx) => (
                      <div key={`${e.timestamp}-${e.endpoint}-${idx}`} className="debug-api-entry">
                        <div className="debug-api-meta">
                          <span className="debug-ts">{e.timestamp}</span>
                          <span className={`debug-status ${e.status >= 400 ? "bad" : ""}`}>{e.status}</span>
                          <span className="debug-endpoint">{e.endpoint}</span>
                        </div>
                        <pre className="debug-pre">{e.responseBody}</pre>
                      </div>
                    ))
                  )}
                </div>
              </Section>

              <Section title="2 — Last chat response (POST /api/v1/chat)">
                {lastChatResponse === null ? (
                  <p className="op-muted">No chat POST completed in this session yet.</p>
                ) : (
                  <>
                    <p className="debug-reply-flag">
                      reply field:{" "}
                      <strong className={lastReplyPresent ? "debug-ok" : "debug-bad"}>
                        {lastReplyPresent ? "present (non-empty)" : "missing or empty"}
                      </strong>
                    </p>
                    <p className="debug-reply-flag">
                      debug_reply_source:{" "}
                      <strong>
                        {typeof lastChatResponse.debug_reply_source === "string"
                          ? lastChatResponse.debug_reply_source
                          : "empty"}
                      </strong>
                    </p>
                    {lastReplyEmpty ? (
                      <p className="debug-fat-warn">
                        Reply text was empty after checking reply → assistant_reply → message → text. Inspect JSON below.
                      </p>
                    ) : null}
                    <pre className="debug-pre">{JSON.stringify(lastChatResponse, null, 2)}</pre>
                  </>
                )}
              </Section>

              <Section title="3 — Backend debug export">
                <div className="debug-toolbar">
                  <button type="button" className="btn primary" disabled={debugExportBusy} onClick={() => void fetchDebugExport()}>
                    {debugExportBusy ? "Fetching…" : "Fetch debug export"}
                  </button>
                  <button
                    type="button"
                    className="btn ghost"
                    disabled={!debugExportText}
                    onClick={() => void copyToClipboard(debugExportText, "debug export")}
                  >
                    Copy to clipboard
                  </button>
                </div>
                <p className="op-muted">
                  GET <code>/api/v1/debug/export</code> — plain-text bundle from{" "}
                  <code>build_debug_export(host)</code>.
                </p>
                <pre className="debug-pre tall">{debugExportText || "(not fetched)"}</pre>
              </Section>

              <Section title="4 — Snapshot raw (last poll)">
                <button
                  type="button"
                  className="btn ghost"
                  disabled={!lastSnapshotRaw}
                  onClick={() =>
                    lastSnapshotRaw && void copyToClipboard(JSON.stringify(lastSnapshotRaw, null, 2), "snapshot JSON")
                  }
                >
                  Copy to clipboard
                </button>
                <pre className="debug-pre tall">
                  {lastSnapshotRaw ? JSON.stringify(lastSnapshotRaw, null, 2) : "(no snapshot yet)"}
                </pre>
              </Section>

              <Section title="5 — App event log">
                <button
                  type="button"
                  className="btn ghost"
                  disabled={appEventLog.length === 0}
                  onClick={() =>
                    void copyToClipboard(
                      appEventLog.map((r) => `[${r.ts}] ${r.message}`).join("\n"),
                      "event log"
                    )
                  }
                >
                  Copy to clipboard
                </button>
                <pre className="debug-pre tall">
                  {appEventLog.length === 0
                    ? "(no events)"
                    : appEventLog.map((r) => `[${r.ts}] ${r.message}`).join("\n")}
                </pre>
              </Section>
            </div>
          )}
          {customTabs.map((ct) =>
            tab === ct.id ? (
              <CustomTabRenderer key={`pane_${ct.id}`} config={ct} snap={snap} />
            ) : null
          )}
        </main>
      </div>
      {shutdownConfirmOpen && (
        <div className="shutdown-modal-backdrop">
          <div className="shutdown-modal">
            <h2>Shut down Ava?</h2>
            <p>She will save her thoughts before closing.</p>
            <div className="shutdown-modal-actions">
              <button type="button" className="btn op-shutdown-btn" onClick={() => void runShutdown()}>
                Shut Down
              </button>
              <button type="button" className="btn ghost" onClick={() => setShutdownConfirmOpen(false)}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}
      {shutdownInProgress && (
        <div className="shutdown-overlay">
          <div className="shutdown-overlay-card">
            <h2>{shutdownGoodbye ? "Goodnight from Ava" : "Ava is saving her thoughts..."}</h2>
            {shutdownGoodbye ? <p className="shutdown-goodbye-text">{shutdownGoodbye}</p> : <p>Please wait.</p>}
            {shutdownDone ? <p className="shutdown-goodbye-done">Ava has shut down.</p> : null}
            {shutdownWindowCloseHint ? <p className="shutdown-goodbye-done">Ava has shut down. You can close this window.</p> : null}
            {shutdownError ? <p className="op-error">{shutdownError}</p> : null}
          </div>
        </div>
      )}
    </div>
  );
}
