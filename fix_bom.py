files = [
    'brain/emotion.py',
    'brain/attention.py',
    'brain/initiative.py',
]
for f in files:
    try:
        txt = open(f, encoding='utf-8-sig').read()
        open(f, 'w', encoding='utf-8').write(txt)
        print('  stripped BOM:', f)
    except Exception as e:
        print('  error on', f, ':', e)
