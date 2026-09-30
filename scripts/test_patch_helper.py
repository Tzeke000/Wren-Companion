"""Tests for scripts/_patch.py — the helper must refuse before it can damage a file.

Run:  .venv\\Scripts\\python.exe scripts\\test_patch_helper.py
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts._patch import PatchError, apply_to_text, patch  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ((" — " + detail) if detail else ""))


tmp = tempfile.mkdtemp(prefix="patch_")
py = os.path.join(tmp, "mod.py")
SRC = 'def f():\n    return "a\\nb"\n\nX = 1\n'
io.open(py, "w", encoding="utf-8", newline="\n").write(SRC)

# 1. success: exact-once replacement, compiles, written atomically
r = patch(py, [("X = 1\n", "X = 2\n")])
check("patch applies", r["changed"] and io.open(py, encoding="utf-8").read().endswith("X = 2\n"))

# 2. refuse: old occurs 0 times → untouched
before = io.open(py, encoding="utf-8").read()
try:
    patch(py, [("NOPE", "x")]); check("0 occurrences refused", False)
except PatchError as e:
    check("0 occurrences refused", "0 times" in str(e))
check("file untouched after refusal", io.open(py, encoding="utf-8").read() == before)

# 3. refuse: old occurs 2+ times
io.open(py, "a", encoding="utf-8", newline="\n").write("X = 2\n")
try:
    patch(py, [("X = 2\n", "X = 3\n")]); check("2 occurrences refused", False)
except PatchError as e:
    check("2 occurrences refused", "2 times" in str(e))

# 4. THE TRAP: a replacement that would not compile is refused BEFORE writing
before = io.open(py, encoding="utf-8").read()
try:
    patch(py, [('return "a\\nb"', 'return "a\nb"')])   # real newline inside a string literal
    check("non-compiling result refused", False)
except PatchError as e:
    check("non-compiling result refused", "would not compile" in str(e), str(e)[:80])
check("file untouched after compile refusal", io.open(py, encoding="utf-8").read() == before)

# 5. dry run writes nothing but reports the change
r = patch(py, [("def f():", "def g():")], dry_run=True)
check("dry run reports change, writes nothing", r["changed"] and r["dry_run"] and "def f():" in io.open(py, encoding="utf-8").read())

# 6. non-.py files skip compile
txt = os.path.join(tmp, "notes.md")
io.open(txt, "w", encoding="utf-8", newline="\n").write("hello\n")
r = patch(txt, [("hello", "bye")])
check("non-py patched without compile", r["changed"] and io.open(txt, encoding="utf-8").read() == "bye\n")

# 7. pairs apply in order, each against the updated text
check("ordered pairs", apply_to_text("a b c", [("a", "x"), ("x b", "y")]) == "y c")

# 8. CLI with a JSON spec: escapes are unambiguous
spec = os.path.join(tmp, "spec.json")
io.open(spec, "w", encoding="utf-8").write(json.dumps([{"old": "X = 2\nX = 2\n", "new": "X = 4\n"}]))
p = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "_patch.py"), py, spec], capture_output=True, text=True)
check("CLI applies a JSON spec", p.returncode == 0 and '"changed": true' in p.stdout, p.stdout.strip()[:100] + p.stderr.strip()[:100])
io.open(spec, "w", encoding="utf-8").write(json.dumps([{"old": "zzz", "new": "q"}]))
p = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "_patch.py"), py, spec, "--check"], capture_output=True, text=True)
check("CLI refusal exits 1 with REFUSED", p.returncode == 1 and "REFUSED" in p.stdout)

# 9. missing file
try:
    patch(os.path.join(tmp, "missing.py"), [("a", "b")]); check("missing file refused", False)
except PatchError:
    check("missing file refused", True)

n_fail = sum(1 for _, ok, _ in RESULTS if not ok)
print("\n%d/%d passed" % (len(RESULTS) - n_fail, len(RESULTS)))
sys.exit(1 if n_fail else 0)
