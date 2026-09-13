"""Quarantine non-plugin junk out of Zeke's Ableton VST2/VST3 scan folders.

Ableton scans ONLY these two custom folders (confirmed from PluginScanner.txt):
  VST3: D:\\Users\\Owner\\Documents\\Ableton\\Vst 3
  VST2: D:\\Users\\Owner\\Documents\\Ableton\\Plugins music\\VST2 Plugins

This MOVES inert files (installers, archives, docs) into a dated quarantine
folder on the SAME volume, writes a manifest, and emits a restore script.
Nothing is deleted; nothing is copied to C: (C: is full).

HARD SAFETY RULES:
  - Only FILES are touched. Directories are never moved (may hold plugin data).
  - Only these extensions: .exe .zip .pdf .txt .rtf
  - NEVER touch .dll .vst3 .ini .gvi .gx99 or anything else.
  - NEVER touch anything whose name contains "serum" (case-insensitive):
    deleting/moving Serum 1 breaks old projects AND Serum 2 resolves the
    Serum 1 preset library THROUGH Serum 1.
  - NEVER touch an uninstaller (name starts with "uninstal").

Usage:  python vst_quarantine.py            (dry run)
        python vst_quarantine.py --apply    (do it)
"""
import os
import shutil
import sys
from datetime import date
from pathlib import Path

VST3 = Path(r'D:\Users\Owner\Documents\Ableton\Vst 3')
VST2 = Path(r'D:\Users\Owner\Documents\Ableton\Plugins music\VST2 Plugins')
QROOT = Path(r'D:\Users\Owner\Documents\Ableton') / ('_vst_quarantine_%s' % date.today().isoformat())

MOVE_EXT = {'.exe', '.zip', '.pdf', '.txt', '.rtf'}
NEVER_NAME_CONTAINS = ('serum',)
NEVER_NAME_STARTS = ('uninstal',)

APPLY = '--apply' in sys.argv


def should_move(p: Path):
    """Return (bool, reason). Conservative: anything uncertain stays put."""
    if p.is_dir():
        return False, 'directory (never touched)'
    ext = p.suffix.lower()
    name = p.name.lower()
    if ext not in MOVE_EXT:
        return False, 'extension %s not in move list' % (ext or '(none)')
    for frag in NEVER_NAME_CONTAINS:
        if frag in name:
            return False, 'PROTECTED: name contains "%s"' % frag
    for frag in NEVER_NAME_STARTS:
        if name.startswith(frag):
            return False, 'PROTECTED: looks like an uninstaller'
    return True, 'inert %s' % ext


def main():
    plan = []
    for label, src in (('VST3', VST3), ('VST2', VST2)):
        if not src.is_dir():
            print('!! missing source folder:', src)
            continue
        for p in sorted(src.iterdir()):
            ok, why = should_move(p)
            if ok:
                plan.append((label, p, p.stat().st_size, why))

    if not plan:
        print('nothing to quarantine.')
        return 0

    total = sum(x[2] for x in plan)
    print('%s  %d file(s), %.1f MB' % ('APPLYING' if APPLY else 'DRY RUN', len(plan), total / 1024 / 1024))
    print('quarantine root:', QROOT)
    print('-' * 78)
    for label, p, sz, why in plan:
        print('  [%s] %-52s %8.1f MB' % (label, p.name[:52], sz / 1024 / 1024))
    print('-' * 78)

    # sanity: assert nothing protected slipped through
    for _, p, _, _ in plan:
        n = p.name.lower()
        assert p.suffix.lower() in MOVE_EXT, 'BUG: bad ext %s' % p
        assert 'serum' not in n, 'BUG: serum file in plan! %s' % p
        assert not n.startswith('uninstal'), 'BUG: uninstaller in plan! %s' % p
    print('safety assertions passed (no serum, no uninstaller, no dll/vst3).')

    if not APPLY:
        print('\nDRY RUN — nothing moved. Re-run with --apply.')
        return 0

    manifest = ['# VST quarantine manifest %s' % date.today().isoformat(),
                '# Each line: <quarantined path>\t<original path>', '']
    moved = 0
    for label, p, sz, why in plan:
        dest_dir = QROOT / label
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / p.name
        if dest.exists():
            dest = dest_dir / (p.stem + '__dup' + p.suffix)
        shutil.move(str(p), str(dest))
        manifest.append('%s\t%s' % (dest, p))
        moved += 1

    (QROOT / 'MANIFEST.tsv').write_text('\n'.join(manifest), encoding='utf-8')

    # restore script - plain, readable, reversible
    lines = ['"""Undo the VST quarantine: move every file back where it came from."""',
             'import shutil, sys', 'from pathlib import Path', '',
             'M = Path(__file__).with_name("MANIFEST.tsv")', 'n = 0',
             'for line in M.read_text(encoding="utf-8").splitlines():',
             '    if not line or line.startswith("#"):', '        continue',
             '    q, orig = line.split("\\t")',
             '    if Path(q).exists():',
             '        Path(orig).parent.mkdir(parents=True, exist_ok=True)',
             '        shutil.move(q, orig); n += 1',
             'print("restored", n, "file(s)")']
    (QROOT / 'RESTORE.py').write_text('\n'.join(lines), encoding='utf-8')

    print('\nmoved %d file(s), freed %.1f MB from the scan folders.' % (moved, total / 1024 / 1024))
    print('manifest : %s' % (QROOT / 'MANIFEST.tsv'))
    print('undo     : python "%s"' % (QROOT / 'RESTORE.py'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
