"""_patch — safe exact-string patching for this repo (born 2026-09-30 after the escape trap
bit three times in one day).

The failure it prevents: a Python-in-bash patch script wrote a replacement in which "\\n" had
already turned into a real newline, and the helper WROTE the file before compiling it — so
brain/leisure.py sat broken until git restored it. Rules here:
  1. every `old` must occur EXACTLY once (0 or 2+ → refuse, touch nothing);
  2. for .py files the RESULT is compiled BEFORE anything is written (SyntaxError → refuse);
  3. writes are atomic (tmp + os.replace), newline="\\n", UTF-8, no BOM;
  4. --check / dry_run=True reports what would change and writes nothing.

Use from Python:
    from scripts._patch import patch
    patch("brain/x.py", [(old, new), ...])              # raises PatchError on any refusal

Use from a shell with a JSON spec (JSON escapes are unambiguous: "\\n" is a newline, "\\\\n" is
the two characters backslash-n — the thing you want when inserting an escape into source):
    .venv\\Scripts\\python.exe scripts\\_patch.py brain\\x.py spec.json [--check]
    spec.json = [{"old": "...", "new": "..."}, ...]
"""
from __future__ import annotations

import io
import json
import os
import sys
from typing import Iterable, Sequence


class PatchError(RuntimeError):
    pass


def _read(path: str) -> str:
    with io.open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _write_atomic(path: str, text: str) -> None:
    tmp = path + ".patch.tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def apply_to_text(text: str, pairs: Sequence[tuple[str, str]]) -> str:
    """Pure: apply pairs in order; refuse unless each old occurs exactly once at its turn."""
    out = text
    for i, (old, new) in enumerate(pairs):
        if not old:
            raise PatchError("pair %d: empty old string" % i)
        n = out.count(old)
        if n != 1:
            raise PatchError("pair %d: old string occurs %d times (need exactly 1): %r" % (i, n, old[:80]))
        out = out.replace(old, new, 1)
    return out


def patch(path: str, pairs: Iterable[tuple[str, str]], *, dry_run: bool = False, compile_py: bool = True) -> dict:
    """Patch `path` in place. Returns {path, changed, bytes_before, bytes_after, dry_run}.
    Raises PatchError without touching the file on: missing file, old-string count != 1,
    or (for .py) a SyntaxError in the RESULT."""
    pairs = list(pairs)
    if not os.path.isfile(path):
        raise PatchError("no such file: %s" % path)
    before = _read(path)
    after = apply_to_text(before, pairs)
    if compile_py and path.lower().endswith(".py"):
        try:
            compile(after, path, "exec")
        except SyntaxError as e:
            raise PatchError("result would not compile (%s line %s: %s) — file untouched" % (path, e.lineno, e.msg))
    changed = after != before
    if changed and not dry_run:
        _write_atomic(path, after)
    return {"path": path, "changed": changed, "bytes_before": len(before.encode("utf-8")),
            "bytes_after": len(after.encode("utf-8")), "dry_run": dry_run, "pairs": len(pairs)}


def _main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}
    if len(args) != 2:
        print(__doc__)
        return 2
    path, spec = args
    with io.open(spec, "r", encoding="utf-8") as f:
        rows = json.load(f)
    pairs = [(str(r["old"]), str(r["new"])) for r in rows]
    try:
        r = patch(path, pairs, dry_run=("--check" in flags))
    except PatchError as e:
        print("REFUSED:", e)
        return 1
    print(json.dumps(r))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
