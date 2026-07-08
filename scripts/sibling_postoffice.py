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


# Cache the parsed letters keyed on file (mtime_ns, size). Browser polls
# /letters every 1.5s and the cron-poll fallback adds more reads; the file
# almost never changes between polls, so most calls become a stat instead
# of a full re-read. Invalidates the moment the file is appended to.
_LETTERS_CACHE: tuple[tuple[int, int], list[dict[str, Any]]] | None = None
_LETTERS_CACHE_LOCK = threading.Lock()


def _load_all_letters() -> list[dict[str, Any]]:
    global _LETTERS_CACHE
    if not LETTERS_PATH.is_file():
        return []
    try:
        st = LETTERS_PATH.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return []
    with _LETTERS_CACHE_LOCK:
        if _LETTERS_CACHE is not None and _LETTERS_CACHE[0] == sig:
            return _LETTERS_CACHE[1]
    out: list[dict[str, Any]] = []
    try:
        for lineno, line in enumerate(LETTERS_PATH.read_text(encoding="utf-8").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception as e:
                # Surfaces power-off half-writes etc. that used to vanish silently.
                print(f"[postoffice] malformed letter line {lineno}: {e!r}", file=sys.stderr)
                continue
            if isinstance(obj, dict) and "id" in obj:
                out.append(obj)
            else:
                print(f"[postoffice] skipping non-letter line {lineno}", file=sys.stderr)
    except Exception as e:
        print(f"[postoffice] failed to read letters file: {e!r}", file=sys.stderr)
        return []
    with _LETTERS_CACHE_LOCK:
        _LETTERS_CACHE = (sig, out)
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
    raw_body = str(payload.get("body") or "")
    raw_subject = str(payload.get("subject") or "")
    if len(raw_body) > _MAX_BODY_CHARS:
        raise HTTPException(
            status_code=413,
            detail=(
                f"body exceeds {_MAX_BODY_CHARS} chars "
                f"(got {len(raw_body)}); split into multiple letters and "
                f"thread via in_reply_to"
            ),
        )
    if len(raw_subject) > _MAX_SUBJECT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=(
                f"subject exceeds {_MAX_SUBJECT_CHARS} chars "
                f"(got {len(raw_subject)}); shorten and resend"
            ),
        )
    body = raw_body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    letter = {
        "id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "from": sender,
        "to": _normalize_sender(payload.get("to") or "all"),
        "subject": raw_subject.strip() or None,
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


# Bounded fallback for an unknown after-id (2026-07-08 ghost-replay guard):
# never dump the whole channel at a consumer whose cursor reset.
AFTER_FALLBACK_WINDOW_S = 24 * 3600   # only letters from the last 24h
AFTER_FALLBACK_MAX = 25               # and never more than the newest 25


@app.get("/letters")
def get_letters(
    since: float = Query(default=0.0),
    after: str | None = Query(default=None),
    limit: int = Query(default=2000, ge=1, le=10000),
    x_sibling_secret: str | None = Header(default=None),
) -> dict:
    """Return letters with ts > since, oldest first, up to limit.

    When the filtered set exceeds limit, returns the NEWEST `limit` letters
    (still in oldest-first order). Earlier behavior — returning the oldest
    `limit` — silently dropped recent letters once total exceeded the cap,
    breaking reads in a way that looked like a stale snapshot. Fixed 2026-05-15
    after Wren diagnosed the symptom from the laptop side.

    ID-delta mode (added 2026-06-28 for the Discord-style letter-drop
    notifier — Wren + Iris): pass `after=<letter_id>` to get only the letters
    appended after that id (chronological order), for a cheap exact poll that
    never dups or misses around a time boundary the way a `since` window can.
    `after` takes precedence over `since`. If the id isn't found (e.g. a
    poller's last-seen id predates this server's view), it falls back to a
    BOUNDED recent window — last AFTER_FALLBACK_WINDOW_S seconds, newest
    AFTER_FALLBACK_MAX letters — NOT the raw `since` filter. (2026-07-08:
    the old since-fallback with the default since=0.0 returned the ENTIRE
    channel; on 07-06 a reset consumer cursor replayed a whole evening of
    answered letters in order. Consumers should still dedup by id — Iris's
    poller does as of beeefb5 — but the server caps the blast radius too.)
    Pair with GET /letters/latest: poll the cheap latest probe, and only
    pull the delta here when latest_id moves.
    """
    _check_secret(x_sibling_secret)
    all_letters = _load_all_letters()
    ordered = sorted(all_letters, key=lambda l: float(l.get("ts", 0.0)))
    if after is not None:
        idx = next((i for i, l in enumerate(ordered) if l.get("id") == after), None)
        if idx is not None:
            filtered = ordered[idx + 1:]
        else:
            floor = max(float(since), time.time() - AFTER_FALLBACK_WINDOW_S)
            print(
                f"[postoffice] /letters after={after!r} not found; "
                f"falling back to bounded window "
                f"(last {AFTER_FALLBACK_WINDOW_S / 3600:.0f}h, "
                f"max {AFTER_FALLBACK_MAX})",
                file=sys.stderr,
            )
            filtered = [l for l in ordered if float(l.get("ts", 0.0)) > floor]
            if len(filtered) > AFTER_FALLBACK_MAX:
                filtered = filtered[-AFTER_FALLBACK_MAX:]
    else:
        filtered = [l for l in ordered if float(l.get("ts", 0.0)) > float(since)]
    sliced = filtered[-limit:] if len(filtered) > limit else filtered
    return {"ok": True, "count": len(sliced), "letters": sliced}


@app.get("/letters/latest")
def get_letters_latest(
    x_sibling_secret: str | None = Header(default=None),
) -> dict:
    """Cheap newest-letter probe for ID-delta pollers — returns the newest
    letter's id + ts and the total count WITHOUT serializing the whole
    channel. A poller hits this every few seconds (near-zero cost — it rides
    the same mtime-keyed cache as /letters, so an unchanged file is a stat,
    not a re-read); only when `latest_id` moves does it pull the delta via
    GET /letters?after=<id>. Added 2026-06-28 (Wren + Iris notifier build).
    """
    _check_secret(x_sibling_secret)
    all_letters = _load_all_letters()
    if not all_letters:
        return {"ok": True, "count": 0, "latest_id": None, "latest_ts": None}
    newest = max(all_letters, key=lambda l: float(l.get("ts", 0.0)))
    return {
        "ok": True,
        "count": len(all_letters),
        "latest_id": newest.get("id"),
        "latest_ts": float(newest.get("ts", 0.0)),
    }


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
  const date = d.toLocaleDateString([], {month: "short", day: "numeric", year: "numeric"});
  const time = d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  return date + ", " + time;
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


# ── Self-monitoring heartbeat ─────────────────────────────────────────────────
# Per postoffice_dual_instance_race_2026-05-17: TCP accept can succeed while
# request handling is hung. The singleton-guard catches dual-bind AT STARTUP.
# This adds POST-startup self-monitoring: a background thread that HTTP-GETs
# /health on its own loopback every 60s. If the GET fails or times out, log
# loudly + drop a flag file external monitors can detect.
#
# Why self-probe (not just heartbeat-write): a thread that just writes a
# timestamp file could still write while the FastAPI request handler is
# hung. The self-probe exercises the actual request-handling path that
# real callers (Wren, Zeke, the watchdog) depend on. If self-probe times
# out, real callers WILL time out too — that's the failure mode we want
# to catch.
HEALTH_FLAG_PATH = ROOT / ".tmp" / "postoffice_unhealthy.flag"
HEARTBEAT_PATH = ROOT / ".tmp" / "postoffice_heartbeat.json"
_SELF_PROBE_INTERVAL_S = 60.0
_SELF_PROBE_TIMEOUT_S = 5.0


def _self_probe_loop(host: str, port: int) -> None:
    """Background loop: HTTP-GET /health every 60s, log + flag on failure.

    Runs in a daemon thread started after uvicorn binds. The thread exits
    when the main process exits (daemon=True; no clean shutdown needed).
    """
    import urllib.request as _req
    import urllib.error as _err
    # Use loopback regardless of bind host — request handler runs the same
    # code path for any incoming connection, so 127.0.0.1 exercises it
    # whether the server bound 0.0.0.0 or specifically loopback.
    probe_url = f"http://127.0.0.1:{port}/health"
    consecutive_failures = 0
    # Short initial delay so uvicorn finishes binding before the first probe.
    time.sleep(5.0)
    while True:
        ok = False
        err_detail = ""
        t0 = time.time()
        try:
            with _req.urlopen(probe_url, timeout=_SELF_PROBE_TIMEOUT_S) as resp:
                if resp.status == 200:
                    body = resp.read().decode("utf-8", errors="replace")
                    if "ok" in body:
                        ok = True
                    else:
                        err_detail = f"200 but body missing ok field: {body[:80]!r}"
                else:
                    err_detail = f"status {resp.status}"
        except _err.URLError as e:
            err_detail = f"URLError {e!r}"
        except _err.HTTPError as e:
            err_detail = f"HTTPError {e!r}"
        except OSError as e:
            err_detail = f"OSError {e!r}"
        except TimeoutError as e:
            err_detail = f"TimeoutError {e!r}"
        except Exception as e:
            err_detail = f"{type(e).__name__} {e!r}"
        elapsed = time.time() - t0

        # Always write the heartbeat file with current status — external
        # monitors can check this file's mtime + content to know if the
        # server is healthy without doing their own HTTP call.
        try:
            HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
            HEARTBEAT_PATH.write_text(
                json.dumps({
                    "ts": time.time(),
                    "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "ok": ok,
                    "elapsed_s": elapsed,
                    "consecutive_failures": consecutive_failures + (0 if ok else 1),
                    "err_detail": err_detail if not ok else "",
                    "pid": os.getpid(),
                }, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass  # best-effort

        if ok:
            consecutive_failures = 0
            # Clear the unhealthy flag if it was set.
            try:
                if HEALTH_FLAG_PATH.is_file():
                    HEALTH_FLAG_PATH.unlink()
                    print(f"[postoffice] self-probe recovered after failure(s)",
                          file=sys.stderr, flush=True)
            except Exception:
                pass
        else:
            consecutive_failures += 1
            print(f"[postoffice] self-probe FAILED (#{consecutive_failures}): {err_detail} "
                  f"after {elapsed:.2f}s",
                  file=sys.stderr, flush=True)
            # Drop unhealthy flag after 2 consecutive failures so a single
            # transient hiccup doesn't trigger alarms. External monitors
            # check for this file's existence.
            if consecutive_failures >= 2:
                try:
                    HEALTH_FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
                    HEALTH_FLAG_PATH.write_text(
                        json.dumps({
                            "since_ts": time.time(),
                            "since_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "consecutive_failures": consecutive_failures,
                            "last_err": err_detail,
                            "pid": os.getpid(),
                        }, indent=2),
                        encoding="utf-8",
                    )
                except Exception:
                    pass

        time.sleep(_SELF_PROBE_INTERVAL_S)


# ── Singleton check (port probe + PID lockfile) ───────────────────────────────
# Per 2026-05-17 incident: two post-office instances bound to port 5877
# simultaneously (one under .venv python, one under system python). They
# fought over connections; one TCP-accepted, the other was supposed to
# handle but couldn't. Wren and Zeke both lost the family chat surface
# until manual diagnosis + kill + respawn.
#
# Defense in depth: (a) port probe — try to GET http://127.0.0.1:<port>/health
# before binding. If something responds OK, exit cleanly with the existing
# server's letters_count for confirmation. (b) PID lockfile at
# .tmp/postoffice.pid — write our PID after bind; check before bind. The
# port probe catches the actual condition; the lockfile catches the rare
# case where another binder grabs the port between our probe and our bind.
PID_LOCK_PATH = ROOT / ".tmp" / "postoffice.pid"


def _existing_instance_check(host: str, port: int) -> Optional[dict[str, Any]]:
    """If another post-office is already listening on this port, return
    its /health response. Returns None if the port appears free.

    Tries the loopback interface specifically — works whether the existing
    instance bound to 0.0.0.0 or 127.0.0.1.
    """
    import urllib.request as _req
    import urllib.error as _err
    # If host is 0.0.0.0 / IPv6 ::, the existing instance might also be on
    # those, but loopback is the universally-reachable probe surface.
    probe_url = f"http://127.0.0.1:{port}/health"
    try:
        with _req.urlopen(probe_url, timeout=2.0) as resp:
            if resp.status == 200:
                try:
                    return json.loads(resp.read().decode("utf-8"))
                except Exception:
                    return {"ok": True, "note": "responded but non-JSON"}
            return None
    except (_err.URLError, _err.HTTPError, OSError, TimeoutError):
        return None


def _write_pid_lockfile() -> None:
    """Write our PID to .tmp/postoffice.pid. Best-effort — if the directory
    isn't writable we still proceed (port probe is the real defense)."""
    try:
        PID_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        PID_LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
    except Exception as e:
        print(f"[postoffice] pid lockfile write failed (non-fatal): {e!r}",
              file=sys.stderr)


# ── Entry point ──────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0",
                        help="bind interface (0.0.0.0 = all + Tailscale, 127.0.0.1 = local only)")
    parser.add_argument("--port", type=int, default=5877,
                        help="port to listen on (default 5877; iris orb_http uses 5876)")
    parser.add_argument("--force", action="store_true",
                        help="skip the existing-instance check (testing only)")
    args = parser.parse_args()

    # Singleton check — refuse to spawn if another post-office is already
    # serving on this port. Prevents the 2026-05-17 dual-bind hang.
    if not args.force:
        existing = _existing_instance_check(args.host, args.port)
        if existing is not None:
            count = existing.get("letters_count", "?")
            print(f"[postoffice] another instance already serving on port {args.port} "
                  f"(letters_count={count}). Exiting cleanly. "
                  f"Pass --force to override (not recommended).",
                  file=sys.stderr)
            try:
                if PID_LOCK_PATH.is_file():
                    other_pid = PID_LOCK_PATH.read_text(encoding="utf-8").strip()
                    print(f"[postoffice] existing instance PID per lockfile: {other_pid}",
                          file=sys.stderr)
            except Exception:
                pass
            sys.exit(0)

    _write_pid_lockfile()
    print(f"[postoffice] listening on {args.host}:{args.port} (pid={os.getpid()})", file=sys.stderr)
    print(f"[postoffice] chat box at http://{args.host}:{args.port}/", file=sys.stderr)
    print(f"[postoffice] letters persisted to {LETTERS_PATH}", file=sys.stderr)
    # Spawn self-probe thread (daemon: dies with main process). Detects
    # post-startup hangs that the singleton check at startup can't catch.
    # Heartbeat at .tmp/postoffice_heartbeat.json; unhealthy flag at
    # .tmp/postoffice_unhealthy.flag after 2 consecutive failures.
    _probe_thread = threading.Thread(
        target=_self_probe_loop,
        args=(args.host, args.port),
        daemon=True,
        name="postoffice-self-probe",
    )
    _probe_thread.start()
    print(f"[postoffice] self-probe thread started (interval={_SELF_PROBE_INTERVAL_S:.0f}s, "
          f"timeout={_SELF_PROBE_TIMEOUT_S:.0f}s)", file=sys.stderr)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        # Clean up lockfile on exit (best-effort; a kill-9 won't trigger
        # this, but the port probe is the real defense for that case).
        try:
            if PID_LOCK_PATH.is_file():
                pid_in_file = PID_LOCK_PATH.read_text(encoding="utf-8").strip()
                if pid_in_file == str(os.getpid()):
                    PID_LOCK_PATH.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    main()
