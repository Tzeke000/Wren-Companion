"""Restage VST2 .dll files that are stranded in Zeke's VST3-only scan folder.

Ableton scans "Vst 3" as VST3 ONLY, so any .dll sitting there is dead weight -
the plugin never loads. Two cases:

  MOVE  - the dll has no counterpart in the VST2 folder => the plugin is
          currently loading NOWHERE. Move it into the VST2 folder so Live
          finds it.
  DUP   - a file of the same name already exists in the VST2 folder (which IS
          scanned, so that copy is the one actually in use). The Vst 3 copy is
          redundant => remove it.

SAFETY: a name match is NOT proof of a content match. Before removing any dup
we compare size + SHA256. If they differ we touch NOTHING and report it - the
Vst 3 copy could be a NEWER build, and silently keeping the older one would be
a downgrade disguised as a cleanup.

Removal is a MOVE into the dated quarantine folder, not an unlink, so it stays
reversible. Zeke asked to "just delete them"; quarantine is functionally
identical from Ableton's point of view and costs nothing, so it is used here
and reported plainly.

Usage:  python vst_restage.py            (dry run)
        python vst_restage.py --apply
"""
import hashlib
import shutil
import sys
from datetime import date
from pathlib import Path

VST3 = Path(r'D:\Users\Owner\Documents\Ableton\Vst 3')
VST2 = Path(r'D:\Users\Owner\Documents\Ableton\Plugins music\VST2 Plugins')
QROOT = Path(r'D:\Users\Owner\Documents\Ableton') / ('_vst_quarantine_%s' % date.today().isoformat())
QDUP = QROOT / 'VST3_redundant_dlls'

APPLY = '--apply' in sys.argv


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    moves, dups, conflicts = [], [], []

    for p in sorted(VST3.glob('*.dll')):
        twin = VST2 / p.name
        if not twin.exists():
            moves.append(p)
            continue
        a, b = p.stat().st_size, twin.stat().st_size
        if a == b and sha256(p) == sha256(twin):
            dups.append((p, twin))
        else:
            conflicts.append((p, twin, a, b))

    print('=== MOVE (plugin currently loads nowhere) ===')
    for p in moves:
        print('   %-34s -> VST2 folder  (%.1f MB)' % (p.name, p.stat().st_size / 1024 / 1024))
    if not moves:
        print('   (none)')

    print('\n=== REDUNDANT (byte-identical twin already in VST2 folder) ===')
    for p, t in dups:
        print('   %-34s  identical, safe to remove' % p.name)
    if not dups:
        print('   (none)')

    print('\n=== CONFLICT - NOT TOUCHED (same name, DIFFERENT file) ===')
    for p, t, a, b in conflicts:
        print('   %-34s  Vst3=%.2f MB  VST2=%.2f MB  <-- decide by hand' % (p.name, a / 1024 / 1024, b / 1024 / 1024))
    if not conflicts:
        print('   (none)')

    if not APPLY:
        print('\nDRY RUN - nothing changed. Re-run with --apply.')
        return 0

    manifest = []
    for p in moves:
        dest = VST2 / p.name
        assert not dest.exists(), 'refusing to overwrite %s' % dest
        shutil.move(str(p), str(dest))
        manifest.append('%s\t%s' % (dest, p))
    for p, t in dups:
        QDUP.mkdir(parents=True, exist_ok=True)
        dest = QDUP / p.name
        if dest.exists():
            dest = QDUP / (p.stem + '__dup' + p.suffix)
        shutil.move(str(p), str(dest))
        manifest.append('%s\t%s' % (dest, p))

    if manifest:
        mf = QROOT / 'MANIFEST.tsv'
        QROOT.mkdir(parents=True, exist_ok=True)
        prev = mf.read_text(encoding='utf-8') if mf.exists() else '# manifest\n'
        mf.write_text(prev.rstrip('\n') + '\n' + '\n'.join(manifest) + '\n', encoding='utf-8')

    print('\nmoved %d plugin(s) into the VST2 folder.' % len(moves))
    print('quarantined %d redundant copy/copies.' % len(dups))
    print('left %d conflict(s) alone.' % len(conflicts))
    return 0


if __name__ == '__main__':
    sys.exit(main())
