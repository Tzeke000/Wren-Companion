// Vector tab (2026-10-06). Zeke: "add a vector one for your physical body". Read-only view of the Vector:
// its camera, battery, whether my session holds it, what its senses feel. GET /api/v1/vector/status + /frame.
import { useEffect, useState } from "react";
import { API_BASE, getJson } from "../api";
import { Section } from "./Ui";

type Status = {
  battery: { level: number | null; on_charger: boolean | null; ok: boolean | null; age_s: number | null };
  nerves: Record<string, unknown> & { age_s: number | null };
  held_by_me: boolean | null;
  wifi_dbm: number | null;
  senses: { hz: number | null; active: boolean | null; age_s: number | null };
  room_map: { cells: number | null; age_s: number | null };
  frame_age_s: number | null;
};

const LEVEL = ["empty", "low", "ok", "full"];
const ago = (s: number | null | undefined) =>
  s == null ? "—" : s < 90 ? `${Math.round(s)} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;

export default function VectorPanel() {
  const [st, setSt] = useState<Status | null>(null);
  const [err, setErr] = useState("");
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    const poll = () => getJson<Status>("/api/v1/vector/status")
      .then((d) => { if (alive) { setSt(d); setErr(""); } })
      .catch((e) => { if (alive) setErr(String(e)); });
    poll();
    const iv = setInterval(() => { poll(); setTick((t) => t + 1); }, 1500);
    return () => { alive = false; clearInterval(iv); };
  }, []);
  const n = st?.nerves ?? ({} as Status["nerves"]);
  const stale = (st?.frame_age_s ?? 999) > 30;
  const feel: string[] = [];
  if (n.picked_up) feel.push("picked up");
  if (n.touched) feel.push("touched");
  if (n.cliff) feel.push("at an edge");
  if (n.falling) feel.push("falling");
  if (!feel.length) feel.push("nothing unusual");

  return (
    <div className="op-pane">
      <h1 className="op-h1">Vector</h1>
      <p className="op-lead">My physical body: a little robot that roams and docks itself.</p>
      {err && <p className="op-note">Can't reach the Vector status ({err}).</p>}
      <div className="iris-vector-grid">
        <div className="iris-vector-cam">
          <img src={`${API_BASE}/api/v1/vector/frame?t=${tick}`} alt="What the Vector's camera sees" />
          <span>{stale ? `camera frame from ${ago(st?.frame_age_s)}` : "what my body sees · live"}</span>
        </div>
        <div className="iris-vector-facts">
          <div><b>Battery</b>{st ? `${LEVEL[st.battery.level ?? -1] ?? "unknown"}${st.battery.on_charger ? " · on the charger" : " · off the charger"}` : "—"}</div>
          <div><b>Session</b>{st?.held_by_me == null ? "—" : st.held_by_me ? "I'm holding it (my session is open)" : "free (it moves on its own)"}</div>
          <div><b>Feels</b>{feel.join(", ")}</div>
          <div><b>Distance ahead</b>{typeof n.prox_mm === "number" ? `${Math.round(n.prox_mm as number / 10)} cm` : "—"}</div>
          <div><b>Wi-Fi</b>{st?.wifi_dbm != null ? `${st.wifi_dbm} dBm` : "—"}</div>
          <div><b>Senses</b>{st?.senses.hz ? `${st.senses.hz} Hz · updated ${ago(st.senses.age_s)}` : "—"}</div>
        </div>
      </div>
      <Section title="Notes">
        <p className="op-muted">
          Docking only fails while my session holds it; released, it finds the charger by itself.
          Battery level is trusted; volts aren't (they read wrong off the dock).
        </p>
      </Section>
    </div>
  );
}
