"""Freshness-check for always-loaded self-claims (Iris_fixes #3, 2026-09-30).

The incident (CLAUDE.md line ~220, verbatim): for ~a month CLAUDE.md asserted
"Voice + ears are OFF ... 8769/8770 being DOWN is CORRECT — do not heal them"
AFTER that stopped being true. An always-loaded file that outranks memory was
actively instructing a future me to leave WORKING services dead and to treat a
live mouth as a fault. A stale state-claim is not inert; it is a standing wrong
instruction.

CLAUDE.md's own rule: "When a claim in here stops matching what a probe says, fix
it that same session." But nothing FIRES that check — I only catch drift if I
happen to re-probe the exact thing. This module fires it: it reads the CURRENT
polarity of each load-bearing self-claim straight out of CLAUDE.md prose and
compares it to a LIVE probe, so drift is surfaced instead of silently obeyed.

It deliberately covers only a handful of binary, probe-backed claims (the ones
that have actually bitten) — not all prose. Each claim: extract the doc's current
assertion via a targeted regex, probe reality, flag if they contradict.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _claude_md_text(path: Optional[Path] = None) -> str:
    p = path or (_repo_root() / "CLAUDE.md")
    try:
        return Path(p).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


# ── live reality probes (reuse the real health probes where possible) ─────────

def _reality_voice() -> str:
    """'on' if the mouth+daemon ports are live, 'off' if down, 'disabled' if a
    deliberate-off flag is set."""
    try:
        from brain import iris_health_probes as hp
        st = hp.probe_voice()["state"]
    except Exception:
        return "unknown"
    return {"ok": "on", "degraded": "on", "down": "off", "disabled": "disabled"}.get(st, "unknown")


def _reality_camera() -> str:
    try:
        from brain import iris_health_probes as hp
        st = hp.probe_camera()["state"]
    except Exception:
        return "unknown"
    return {"ok": "on", "degraded": "on", "down": "off"}.get(st, "unknown")


def _reality_wake() -> str:
    # Wake is off by design on this host; the probe encodes that invariant.
    try:
        from brain import iris_health_probes as hp
        return "disabled" if hp.probe_wake()["state"] == "disabled" else "on"
    except Exception:
        return "unknown"


# ── claim registry ───────────────────────────────────────────────────────────
# doc_regex: captures the polarity token from CLAUDE.md.
# doc_map:   maps the captured token -> normalized claim value.
# reality:   probe -> normalized reality value.
# A claim DRIFTS when doc-value and reality-value are both known AND on-vs-off
# opposite. 'disabled' reality never counts as drift against an 'on' doc claim
# unless the doc explicitly claims 'off' (a disabled subsystem is intentionally
# down, not broken).

_CLAIMS = [
    {
        "id": "voice_state",
        "describe": "CLAUDE.md 'Voice is ON/OFF' vs mouth+daemon ports live",
        "doc_regex": r"Voice is \*{0,2}(ON|OFF)",
        "doc_map": {"ON": "on", "OFF": "off"},
        "reality": _reality_voice,
    },
    {
        "id": "voice_ports_listening",
        "describe": "CLAUDE.md '8769 and 8770 are listening' vs live ports",
        "doc_regex": r"8769 and 8770 are\s+\*{0,2}(listening|DOWN)",
        "doc_map": {"listening": "on", "DOWN": "off"},
        "reality": _reality_voice,
    },
    {
        "id": "eyes_present",
        "describe": "CLAUDE.md 'Eyes are BACK/DOWN' vs live camera frame",
        "doc_regex": r"Eyes are \*{0,2}(BACK|DOWN)",
        "doc_map": {"BACK": "on", "DOWN": "off"},
        "reality": _reality_camera,
    },
    {
        "id": "wake_by_design_off",
        "describe": "CLAUDE.md 'wake.loaded=false IS CORRECT' vs live wake state",
        "doc_regex": r"wake\.loaded=false[^A-Za-z]{1,4}IS\s+(CORRECT)",
        "doc_map": {"CORRECT": "disabled"},
        "reality": _reality_wake,
    },
]

# on/off are opposites; disabled is a benign third state.
_OPPOSITE = {("on", "off"), ("off", "on")}


def _line_of(text: str, span_start: int) -> int:
    return text.count("\n", 0, span_start) + 1


def check_all(claude_md_path: Optional[Path] = None,
              doc_text: Optional[str] = None,
              reality_overrides: Optional[dict] = None) -> dict:
    """Compare each load-bearing self-claim in CLAUDE.md against live reality.

    reality_overrides / doc_text let the adversarial test inject a doc that says
    the wrong thing (or a fake reality) and confirm drift is caught.

    Returns {ok, drift: bool, claims: [ {id, doc_says, reality_says, drift,
    doc_line, describe, action} ]}.
    """
    text = doc_text if doc_text is not None else _claude_md_text(claude_md_path)
    results = []
    any_drift = False
    for c in _CLAIMS:
        doc_val = None
        doc_line = None
        m = re.search(c["doc_regex"], text)
        if m:
            token = m.group(1)
            doc_val = c["doc_map"].get(token, token)
            doc_line = _line_of(text, m.start())
        if reality_overrides and c["id"] in reality_overrides:
            real_val = reality_overrides[c["id"]]
        else:
            real_val = c["reality"]()
        drift = False
        action = None
        if doc_val is None:
            action = "claim not found in CLAUDE.md (regex miss or claim removed)"
        elif real_val in ("unknown", None):
            action = "reality unknown — probe failed, cannot verify"
        elif (doc_val, real_val) in _OPPOSITE:
            drift = True
            action = (f"CLAUDE.md line {doc_line} says '{doc_val}' but a live probe "
                      f"says '{real_val}' — FIX THAT LINE THIS SESSION")
        else:
            action = "matches reality"
        any_drift = any_drift or drift
        results.append({
            "id": c["id"], "describe": c["describe"],
            "doc_says": doc_val, "reality_says": real_val,
            "doc_line": doc_line, "drift": drift, "action": action,
        })
    return {"ok": True, "drift": any_drift, "claims": results}


if __name__ == "__main__":
    import pprint
    pprint.pprint(check_all())
