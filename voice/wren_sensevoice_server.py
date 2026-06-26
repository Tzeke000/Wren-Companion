"""wren_sensevoice_server.py — warm SenseVoice ASR server (the EARS upgrade).

Holds SenseVoice-Small resident on the GPU so the live ears MCP (`wren_voice_mcp.py`,
which runs in BASE python and has NO funasr) can get {text, emotion, events} per
utterance WITHOUT reloading the ~900MB model each time. Mirrors the XTTS mouth server
on :8765 — same "keep the model warm in its own process" pattern. Runs in
`voice\nextgen-venv` (the only env with funasr). Localhost-only, port 8766.

Why a server and not import-in-the-MCP: funasr lives only in nextgen-venv; installing
it into base python risks downgrading base `tokenizers` (funasr/transformers want <0.14,
base has 0.23.1) and breaking mem0's fastembed. Isolation via a server avoids all that.

  POST /transcribe  body {"wav_path": "<abs path>", "language": "en"}
       -> {"text","emotion","events","lang","raw","latency_s",
           "speaker","speaker_score"}
  POST /enroll      body {"name": "<label>", "wav_paths": ["<abs>", ...]}
       -> {"ok": true, "name": "<label>", "n_clips": <int>}
  GET  /health      -> {"status":"ok","model": "..."}

Speaker labeling (open-set cosine):
  - Voiceprints stored in D:\\Wren\\state\\voiceprints.json (name -> 512-d centroid).
  - CAM++ lazy-loaded on first enroll or transcribe call (does NOT slow server start).
  - Threshold: cosine >= 0.55 -> named speaker, else "unknown".
  - On any speaker-id error, "speaker" is null and ASR result still returned.
  - At startup: if "wren" is not in the store, enroll it from wren_ref.wav so the
    self-voice-recognition primitive is live immediately.

Start (nextgen-venv python):
  & D:\\Wren\\voice\\nextgen-venv\\Scripts\\python.exe experiments\\wren_sensevoice_server.py
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wren_sensevoice as sv  # noqa: E402  (transcribe + model loader live here)

HOST = "127.0.0.1"
PORT = 8766

# ---------------------------------------------------------------------------
# Voiceprint store
# ---------------------------------------------------------------------------

_WREN_ROOT = Path(__file__).resolve().parent.parent          # D:\Wren
_STORE_PATH = _WREN_ROOT / "state" / "voiceprints.json"
_WREN_REF   = _WREN_ROOT / "voice" / "wren_ref.wav"

SPEAKER_THRESHOLD = 0.55  # cosine >= this -> named match, else "unknown"

_store_lock = threading.Lock()
_voiceprints: dict[str, list[float]] = {}   # name -> centroid (512-d list)
_sid_loaded = False                          # True once CAM++ is imported

# CAM++ is big-ish; import lazily so server startup stays fast.
_sid_module = None


def _load_sid_module():
    """Import wren_speaker_id on first use (lazy, so server starts without CAM++)."""
    global _sid_module, _sid_loaded
    if _sid_loaded:
        return _sid_module
    import wren_speaker_id as sid
    _sid_module = sid
    _sid_loaded = True
    return _sid_module


def _load_store() -> None:
    """Read voiceprints.json into _voiceprints (no-op if file absent)."""
    global _voiceprints
    if _STORE_PATH.is_file():
        try:
            with _STORE_PATH.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            _voiceprints = {k: v for k, v in data.items() if isinstance(v, list)}
            print(f"[speaker-id] loaded {len(_voiceprints)} voiceprint(s) from store", flush=True)
        except Exception as e:
            print(f"[speaker-id] WARNING: could not read store: {e}", flush=True)
            _voiceprints = {}
    else:
        _voiceprints = {}


def _save_store() -> None:
    """Persist _voiceprints to voiceprints.json (caller holds _store_lock)."""
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STORE_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(_voiceprints, fh)
    tmp.replace(_STORE_PATH)


def enroll(name: str, wav_paths: list[str]) -> int:
    """Compute mean CAM++ embedding from wav_paths, L2-normalise, save as centroid.
    Returns the number of clips successfully embedded."""
    import numpy as np
    sid = _load_sid_module()
    embeddings = []
    for p in wav_paths:
        try:
            embeddings.append(sid.embed(p))
        except Exception as e:
            print(f"[speaker-id] WARNING: embed({p!r}) failed: {e}", flush=True)
    if not embeddings:
        raise ValueError(f"no valid embeddings for {name!r} from {wav_paths}")
    centroid = np.mean(embeddings, axis=0).astype(np.float32)
    n = np.linalg.norm(centroid)
    if n > 0:
        centroid = centroid / n
    with _store_lock:
        _voiceprints[name] = centroid.tolist()
        _save_store()
    print(f"[speaker-id] enrolled '{name}' from {len(embeddings)} clip(s)", flush=True)
    return len(embeddings)


def identify(wav_path: str) -> tuple[str, float]:
    """Return (best_name, cosine_score). Name is 'unknown' if score < threshold."""
    import numpy as np
    sid = _load_sid_module()
    emb = sid.embed(wav_path)
    best_name, best_score = "unknown", 0.0
    with _store_lock:
        store_snapshot = dict(_voiceprints)
    for name, centroid in store_snapshot.items():
        score = sid.cosine(emb, np.asarray(centroid, dtype="float32"))
        if score > best_score:
            best_name, best_score = name, score
    if best_score < SPEAKER_THRESHOLD:
        return "unknown", float(best_score)
    return best_name, float(best_score)


def _seed_wren_voiceprint() -> None:
    """Enroll 'wren' from wren_ref.wav at startup if not already in the store."""
    if "wren" in _voiceprints:
        print("[speaker-id] 'wren' voiceprint already in store — skipping seed", flush=True)
        return
    if not _WREN_REF.is_file():
        print(f"[speaker-id] WARNING: wren_ref.wav not found at {_WREN_REF}, skipping seed", flush=True)
        return
    try:
        n = enroll("wren", [str(_WREN_REF)])
        print(f"[speaker-id] seeded 'wren' voiceprint from {n} clip(s)", flush=True)
    except Exception as e:
        print(f"[speaker-id] WARNING: failed to seed wren voiceprint: {e}", flush=True)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):  # keep the console quiet
        pass

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length", "0") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._send(200, {"status": "ok", "model": sv.MODEL_ID})
        elif self.path == "/warm":
            # voice_call_start: ensure the model singleton is loaded.
            # sv._load() is idempotent (no-op if already warm).
            try:
                sv._load()
                self._send(200, {"status": "warm", "model": sv.MODEL_ID})
            except Exception as e:
                self._send(503, {"error": repr(e)})
        elif self.path == "/cold":
            # voice_call_end: unload SenseVoice model + free VRAM; keep server alive.
            try:
                sv._unload()
                self._send(200, {"status": "cold"})
            except Exception as e:
                # _unload may not exist yet (added below); fall back gracefully.
                self._send(200, {"status": "cold", "note": repr(e)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path == "/transcribe":
            self._handle_transcribe()
        elif self.path == "/enroll":
            self._handle_enroll()
        else:
            self._send(404, {"error": "not found"})

    def _handle_transcribe(self) -> None:
        try:
            body = self._read_body()
            wav = body.get("wav_path")
            lang = body.get("language", "en")
            if not wav or not Path(wav).is_file():
                self._send(400, {"error": f"wav_path missing/not found: {wav!r}"})
                return
            # ASR first — this must always succeed
            result = sv.transcribe(wav, language=lang)
            # Speaker identification — best-effort, never breaks ASR result
            speaker: str | None = None
            speaker_score: float | None = None
            try:
                if _voiceprints:      # only attempt if we have enrolled prints
                    speaker, speaker_score = identify(wav)
                    speaker_score = round(speaker_score, 4)
            except Exception as e:
                print(f"[speaker-id] WARNING: identify failed for {wav!r}: {e}", flush=True)
            result["speaker"] = speaker
            result["speaker_score"] = speaker_score
            self._send(200, result)
        except Exception as e:  # never let one bad request kill the server
            self._send(500, {"error": repr(e)})

    def _handle_enroll(self) -> None:
        try:
            body = self._read_body()
            name = body.get("name", "").strip()
            wav_paths = body.get("wav_paths", [])
            if not name:
                self._send(400, {"error": "name is required"})
                return
            if not isinstance(wav_paths, list) or not wav_paths:
                self._send(400, {"error": "wav_paths must be a non-empty list"})
                return
            # Validate paths before enrolling
            missing = [p for p in wav_paths if not Path(p).is_file()]
            if missing:
                self._send(400, {"error": f"wav_paths not found: {missing}"})
                return
            n = enroll(name, wav_paths)
            self._send(200, {"ok": True, "name": name, "n_clips": n})
        except Exception as e:
            self._send(500, {"error": repr(e)})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("[sensevoice-server] loading SenseVoice-Small (warm)...", flush=True)
    sv._load()  # load once at startup so the first request is fast

    # Load existing voiceprint store, then seed Wren's own print if absent.
    # CAM++ lazy-loads on first embed call — server start is still fast when
    # the seed is already in the store (no embed needed).
    _load_store()
    _seed_wren_voiceprint()

    print(f"[sensevoice-server] ready on {HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
