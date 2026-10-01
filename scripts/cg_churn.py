import os, sys, time
p = "state/concept_graph.json"
dur = float(sys.argv[1]) if len(sys.argv) > 1 else 80
last = os.stat(p).st_mtime_ns; n = 0; t0 = time.time()
while time.time() - t0 < dur:
    time.sleep(0.2)
    try:
        m = os.stat(p).st_mtime_ns
    except FileNotFoundError:
        continue
    if m != last:
        n += 1; last = m
print(f"concept_graph.json mtime changes in {dur:.0f}s: {n}  size={os.path.getsize(p)}")
