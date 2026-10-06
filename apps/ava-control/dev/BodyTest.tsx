// Dev-only parity harness for the iris body (not part of the app build). ?page=states|emotions
import { createRoot } from "react-dom/client";
import IrisBody from "../src/components/IrisBody";
import OrbCanvas, { setBodyStyle } from "../src/components/OrbCanvas";

const q = new URLSearchParams(location.search);
const page = q.get("page") || "states";
type Cell = { label: string; props: Record<string, unknown> };
const teal = "#00d4d4";
const states: Cell[] = [
  { label: "idle", props: { state: "idle" } },
  { label: "listening", props: { state: "listening" } },
  { label: "attentive", props: { state: "attentive" } },
  { label: "thinking", props: { state: "thinking" } },
  { label: "deep", props: { state: "deep" } },
  { label: "speaking amp .8", props: { state: "speaking", amplitude: 0.8 } },
  { label: "bored", props: { state: "bored" } },
  { label: "excited", props: { state: "excited" } },
  { label: "offline", props: { state: "offline" } },
  { label: "sleeping 60%", props: { state: "sleeping", sleepProgress: 0.6, sleepRemainingSeconds: 754 } },
  { label: "waking 40%", props: { state: "waking", wakeProgress: 0.4 } },
  { label: "state=pointing", props: { state: "pointing" } },
  { label: "pointer 135° (widget)", props: { state: "idle", shapeOverride: "pointer", pointerAngleDeg: 135 } },
  { label: "gaze left-up", props: { state: "idle", gaze: { x: -0.9, y: 0.3 } } },
  { label: "listening no-cube", props: { state: "listening", cubeMorphEnabled: false } },
];
const emo = ["joy", "sadness", "anger", "fear", "love", "pride", "nostalgia", "logical", "analyzing", "neutral",
  "bored2", "thinking_deep", "realization", "scared", "proud"];
const emotions: Cell[] = emo.map(e => ({ label: e, props: { state: "idle", emotion: e, emotionColor: "" } }));
const fitCells: Cell[] = [...emo, "excitement", "surprise", "awe"].map(e => ({ label: e, props: { state: "idle", emotion: e, emotionColor: "", size: 150 } }))
  .concat([{ label: "speaking .9", props: { state: "speaking", amplitude: 0.9, size: 150 } },
           { label: "pointer 45°", props: { state: "idle", shapeOverride: "pointer", pointerAngleDeg: 45, size: 150 } }]);
const BS = Number(q.get("bs") || 1);
const part = Number(q.get("part") || 1);
const cells = page === "emotions" ? emotions : page === "fit" ? fitCells.slice((part - 1) * 10, part * 10) : states;
if (page === "switch") {
  // the real wrapper: flips iris -> classic -> iris live, as the Body toggle does
  setBodyStyle((q.get("style") as "iris" | "classic") || "classic");
}

createRoot(document.getElementById("root")!).render(page === "switch" ? (
  <div className="cell"><span>wrapper: {q.get("style") || "classic"}</span>
    <OrbCanvas emotion="interest" emotionColor={teal} state="speaking" amplitude={0.6} size={190} />
  </div>
) : (
  <>
    {cells.map((c, i) => (
      <div className="cell" key={i}>
        <span>{c.label}</span>
        <div style={{ outline: page === "fit" ? "1px solid #334" : "none" }}><IrisBody emotion="interest" emotionColor={teal} state="idle" size={c.props.shapeOverride ? 150 : 190} bodyScale={BS} {...(c.props as object)} /></div>
      </div>
    ))}
  </>
));
