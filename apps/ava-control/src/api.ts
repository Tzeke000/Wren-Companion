// Which me this app talks to (Zeke 2026-10-06): the tower's runtime, or the one on the server
// (iris-home, VM 100) after the cutover. Stored in localStorage so the main window and the widget
// agree; switching RELOADS both windows (sockets + polls rebind cleanly). The Server tab flips it.
export type Backend = "tower" | "server";
export const BACKENDS: Record<Backend, string> = {
  tower: "http://127.0.0.1:5876",
  // From the git-ignored apps/ava-control/.env.local (VITE_IRIS_SERVER_URL) — the repo is public.
  server: import.meta.env.VITE_IRIS_SERVER_URL || "",
};
export const BACKEND_KEY = "iris.backend";
export function readBackend(): Backend {
  try { return localStorage.getItem(BACKEND_KEY) === "server" ? "server" : "tower"; } catch { return "tower"; }
}
export function setBackend(b: Backend): void {
  try { localStorage.setItem(BACKEND_KEY, b); } catch { /* storage unavailable */ }
  location.reload();
}
if (typeof window !== "undefined") {
  window.addEventListener("storage", (e) => { if (e.key === BACKEND_KEY) location.reload(); });
}
const API_BASE: string = import.meta.env.VITE_OPERATOR_API || BACKENDS[readBackend()];

/** Emitted after each completed HTTP API response (before JSON throw on error). */
export type ApiLogEntry = {
  timestamp: string;
  endpoint: string;
  status: number;
  responseBody: string;
};

type ApiLogger = (entry: ApiLogEntry) => void;

let apiLogger: ApiLogger | null = null;

export function registerApiLogger(fn: ApiLogger | null): void {
  apiLogger = fn;
}

function emitLog(method: string, path: string, status: number, rawBody: string): void {
  let pretty = rawBody;
  try {
    pretty = JSON.stringify(JSON.parse(rawBody), null, 2);
  } catch {
    /* plain text or empty */
  }
  apiLogger?.({
    timestamp: new Date().toISOString(),
    endpoint: `${method.toUpperCase()} ${path}`,
    status,
    responseBody: pretty,
  });
}

export async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { method: "GET" });
  const txt = await res.text();
  emitLog("GET", path, res.status, txt);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return JSON.parse(txt) as T;
}

export async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const txt = await res.text();
  emitLog("POST", path, res.status, txt);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return JSON.parse(txt) as T;
}

export async function getText(path: string): Promise<string> {
  const res = await fetch(`${API_BASE}${path}`, { method: "GET" });
  const txt = await res.text();
  emitLog("GET", path, res.status, txt);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return txt;
}

export { API_BASE };
