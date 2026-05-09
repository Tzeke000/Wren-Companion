const API_BASE = import.meta.env.VITE_OPERATOR_API || "http://127.0.0.1:5876";

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
