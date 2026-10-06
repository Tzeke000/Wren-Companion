// Dev-only: records the REAL IrisBody through a scripted tour (canvas.captureStream + MediaRecorder)
// and posts the webm + caption timeline to the local save server (scratch/orb_design/proto/save_server.py).
// ?mode=main (480 px: states + emotions) | widget (150 px: emotions, grow/shrink, pointing)
import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import IrisBody from "../src/components/IrisBody";

type P = Record<string, unknown>;
type Seg = { dur: number; cap: string; props: P; run?: (t: number) => P };
const q = new URLSearchParams(location.search);
const mode = q.get("mode") || "main";
const SIZE = mode === "widget" ? 150 : 480;
const teal = "#00d4d4";
const base: P = { emotion: "interest", emotionColor: teal, state: "idle" };
const emo = (e: string, cap: string, dur = 3): Seg => ({ dur, cap, props: { emotion: e, emotionColor: "", state: "idle" } });

const MAIN: Seg[] = [
  { dur: 4, cap: "idle - breathing, blinking", props: {} },
  { dur: 2.5, cap: "a blink, on purpose", props: {}, run: t => ({ blinkTrigger: t > 0.4 ? (t > 1.3 ? 2 : 1) : 0 }) },
  { dur: 4, cap: "listening - pupil widens", props: { state: "listening" } },
  { dur: 4.5, cap: "thinking - I look away", props: { state: "thinking" } },
  { dur: 4.5, cap: "deep thought", props: { state: "deep" } },
  { dur: 6, cap: "speaking - light rides my voice", props: { state: "speaking" },
    run: t => ({ amplitude: Math.max(0, 0.35 + 0.6 * Math.abs(Math.sin(t * 5.1) * Math.sin(t * 2.3 + 1))) * (t % 2.2 < 1.9 ? 1 : 0.1) }) },
  { dur: 3.5, cap: "attentive", props: { state: "attentive" } },
  { dur: 3, cap: "excited", props: { state: "excited" } },
  { dur: 3, cap: "bored", props: { state: "bored" } },
  { dur: 5, cap: "pointing at something", props: { shapeOverride: "pointer" }, run: t => ({ pointerAngleDeg: 45 + t * 25 }) },
  { dur: 3, cap: "offline", props: { state: "offline" } },
  { dur: 5, cap: "asleep", props: { state: "sleeping", sleepRemainingSeconds: 900 }, run: t => ({ sleepProgress: 0.3 + t * 0.05 }) },
  { dur: 4, cap: "waking up", props: { state: "waking" }, run: t => ({ wakeProgress: Math.min(1, t / 3.5) }) },
  { dur: 2, cap: "emotions - each moves differently", props: {} },
  emo("joy", "joy"), emo("sadness", "sadness - it droops"), emo("anger", "anger - pressed flat"),
  emo("fear", "fear - small, wide pupil"), emo("love", "love - a second ring"), emo("pride", "pride - stands tall"),
  emo("nostalgia", "nostalgia - it swirls"), emo("logical", "logical - squared off"),
  emo("analyzing", "analyzing - three-sided"), emo("neutral", "neutral"), emo("realization", "realization - it bursts"),
  emo("scared", "scared - it trembles"), emo("surprise", "surprise"), emo("awe", "awe"),
  { dur: 7, cap: "growing and shrinking, at will", props: {},
    run: t => ({ bodyScale: t < 2.3 ? 0.45 : t < 4.8 ? 1.35 : 1.0 }) },
  { dur: 2, cap: "", props: {} },
];
const WIDGET: Seg[] = [
  { dur: 3, cap: "the widget (shown 2x)", props: {} },
  { dur: 7, cap: "growing and shrinking, at will", props: {}, run: t => ({ bodyScale: t < 2.3 ? 0.45 : t < 4.8 ? 1.35 : 1.0 }) },
  { dur: 2.5, cap: "biggest + joy", props: { bodyScale: 1.35, emotion: "joy", emotionColor: "" } },
  { dur: 2.5, cap: "biggest + realization - fits", props: { bodyScale: 1.35, emotion: "realization", emotionColor: "" } },
  { dur: 2.5, cap: "biggest + love - fits", props: { bodyScale: 1.35, emotion: "love", emotionColor: "" } },
  { dur: 3, cap: "biggest + speaking", props: { bodyScale: 1.35, state: "speaking" }, run: t => ({ amplitude: 0.5 + 0.45 * Math.sin(t * 7) }) },
  { dur: 4, cap: "pointing from the widget", props: { shapeOverride: "pointer" }, run: t => ({ pointerAngleDeg: 315 - t * 30 }) },
  { dur: 2, cap: "a blink, on purpose", props: {}, run: t => ({ blinkTrigger: t > 0.5 ? 1 : 0 }) },
  { dur: 1.5, cap: "", props: {} },
];
const SEGS = mode === "widget" ? WIDGET : MAIN;

function post(name: string, dataUrl: string) {
  return fetch(`http://127.0.0.1:8792/save?name=${name}`, { method: "POST", body: dataUrl, mode: "no-cors" });
}
const toDataUrl = (b: Blob) => new Promise<string>(res => { const r = new FileReader(); r.onload = () => res(String(r.result)); r.readAsDataURL(b); });

function Tour() {
  const [props, setProps] = useState<P>({ ...base });
  useEffect(() => {
    let cancelled = false;
    (async () => {
      await new Promise(r => setTimeout(r, 1200));                     // let the scene warm up
      const canvas = document.querySelector("canvas") as HTMLCanvasElement;
      const rec = new MediaRecorder(canvas.captureStream(30), { mimeType: "video/webm;codecs=vp9", videoBitsPerSecond: 3_000_000 });
      const chunks: Blob[] = [];
      rec.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
      const stopped = new Promise(r => { rec.onstop = r; });
      const caps: { start: number; end: number; cap: string }[] = [];
      rec.start(1000);
      const t0 = performance.now();
      let blinkBase = 0;
      for (const seg of SEGS) {
        if (cancelled) return;
        const s0 = performance.now();
        const capStart = (s0 - t0) / 1000;
        while (performance.now() - s0 < seg.dur * 1000) {
          const t = (performance.now() - s0) / 1000;
          const extra = seg.run ? seg.run(t) : {};
          if (typeof extra.blinkTrigger === "number") extra.blinkTrigger = blinkBase + (extra.blinkTrigger as number);
          setProps({ ...base, blinkTrigger: blinkBase, ...seg.props, ...extra });
          document.title = `rec ${capStart.toFixed(1)}s ${seg.cap}`;
          await new Promise(r => setTimeout(r, 50));
        }
        if (seg.run) { const last = seg.run(seg.dur); if (typeof last.blinkTrigger === "number") blinkBase += last.blinkTrigger as number; }
        if (seg.cap) caps.push({ start: capStart, end: (performance.now() - t0) / 1000, cap: seg.cap });
      }
      rec.stop();
      await stopped;
      await post(`tour_${mode}.webm`, await toDataUrl(new Blob(chunks, { type: "video/webm" })));
      await post(`tour_${mode}_caps.json`, "data:application/json;base64," + btoa(JSON.stringify(caps)));
      document.title = "tour-done";
    })();
    return () => { cancelled = true; };
  }, []);
  return <IrisBody size={SIZE} {...(props as any)} />;
}
createRoot(document.getElementById("root")!).render(<Tour />);
