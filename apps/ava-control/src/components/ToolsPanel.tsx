// Tools tab (2026-10-06). Zeke: "your tool tab says 0 tools" — the old pane read snap.tools.tools_registry,
// which the snapshot never carried. This reads the live registry from GET /api/v1/tools.
import { useEffect, useMemo, useState } from "react";
import { getJson } from "../api";
import { Section } from "./Ui";

type Tool = { name: string; description: string; tier: number };
type Listing = { ok: boolean; count: number; tools: Tool[]; last_tool_used: string; execution_count: number };

const PREFIXES: [string, string][] = [
  ["body_", "Vector body"], ["vector_", "Vector body"], ["attention", "Eyes & head"], ["ptz", "Eyes & head"],
  ["eyes", "Eyes & head"], ["face", "Eyes & head"], ["room_", "Eyes & head"], ["human_pose", "Eyes & head"],
  ["voice", "Voice"], ["memory", "Memory"], ["widget", "Widget"], ["gpu", "System"], ["resilience", "System"],
  ["iris_", "System"], ["shadow", "System"], ["heap", "System"], ["goal", "Mind"], ["belief", "Mind"],
  ["self_", "Mind"], ["wake_", "Mind"], ["act", "Mind"], ["skill", "Mind"], ["adaptive", "Mind"],
];
const groupOf = (n: string) => (PREFIXES.find(([p]) => n.startsWith(p))?.[1] ?? "Other");

export default function ToolsPanel() {
  const [data, setData] = useState<Listing | null>(null);
  const [err, setErr] = useState("");
  const [q, setQ] = useState("");
  useEffect(() => {
    getJson<Listing>("/api/v1/tools").then(setData).catch((e) => setErr(String(e)));
  }, []);
  const groups = useMemo(() => {
    const out = new Map<string, Tool[]>();
    const needle = q.trim().toLowerCase();
    for (const t of data?.tools ?? []) {
      if (needle && !`${t.name} ${t.description}`.toLowerCase().includes(needle)) continue;
      const g = groupOf(t.name);
      out.set(g, [...(out.get(g) ?? []), t]);
    }
    return [...out.entries()].sort((a, b) => (a[0] === "Other" ? 1 : b[0] === "Other" ? -1 : a[0].localeCompare(b[0])));
  }, [data, q]);

  return (
    <div className="op-pane">
      <h1 className="op-h1">Tools</h1>
      <p className="op-lead">
        {data ? `${data.count} tools I can use.` : err ? `Couldn't load the tool list (${err}).` : "Loading…"}
        {data?.last_tool_used ? ` Last used: ${data.last_tool_used}.` : ""}
      </p>
      <input className="iris-search" placeholder="Search tools…" value={q} onChange={(e) => setQ(e.target.value)} />
      {groups.map(([g, tools]) => (
        <Section key={g} title={`${g} (${tools.length})`}>
          <div className="iris-tool-list">
            {tools.map((t) => (
              <div key={t.name} className="iris-tool">
                <div className="iris-tool-name">{t.name}<span className={`iris-tier t${t.tier}`}>tier {t.tier}</span></div>
                <div className="iris-tool-desc">{t.description}</div>
              </div>
            ))}
          </div>
        </Section>
      ))}
    </div>
  );
}
