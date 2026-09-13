"""Install downloaded Serum packs into Serum 2's configured content folders.

Destination is NOT guessed - it is read from Serum's own prefs:
  %APPDATA%\\Xfer\\Serum 2\\Serum2Prefs.json  ->  "Serum Presets Path"
and the subfolder for each content type is the one Serum itself labels with a
SaveYourXHere.txt placeholder (Presets/User, Tables/User, Samples/User).

Mapping is EXPLICIT per zip rather than inferred, because the packs disagree
about whether the top-level folder is the pack name ("Echo Sound Works Grey")
or a generic type name ("Wavetables") that would otherwise nest redundantly.

Deliberately NOT installed into Serum: the Smores bonus pack is mostly drum
loops, construction kits, MIDI and a *Sylenth1* bank - none of which Serum can
read. Only its Serum skin is taken.

Mac junk (__MACOSX/, .DS_Store, ._resource forks) is stripped throughout.

Usage:  python serum_install_packs.py           (dry run)
        python serum_install_packs.py --apply
"""
import json
import os
import sys
import zipfile
from pathlib import Path

DL = Path(r'D:\Users\Owner\Downloads')
PREFS = Path(os.environ['APPDATA']) / 'Xfer' / 'Serum 2' / 'Serum2Prefs.json'
APPLY = '--apply' in sys.argv

ROOT = Path(json.loads(PREFS.read_text(encoding='utf-8'))['Serum Presets Path'])

# zip -> (destination subpath, levels of leading path to strip, only-this-prefix or None)
PLAN = [
    ('Echo Sound Works - Smores.zip',   'Presets/User', 0, None),
    ('Echo Sound Works Grey Area.zip',  'Presets/User', 0, None),
    ('Resonance Sound - TITAN-2.zip',   'Presets/User', 0, None),
    ('Wavetables.zip',                  'Tables/User',  1, None),
    ('Noise Samples.zip',               'Samples/User', 1, None),
    ('ESW Smores - Bonus.zip',          'Skins',        2, 'ESW Smores | Bonus/ESW Dirty Teal Serum Skin/'),
]
# nested: outer zip -> inner zip -> destination
NESTED = [('ESW Grey Area Wavetables.zip', 'ESW Grey Area Wavetables/Echo Sound Works Grey.zip', 'Tables/User', 0)]


def is_junk(name: str) -> bool:
    if name.startswith('__MACOSX'):
        return True
    base = name.rsplit('/', 1)[-1]
    return base.startswith('._') or base in ('.DS_Store', 'Icon\r', '')


ILLEGAL = '<>:"|?*'
RENAMED = []


def safe_component(part: str) -> str:
    """These packs are authored on macOS, where <>:"|?* and trailing dots/spaces
    are all legal in filenames. On Windows they are not - io.open raises
    EINVAL. Sanitise per path component and record it, so the renames are
    reported rather than silently applied to someone's preset names."""
    out = ''.join('_' if c in ILLEGAL else c for c in part)
    out = out.rstrip(' .') or '_'
    if out != part:
        RENAMED.append((part, out))
    return out


def strip_path(name: str, levels: int) -> str:
    parts = [p for p in name.split('/') if p]
    return '/'.join(safe_component(p) for p in parts[levels:])


def install(zf: zipfile.ZipFile, dest_sub: str, levels: int, only: str, label: str):
    dest_root = ROOT / dest_sub
    written = skipped = 0
    total = 0
    for info in zf.infolist():
        n = info.filename
        if info.is_dir() or is_junk(n):
            continue
        if only and not n.startswith(only):
            continue
        rel = strip_path(n, levels)
        if not rel:
            continue
        target = dest_root / rel
        total += info.file_size
        if target.exists():
            skipped += 1
            continue
        written += 1
        if APPLY:
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open('wb') as out:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
    print('  %-34s -> %-13s  %4d file(s), %6.1f MB%s'
          % (label[:34], dest_sub, written, total / 1024 / 1024,
             ('  [%d already present, skipped]' % skipped) if skipped else ''))
    return written


def main():
    print('Serum 2 content root (from Serum2Prefs.json):')
    print('  %s\n' % ROOT)
    if not ROOT.is_dir():
        sys.exit('ERROR: content root does not exist')

    print('%s:' % ('INSTALLING' if APPLY else 'DRY RUN'))
    n = 0
    for zname, dest, levels, only in PLAN:
        p = DL / zname
        if not p.exists():
            print('  !! missing download: %s' % zname)
            continue
        with zipfile.ZipFile(p) as zf:
            n += install(zf, dest, levels, only, zname)

    for outer, inner, dest, levels in NESTED:
        p = DL / outer
        if not p.exists():
            print('  !! missing download: %s' % outer)
            continue
        import io
        with zipfile.ZipFile(p) as ozf:
            try:
                blob = ozf.read(inner)
            except KeyError:
                print('  !! inner zip not found in %s: %s' % (outer, inner))
                continue
            with zipfile.ZipFile(io.BytesIO(blob)) as izf:
                n += install(izf, dest, levels, None, outer + ' > inner')

    print('\n%d file(s) %s.' % (n, 'installed' if APPLY else 'would be installed'))
    if RENAMED:
        uniq = sorted(set(RENAMED))
        print('\n%d name(s) sanitised for Windows (macOS-legal chars):' % len(uniq))
        for a, b in uniq[:15]:
            print('   %s   ->   %s' % (a, b))
        if len(uniq) > 15:
            print('   ...and %d more' % (len(uniq) - 15))
    if not APPLY:
        print('DRY RUN - nothing written. Re-run with --apply.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
