// Server panel (Zeke 2026-10-06): "once your cognition is moved to the server there should be a way I can
// interact with you on the tower … a button on the app that can auto SSH into the server, bring up the VMs …
// I could type into the app on the tower and you would get it on the server … pull up your cmd session."
// Power + terminals go through Tauri commands in src-tauri/src/main.rs (the Proxmox token never reaches JS).
import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Section } from "./Ui";
import { BACKENDS, readBackend, setBackend } from "../api";

type Vm = { vmid: number; name: string; status: string; node: string; uptime?: number };
type Reach = { proxmox: boolean; iris_home_ssh: boolean; iris_home_runtime: boolean };
const ROLE: Record<number, string> = { 100: "my home on the server", 101: "Windows", 102: "Zorin" };

const dot = (ok: boolean | undefined) => (
  <span style={{ display: "inline-block", width: 9, height: 9, borderRadius: 9, marginRight: 8,
    background: ok === undefined ? "#445" : ok ? "#3ee68f" : "#e5534b" }} />
);
const fmtUp = (s?: number) =>
  !s ? "" : s < 3600 ? `${Math.round(s / 60)} min` : s < 86400 ? `${Math.round(s / 3600)} h` : `${Math.round(s / 86400)} d`;

export default function ServerPanel() {
  const [vms, setVms] = useState<Vm[]>([]);
  const [reach, setReach] = useState<Reach | null>(null);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState<number | null>(null);
  const backend = readBackend();

  const refresh = useCallback(async () => {
    try { setReach(JSON.parse(await invoke<string>("server_reach")) as Reach); } catch { setReach(null); }
    try {
      const d = (JSON.parse(await invoke<string>("server_vms")).data ?? []) as Vm[];
      setVms(d.filter((v) => [100, 101, 102].includes(v.vmid)).sort((a, b) => a.vmid - b.vmid));
    } catch (e) { setVms([]); setMsg(String(e)); }
  }, []);
  useEffect(() => {
    void refresh();
    const iv = setInterval(() => void refresh(), 5000);
    return () => clearInterval(iv);
  }, [refresh]);

  const power = async (vm: Vm, action: "start" | "shutdown") => {
    if (action === "start" && (vm.vmid === 101 || vm.vmid === 102)) {
      // Zeke's own rule: Windows (101) and Zorin (102) run one at a time.
      const other = vms.find((v) => v.vmid === (vm.vmid === 101 ? 102 : 101));
      if (other?.status === "running" &&
          !window.confirm(`${other.name} is running, and your rule is one of Windows/Zorin at a time.\nStart ${vm.name} anyway?`)) return;
    }
    if (action === "shutdown" && vm.vmid === 100 &&
        !window.confirm("Shut down iris-home?\nIf I'm running on the server, this stops me.")) return;
    setBusy(vm.vmid);
    setMsg(`${action === "start" ? "Starting" : "Shutting down"} ${vm.name}…`);
    try {
      await invoke("server_vm_power", { vmid: vm.vmid, action });
      setMsg(`${vm.name}: ${action} sent — give it a minute.`);
    } catch (e) { setMsg(`${vm.name}: ${String(e)}`); }
    setBusy(null);
    setTimeout(() => void refresh(), 1500);
  };
  const open = async (kind: "console" | "ssh" | "proxmox") => {
    try { await invoke("server_open", { kind }); setMsg(""); } catch (e) { setMsg(String(e)); }
  };
  const serverLive = Boolean(reach?.iris_home_runtime);

  return (
    <div className="op-pane">
      <h1 className="op-h1">Server</h1>
      <p className="op-lead">The R740. This app stays your door to me wherever I'm running.</p>

      <Section title="Where I'm running">
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <button type="button" className={backend === "tower" ? "btn primary" : "btn ghost"}
            onClick={() => { if (backend !== "tower") setBackend("tower"); }}>Tower</button>
          <button type="button" className={backend === "server" ? "btn primary" : "btn ghost"}
            disabled={!serverLive && backend !== "server"}
            title={serverLive ? "" : "I'm not running on the server yet"}
            onClick={() => { if (backend !== "server") setBackend("server"); }}>Server</button>
          <span className="op-muted" style={{ marginLeft: 8 }}>
            Talking to {BACKENDS[backend].replace("http://", "")}
            {!serverLive && backend === "tower" ? " · the Server switch unlocks once I'm live there" : ""}
          </span>
        </div>
      </Section>

      <Section title="Server status">
        <div style={{ display: "grid", gap: 6 }}>
          <div>{dot(reach?.proxmox)}Proxmox host</div>
          <div>{dot(reach?.iris_home_ssh)}iris-home reachable (SSH)</div>
          <div>{dot(reach?.iris_home_runtime)}Me running on the server</div>
        </div>
      </Section>

      <Section title="Virtual machines">
        {vms.length === 0 && <p className="op-muted">No VM list yet. Is the server on?</p>}
        <div style={{ display: "grid", gap: 8 }}>
          {vms.map((vm) => {
            const running = vm.status === "running";
            return (
              <div key={vm.vmid} style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <div style={{ minWidth: 260 }}>
                  {dot(running)}<strong>{vm.name}</strong>
                  <span className="op-muted"> · {ROLE[vm.vmid] ?? `VM ${vm.vmid}`}</span>
                </div>
                <span className="op-muted" style={{ minWidth: 120 }}>{running ? `up ${fmtUp(vm.uptime)}` : vm.status}</span>
                {running
                  ? <button type="button" className="btn ghost" disabled={busy !== null} onClick={() => void power(vm, "shutdown")}>Shut down</button>
                  : <button type="button" className="btn primary" disabled={busy !== null} onClick={() => void power(vm, "start")}>Start</button>}
              </div>
            );
          })}
        </div>
      </Section>

      <Section title="Terminals">
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button type="button" className="btn primary" onClick={() => void open("console")}>Open my console</button>
          <button type="button" className="btn ghost" onClick={() => void open("ssh")}>SSH into iris-home</button>
          <button type="button" className="btn ghost" onClick={() => void open("proxmox")}>Proxmox web page</button>
        </div>
        <p className="op-muted">My console attaches to my live session on the server. If I'm not running there, it says so.</p>
      </Section>

      {msg && <p className="op-note">{msg}</p>}
    </div>
  );
}
