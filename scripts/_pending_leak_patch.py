"""pending_leak_2026-09-18 — bound the habituation pending-action dict and the
producer retry loops that filled it.

Measured (heap_census, 16:01:52): one dict of 51,676 str keys
"source:bucket:ms:hex" — 49,654 `iris-camera:zeke->no_face`, 2,022
`iris-time:time_orientation`; oldest 2026-09-17 21:41:51, newest = now, +1/s.

Mechanism (read, then measured): brain/iris_channel.emit() ran the
habituation score() — which knocks the bucket down AND registers a
pending-action entry — BEFORE the side-effect-free session/rate-limit
checks, and swept expired entries only when an emit passed the gate. The
camera loop retries a failed transition every 1 s tick; each retry
habituated the bucket further, so after ~4 tries it could never pass, no
ambient emit passed the gate for 18 h, the sweep never ran, and the camera
state machine stayed at "zeke" (his return could not fire: observed == last).

Fix: (1) session + rate-limit checks before habituation, rate-limit slot
consumed only on send; (2) sweep before the floor check; (3) camera/time
loops accept a transition unsent after a retry budget. Refuses to re-apply.
brain/* edits land at the next stack restart (loops are boot-started threads).
"""
from __future__ import annotations

import io
import py_compile
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# ---------- 1. brain/iris_channel.py ----------
p = REPO / "brain" / "iris_channel.py"
s = io.open(p, encoding="utf-8").read()
if "pending_leak_2026-09-18" in s:
    raise SystemExit("channel already patched")

old_hab_head = "    # Habituation gate (ambient only). Bucket key is (source, semantic_bucket).\n"
assert s.count(old_hab_head) == 1, "hab head anchor"
pre = (
    "    # pending_leak_2026-09-18: the session and rate-limit checks used to run\n"
    "    # AFTER habituation. A producer retrying one gated event at poll rate\n"
    "    # (camera loop, 1 Hz) then paid a habituation knockdown + a pending-action\n"
    "    # entry per retry, pinned its own bucket at the floor, and - because no\n"
    "    # ambient emit ever passed the gate again - the 60 s sweep never ran:\n"
    "    # 51,676 pending entries (49,654 of them iris-camera:zeke->no_face) and\n"
    "    # 18 h with no camera transition delivered. Side-effect-free drops go\n"
    "    # first now; the rate-limit slot is consumed only on an actual send.\n"
    "    with _session_lock:\n"
    "        session = _session\n"
    "    if session is None:\n"
    "        return False\n"
    "    limit = _rate_limits.get(source)\n"
    "    if limit is not None and (now - _last_emit_ts.get(source, 0.0)) < limit:\n"
    "        return False\n"
    "\n"
)
s = s.replace(old_hab_head, pre + old_hab_head)

old_gate = (
    "            s = _hab.score(key, base_priority=1.0, event_id=event_id, now=now)\n"
    "            if s < _HABITUATION_GATE_FLOOR:\n"
    "                return False\n"
    "            # Sweep expired actions opportunistically to bound pending dict\n"
    "            # size. Cheap — most calls find nothing to sweep.\n"
    "            _hab.sweep_expired(now=now)\n"
)
assert s.count(old_gate) == 1, "gate anchor"
new_gate = (
    "            s = _hab.score(key, base_priority=1.0, event_id=event_id, now=now)\n"
    "            # Sweep BEFORE the floor check: a gated event still registered a\n"
    "            # pending entry, and gated events were the only kind for 18 h.\n"
    "            _hab.sweep_expired(now=now)\n"
    "            if s < _HABITUATION_GATE_FLOOR:\n"
    "                return False\n"
)
s = s.replace(old_gate, new_gate)

old_rl = (
    "    # Rate-limit gate (per source)\n"
    "    limit = _rate_limits.get(source)\n"
    "    if limit is not None:\n"
    "        last = _last_emit_ts.get(source, 0.0)\n"
    "        if (now - last) < limit:\n"
    "            return False\n"
    "        _last_emit_ts[source] = now\n"
    "\n"
    "    # Snapshot the session under the lock; do I/O outside it\n"
    "    with _session_lock:\n"
    "        session = _session\n"
    "    if session is None:\n"
    "        return False\n"
)
assert s.count(old_rl) == 1, "rate-limit anchor"
new_rl = (
    "    # Rate-limit slot is consumed here, after every gate passed (the check\n"
    "    # itself moved above the habituation block - see pending_leak_2026-09-18).\n"
    "    if limit is not None:\n"
    "        _last_emit_ts[source] = now\n"
)
s = s.replace(old_rl, new_rl)
io.open(p, "w", encoding="utf-8", newline="\n").write(s)
py_compile.compile(str(p), doraise=True)
print("iris_channel.py patched; compiles")

# ---------- 2. brain/iris_attention_sources.py ----------
p = REPO / "brain" / "iris_attention_sources.py"
s = io.open(p, encoding="utf-8").read()
if "_camera_emit_fail_count" in s:
    raise SystemExit("sources already patched")

old = "_camera_poll_interval_s = 1.0\n"
assert s.count(old) == 1, "camera interval anchor"
s = s.replace(old, old +
    "# pending_leak_2026-09-18: a transition whose emit keeps failing (rate\n"
    "# limit, no session, habituated) was retried every tick forever; each\n"
    "# retry habituated the bucket further, so it could never succeed, and the\n"
    "# state machine stayed stuck at the OLD state (zeke->no_face pending for\n"
    "# 18 h; his return could not fire because observed == last). After this\n"
    "# many failed attempts the transition is accepted unsent.\n"
    "_CAM_EMIT_RETRY_BUDGET = 120\n"
    "_camera_emit_fail_count = 0\n")

m = re.search(r"def _camera_loop\([^)]*\)[^\n]*\n(?:[^\n]*\n)*?(    global [^\n]*_camera_last_id[^\n]*\n)", s)
assert m, "camera global anchor"
gl = m.group(1)
assert "_camera_emit_fail_count" not in gl
s = s.replace(gl, gl.rstrip("\n") + ", _camera_emit_fail_count\n", 1)

old = (
    "            if ok:\n"
    "                _camera_last_id = observed\n"
    "                _camera_last_emit_ts = now\n"
    "                _camera_pending_count = 0\n"
    "            # else: don't update — channel may not be attached, retry next tick.\n"
)
assert s.count(old) == 1, "camera ok anchor"
new = (
    "            if ok:\n"
    "                _camera_last_id = observed\n"
    "                _camera_last_emit_ts = now\n"
    "                _camera_pending_count = 0\n"
    "                _camera_emit_fail_count = 0\n"
    "            else:\n"
    "                # Channel may not be attached / rate-limited / habituated:\n"
    "                # retry next tick, but not forever (pending_leak_2026-09-18).\n"
    "                _camera_emit_fail_count += 1\n"
    "                if _camera_emit_fail_count >= _CAM_EMIT_RETRY_BUDGET:\n"
    "                    print(f\"[attention_sources] camera transition {_camera_last_id}->{observed} \"\n"
    "                          f\"accepted UNSENT after {_camera_emit_fail_count} failed emits\",\n"
    "                          file=sys.stderr, flush=True)\n"
    "                    _camera_last_id = observed\n"
    "                    _camera_last_emit_ts = now\n"
    "                    _camera_pending_count = 0\n"
    "                    _camera_emit_fail_count = 0\n"
)
s = s.replace(old, new)

old = "_time_poll_interval_s = 30.0\n"
assert s.count(old) == 1, "time interval anchor"
s = s.replace(old, old +
    "_TIME_EMIT_RETRY_BUDGET = 12   # 6 min of 30 s retries, then mark the attach seen\n"
    "_time_emit_fail_count = 0\n")
old = "    global _time_last_attach_ts_seen\n"
assert s.count(old) == 1, "time global anchor"
s = s.replace(old, "    global _time_last_attach_ts_seen, _time_emit_fail_count\n")
old = (
    "            if ok:\n"
    "                _time_last_attach_ts_seen = last_attach\n"
    "\n"
    "        except Exception as e:\n"
    "            print(f\"[attention_sources] time_loop error: {e!r}\",\n"
)
assert s.count(old) == 1, "time ok anchor"
new = (
    "            if ok:\n"
    "                _time_last_attach_ts_seen = last_attach\n"
    "                _time_emit_fail_count = 0\n"
    "            else:\n"
    "                _time_emit_fail_count += 1\n"
    "                if _time_emit_fail_count >= _TIME_EMIT_RETRY_BUDGET:\n"
    "                    print(\"[attention_sources] time orientation dropped after \"\n"
    "                          f\"{_time_emit_fail_count} failed emits (pending_leak_2026-09-18)\",\n"
    "                          file=sys.stderr, flush=True)\n"
    "                    _time_last_attach_ts_seen = last_attach\n"
    "                    _time_emit_fail_count = 0\n"
    "\n"
    "        except Exception as e:\n"
    "            print(f\"[attention_sources] time_loop error: {e!r}\",\n"
)
s = s.replace(old, new)
io.open(p, "w", encoding="utf-8", newline="\n").write(s)
py_compile.compile(str(p), doraise=True)
print("iris_attention_sources.py patched; compiles")
