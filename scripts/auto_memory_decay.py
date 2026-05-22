#!/usr/bin/env python3
"""
scripts/auto_memory_decay.py

Earned-memory machinery for the auto-memory .md directory at
`C:/Users/Owner/.claude/projects/D--Wren-Companion/memory/`, mirroring
brain/iris_human_memory.py's design at the file-level rather than the
JSONL-entry level.

Why this exists: brain/iris_human_memory.py implements Ebbinghaus decay +
Bjork retrieval strengthening for state/iris_memory.jsonl entries (the
semantic-fact log). But the .md auto-memory directory has no "earned"
tracking — files accumulate. This script extends the same principle:
wiki-link backrefs from recent files act as the retrieval signal, file
age + inactivity act as the decay signal.

Cognitive-science basis (per brain/iris_human_memory.py docstring):
  - Ebbinghaus forgetting curve — exponential decay without rehearsal
  - Bjork's New Theory of Disuse — retrieval bumps storage strength
  - Active systems consolidation — periodic "replay" maintains durability
  - Flashbulb pathway — encoding-time importance can persist

The .md-side equivalent (this script):
  - decay_factor = exp(-elapsed_days / τ) where τ=30 days, elapsed = days since file mtime
  - backref boost: each recent (<14d) [[wiki-link]] backref adds +0.05, capped at +0.5
  - base importance from frontmatter `importance:` flag (permanent | high | default | low)
  - permanent files are exempt from decay (score=1.0 always)
  - shadow meta at state/auto_memory_meta.json for manual flag overrides

Usage:
    python scripts/auto_memory_decay.py summary
    python scripts/auto_memory_decay.py report [-n 30]
    python scripts/auto_memory_decay.py list-candidates [--threshold 0.15] [-n 20]
    python scripts/auto_memory_decay.py mark <stem> <permanent|high|default|low>
"""
from __future__ import annotations

import argparse
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any


# --- Configuration ---

MEMORY_DIR = Path("C:/Users/Owner/.claude/projects/D--Wren-Companion/memory")
META_PATH = Path(__file__).resolve().parent.parent / "state" / "auto_memory_meta.json"

# Ebbinghaus decay constant — half-life of base importance without retrieval.
# 30 days matches brain/iris_human_memory.py's _DECAY_TAU_DAYS.
DECAY_TAU_DAYS = 30.0

# Per-backref boost from a recent referring file. Matches the +0.05 in
# brain/iris_human_memory.py's _RETRIEVAL_BOOST_PER_HIT.
RECENT_BACKREF_BOOST = 0.05

# Cap on cumulative backref boost — files with 10+ recent backrefs are
# already "highly earned" regardless of additional links.
BACKREF_BOOST_CAP = 0.5

# A backref counts as "recent" if the REFERRING file was modified in the
# last 14 days. Old referrers count toward total_backref_count but not
# toward the boost — stale wiki-links shouldn't keep a file alive forever.
RECENT_THRESHOLD_DAYS = 14.0

# Default importance flag if neither shadow-meta nor frontmatter specifies.
DEFAULT_IMPORTANCE = "default"

# Base importance per flag.
BASE_IMPORTANCE = {
    "permanent": 1.0,  # exempt from decay
    "high":      0.75,
    "default":   0.50,
    "low":       0.25,
}

WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


# --- Helpers ---

def slug_variants(name: str) -> set[str]:
    """Given a wiki-link name or file stem, return possible matching variants.

    Wiki-links in the corpus use both snake_case (e.g. [[wren_silent_override_2026-05-11]])
    and kebab-case (e.g. [[wren-silent-override-2026-05-11]]). File stems use
    snake_case. Frontmatter `name:` uses kebab-case. We canonicalize all
    three so a wiki-link to either form matches the right file.
    """
    name = name.strip().lower()
    return {name, name.replace("-", "_"), name.replace("_", "-")}


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Quick + dirty YAML parse — just extract known top-level scalar fields.

    Not a full YAML parser; only handles `key: value` lines at column 0.
    Nested structures (like `metadata: {type: foo}` with indented children)
    are skipped — we only need top-level `name:` and `importance:`.

    Bug fixed 2026-05-21: previously checked `startswith(" ")` AFTER strip,
    which made the indent-guard dead code and inadvertently promoted
    nested `importance:` values to top-level. Now check indent BEFORE strip.
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fields = {}
    for raw_line in m.group(1).splitlines():
        # Indent-guard MUST run before strip — that's the bug fix.
        if raw_line.startswith((" ", "\t")):
            continue
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        v = v.strip().strip("\"'")
        if v:
            fields[k.strip()] = v
    return fields


def extract_wikilinks(text: str) -> list[str]:
    return [m.group(1) for m in WIKILINK_RE.finditer(text)]


# --- Metadata persistence ---

def load_meta() -> dict[str, dict[str, Any]]:
    if not META_PATH.is_file():
        return {}
    try:
        d = json.loads(META_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_meta(meta: dict[str, dict[str, Any]]) -> None:
    META_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = META_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(META_PATH)


# --- Corpus scan + backref graph ---

def build_corpus() -> dict[str, dict[str, Any]]:
    """Scan all .md files in the memory dir, parse frontmatter, extract wiki-links."""
    if not MEMORY_DIR.is_dir():
        raise FileNotFoundError(f"Memory dir not found: {MEMORY_DIR}")
    now = time.time()
    files = {}
    for path in sorted(MEMORY_DIR.glob("*.md")):
        if path.name == "MEMORY.md":
            continue  # index file, not a memory itself
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        frontmatter = parse_frontmatter(text)
        wikilinks = extract_wikilinks(text)
        st = path.stat()
        files[path.stem] = {
            "path": str(path),
            "stem": path.stem,
            "frontmatter": frontmatter,
            "wikilinks": wikilinks,
            "mtime": st.st_mtime,
            "mtime_iso": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "size": st.st_size,
            "age_days": (now - st.st_mtime) / 86400.0,
        }
    return files


def build_backref_graph(corpus: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """For each file, build a list of {referrer, referrer_mtime, referrer_age_days}."""
    backrefs: dict[str, list[dict[str, Any]]] = {stem: [] for stem in corpus}
    # Map every possible slug variant → canonical file stem.
    slug_to_stem: dict[str, str] = {}
    for stem, info in corpus.items():
        for variant in slug_variants(stem):
            slug_to_stem.setdefault(variant, stem)
        # Also map the frontmatter `name:` (often the kebab-case form).
        name = info["frontmatter"].get("name", "")
        if name:
            for variant in slug_variants(name):
                slug_to_stem.setdefault(variant, stem)

    for referrer_stem, info in corpus.items():
        seen_targets: set[str] = set()
        for link in info["wikilinks"]:
            target = None
            for variant in slug_variants(link):
                if variant in slug_to_stem:
                    target = slug_to_stem[variant]
                    break
            if target is None or target == referrer_stem:
                continue
            if target in seen_targets:
                continue  # don't double-count same-file-twice references
            seen_targets.add(target)
            backrefs[target].append({
                "referrer": referrer_stem,
                "referrer_mtime": info["mtime"],
                "referrer_age_days": info["age_days"],
            })
    return backrefs


# --- Effective importance ---

def effective_importance(
    stem: str,
    info: dict[str, Any],
    file_backrefs: list[dict[str, Any]],
    meta_entry: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    """Compute the earned-score for a file. Returns (score, details).

    Score formula:
        if importance_flag == permanent: 1.0
        else: base * exp(-age_days/τ) + min(cap, recent_backref_count * boost)

    Bounded to [0, 1].
    """
    flag = (meta_entry.get("importance_flag")
            or info["frontmatter"].get("importance")
            or DEFAULT_IMPORTANCE)

    if flag == "permanent":
        return 1.0, {
            "flag": flag,
            "reason": "permanent_exempt",
            "base": 1.0,
            "decay_factor": 1.0,
            "boost": 0.0,
        }

    base = BASE_IMPORTANCE.get(flag, BASE_IMPORTANCE[DEFAULT_IMPORTANCE])
    age_days = info["age_days"]
    decay_factor = math.exp(-age_days / DECAY_TAU_DAYS)

    recent_count = sum(1 for b in file_backrefs if b["referrer_age_days"] < RECENT_THRESHOLD_DAYS)
    total_count = len(file_backrefs)
    boost = min(BACKREF_BOOST_CAP, recent_count * RECENT_BACKREF_BOOST)

    score = base * decay_factor + boost
    score = max(0.0, min(1.0, score))

    return score, {
        "flag": flag,
        "base": base,
        "age_days": round(age_days, 2),
        "decay_factor": round(decay_factor, 4),
        "total_backrefs": total_count,
        "recent_backrefs": recent_count,
        "boost": round(boost, 3),
    }


# --- Reporting ---

def generate_report(
    corpus: dict[str, dict[str, Any]],
    backrefs: dict[str, list[dict[str, Any]]],
    meta: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for stem, info in corpus.items():
        score, details = effective_importance(
            stem, info, backrefs[stem], meta.get(stem, {})
        )
        rows.append({
            "stem": stem,
            "score": round(score, 3),
            "age_days": round(info["age_days"], 1),
            "size_kb": round(info["size"] / 1024.0, 2),
            "backref_count": details.get("total_backrefs", 0),
            "recent_backref_count": details.get("recent_backrefs", 0),
            "flag": details["flag"],
        })
    rows.sort(key=lambda r: r["score"])
    return rows


def cmd_summary(corpus, backrefs, meta) -> None:
    rows = generate_report(corpus, backrefs, meta)
    total = len(rows)
    mean = sum(r["score"] for r in rows) / max(1, total)
    decay_candidates = sum(1 for r in rows if r["score"] < 0.15)
    highly_earned = sum(1 for r in rows if r["score"] > 0.70)
    permanent = sum(1 for r in rows if r["flag"] == "permanent")
    high = sum(1 for r in rows if r["flag"] == "high")
    low = sum(1 for r in rows if r["flag"] == "low")
    total_kb = sum(r["size_kb"] for r in rows)
    print(f"Auto-memory corpus summary ({MEMORY_DIR}):")
    print(f"  Total files: {total}")
    print(f"  Total size: {total_kb:.1f} KB")
    print(f"  Mean earned-score: {mean:.3f}")
    print(f"  Highly earned (>0.70): {highly_earned}")
    print(f"  Decay candidates (<0.15): {decay_candidates}")
    print(f"  Marked permanent: {permanent}")
    print(f"  Marked high: {high}")
    print(f"  Marked low: {low}")


def cmd_report(corpus, backrefs, meta, n: int) -> None:
    rows = generate_report(corpus, backrefs, meta)
    if n > 0:
        rows = rows[:n]
    print(f"{'score':>6} {'age_d':>6} {'size_kb':>8} {'all/rec':>9}  {'flag':<10}  file")
    print(f"{'-'*6:>6} {'-'*6:>6} {'-'*8:>8} {'-'*9:>9}  {'-'*10:<10}  ----")
    for r in rows:
        print(f"{r['score']:>6.3f} {r['age_days']:>6.1f} {r['size_kb']:>8.2f} "
              f"{r['backref_count']:>3}/{r['recent_backref_count']:<5}  "
              f"{r['flag']:<10}  {r['stem']}")


def cmd_list_candidates(corpus, backrefs, meta, threshold: float, n: int) -> None:
    rows = generate_report(corpus, backrefs, meta)
    candidates = [r for r in rows if r["score"] < threshold][:n]
    if not candidates:
        print(f"No candidates below threshold {threshold}.")
        return
    total_size = sum(r["size_kb"] for r in candidates)
    print(f"Decay candidates (score < {threshold}, top {n}):")
    print(f"Total size of candidate set: {total_size:.1f} KB")
    print()
    for r in candidates:
        print(f"  score={r['score']:.3f}  age={r['age_days']:.1f}d  "
              f"size={r['size_kb']:.2f}KB  backrefs={r['backref_count']}/"
              f"{r['recent_backref_count']}r  flag={r['flag']}  {r['stem']}")


def cmd_mark(corpus, meta, stem: str, flag: str) -> None:
    if stem not in corpus:
        # Try variants in case Zeke types kebab-case.
        for variant in slug_variants(stem):
            if variant in corpus:
                stem = variant
                break
        else:
            print(f"File not found in corpus: {stem}")
            print(f"(Looked in {MEMORY_DIR})")
            return
    if flag not in BASE_IMPORTANCE:
        print(f"Invalid flag: {flag}. Use: permanent | high | default | low")
        return
    meta.setdefault(stem, {})["importance_flag"] = flag
    meta[stem]["set_ts"] = time.time()
    meta[stem]["set_iso"] = datetime.now().isoformat(timespec="seconds")
    save_meta(meta)
    print(f"Marked {stem} as {flag}")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("summary", help="quick summary stats")

    p_report = sub.add_parser("report", help="all files sorted ascending by earned-score")
    p_report.add_argument("-n", type=int, default=0, help="limit to lowest N (0=all)")

    p_cand = sub.add_parser("list-candidates", help="files below threshold (decay candidates)")
    p_cand.add_argument("--threshold", type=float, default=0.15)
    p_cand.add_argument("-n", type=int, default=20)

    p_mark = sub.add_parser("mark", help="set importance flag (override frontmatter)")
    p_mark.add_argument("stem", help="file stem (with or without .md)")
    p_mark.add_argument("flag", choices=list(BASE_IMPORTANCE.keys()))

    args = parser.parse_args()

    if args.cmd == "mark":
        # Mark needs corpus only to validate stem; doesn't need backref graph.
        stem = args.stem.removesuffix(".md")
        corpus = build_corpus()
        meta = load_meta()
        cmd_mark(corpus, meta, stem, args.flag)
        return

    corpus = build_corpus()
    backrefs = build_backref_graph(corpus)
    meta = load_meta()

    if args.cmd == "summary":
        cmd_summary(corpus, backrefs, meta)
    elif args.cmd == "report":
        cmd_report(corpus, backrefs, meta, args.n)
    elif args.cmd == "list-candidates":
        cmd_list_candidates(corpus, backrefs, meta, args.threshold, args.n)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
