// PC tab (2026-10-07, Zeke's "#2"): sound + media on the tower without clicking around — master volume, every
// app that's making sound, play/pause/skip, and what Spotify is playing. GET /api/v1/app/pc/audio;
// POST /api/v1/app/pc/volume, /api/v1/app/pc/media. Reads state back from Windows after every change.
import { useCallback, useEffect, useRef, useState } from "react";
import { getJson, postJson } from "../api";
import { Section } from "./Ui";

type Master = { volume_pct: number; muted: boolean; device?: string };
type Sess = { app: string; volume_pct: number; muted: boolean };
type Audio = { ok: boolean; master?: Master; sessions?: Sess[]; now_playing?: Record<string, string>; error?: string };

const pretty = (a: string) => a.replace(/\.exe$/i, "").replace(/^python$/i, "me (Iris's voice)");

export default function PcPanel() {
  const [au, setAu] = useState<Audio | null>(null);
  const [msg, setMsg] = useState("");
  const dragging = useRef(false);

  const load = useCallback(async () => {
    if (dragging.current) return;
    try { setAu(await getJson<Audio>("/api/v1/app/pc/audio")); } catch (e) { setMsg(String(e)); }
  }, []);
  useEffect(() => { void load(); const iv = setInterval(() => void load(), 3000); return () => clearInterval(iv); }, [load]);

  const send = async (path: string, body: unknown) => {
    try {
      const r = await postJson<{ ok: boolean; error?: string }>(path, body);
      if (!r.ok) setMsg(r.error ?? "didn't work"); else setMsg("");
    } catch (e) { setMsg(String(e)); }
    dragging.current = false;
    void load();
  };

  const np = au?.now_playing;
  return (
    <div className="op-pane">
      <h1 className="op-h1">PC</h1>
      <p className="op-lead">Sound and media on this computer. Changes go straight to Windows.</p>
      {msg && <p className="op-note">{msg}</p>}
      <Section title="Now playing">
        <p className="iris-world-now">
          {np?.title || np?.track
            ? <>{np.title ?? np.track}{np.artist ? <span className="op-muted"> · {np.artist}</span> : null}
                <span className="op-muted"> · {(np.app ?? "").replace(/\.exe$/i, "")}{np.status ? ` · ${np.status}` : ""}</span></>
            : "Nothing is playing right now."}
        </p>
        <div className="iris-world-toolbar">
          <button onClick={() => void send("/api/v1/app/pc/media", { key: "prev" })} aria-label="Previous">⏮</button>
          <button onClick={() => void send("/api/v1/app/pc/media", { key: "play_pause" })} aria-label="Play or pause">⏯</button>
          <button onClick={() => void send("/api/v1/app/pc/media", { key: "next" })} aria-label="Next">⏭</button>
        </div>
      </Section>
      <Section title={`Volume${au?.master?.device ? ` · ${au.master.device}` : ""}`}>
        {au?.master && (
          <div className="iris-pc-row master">
            <b>Everything</b>
            <input type="range" min={0} max={100} defaultValue={au.master.volume_pct} key={`m${au.master.volume_pct}`}
              onMouseDown={() => { dragging.current = true; }}
              onMouseUp={(e) => void send("/api/v1/app/pc/volume", { percent: Number((e.target as HTMLInputElement).value) })}
              aria-label="Master volume" />
            <span>{au.master.volume_pct}%</span>
            <button className={au.master.muted ? "on" : ""} onClick={() => void send("/api/v1/app/pc/volume", { mute: !au.master?.muted })}>
              {au.master.muted ? "Unmute" : "Mute"}</button>
          </div>
        )}
        {(au?.sessions ?? []).filter((s) => s.app !== "System sounds").map((s) => (
          <div className="iris-pc-row" key={s.app}>
            <b>{pretty(s.app)}</b>
            <input type="range" min={0} max={100} defaultValue={s.volume_pct} key={`${s.app}${s.volume_pct}`}
              onMouseDown={() => { dragging.current = true; }}
              onMouseUp={(e) => void send("/api/v1/app/pc/volume", { app: s.app, percent: Number((e.target as HTMLInputElement).value) })}
              aria-label={`${pretty(s.app)} volume`} />
            <span>{s.volume_pct}%</span>
            <button className={s.muted ? "on" : ""} onClick={() => void send("/api/v1/app/pc/volume", { app: s.app, mute: !s.muted })}>
              {s.muted ? "Unmute" : "Mute"}</button>
          </div>
        ))}
        {au && !au.ok && <p className="op-note">{au.error}</p>}
      </Section>
    </div>
  );
}
