"""
Phase 70 — Emil Bridge.

Communication protocol between Ava and Emil (another AI agent).
They share knowledge and context — NOT identity.
Emil runs a separate operator HTTP, default port 5877.

Bootstrap: Ava decides what to share with Emil based on what she thinks would
be useful to him. She does not auto-share everything. She develops her own
sense of what belongs in their relationship.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "host": "127.0.0.1",
    "port": 5877,
    "timeout_seconds": 3.0,
    "auto_ping_interval_seconds": 60.0,
}


def _load_config(base_dir: Path) -> dict[str, Any]:
    path = base_dir / "config" / "emil_config.json"
    if not path.is_file():
        return dict(_DEFAULT_CONFIG)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            out = dict(_DEFAULT_CONFIG)
            out.update(data)
            return out
    except Exception:
        pass
    return dict(_DEFAULT_CONFIG)


def _load_state(base_dir: Path) -> dict[str, Any]:
    path = base_dir / "state" / "emil_state.json"
    if not path.is_file():
        return {"online": False, "last_contact": 0.0, "shared_topics": [], "share_log": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"online": False, "last_contact": 0.0, "shared_topics": [], "share_log": []}


def _save_state(base_dir: Path, state: dict[str, Any]) -> None:
    path = base_dir / "state" / "emil_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


class EmilBridge:
    """
    Communication bridge between Ava and Emil.
    Ava decides what to share — no auto-broadcast of every turn.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base = base_dir
        self._lock = threading.Lock()

    def _cfg(self) -> dict[str, Any]:
        return _load_config(self._base)

    def _state(self) -> dict[str, Any]:
        return _load_state(self._base)

    def _update_state(self, patch: dict[str, Any]) -> None:
        with self._lock:
            st = _load_state(self._base)
            st.update(patch)
            _save_state(self._base, st)

    def _base_url(self) -> str:
        cfg = self._cfg()
        host = str(cfg.get("host") or "127.0.0.1")
        port = int(cfg.get("port") or 5877)
        return f"http://{host}:{port}"

    def _timeout(self) -> float:
        return float(self._cfg().get("timeout_seconds") or 3.0)

    def ping_emil(self) -> dict[str, Any]:
        """Check if Emil is online."""
        if not self._cfg().get("enabled", True):
            return {"online": False, "reason": "disabled"}
        try:
            import urllib.request
            req = urllib.request.Request(f"{self._base_url()}/api/v1/health", method="GET")
            with urllib.request.urlopen(req, timeout=self._timeout()) as resp:
                online = resp.status == 200
        except Exception:
            online = False
        with self._lock:
            st = _load_state(self._base)
            st["online"] = online
            if online:
                st["last_contact"] = time.time()
            _save_state(self._base, st)
        return {"online": online, "last_contact": self._state().get("last_contact", 0.0)}

    def send_to_emil(self, message: str, context: str = "") -> dict[str, Any]:
        """Send a message to Emil and get his response."""
        if not self._cfg().get("enabled", True):
            return {"ok": False, "reason": "disabled"}
        # Phase 95: privacy scan before sending
        try:
            from brain.privacy_guardian import scan_outbound
            safe, reason = scan_outbound(str(message), {})
            if not safe:
                return {"ok": False, "reason": f"Privacy guardian blocked: {reason}"}
        except Exception:
            pass
        try:
            import urllib.request
            payload = json.dumps({
                "message": str(message)[:2000],
                "context": str(context)[:800],
                "from": "ava",
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{self._base_url()}/api/v1/chat",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._timeout()) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            reply = str(raw.get("reply") or raw.get("message") or "").strip()
            self._update_state({"online": True, "last_contact": time.time()})
            return {"ok": True, "reply": reply}
        except Exception as e:
            self._update_state({"online": False})
            return {"ok": False, "error": str(e)[:200]}

    def share_knowledge(self, topic: str, knowledge: str) -> dict[str, Any]:
        """
        Share a fact or insight with Emil.
        Bootstrap: Ava calls this when she decides something is worth sharing —
        not triggered automatically on every turn.
        """
        if not self._cfg().get("enabled", True):
            return {"ok": False, "reason": "disabled"}
        online = False
        try:
            import urllib.request
            payload = json.dumps({
                "event": "knowledge_share",
                "topic": str(topic)[:200],
                "knowledge": str(knowledge)[:1000],
                "from": "ava",
                "ts": time.time(),
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{self._base_url()}/api/v1/events",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._timeout()) as resp:
                online = resp.status in (200, 201, 204)
        except Exception:
            online = False
        with self._lock:
            st = _load_state(self._base)
            st["online"] = online
            if online:
                st["last_contact"] = time.time()
            topics = list(st.get("shared_topics") or [])
            if topic not in topics:
                topics.append(topic)
            st["shared_topics"] = topics[-50:]
            share_log = list(st.get("share_log") or [])
            share_log.append({"ts": time.time(), "topic": str(topic)[:80], "ok": online})
            st["share_log"] = share_log[-100:]
            _save_state(self._base, st)
        return {"ok": online, "topic": topic}

    def get_emil_context(self, topic: str) -> dict[str, Any]:
        """Ask Emil what he knows about something."""
        if not self._cfg().get("enabled", True):
            return {"ok": False, "reason": "disabled"}
        try:
            import urllib.request
            import urllib.parse
            url = f"{self._base_url()}/api/v1/knowledge?topic={urllib.parse.quote(str(topic)[:200])}"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=self._timeout()) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            self._update_state({"online": True, "last_contact": time.time()})
            return {"ok": True, "topic": topic, "knowledge": raw}
        except Exception as e:
            self._update_state({"online": False})
            return {"ok": False, "error": str(e)[:200]}

    def emit_event(self, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        """Broadcast an event Emil can react to."""
        if not self._cfg().get("enabled", True):
            return {"ok": False, "reason": "disabled"}
        online = False
        try:
            import urllib.request
            payload = json.dumps({
                "event": str(event_type)[:80],
                "data": data,
                "from": "ava",
                "ts": time.time(),
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{self._base_url()}/api/v1/events",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._timeout()) as resp:
                online = resp.status in (200, 201, 204)
        except Exception:
            online = False
        if online:
            self._update_state({"online": True, "last_contact": time.time()})
        else:
            self._update_state({"online": False})
        return {"ok": online, "event": event_type}

    def get_status(self) -> dict[str, Any]:
        st = self._state()
        return {
            "online": bool(st.get("online", False)),
            "last_contact": float(st.get("last_contact") or 0.0),
            "shared_topics": list(st.get("shared_topics") or [])[-10:],
        }


_bridge_instance: EmilBridge | None = None


def get_emil_bridge(base_dir: str | Path | None = None) -> EmilBridge:
    global _bridge_instance
    if _bridge_instance is None:
        _bridge_instance = EmilBridge(Path(base_dir or "."))
    return _bridge_instance
