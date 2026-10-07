// World tab (2026-10-07). Zeke: "the ability to know what the weather is in a given place … get the map of a
// place if you wanna know how it looks … a 2D and a 3D one". Backend: GET /api/v1/app/weather, /map,
// /map/image/{name}; POST /api/v1/app/open (allow-listed map links only) for the Google Earth 3D view.
import { useState } from "react";
import { API_BASE, getJson, postJson } from "../api";
import { Section } from "./Ui";

type Day = { date: string; summary: string; high: number; low: number; rain_chance_pct: number | null };
type Weather = { ok: boolean; place?: string; spoken?: string; now?: string; daily?: Day[]; units?: string;
  local_time?: string; error?: string };
type MapRes = { ok: boolean; place?: string; lat?: number; lon?: number; zoom?: number; image_urls?: Record<string, string>;
  earth_3d_url?: string; google_maps_url?: string; error?: string; errors?: Record<string, string> | null };

const dayName = (iso: string) => {
  const d = new Date(`${iso}T12:00:00`);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
};

export default function WorldPanel() {
  const [place, setPlace] = useState("");
  const [units, setUnits] = useState<"f" | "c">("f");
  const [wx, setWx] = useState<Weather | null>(null);
  const [map, setMap] = useState<MapRes | null>(null);
  const [view, setView] = useState<"2d" | "satellite" | "3d" | "3dsat">("satellite");
  const [zoom, setZoom] = useState(16);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const look = async (z = zoom) => {
    const q = place.trim();
    if (!q || busy) return;
    setBusy(true); setErr("");
    try {
      const enc = encodeURIComponent(q);
      const [w, m] = await Promise.all([
        getJson<Weather>(`/api/v1/app/weather?place=${enc}&days=5&units=${units}`),
        getJson<MapRes>(`/api/v1/app/map?place=${enc}&zoom=${z}&kind=both`),
      ]);
      setWx(w); setMap(m);
      if (!w.ok && !m.ok) setErr(w.spoken || m.error || "couldn't find that place");
    } catch (e) { setErr(String(e)); } finally { setBusy(false); }
  };

  const open3d = async () => {
    if (!map?.earth_3d_url) return;
    try { await postJson("/api/v1/app/open", { url: map.earth_3d_url }); } catch (e) { setErr(String(e)); }
  };

  const is3d = view === "3d" || view === "3dsat";
  const img = is3d ? undefined : map?.image_urls?.[view];
  const url3d = map?.lat != null && map?.lon != null
    ? `${API_BASE}/api/v1/app/map3d?lat=${map.lat}&lon=${map.lon}&mode=${view === "3dsat" ? "satellite" : "buildings"}` +
      `&zoom=${view === "3dsat" ? Math.min(zoom, 15) - 1.5 : zoom}&pitch=${view === "3dsat" ? 70 : 60}&bearing=-20`
    : "";
  return (
    <div className="op-pane">
      <h1 className="op-h1">World</h1>
      <p className="op-lead">Weather and maps for anywhere: a city, a town, a landmark, an address.</p>
      <form className="iris-world-search" onSubmit={(e) => { e.preventDefault(); void look(); }}>
        <input value={place} onChange={(e) => setPlace(e.target.value)} placeholder="Where? (e.g. Tokyo, Eiffel Tower)"
          maxLength={120} aria-label="Place" />
        <select value={units} onChange={(e) => setUnits(e.target.value as "f" | "c")} aria-label="Units">
          <option value="f">°F</option><option value="c">°C</option>
        </select>
        <button type="submit" disabled={busy || !place.trim()}>{busy ? "Looking…" : "Look"}</button>
      </form>
      {err && <p className="op-note">{err}</p>}

      {wx?.ok && (
        <Section title={`Weather · ${wx.place ?? ""}`}>
          <p className="iris-world-now">{wx.now}{wx.local_time ? ` · local time ${wx.local_time.slice(11, 16)}` : ""}</p>
          {!!wx.daily?.length && (
            <div className="iris-world-days">
              {wx.daily.map((d) => (
                <div key={d.date}>
                  <b>{dayName(d.date)}</b>
                  <span>{d.summary}</span>
                  <span>{d.high}° / {d.low}°</span>
                  {d.rain_chance_pct != null && <span className="op-muted">{d.rain_chance_pct}% rain</span>}
                </div>
              ))}
            </div>
          )}
        </Section>
      )}

      {map?.ok && (
        <Section title={`Map · ${(map.place ?? "").split(",").slice(0, 3).join(",")}`}>
          <div className="iris-world-toolbar">
            <button className={view === "satellite" ? "on" : ""} onClick={() => setView("satellite")}>Satellite</button>
            <button className={view === "2d" ? "on" : ""} onClick={() => setView("2d")}>Street map</button>
            <button className={view === "3d" ? "on" : ""} onClick={() => setView("3d")} title="3D buildings (drag to tilt/rotate)">3D city</button>
            <button className={view === "3dsat" ? "on" : ""} onClick={() => setView("3dsat")} title="Satellite imagery on real terrain">3D terrain</button>
            <button onClick={() => void open3d()} title="Opens Google Earth in your browser">Google Earth ↗</button>
            <label>Zoom
              <input type="range" min={10} max={19} value={zoom}
                onChange={(e) => setZoom(Number(e.target.value))}
                onMouseUp={() => void look(zoom)} onKeyUp={() => void look(zoom)} />
              <span>{zoom}</span>
            </label>
          </div>
          {is3d ? (url3d ? <iframe className="iris-world-map" src={url3d} title="3D map" /> : <p className="op-muted">No coordinates for a 3D view.</p>)
            : img ? <img className="iris-world-map" src={`${API_BASE}${img}?t=${Date.now()}`} alt={`${view} map`} />
            : <p className="op-muted">That view didn't load{map.errors?.[view] ? ` (${map.errors[view]})` : ""}.</p>}
          <p className="op-muted">Map data © OpenStreetMap contributors · OpenFreeMap · Imagery © Esri, Vantor, Earthstar Geographics · Terrain © Mapterhorn</p>
        </Section>
      )}
    </div>
  );
}
