"""Run the REAL patched launchers with only destructive commands neutered, so
cmd.exe parses the actual control flow (including the :log label in a CRLF file).

Three scenarios per launcher:
  main : elevated + venv present -> full run, host fails with rc=3
  uac  : NOT elevated            -> UAC branch taken (inside an if-block)
  venv : venv missing            -> FATAL branch taken (inside an if-block), rc=2
"""
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(r'D:\Wren-Companion')


def neuter(src, log, scenario):
    t = src.read_text(encoding='utf-8')
    t = t.replace(r'set "IRISLOG=D:\Wren-Companion\state\launcher_boot.log"', 'set "IRISLOG=%s"' % log)
    t = re.sub(r'^powershell -NoProfile -Command ".*"$', 'echo    [NEUTERED powershell]', t, flags=re.M)
    t = re.sub(r'^start "iris-[^"]*" /B .*$', 'echo    [NEUTERED service]', t, flags=re.M)
    t = t.replace('taskkill /f /im iris-control.exe >nul 2>&1', 'echo    [NEUTERED taskkill]')
    t = t.replace(r'if exist "D:\Wren-Companion\state\iris.pid" del /q "D:\Wren-Companion\state\iris.pid" >nul 2>&1',
                  'echo    [NEUTERED pidfile]')
    t = t.replace(r'call "D:\Wren-Companion\start_postoffice_stack.bat"', 'echo    [NEUTERED postoffice]')
    t = re.sub(r'^"D:\\Wren-Companion\\\.venv\\Scripts\\python\.exe" "D:\\Wren-Companion\\iris_body_host\.py" 2>>"%IRISLOG%"$',
               '"D:\\\\Wren-Companion\\\\.venv\\\\Scripts\\\\python.exe" -c "import sys; sys.stderr.write(\'SIMULATED HOST TRACEBACK\\\\n\'); sys.exit(3)" 2>>"%IRISLOG%"',
               t, flags=re.M)
    # the UAC relaunch spawn must never actually fire during a test
    t = t.replace('''powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"''',
                  'echo    [NEUTERED uac relaunch]')

    if scenario == 'main':
        t = t.replace('net session >nul 2>&1', 'ver >nul')
    elif scenario == 'uac':
        t = t.replace('net session >nul 2>&1', 'cmd /c exit /b 1')   # force not-elevated
    elif scenario == 'venv':
        t = t.replace('net session >nul 2>&1', 'ver >nul')
        t = t.replace(r'if not exist "D:\Wren-Companion\.venv\Scripts\python.exe" (',
                      'if not exist "D:\\Wren-Companion\\.venv\\NOPE\\python.exe" (')
    assert 'iris_body_host.py" 2>>' not in t, 'real host still present!'
    return t.encode('utf-8').replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')


EXPECT = {
    'main': dict(rc=3, must=['launcher START', 'running elevated', 'model pin: IRIS_MODEL=',
                             'venv present', 'sweep 1/3', 'sweep 2/3', 'sweep 3/3',
                             'service: voice watchdog', 'starting iris_body_host',
                             'SIMULATED HOST TRACEBACK', 'EXITED rc=3', 'launcher END'],
                 forbid=['running elevated.\nrunning elevated']),
    'uac':  dict(rc=0, must=['launcher START', 'NOT elevated', 'UAC relaunch spawned'],
                 forbid=['] running elevated.', 'sweep 1/3', 'starting iris_body_host']),
    'venv': dict(rc=2, must=['launcher START', 'running elevated', 'FATAL: venv missing'],
                 forbid=['venv present', 'sweep 1/3', 'starting iris_body_host']),
}

fail = 0
for name in ('start_iris_v2.bat', 'start_iris_v2_fable.bat'):
    for scen, exp in EXPECT.items():
        dst = REPO / 'state' / ('_n_%s_%s' % (scen, name))
        log = REPO / 'state' / ('_n_%s_%s.log' % (scen, name))
        if log.exists():
            log.unlink()
        dst.write_bytes(neuter(REPO / name, log, scen))
        r = subprocess.run(['cmd', '/c', str(dst)], capture_output=True, text=True)
        body = log.read_text(encoding='utf-8', errors='replace') if log.exists() else ''

        probs = []
        if r.returncode != exp['rc']:
            probs.append('rc=%s expected %s' % (r.returncode, exp['rc']))
        for m in exp['must']:
            if m not in body:
                probs.append('MISSING: ' + m)
        for m in exp['forbid']:
            if m in body:
                probs.append('SHOULD NOT APPEAR: ' + m)
        fail += len(probs)
        print('%-24s %-5s  %s' % (name, scen, 'PASS' if not probs else 'FAIL'))
        for p in probs:
            print('      ->', p)

print('\nTOTAL FAILURES:', fail)
sys.exit(1 if fail else 0)
