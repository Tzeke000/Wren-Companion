// Reminders tab (2026-10-07, Zeke's "#3"). Each reminder is a one-shot Windows scheduled task that DMs him on
// Discord at the time — it survives my restarts and his reboots. GET/POST/DELETE /api/v1/app/reminders*.
import { useCallback, useEffect, useState } from "react";
import { API_BASE, getJson, postJson } from "../api";
import { Section } from "./Ui";

type Rem = { id: string; text: string; due_iso: string; status: string };
const PRESETS = ["in 10m", "in 30m", "in 1h", "in 3h", "tomorrow 8:00"];

const when = (iso: string, relative = true) => {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const mins = Math.round((d.getTime() - Date.now()) / 60000);
  const rel = relative && mins > 0 ? (mins < 90 ? `in ${mins} min` : mins < 2880 ? `in ${Math.round(mins / 60)} h` : `in ${Math.round(mins / 1440)} d`) : "";
  return `${d.toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" })}${rel ? ` · ${rel}` : ""}`;
};

export default function RemindersPanel() {
  const [rows, setRows] = useState<Rem[]>([]);
  const [text, setText] = useState("");
  const [at, setAt] = useState("in 30m");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try { setRows((await getJson<{ reminders: Rem[] }>("/api/v1/app/reminders")).reminders ?? []); }
    catch (e) { setMsg(String(e)); }
  }, []);
  useEffect(() => { void load(); const iv = setInterval(() => void load(), 15000); return () => clearInterval(iv); }, [load]);

  const add = async () => {
    if (!text.trim() || !at.trim() || busy) return;
    setBusy(true); setMsg("");
    try {
      const r = await postJson<{ ok: boolean; due?: string; error?: string }>("/api/v1/app/reminders/add", { text: text.trim(), when: at.trim() });
      if (r.ok) { setText(""); setMsg(`Set for ${when(r.due ?? "")}.`); } else setMsg(r.error ?? "couldn't set it");
      await load();
    } catch (e) { setMsg(String(e)); } finally { setBusy(false); }
  };
  const cancel = async (id: string) => {
    try {
      const res = await fetch(`${API_BASE}/api/v1/app/reminders/${id}`, { method: "DELETE" });
      const j = await res.json(); if (!j.ok) setMsg(j.error ?? "couldn't cancel");
      await load();
    } catch (e) { setMsg(String(e)); }
  };

  const pending = rows.filter((r) => r.status === "pending");
  const past = rows.filter((r) => r.status !== "pending").slice(0, 15);
  return (
    <div className="op-pane">
      <h1 className="op-h1">Reminders</h1>
      <p className="op-lead">I message you on Discord at the time — even if I restarted or the PC rebooted in between.</p>
      <form className="iris-world-search" onSubmit={(e) => { e.preventDefault(); void add(); }}>
        <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Remind you to…" maxLength={300} aria-label="Reminder text" />
        <input value={at} onChange={(e) => setAt(e.target.value)} placeholder="in 20m · at 17:30 · tomorrow 9:00" maxLength={40}
          aria-label="When" className="iris-rem-when" />
        <button type="submit" disabled={busy || !text.trim()}>{busy ? "Setting…" : "Set"}</button>
      </form>
      <div className="iris-world-toolbar">
        {PRESETS.map((p) => <button key={p} className={at === p ? "on" : ""} onClick={() => setAt(p)}>{p}</button>)}
      </div>
      {msg && <p className="op-note">{msg}</p>}
      <Section title={`Coming up (${pending.length})`}>
        {!pending.length && <p className="op-muted">Nothing scheduled.</p>}
        <ul className="iris-rem-list">
          {pending.map((r) => (
            <li key={r.id}><span>{r.text}</span><em>{when(r.due_iso)}</em>
              <button onClick={() => void cancel(r.id)} aria-label={`Cancel ${r.text}`}>Cancel</button></li>
          ))}
        </ul>
      </Section>
      {!!past.length && (
        <Section title="Earlier">
          <ul className="iris-rem-list past">
            {past.map((r) => <li key={r.id}><span>{r.text}</span><em>{r.status} · {when(r.due_iso, false)}</em></li>)}
          </ul>
        </Section>
      )}
    </div>
  );
}
