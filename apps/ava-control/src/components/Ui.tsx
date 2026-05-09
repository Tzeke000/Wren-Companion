/** Small presentational helpers — operator console readability */

import type { ReactNode } from "react";

export function Kv({
  items,
}: {
  items: { label: string; value: unknown }[];
}) {
  return (
    <dl className="kv">
      {items.map(({ label, value }) => (
        <div key={label} className="kv-row">
          <dt>{label}</dt>
          <dd>{value === undefined || value === null || value === "" ? "—" : String(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Section({
  title,
  children,
  muted,
}: {
  title: string;
  children: ReactNode;
  muted?: string;
}) {
  return (
    <section className="section">
      <h3>{title}</h3>
      {muted && <p className="section-muted">{muted}</p>}
      {children}
    </section>
  );
}

export function ExpandPre({ title, body }: { title: string; body: string }) {
  if (!body.trim()) return null;
  return (
    <details className="expand">
      <summary>{title}</summary>
      <pre className="mono">{body}</pre>
    </details>
  );
}

export function JsonBlock({ data, maxHeight = 280 }: { data: unknown; maxHeight?: number }) {
  let s = "";
  try {
    s = JSON.stringify(data, null, 2);
  } catch {
    s = String(data);
  }
  return (
    <pre className="mono dump" style={{ maxHeight }}>
      {s.length > 12000 ? `${s.slice(0, 12000)}\n… (truncated)` : s}
    </pre>
  );
}
