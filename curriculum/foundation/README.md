# Foundation Tier

Short fables and tales — Ava's first moral substrate.

## Source

All entries in this tier are from **Project Gutenberg**, public domain, no attribution required for reuse. The seed corpus is *The Aesop for Children* (1919, illustrated by Milo Winter, translation curated for child readers): https://www.gutenberg.org/ebooks/19994.

The 25 fables here were selected from the front of that book, where the editing is tightest and the morals are explicit. Each fable runs 200–2000 characters of body text plus a single-sentence moral.

## How entries were generated

`scripts/_parse_aesop.py` (one-shot, not a runtime tool) downloads the raw text, splits on title boundaries (all-caps titles), strips `[Illustration]` markers, extracts morals from `_…_` italic markers, slugifies titles, writes per-fable `.txt` files with metadata headers, and produces `_index.json`.

Re-run the parser if you want to refresh the corpus. The raw download (`_raw_aesop_for_children.txt`) is gitignored and regenerated on each run.

## Themes

Each entry has a hand-curated `themes` tag list in its metadata header. Themes are short keywords useful for filtering during consolidation (e.g., when Ava's been bored or frustrated, prefer entries with `contentment` or `humility` themes). The full list of themes used:

`focus`, `purpose`, `distraction`, `humility`, `vanity`, `consequences`, `lead_by_example`, `hypocrisy`, `envy`, `knowing_your_size`, `pride`, `plans_vs_action`, `courage`, `responsibility`, `overreach`, `self_knowledge`, `greed`, `moderation`, `patience`, `self_reliance`, `asking_for_help_after_effort`, `distance_lends_courage`, `boasting`, `contentment`, `values`, `simple_life`, `sour_grapes`, `self_deception`, `unity`, `strength_in_numbers`, `family`, `ingratitude`, `trusting_the_untrustworthy`, `stubbornness`, `loud_complaints_smallest_problems`, `kindness_returns`, `underestimating_others`, `honesty`, `credibility`, `lying`, `importance`, `self_inflation`, `taking_for_granted`, `gratitude`, `company_we_keep`, `guilt_by_association`, `different_concerns`, `perspective`, `shared_burden`, `shared_reward`, `dignity`, `ignoring_insults`, `careful_what_you_wish_for`, `leadership`, `flattery`, `manipulation`.

## Adding entries

To add additional fables or moral stories:

1. Add the `.txt` file with the metadata header format documented in `../README.md`.
2. Append the entry to `_index.json`.
3. (Optional) Add themes to the parser script's `FABLE_THEMES` dict if you re-run it.
