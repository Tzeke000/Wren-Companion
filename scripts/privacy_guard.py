"""scripts/privacy_guard.py — block private info and photos from ever reaching GitHub (2026-10-07).

Zeke, 2026-10-07: "make sure none of that info can be pushed to GitHub again" (and "get rid of any
photos"). The repo is PUBLIC. This guard runs as a git hook in two places:

    pre-commit  — scans what is STAGED (file names + added lines + commit identity)
    pre-push    — scans EVERY commit about to leave (names, added lines, messages, author/committer)

It blocks:
  1. Any pattern in the git-ignored list config/privacy_guard.local.txt (one Python regex per line,
     '#' comments). That list holds the actual private strings (names, IDs, addresses), which is
     exactly why it lives outside git. NO LIST => FAIL CLOSED (nothing commits or pushes until it
     exists) — a guard that silently passes is worse than none.
  2. Photos / images anywhere except the allow-listed app icon folder.
  3. Private paths (notes/, state/, memory/, faces/, profiles/, *.local.*, config/private*).
  4. Generic leaks that need no list: real-looking MAC addresses, tailnet (100.64/10) addresses,
     non-noreply author/committer emails.

Findings print the file/commit and the RULE that fired, with the match MASKED (the guard never
echoes the secret it caught). Install / repair the hooks:  python scripts/privacy_guard.py install
Exit 0 = clean, 1 = blocked, 2 = guard misconfigured (fail closed).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                           encoding="utf-8").stdout.strip() or Path(__file__).resolve().parents[1])
LIST = ROOT / "config" / "privacy_guard.local.txt"
ZERO = "0" * 40

IMAGE_RE = re.compile(r"\.(jpe?g|png|webp|gif|bmp|heic|heif|tiff?|raw|dng|cr2|nef)$", re.I)
IMAGE_ALLOW = re.compile(r"^apps/ava-control/src-tauri/icons/")
PRIVATE_PATH_RE = re.compile(
    r"(^|/)(notes|state|memory|faces|profiles)/|(^|/)[^/]*\.local\.[^/]+$|^config/private", re.I)
PRIVATE_PATH_ALLOW = re.compile(r"^(state/games/|art/notes/)")  # tracked on purpose: game + technique notes
GENERIC = [
    ("mac-address", re.compile(
        r"\b(?!(?:ff[-:]){5}ff\b|(?:aa[-:]bb[-:]cc[-:]dd[-:]ee[-:]ff)\b|(?:00[-:]){5}00\b)"
        r"(?:[0-9a-f]{2}[-:]){5}[0-9a-f]{2}\b", re.I)),
    ("tailnet-ip", re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b(?<!100\.64\.0\.1)")),
]
EMAIL_OK = re.compile(r"(@users\.noreply\.github\.com|noreply@anthropic\.com)$", re.I)


def _git(*args: str, inp: str | None = None) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", input=inp, cwd=str(ROOT)).stdout


def _mask(s: str) -> str:
    s = s.strip()
    return (s[:1] + "***" + s[-1:]) if len(s) > 4 else "***"


def load_list() -> list[re.Pattern]:
    if not LIST.is_file():
        print(f"privacy_guard: {LIST} is MISSING — failing closed. Create it (one regex per line) "
              f"before committing/pushing.", file=sys.stderr)
        sys.exit(2)
    pats = []
    for i, line in enumerate(LIST.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            pats.append(re.compile(line, re.I))
        except re.error as e:
            print(f"privacy_guard: bad regex on line {i} of the list ({e}) — failing closed.", file=sys.stderr)
            sys.exit(2)
    if not pats:
        print("privacy_guard: the pattern list is EMPTY — failing closed.", file=sys.stderr)
        sys.exit(2)
    return pats


def scan_text(text: str, pats: list[re.Pattern]) -> list[str]:
    hits = []
    for i, p in enumerate(pats, 1):
        m = p.search(text)
        if m:
            hits.append(f"private-list rule #{i} ({_mask(m.group(0))})")
    for name, p in GENERIC:
        m = p.search(text)
        if m:
            hits.append(f"{name} ({_mask(m.group(0))})")
    return hits


def scan_path(path: str) -> list[str]:
    out = []
    if IMAGE_RE.search(path) and not IMAGE_ALLOW.search(path):
        out.append("image/photo file")
    if PRIVATE_PATH_RE.search(path) and not PRIVATE_PATH_ALLOW.search(path):
        out.append("private path")
    return out


def check_identity(label: str, name: str, email: str, pats) -> list[str]:
    out = []
    if email and not EMAIL_OK.search(email):
        out.append(f"{label} email is not a GitHub noreply address ({_mask(email)})")
    out += [f"{label}: {h}" for h in scan_text(name, pats)]
    return out


def pre_commit() -> int:
    pats = load_list()
    problems: list[str] = []
    for path in [p for p in _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0") if p]:
        for h in scan_path(path) + scan_text(path, pats):
            problems.append(f"{path}: {h}")
    cur = None  # added-line hits, attributed per file
    for l in _git("diff", "--cached", "-U0", "--no-color", "--diff-filter=ACMR").splitlines():
        if l.startswith("+++ "):
            cur = l[6:] if l.startswith("+++ b/") else l[4:]
        elif l.startswith("+") and cur:
            for h in scan_text(l[1:], pats):
                problems.append(f"{cur}: added line - {h}")
    name =_git("config", "user.name").strip()
    email = _git("config", "user.email").strip()
    problems += check_identity("committer", name, email, pats)
    return report(problems, "commit")


def commits_for(local_sha: str, remote_sha: str) -> list[str]:
    if local_sha == ZERO:
        return []                       # deleting a remote branch — nothing leaves
    rng = [local_sha, "--not", "--remotes"] if remote_sha == ZERO else [f"{remote_sha}..{local_sha}"]
    return [c for c in _git("rev-list", *rng).split() if c]


def scan_commit(c: str, pats) -> list[str]:
    out = []
    meta = _git("show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce%x00%B", c).split("\0")
    if len(meta) >= 5:
        out += check_identity("author", meta[0], meta[1], pats)
        out += check_identity("committer", meta[2], meta[3], pats)
        out += [f"message: {h}" for h in scan_text(meta[4], pats)]
    for path in [p for p in _git("show", "--format=", "--name-only", "--diff-filter=ACMR", "-z", c)
                 .split("\0") if p.strip()]:
        out += [f"{path.strip()}: {h}" for h in scan_path(path.strip()) + scan_text(path, pats)]
    cur = None
    for l in _git("show", "--format=", "-U0", "--no-color", "--diff-filter=ACMR", c).splitlines():
        if l.startswith("+++ "):
            cur = l[6:] if l.startswith("+++ b/") else l[4:]
        elif l.startswith("+") and cur:
            out += [f"{cur}: added line - {h}" for h in scan_text(l[1:], pats)]
    return [f"{c[:9]} {p}" for p in out]


def pre_push() -> int:
    pats = load_list()
    problems: list[str] = []
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        _lref, lsha, _rref, rsha = parts
        for c in commits_for(lsha, rsha):
            problems += scan_commit(c, pats)
    return report(problems, "push")


def report(problems: list[str], what: str) -> int:
    if not problems:
        return 0
    print(f"\nprivacy_guard BLOCKED this {what}: private info or a photo would reach the PUBLIC repo:",
          file=sys.stderr)
    for p in problems[:40]:
        print("   - " + p, file=sys.stderr)
    if len(problems) > 40:
        print(f"   ... and {len(problems) - 40} more", file=sys.stderr)
    print("Move the detail into a *.local.* file / config/private.local.json / state, then try again.\n",
          file=sys.stderr)
    return 1


HOOK = """#!/bin/sh
# privacy guard (2026-10-07) — see scripts/privacy_guard.py. Fails closed.
top="$(git rev-parse --show-toplevel)"
py="$top/.venv/Scripts/python.exe"; [ -x "$py" ] || py="$(command -v python3 || command -v python)"
"$py" "$top/scripts/privacy_guard.py" {mode} || exit 1
{chain}
"""


def install() -> int:
    hooks = Path(_git("rev-parse", "--git-path", "hooks").strip())
    if not hooks.is_absolute():
        hooks = ROOT / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    chain_pc = ('exec "$py" "$top/scripts/precommit_pycompile.py"'
                if (ROOT / "scripts" / "precommit_pycompile.py").is_file() else "exit 0")
    for mode, chain in (("pre-commit", chain_pc), ("pre-push", "exit 0")):
        p = hooks / mode
        p.write_text(HOOK.format(mode=mode, chain=chain), encoding="utf-8", newline="\n")
        try:
            os.chmod(p, 0o755)
        except OSError:
            pass
        print("installed", p)
    load_list()  # prove the list exists now, not at the first commit
    return 0


if __name__ == "__main__":
    mode = (sys.argv[1] if len(sys.argv) > 1 else "pre-commit").lower()
    sys.exit({"pre-commit": pre_commit, "pre-push": pre_push, "install": install}.get(mode, pre_commit)())
