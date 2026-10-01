"""scripts/precommit_pycompile.py — round-2 fix 3.13 (2026-10-01): no SyntaxError ever reaches a commit.

Compiles the STAGED content (git show :path, not the worktree) of every added/changed .py file.
Installed as .git/hooks/pre-commit by scripts/install_git_hooks.sh; also runnable by hand:

    .venv\\Scripts\\python.exe scripts\\precommit_pycompile.py
"""
from __future__ import annotations

import subprocess
import sys


def _staged_py() -> list[str]:
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "--", "*.py"],
                         capture_output=True, text=True, check=False)
    return [l.strip() for l in out.stdout.splitlines() if l.strip()]


def main() -> int:
    bad = 0
    files = _staged_py()
    for path in files:
        src = subprocess.run(["git", "show", f":{path}"], capture_output=True, check=False).stdout
        try:
            compile(src, path, "exec")
        except SyntaxError as e:
            bad += 1
            print(f"[pre-commit] SyntaxError in STAGED {path}: line {e.lineno}: {e.msg}", file=sys.stderr)
    if bad:
        print(f"[pre-commit] refusing commit: {bad} staged .py file(s) do not compile", file=sys.stderr)
        return 1
    if files:
        print(f"[pre-commit] {len(files)} staged .py file(s) compile", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
