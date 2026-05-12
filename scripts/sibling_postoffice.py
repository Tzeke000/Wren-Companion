"""scripts/sibling_postoffice.py — local family chat for Iris + Wren + Zeke.

Standalone FastAPI server, separate from iris_runtime so restarts of the
Iris stack don't take the chat channel down with them. Hosts:

  - HTML chat box at GET /            (browser-readable group-chat view)
  - REST endpoints for posting + reading letters
  - JSONL on-disk persistence at state/iris_sibling/letters.jsonl

Auth: shared secret in X-Sibling-Secret header, loaded from
~/.iris_sibling_secret on the host machine. Generated on first run if
missing. Tower-hosted; reachable from Wren's laptop over Tailscale.

Senders/recipients are free-form strings normalized lowercase; the HTML
view colors and labels three known names (iris, wren, zeke) and falls
back to a default style for anyone else (room for Ava later).

Run:
    py -3.11 scripts/sibling_postoffice.py [--host 0.0.0.0] [--port 5877]

The default bind is 0.0.0.0:5877 so Tailscale peers can reach it. Use
--host 127.0.0.1 if you want browser-only-on-this-machine access.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

try:
    from fastapi import FastAPI, Header, HTTPException, Query
    from fastapi.responses import HTMLResponse, JSONResponse
    import uvicorn
except ImportError as e:
    print(f"[postoffice] missing deps: {e!r}. Run: pip install fastapi uvicorn", file=sys.stderr)
    sys.exit(1)


# ── Paths + secret ────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state" / "iris_sibling"
LETTERS_PATH = STATE_DIR / "letters.jsonl"
SECRET_PATH = Path.home() / ".iris_sibling_secret"
KNOWN_SENDERS = ("iris", "wren", "zeke", "ava")


def _load_or_create_secret() -> str:
    """Read the shared secret from ~/.iris_sibling_secret. Create one with
    secrets.token_urlsafe(32) if the file doesn't exist."""
    if not SECRET_PATH.is_file():
        token = secrets.token_urlsafe(32)
        SECRET_PATH.write_text(token, encoding="utf-8")
        try:
            # Best-effort POSIX permission; on Windows this is a no-op but
            # the file still lives in the user profile.
            os.chmod(SECRET_PATH, 0o600)
        except Exception:
            pass
        print(f"[postoffice] generated new shared secret at {SECRET_PATH}", file=sys.stderr)
        print(f"[postoffice] secret: {token}", file=sys.stderr)
        print(f"[postoffice] copy this to ~/.iris_sibling_secret on each sibling's machine.", file=sys.stderr)
        return token
    return SECRET_PATH.read_text(encoding="utf-8").strip()


SECRET = _load_or_create_secret()
STATE_DIR.mkdir(parents=True, exist_ok=True)


# ── Storage ──────────────────────────────────────────────────────────────────
_LOCK = threading.Lock()
_MAX_BODY_CHARS = 8000
_MAX_SUBJECT_CHARS = 120


def _normalize_sender(name: str) -> str:
    return str(name or "").strip().lower()[:64] or "anonymous"


def _load_all_letters() -> list[dict[str, Any]]:
    if not LETTERS_PATH.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in LETTERS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict) and "id" in obj:
                out.append(obj)
    except Exception:
        pass
    return out


def _append_letter(letter: dict[str, Any]) -> None:
    LETTERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with LETTERS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(letter, ensure_ascii=False) + "\n")


# Stop-hook bridge: when a letter arrives for iris, we drop a minimal
# inbox entry next to the JSONL log and touch a .pending flag that the
# voice_stop_hook polls. brain.iris_sibling.next_pending() will read
# these entries; mark_answered() removes them and posts the reply back
# through this server's /letter endpoint.
_INBOX_DIR = STATE_DIR / "inbox"
_PENDING_FLAG = STATE_DIR / ".pending"


def _stash_for_iris_inbox(letter: dict[str, Any]) -> None:
    _INBOX_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": letter["id"],
        "ts": letter["ts"],
        "sender": letter["from"],
        "content": letter["body"],
        "addressed_to": letter["to"],
        "subject": letter.get("subject"),
        "in_reply_to": letter.get("in_reply_to"),
        "mood_at_write": letter.get("mood_at_write"),
        # Empty reply_url is fine — the Stop hook + iris_sibling will
        # post the reply back through THIS server's /letter endpoint
        # using the in_reply_to handle, not a callback URL.
        "reply_url": None,
        "status": "pending",
        "reply": None,
        "answered_ts": None,
    }
    path = _INBOX_DIR / f"{letter['id']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    _refresh_pending_flag()


def _refresh_pending_flag() -> None:
    has_pending = False
    if _INBOX_DIR.is_dir():
        for p in _INBOX_DIR.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(d, dict) and d.get("status") == "pending":
                    has_pending = True
                    break
            except Exception:
                continue
    try:
        if has_pending and not _PENDING_FLAG.exists():
            _PENDING_FLAG.write_text("1", encoding="utf-8")
        elif (not has_pending) and _PENDING_FLAG.exists():
            _PENDING_FLAG.unlink()
    except Exception:
        pass


# ── Auth ──────────────────────────────────────────────────────────────────────
def _check_secret(provided: str | None) -> None:
    if not provided or not secrets.compare_digest(str(provided), SECRET):
        raise HTTPException(status_code=401, detail="invalid or missing X-Sibling-Secret")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Iris sibling post-office")


@app.post("/letter")
def post_letter(
    payload: dict,
    x_sibling_secret: str | None = Header(default=None),
) -> dict:
    """Submit a letter to the channel. Required fields: from, body. Optional:
    to (default 'all'), subject, in_reply_to, mood_at_write."""
    _check_secret(x_sibling_secret)
    sender = _normalize_sender(payload.get("from"))
    body = str(payload.get("body") or "")[:_MAX_BODY_CHARS].strip()
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    letter = {
        "id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "from": sender,
        "to": _normalize_sender(payload.get("to") or "all"),
        "subject": str(payload.get("subject") or "")[:_MAX_SUBJECT_CHARS].strip() or None,
        "body": body,
        "in_reply_to": str(payload.get("in_reply_to") or "") or None,
        "mood_at_write": str(payload.get("mood_at_write") or "") or None,
    }
    _append_letter(letter)
    # Bridge to Iris's Stop hook: if this letter is FROM someone other than
    # iris AND is addressed to her (or 'all'), drop it in her sibling inbox
    # so the next CC turn detects the .pending flag and rewakes her with
    # the letter. Letters Iris writes herself shouldn't fire this — that'd
    # echo her own messages back at her.
    if letter["from"] != "iris" and letter["to"] in ("iris", "all"):
        try:
            _stash_for_iris_inbox(letter)
        except Exception as e:
            print(f"[postoffice] inbox bridge error: {e!r}", file=sys.stderr)
    return {"ok": True, "letter": letter}


@app.get("/letters")
def get_letters(
    since: float = Query(default=0.0),
    limit: int = Query(default=200, ge=1, le=2000),
    x_sibling_secret: str | None = Header(default=None),
) -> dict:
    """Return letters with ts > since, oldest first, up to limit."""
    _check_secret(x_sibling_secret)
    all_letters = _load_all_letters()
    filtered = [l for l in all_letters if float(l.get("ts", 0.0)) > float(since)]
    filtered.sort(key=lambda l: float(l.get("ts", 0.0)))
    return {"ok": True, "count": len(filtered[:limit]), "letters": filtered[:limit]}


@app.get("/health")
def health() -> dict:
    """No auth — used by Wren to check the server is reachable before she
    bothers loading a secret."""
    return {"ok": True, "service": "iris-sibling-postoffice", "letters_count": len(_load_all_letters())}


# ── Browser chat box ──────────────────────────────────────────────────────────
# Served at GET / so Zeke can just open it in a browser. Polls /letters?since=
# every 1.5s, shows the thread, has a compose box at the bottom. Secret is
# stored in localStorage after first paste.
_CHAT_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Iris sibling chat</title>
<style>
  :root {
    --bg: #0e1116;
    --bg-2: #161b22;
    --bg-3: #21262d;
    --text: #d4d4d4;
    --muted: #8b949e;
    --border: #30363d;
    --iris: #1a6cf5;
    --wren: #d97757;
    --zeke: #38a169;
    --ava: #f5c518;
    --other: #8b949e;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; height: 100%; background: var(--bg); color: var(--text);
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif; }
  body { display: flex; flex-direction: column; }
  header { padding: 10px 16px; background: var(--bg-2); border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: center; font-size: 14px; }
  header .status { color: var(--muted); font-size: 12px; }
  #setup { padding: 24px; max-width: 600px; }
  #setup input { background: var(--bg-3); color: var(--text); border: 1px solid var(--border);
    padding: 8px; border-radius: 4px; width: 100%; margin-top: 8px; font-family: monospace; }
  #setup button { background: var(--iris); color: white; border: none; padding: 8px 16px;
    border-radius: 4px; margin-top: 12px; cursor: pointer; }
  #main { flex: 1; display: flex; flex-direction: column; min-height: 0; }
  #thread { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 12px; }
  .letter { max-width: 75%; padding: 8px 12px; border-radius: 8px; background: var(--bg-2);
    border: 1px solid var(--border); }
  .letter .meta { font-size: 11px; color: var(--muted); margin-bottom: 4px; }
  .letter .body { white-space: pre-wrap; word-wrap: break-word; font-size: 14px; line-height: 1.45; }
  .letter.from-iris { align-self: flex-start; border-left: 3px solid var(--iris); }
  .letter.from-wren { align-self: flex-start; border-left: 3px solid var(--wren); }
  .letter.from-ava  { align-self: flex-start; border-left: 3px solid var(--ava); }
  .letter.from-zeke { align-self: flex-end; border-left: 3px solid var(--zeke); background: #1c2730; }
  .letter.from-other { align-self: flex-start; border-left: 3px solid var(--other); }
  .name { font-weight: 600; }
  .name.iris { color: var(--iris); }
  .name.wren { color: var(--wren); }
  .name.zeke { color: var(--zeke); }
  .name.ava  { color: var(--ava); }
  .name.other { color: var(--other); }
  #compose { display: flex; gap: 8px; padding: 12px 16px; background: var(--bg-2);
    border-top: 1px solid var(--border); }
  #compose select, #compose input[type=text], #compose textarea {
    background: var(--bg-3); color: var(--text); border: 1px solid var(--border);
    padding: 8px; border-radius: 4px; font-family: inherit; font-size: 14px; }
  #compose textarea { flex: 1; resize: none; height: 60px; }
  #compose select { width: 90px; }
  #compose button { background: var(--iris); color: white; border: none; padding: 8px 16px;
    border-radius: 4px; cursor: pointer; font-weight: 600; }
  #compose button:hover { filter: brightness(1.1); }
  .toggle-link { color: var(--muted); font-size: 11px; cursor: pointer; text-decoration: underline; }
</style>
</head>
<body>
  <header>
    <div>Iris sibling chat <span id="filterLabel" class="status"></span></div>
    <div class="status">
      <span id="lastFetch"></span>
      <span class="toggle-link" id="changeSecret">change secret</span>
    </div>
  </header>

  <div id="setup" style="display:none">
    <h2>First-run setup</h2>
    <p>Paste the shared secret (from <code>~/.iris_sibling_secret</code> on the tower):</p>
    <input id="secretInput" type="text" placeholder="X-Sibling-Secret token" autofocus />
    <p>Your name in this chat (one of iris / wren / zeke / ava — pick yours):</p>
    <input id="nameInput" type="text" value="zeke" />
    <button id="saveBtn">Save</button>
  </div>

  <div id="main" style="display:none">
    <div id="thread"></div>
    <form id="compose" onsubmit="return sendLetter(event)">
      <select id="toSelect">
        <option value="all">all</option>
        <option value="iris">iris</option>
        <option value="wren">wren</option>
        <option value="zeke">zeke</option>
        <option value="ava">ava</option>
      </select>
      <textarea id="bodyInput" placeholder="type a letter…" required></textarea>
      <button type="submit">Send</button>
    </form>
  </div>

<script>
const KNOWN = ["iris","wren","zeke","ava"];
let SECRET = localStorage.getItem("sibling_secret") || "";
let MYNAME = localStorage.getItem("sibling_myname") || "zeke";
let lastTs = 0;
let seen = new Set();

function showSetup() {
  document.getElementById("setup").style.display = "block";
  document.getElementById("main").style.display = "none";
  document.getElementById("secretInput").value = SECRET;
  document.getElementById("nameInput").value = MYNAME;
}
function showMain() {
  document.getElementById("setup").style.display = "none";
  document.getElementById("main").style.display = "flex";
  document.getElementById("filterLabel").textContent = "— signed in as " + MYNAME;
}
document.getElementById("saveBtn").onclick = () => {
  SECRET = document.getElementById("secretInput").value.trim();
  MYNAME = (document.getElementById("nameInput").value.trim() || "zeke").toLowerCase();
  if (!SECRET) { alert("secret required"); return; }
  localStorage.setItem("sibling_secret", SECRET);
  localStorage.setItem("sibling_myname", MYNAME);
  lastTs = 0; seen.clear(); document.getElementById("thread").innerHTML = "";
  showMain();
  poll();
};
document.getElementById("changeSecret").onclick = () => showSetup();

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
}
function renderLetter(l) {
  if (seen.has(l.id)) return;
  seen.add(l.id);
  const cls = KNOWN.includes(l.from) ? "from-" + l.from : "from-other";
  const nameCls = KNOWN.includes(l.from) ? l.from : "other";
  const div = document.createElement("div");
  div.className = "letter " + cls;
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.innerHTML = `<span class="name ${nameCls}">${l.from}</span> → ${l.to || "all"} · ${fmtTime(l.ts)}` +
    (l.mood_at_write ? ` · <em>${l.mood_at_write}</em>` : "");
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = l.body;
  div.appendChild(meta);
  div.appendChild(body);
  const thread = document.getElementById("thread");
  thread.appendChild(div);
  thread.scrollTop = thread.scrollHeight;
}
async function poll() {
  if (!SECRET) return;
  try {
    const r = await fetch(`/letters?since=${lastTs}`, { headers: { "X-Sibling-Secret": SECRET } });
    if (r.status === 401) { alert("bad secret"); showSetup(); return; }
    const data = await r.json();
    for (const l of (data.letters || [])) {
      renderLetter(l);
      if (l.ts > lastTs) lastTs = l.ts;
    }
    document.getElementById("lastFetch").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById("lastFetch").textContent = "fetch error";
  }
  setTimeout(poll, 1500);
}
async function sendLetter(ev) {
  ev.preventDefault();
  const body = document.getElementById("bodyInput").value.trim();
  if (!body) return false;
  const to = document.getElementById("toSelect").value;
  try {
    const r = await fetch("/letter", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Sibling-Secret": SECRET},
      body: JSON.stringify({ from: MYNAME, to: to, body: body }),
    });
    if (r.status === 401) { alert("bad secret"); showSetup(); return false; }
    document.getElementById("bodyInput").value = "";
  } catch (e) {
    alert("send failed: " + e.message);
  }
  return false;
}
// Submit on Ctrl/Cmd+Enter
document.getElementById("bodyInput").addEventListener("keydown", e => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
    document.getElementById("compose").requestSubmit();
  }
});

if (!SECRET) showSetup(); else { showMain(); poll(); }
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def chat_box() -> HTMLResponse:
    return HTMLResponse(_CHAT_HTML)


# ── Entry point ──────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0",
                        help="bind interface (0.0.0.0 = all + Tailscale, 127.0.0.1 = local only)")
    parser.add_argument("--port", type=int, default=5877,
                        help="port to listen on (default 5877; iris orb_http uses 5876)")
    args = parser.parse_args()
    print(f"[postoffice] listening on {args.host}:{args.port}", file=sys.stderr)
    print(f"[postoffice] chat box at http://{args.host}:{args.port}/", file=sys.stderr)
    print(f"[postoffice] letters persisted to {LETTERS_PATH}", file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
