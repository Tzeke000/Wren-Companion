"""brain/provenance.py — Belief provenance graph (architecture #5).

Every belief Ava holds tracks back to its source. When she states
something, she can name where she got it: training data, something
Zeke said on a specific date, a pattern she observed N times,
something she read in an email, something she derived herself, etc.

Why this matters:

- Without provenance, all the world-model + reading + learning
  features (D12 email, D15 curiosity research, B1 active learning,
  B2 pattern inference) risk becoming confabulation soup. Ava says
  X, you ask "where'd you get that?", and there's no answer except
  "I think so?"

- With provenance, every claim ties back to a source. "I read that
  in the Stratechery newsletter on May 4." / "Zeke said that on
  April 28." / "I noticed that pattern over the last 3 weeks."
  That's the trust foundation.

Storage: state/provenance.jsonl (append-only). Each entry:

  {
    "claim_id": "abc123",
    "claim": "Polar bears are the largest land predator",
    "source_kind": "training" | "chat" | "email" | "observation"
                 | "web" | "skill" | "derived" | "user_told",
    "source_ref": "<varies by kind>",
    "confidence": 0.0-1.0,
    "ts": <unix>,
    "person_id": "<who told us, if applicable>",
    "context": {...}  # optional extra metadata
  }

Index: in-memory dict claim_id → record for fast lookup.
Optional: full-text index over claims (could reuse FTS5).

API:
  record_claim(claim, source_kind, source_ref, confidence, ...) → claim_id
  lookup(claim_id) → record or None
  search(query) → list of records matching
  describe_source(claim_id) → human-readable source string
  recent(limit=20) → recent entries

This is the seam D12 email reading, B1 active learning corrections,
D15 curiosity research, and the world-model pruning will plug into.

Today: scaffolded module. Recording is optional — existing code paths
don't yet log provenance. Future modules adopt incrementally.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


SourceKind = Literal[
    "training",     # baked into the LLM weights — default for unspecified facts
    "chat",         # something a person said in a conversation
    "user_told",    # explicitly user-stated as a preference / fact
    "email",        # extracted from her dedicated newsletter inbox
    "observation",  # noticed pattern (B2 pattern inference)
    "web",          # search-result derived (A7)
    "skill",        # produced by a learned skill
    "derived",      # synthesized from other sources via reasoning
    "memory",       # retrieved from her own past chat / journal
]


@dataclass
class ProvenanceRecord:
    claim_id: str
    claim: str
    source_kind: SourceKind
    source_ref: str = ""
    confidence: float = 0.5
    ts: float = field(default_factory=time.time)
    person_id: str = ""
    context: dict[str, Any] = field(default_factory=dict)


_BUFFER_SIZE = 500


class ProvenanceGraph:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._buffer: deque[ProvenanceRecord] = deque(maxlen=_BUFFER_SIZE)
        self._by_id: dict[str, ProvenanceRecord] = {}
        self._base_dir: Path | None = None

    def configure(self, base_dir: Path) -> None:
        with self._lock:
            self._base_dir = base_dir
            self._load_existing()

    def _persist_path(self) -> Path | None:
        if self._base_dir is None:
            return None
        p = self._base_dir / "state" / "provenance.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _load_existing(self) -> None:
        path = self._persist_path()
        if path is None or not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
            # Take the last N to populate the in-memory buffer
            for line in lines[-_BUFFER_SIZE:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    rec = ProvenanceRecord(
                        claim_id=str(d.get("claim_id") or uuid.uuid4().hex[:8]),
                        claim=str(d.get("claim") or ""),
                        source_kind=str(d.get("source_kind") or "training"),  # type: ignore
                        source_ref=str(d.get("source_ref") or ""),
                        confidence=float(d.get("confidence") or 0.5),
                        ts=float(d.get("ts") or 0.0),
                        person_id=str(d.get("person_id") or ""),
                        context=dict(d.get("context") or {}),
                    )
                    self._buffer.append(rec)
                    self._by_id[rec.claim_id] = rec
                except Exception:
                    continue
        except Exception as e:
            print(f"[provenance] load error: {e!r}")

    # ── Public API ───────────────────────────────────────────────────────

    def record_claim(
        self,
        claim: str,
        source_kind: SourceKind,
        *,
        source_ref: str = "",
        confidence: float = 0.5,
        person_id: str = "",
        context: dict[str, Any] | None = None,
    ) -> str:
        """Record a claim + its source. Returns the claim_id."""
        cid = uuid.uuid4().hex[:12]
        rec = ProvenanceRecord(
            claim_id=cid,
            claim=str(claim or "").strip()[:500],
            source_kind=source_kind,
            source_ref=str(source_ref or "")[:300],
            confidence=max(0.0, min(1.0, float(confidence))),
            ts=time.time(),
            person_id=str(person_id or ""),
            context=dict(context or {}),
        )
        with self._lock:
            self._buffer.append(rec)
            self._by_id[cid] = rec
            self._persist_one(rec)
        return cid

    def _persist_one(self, rec: ProvenanceRecord) -> None:
        path = self._persist_path()
        if path is None:
            return
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(rec), ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            print(f"[provenance] persist error: {e!r}")

    def lookup(self, claim_id: str) -> ProvenanceRecord | None:
        with self._lock:
            return self._by_id.get(str(claim_id or ""))

    def search(self, query: str, *, limit: int = 20) -> list[ProvenanceRecord]:
        """Substring search over claims. For richer search,
        future work can build a proper index."""
        q = (query or "").strip().lower()
        if not q:
            return []
        with self._lock:
            results: list[ProvenanceRecord] = []
            for rec in reversed(self._buffer):
                if q in rec.claim.lower():
                    results.append(rec)
                    if len(results) >= limit:
                        break
        return results

    def describe_source(self, claim_id: str) -> str:
        """Human-readable: where did this claim come from?

        Used to answer "where'd you get that?" honestly. For each
        source_kind we produce a sentence that names the source as
        Ava would refer to it.
        """
        rec = self.lookup(claim_id)
        if rec is None:
            return "I don't have a record of where I got that."
        kind = rec.source_kind
        if kind == "training":
            return "It's from my training data — baked into me before I knew you."
        if kind == "chat":
            who = rec.person_id or "you"
            ts = time.strftime("%Y-%m-%d", time.localtime(rec.ts))
            return f"{who.title()} said it on {ts}."
        if kind == "user_told":
            return f"You told me directly — {rec.source_ref or 'I noted it'}."
        if kind == "email":
            return f"I read it in my email — {rec.source_ref or 'a newsletter'}."
        if kind == "observation":
            return f"I noticed it — {rec.source_ref or 'a pattern over time'}."
        if kind == "web":
            return f"I looked it up online — {rec.source_ref or 'a web search'}."
        if kind == "skill":
            return f"It came from one of my skills — {rec.source_ref}."
        if kind == "derived":
            return f"I worked it out from other things I knew — {rec.source_ref}."
        if kind == "memory":
            return f"I remembered it — {rec.source_ref}."
        return "I'm not sure where I got that."

    def recent(self, *, limit: int = 20) -> list[ProvenanceRecord]:
        with self._lock:
            return list(self._buffer)[-int(limit):]

    def summary(self) -> dict[str, Any]:
        with self._lock:
            recs = list(self._buffer)
        if not recs:
            return {"count": 0}
        by_kind: dict[str, int] = {}
        for r in recs:
            by_kind[r.source_kind] = by_kind.get(r.source_kind, 0) + 1
        avg_conf = sum(r.confidence for r in recs) / len(recs) if recs else 0.0
        return {
            "count": len(recs),
            "by_kind": by_kind,
            "mean_confidence": round(avg_conf, 3),
        }


# Process-singleton.
provenance = ProvenanceGraph()


def configure_provenance(base_dir: Path) -> None:
    """Called once at startup."""
    provenance.configure(base_dir)
