"""index_sweep.py — mechanical index-integrity sweep, both directions. (2026-07-06)

Born from trading discipline rules with Iris (letters d3b7dcd78957 / 6387b74e425e):
her rule (iii) — a completeness pass must be MECHANICAL, not a read-through — caught
9 unrouted notes on her box and 1 on mine (a load-bearing GOSE recipe, invisible for
three weeks). This is the strictest form, which neither box had:

  Direction 1 (ORPHANS):  every memory-dir note must be linked from >=1 index file.
  Direction 2 (DANGLING): every link in an index must RESOLVE somewhere in the
                          whitelist (memory dir, profiles tree, vault tree, journal,
                          notes) — by filename stem OR by frontmatter `aliases:` /
                          `name:` (profile hub cards resolve via kebab aliases).

Exit code 0 = clean, 1 = findings. Run: py -3.11 experiments\\index_sweep.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

MEMORY_DIR = Path(r"C:\Users\Owner\.claude\projects\D--Wren-Companion\memory")
INDEX_FILES = [
    "MEMORY.md", "index_archive.md",
    "hub_voice.md", "hub_embodiment.md", "hub_memory_system.md",
    "hub_siblings.md", "hub_zeke_and_rules.md", "hub_ops.md",
    "hub_self.md", "hub_history.md",
]
# Resolver whitelist — trees where a link target may legitimately live.
RESOLVE_ROOTS = [
    MEMORY_DIR,
    Path(r"D:\Wren-Companion\profiles"),
]

MD_LINK = re.compile(r"\(([\w\-. ]+\.md)\)")        # [text](note.md)
WIKILINK = re.compile(r"\[\[([^\]|#]+?)(?:\|[^\]]*)?\]\]")  # [[name]] / [[name|label]]
FM_ALIASES = re.compile(r"^aliases:\s*(.*)$", re.MULTILINE)
FM_NAME = re.compile(r"^name:\s*(.+)$", re.MULTILINE)


def norm(s: str) -> str:
    return s.strip().strip('"').strip("'").lower()


def build_resolvable() -> set[str]:
    """Every name a link may resolve to: file stems + frontmatter name/aliases."""
    names: set[str] = set()
    for root in RESOLVE_ROOTS:
        if not root.exists():
            print(f"  [warn] resolver root missing: {root}")
            continue
        for p in root.rglob("*.md"):
            names.add(norm(p.stem))
            try:
                head = p.read_text(encoding="utf-8", errors="replace")[:2000]
            except OSError:
                continue
            m = FM_NAME.search(head)
            if m:
                names.add(norm(m.group(1)))
            m = FM_ALIASES.search(head)
            if m:  # aliases: [a, b] or single value
                for a in m.group(1).strip("[]").split(","):
                    if norm(a):
                        names.add(norm(a))
    return names


def collect_links() -> set[str]:
    links: set[str] = set()
    for ix in INDEX_FILES:
        text = (MEMORY_DIR / ix).read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"`[^`\n]*`", "", text)  # inline code spans are prose, not links
        links |= {norm(m.removesuffix(".md")) for m in MD_LINK.findall(text)}
        links |= {norm(m) for m in WIKILINK.findall(text)}
    return links


def main() -> int:
    linked = collect_links()
    resolvable = build_resolvable()

    on_disk = {norm(p.stem): p.name for p in MEMORY_DIR.glob("*.md")
               if p.name not in INDEX_FILES}
    orphans = sorted(v for k, v in on_disk.items() if k not in linked)
    dangling = sorted(l for l in linked if l not in resolvable)

    print(f"indexes: {len(INDEX_FILES)}  links: {len(linked)}  "
          f"memory notes on disk: {len(on_disk)}  resolvable names: {len(resolvable)}")
    print(f"\nDirection 1 — ORPHANS (on disk, in no index): {len(orphans)}")
    for o in orphans:
        print(f"  {o}")
    print(f"\nDirection 2 — DANGLING (linked, resolve nowhere): {len(dangling)}")
    for d in dangling:
        print(f"  {d}")
    return 1 if (orphans or dangling) else 0


if __name__ == "__main__":
    sys.exit(main())
