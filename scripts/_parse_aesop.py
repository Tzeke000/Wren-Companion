"""scripts/_parse_aesop.py — one-shot parser for The Aesop for Children (PG #19994).

Reads curriculum/foundation/_raw_aesop_for_children.txt, splits into individual
fables, writes each as curriculum/foundation/<slug>.txt with a YAML-style metadata
header, generates _index.json.

Picks the first 25 fables (the work order's foundation tier size) from the
beginning of the book — the "PRIMER" section is well-curated for kids.

Run once after the raw file is downloaded; not committed to the registry.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "curriculum" / "foundation" / "_raw_aesop_for_children.txt"
OUT_DIR = ROOT / "curriculum" / "foundation"
TARGET_COUNT = 25

# Themes per fable — short tag list, hand-curated from the moral. Used by
# brain/curriculum.py for filtering / ordering.
FABLE_THEMES = {
    "the_wolf_and_the_kid": ["focus", "purpose", "distraction"],
    "the_tortoise_and_the_ducks": ["humility", "vanity", "consequences"],
    "the_young_crab_and_his_mother": ["lead_by_example", "hypocrisy"],
    "the_frogs_and_the_ox": ["envy", "knowing_your_size", "pride"],
    "belling_the_cat": ["plans_vs_action", "courage", "responsibility"],
    "the_eagle_and_the_jackdaw": ["overreach", "self_knowledge"],
    "the_boy_and_the_filberts": ["greed", "moderation", "patience"],
    "hercules_and_the_wagoner": ["self_reliance", "asking_for_help_after_effort"],
    "the_kid_and_the_wolf": ["distance_lends_courage", "boasting"],
    "the_town_mouse_and_the_country_mouse": ["contentment", "values", "simple_life"],
    "the_fox_and_the_grapes": ["sour_grapes", "self_deception"],
    "the_bundle_of_sticks": ["unity", "strength_in_numbers", "family"],
    "the_wolf_and_the_crane": ["ingratitude", "trusting_the_untrustworthy"],
    "the_ass_and_his_driver": ["stubbornness", "consequences"],
    "the_oxen_and_the_wheels": ["loud_complaints_smallest_problems"],
    "the_lion_and_the_mouse": ["kindness_returns", "underestimating_others"],
    "the_shepherd_boy_and_the_wolf": ["honesty", "credibility", "lying"],
    "the_gnat_and_the_bull": ["importance", "self_inflation"],
    "the_plane_tree": ["taking_for_granted", "gratitude"],
    "the_farmer_and_the_stork": ["company_we_keep", "guilt_by_association"],
    "the_sheep_and_the_pig": ["different_concerns", "perspective"],
    "the_travelers_and_the_purse": ["shared_burden", "shared_reward"],
    "the_lion_and_the_ass": ["dignity", "ignoring_insults"],
    "the_frogs_who_wished_for_a_king": ["careful_what_you_wish_for", "leadership"],
    "the_owl_and_the_grasshopper": ["flattery", "manipulation"],
}


def slugify(title: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9 ]", "", title.replace("Æ", "ae").replace("æ", "ae"))
    s = re.sub(r"\s+", "_", s.strip().lower())
    return s


def is_title_line(line: str) -> bool:
    """All-caps lines >= 5 chars, no leading whitespace, that aren't running
    headers or boilerplate."""
    s = line.rstrip()
    if not s or s != s.upper():
        return False
    if len(s) < 5:
        return False
    if s.startswith(" "):
        return False
    # Strip allowed chars and check it's non-empty alphanum
    cleaned = re.sub(r"[^A-Z]", "", s)
    if len(cleaned) < 4:
        return False
    # Skip running headers
    skip = {
        "THE ÆSOP FOR CHILDREN",
        "A LIST OF THE FABLES",
        "ÆSOP FOR CHILDREN",
        "ILLUSTRATIONS",
        "FABLES",
    }
    if s in skip or s.replace("Æ", "AE") in {x.replace("Æ", "AE") for x in skip}:
        return False
    return True


def parse_fables(text: str) -> list[dict]:
    """Walk the file, find titles, accumulate bodies until next title."""
    lines = text.split("\n")
    fables: list[dict] = []
    current_title = None
    current_body: list[str] = []
    started = False  # Skip front matter; start after first body fable.

    for i, line in enumerate(lines):
        # Once we've seen "THE WOLF AND THE KID" (the first body fable), start collecting.
        if not started:
            if line.rstrip() == "THE WOLF AND THE KID":
                started = True
                current_title = line.rstrip()
                current_body = []
            continue

        if is_title_line(line):
            # Flush previous fable
            if current_title and current_body:
                fables.append({"title": current_title, "body": "\n".join(current_body).strip()})
            current_title = line.rstrip()
            current_body = []
        else:
            current_body.append(line.rstrip())

    # Flush last fable
    if current_title and current_body:
        fables.append({"title": current_title, "body": "\n".join(current_body).strip()})

    # Clean up bodies: strip [Illustration] lines, collapse blank lines.
    for f in fables:
        body = f["body"]
        body = re.sub(r"\n\[Illustration\][^\n]*\n", "\n", body)
        body = re.sub(r"\n{3,}", "\n\n", body)
        f["body"] = body.strip()

    # Extract moral (italicized in raw text as _..._).
    for f in fables:
        moral_match = re.search(r"_([^_\n]{10,200})_", f["body"])
        f["moral"] = moral_match.group(1).strip().rstrip(".") if moral_match else None

    return fables


def write_fable_file(fable: dict, slug: str) -> Path:
    title = fable["title"].title().replace("Æ", "Ae").replace("æ", "ae")
    themes = FABLE_THEMES.get(slug, ["unspecified"])
    moral = fable.get("moral") or ""
    out = OUT_DIR / f"{slug}.txt"
    header = f"""---
title: {title}
source: Project Gutenberg, The Aesop for Children (1919, Milo Winter ed.)
source_url: https://www.gutenberg.org/ebooks/19994
themes: {", ".join(themes)}
moral: {moral}
reading_status: unread
lessons_extracted: []
---

{fable["body"]}
"""
    out.write_text(header, encoding="utf-8")
    return out


def main() -> int:
    if not RAW.is_file():
        print(f"missing raw file: {RAW}")
        return 1
    raw = RAW.read_text(encoding="utf-8")
    fables = parse_fables(raw)
    print(f"parsed {len(fables)} fables")

    # Pick first TARGET_COUNT (25)
    selected = fables[:TARGET_COUNT]
    if len(selected) < TARGET_COUNT:
        print(f"WARN: only {len(selected)} fables found, target was {TARGET_COUNT}")

    index = []
    for f in selected:
        slug = slugify(f["title"])
        path = write_fable_file(f, slug)
        index.append({
            "slug": slug,
            "title": f["title"].title().replace("Æ", "Ae").replace("æ", "ae"),
            "themes": FABLE_THEMES.get(slug, ["unspecified"]),
            "moral": f.get("moral") or "",
            "filename": path.name,
            "reading_status": "unread",
            "lessons_extracted": [],
        })
        print(f"  wrote {path.name}  ({len(f['body'])} chars)")

    # Write index
    idx_path = OUT_DIR / "_index.json"
    idx_path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {idx_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
