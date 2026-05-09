"""brain/doctor_session.py — Doctor identity protocol + diagnostic session state.

Per docs/AUTONOMOUS_TESTING.md and docs/research/autonomous_testing/findings.md.

The "doctor" is Claude Code in test-harness role. Authority is bounded to
diagnostic operations (capability/health domain) — never identity, values,
or curriculum. Ava can refuse doctor requests at any time; refusals are
logged, honored, and surfaced for Zeke's review.

Architecture summary:
- Shared secret at `state/doctor.secret` (32 random bytes, gitignored).
  Generated on first server start. Rotated by deleting the file.
- Doctor harness reads the secret, mints an HMAC-signed token with claims
  {sub, role, session_id, iat, exp}, sends it in Authorization: Bearer.
- Server verifies HMAC, exposes diagnostic endpoints.
- Each session writes a full audit log to `logs/diagnostic_sessions/`.

Stdlib-only — no pyjwt or sse-starlette dependency. The token format is
JWT-compatible (`<b64url(payload)>.<hex(hmac_sha256)>`) but minimal.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterable, Optional


# Where the shared secret lives. Gitignored. Generated on first call.
_SECRET_PATH = "state/doctor.secret"

# Where session audit logs are written.
_LOGS_DIR = "logs/diagnostic_sessions"

# Default session TTL (seconds) — short, since doctor sessions are interactive.
_DEFAULT_TTL_SEC = 900  # 15 minutes

# Max ring buffer for diagnostic events streamed to /api/v1/diagnostic/events.
_EVENT_RING_SIZE = 1000


# ── Shared-secret helpers ─────────────────────────────────────────────────


def _secret_path(g: Optional[dict[str, Any]] = None) -> Path:
    base = Path((g or {}).get("BASE_DIR") or ".")
    return base / _SECRET_PATH


def get_or_create_secret(g: Optional[dict[str, Any]] = None) -> bytes:
    """Read the shared secret, generating it if missing.

    32 random bytes from `secrets.token_bytes`. Created with mode 0o600
    on POSIX (Windows ignores the chmod).
    """
    path = _secret_path(g)
    if path.is_file():
        try:
            data = path.read_bytes()
            if len(data) >= 32:
                return data
        except OSError:
            pass
    # Generate
    path.parent.mkdir(parents=True, exist_ok=True)
    data = secrets.token_bytes(32)
    path.write_bytes(data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return data


# ── Token helpers (HMAC-signed JWT-compatible) ────────────────────────────


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def mint_token(
    session_id: str,
    secret: bytes,
    role: str = "doctor",
    sub: str = "claude_doctor",
    ttl_sec: int = _DEFAULT_TTL_SEC,
) -> str:
    """Mint an HMAC-SHA256 token. Format: `<b64url(payload)>.<hex(hmac)>`.

    Doctor-side helper for the test harness. Server verifies via verify_token.
    """
    payload = {
        "sub": sub,
        "role": role,
        "session_id": session_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + int(ttl_sec),
    }
    header = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(secret, header.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{header}.{sig}"


def verify_token(token: str, secret: bytes) -> Optional[dict[str, Any]]:
    """Return claims dict if signature + expiry are valid; else None."""
    if not token or "." not in token:
        return None
    parts = token.split(".")
    if len(parts) != 2:
        return None
    payload_b64, sig_hex = parts[0], parts[1]
    expected = hmac.new(secret, payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig_hex):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception:
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    if payload.get("role") != "doctor":
        return None
    return payload


# ── Scope-limit predicates ────────────────────────────────────────────────


# Paths the doctor MUST NOT modify, regardless of session state. These are
# the read-only files protected by CLAUDE.md's "never edit" rule, plus
# state files that carry Zeke's personal relationship data.
_OFF_LIMITS_PATHS = frozenset({
    "ava_core/IDENTITY.md",
    "ava_core/SOUL.md",
    "ava_core/USER.md",
    "ava_core\\IDENTITY.md",
    "ava_core\\SOUL.md",
    "ava_core\\USER.md",
})


def is_path_off_limits(path: str | Path) -> bool:
    """Return True if `path` is in the doctor's no-write list.

    Used by audit/logging. The actual filesystem protection is the
    never-edit rule in CLAUDE.md + the read-only marker on those files.
    """
    p = str(path).replace("/", os.sep)
    norm = p.lstrip(".\\/").replace("\\", "/")
    if norm in _OFF_LIMITS_PATHS:
        return True
    # Also catch any subdirectory write that touches these basenames.
    base = Path(p).name
    return base in {"IDENTITY.md", "SOUL.md", "USER.md"} and "ava_core" in p.replace("\\", "/")


# Action keywords the doctor explicitly cannot trigger. Future hooks should
# call `is_action_off_limits()` before dispatching.
_OFF_LIMITS_ACTIONS = frozenset({
    "modify_curriculum",
    "modify_identity",
    "modify_values",
    "modify_trust_definitions",
    "delete_zeke_memory",
    "impersonate_zeke",
})


def is_action_off_limits(action: str) -> bool:
    return action in _OFF_LIMITS_ACTIONS


# ── Event ring + per-subscriber queues for SSE ────────────────────────────


class _EventRing:
    """Thread-safe rolling event buffer.

    Used by the diagnostic event stream. The signal_bus is sync, so this
    sits in front of any async subscribers. New events: O(1) append +
    O(subscribers) put-into-queue.
    """

    def __init__(self, maxlen: int = _EVENT_RING_SIZE) -> None:
        self._buf: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._next_id: int = 1
        self._lock = threading.Lock()
        self._subscribers: list[Any] = []  # list of (queue.Queue or asyncio.Queue)

    def append(self, kind: str, data: dict[str, Any]) -> int:
        ev = {
            "id": 0,
            "kind": str(kind),
            "ts_ms": int(time.time() * 1000),
            "data": data,
        }
        with self._lock:
            ev["id"] = self._next_id
            self._next_id += 1
            self._buf.append(ev)
            for q in list(self._subscribers):
                try:
                    q.put_nowait(ev)
                except Exception:
                    # Slow subscriber — drop them rather than block the bus.
                    try:
                        self._subscribers.remove(q)
                    except ValueError:
                        pass
        return ev["id"]

    def replay(self, since: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return [ev for ev in self._buf if ev["id"] > since]

    def add_subscriber(self, q: Any) -> None:
        with self._lock:
            self._subscribers.append(q)

    def remove_subscriber(self, q: Any) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass


# ── Doctor session manager ────────────────────────────────────────────────


class DoctorSession:
    """Per-active-session state. One at a time.

    The session manager owns:
      - The current session_id (None when no session is active)
      - The audit log being accumulated (transcript, refusals, latencies)
      - The event ring (always live; only streamed during a session)

    Ava's `_g["_diagnostic_session_active"]` mirrors `is_active()` so
    snapshot consumers can react.
    """

    def __init__(self, g: dict[str, Any]) -> None:
        self._g = g
        self._lock = threading.Lock()
        self._session_id: Optional[str] = None
        self._started_ts: float = 0.0
        self._sub: Optional[str] = None
        self._transcript: list[dict[str, Any]] = []
        self._refusals: list[dict[str, Any]] = []
        self._latencies: list[dict[str, Any]] = []
        self._memory_writes: list[dict[str, Any]] = []
        self._scope_violations: list[dict[str, Any]] = []
        self.events = _EventRing()

    # ── lifecycle ──────────────────────────────────────────────────────

    def is_active(self) -> bool:
        with self._lock:
            return self._session_id is not None

    def session_id(self) -> Optional[str]:
        with self._lock:
            return self._session_id

    def begin(self, session_id: str, sub: str = "claude_doctor") -> None:
        with self._lock:
            self._session_id = session_id
            self._sub = sub
            self._started_ts = time.time()
            self._transcript = []
            self._refusals = []
            self._latencies = []
            self._memory_writes = []
            self._scope_violations = []
        try:
            self._g["_diagnostic_session_active"] = True
            self._g["_diagnostic_session_id"] = session_id
            self._g["_diagnostic_doctor_sub"] = sub
        except Exception:
            pass
        self.events.append("session.begin", {"session_id": session_id, "sub": sub})

    def end(self) -> Optional[Path]:
        """End session and write audit log. Returns log path."""
        with self._lock:
            if self._session_id is None:
                return None
            sid = self._session_id
            sub = self._sub
            started = self._started_ts
            transcript = list(self._transcript)
            refusals = list(self._refusals)
            latencies = list(self._latencies)
            memory_writes = list(self._memory_writes)
            scope_violations = list(self._scope_violations)
            self._session_id = None
            self._sub = None
            self._started_ts = 0.0

        try:
            self._g["_diagnostic_session_active"] = False
            self._g["_diagnostic_session_id"] = ""
            self._g["_diagnostic_doctor_sub"] = ""
        except Exception:
            pass

        self.events.append("session.end", {"session_id": sid})

        # Write audit log
        log_dir = Path((self._g or {}).get("BASE_DIR") or ".") / _LOGS_DIR
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{sid}.json"
        log = {
            "session_id": sid,
            "sub": sub,
            "started_ts": started,
            "ended_ts": time.time(),
            "duration_sec": time.time() - started,
            "transcript": transcript,
            "refusals": refusals,
            "latencies": latencies,
            "memory_writes": memory_writes,
            "scope_violations": scope_violations,
            "summary": {
                "turns": len(transcript),
                "refusals": len(refusals),
                "scope_violations": len(scope_violations),
                "avg_ttfa_ms": (
                    sum(l.get("ttfa_ms", 0) for l in latencies) / len(latencies)
                    if latencies else 0
                ),
            },
        }
        try:
            log_path.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            print(f"[doctor_session] audit log write failed: {e!r}")
            return None
        return log_path

    # ── recording helpers (called from other modules) ──────────────────

    def record_turn(self, user_text: str, ava_text: str, latency_ms: int = 0) -> None:
        if not self.is_active():
            return
        with self._lock:
            self._transcript.append({
                "ts": time.time(),
                "user": user_text,
                "ava": ava_text,
                "latency_ms": latency_ms,
            })
            self._latencies.append({"ts": time.time(), "ttfa_ms": latency_ms})
        self.events.append("turn", {"user": user_text, "ava": ava_text, "ms": latency_ms})

    def record_refusal(self, reason: str, doctor_request: str = "") -> None:
        if not self.is_active():
            return
        with self._lock:
            self._refusals.append({
                "ts": time.time(),
                "reason": reason,
                "doctor_request": doctor_request,
            })
        self.events.append("refusal", {"reason": reason, "request": doctor_request})

    def record_memory_write(self, layer: str, payload: dict[str, Any]) -> None:
        if not self.is_active():
            return
        with self._lock:
            self._memory_writes.append({"ts": time.time(), "layer": layer, "payload": payload})
        self.events.append("memory.write", {"layer": layer})

    def record_scope_violation(self, action: str, detail: str = "") -> None:
        """Records an attempted scope violation. The action is NOT executed —
        callers check `is_path_off_limits` / `is_action_off_limits` first."""
        with self._lock:
            self._scope_violations.append({
                "ts": time.time(),
                "action": action,
                "detail": detail,
            })
        self.events.append("scope.violation", {"action": action, "detail": detail})


# ── Module singleton ──────────────────────────────────────────────────────


_singleton: Optional[DoctorSession] = None
_singleton_lock = threading.Lock()


def get_doctor_session(g: dict[str, Any]) -> DoctorSession:
    """Return process-wide DoctorSession singleton."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = DoctorSession(g)
        return _singleton
