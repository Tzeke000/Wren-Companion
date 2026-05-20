"""scripts/bench_orb_snapshot.py — micro-benchmark of the orb HTTP snapshot.

Hits http://127.0.0.1:5876/api/v1/snapshot N times back-to-back and reports
latency percentiles + response size. Used 2026-05-20 to verify that the
snapshot endpoint's profile-load + multi-subsystem-read pattern is actually
cheap (as previously assumed) or whether it merits a caching pass.

Run: py -3.11 scripts/bench_orb_snapshot.py [iterations]
"""
from __future__ import annotations

import statistics
import sys
import time
import urllib.request

URL = "http://127.0.0.1:5876/api/v1/snapshot"


def main(iterations: int = 100) -> int:
    latencies_ms: list[float] = []
    total_bytes = 0
    errors = 0

    print(f"benching {URL} x {iterations} sequentially...", file=sys.stderr)
    start_wall = time.perf_counter()
    for i in range(iterations):
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(URL, timeout=5.0) as resp:
                body = resp.read()
            t1 = time.perf_counter()
            latencies_ms.append((t1 - t0) * 1000.0)
            total_bytes += len(body)
        except Exception as e:
            errors += 1
            print(f"  iter {i}: error {e!r}", file=sys.stderr)
    elapsed = time.perf_counter() - start_wall

    if not latencies_ms:
        print("NO successful requests", file=sys.stderr)
        return 1

    latencies_ms.sort()
    p50 = latencies_ms[len(latencies_ms) // 2]
    p95 = latencies_ms[int(len(latencies_ms) * 0.95)]
    p99 = latencies_ms[int(len(latencies_ms) * 0.99)] if len(latencies_ms) >= 100 else latencies_ms[-1]
    mean = statistics.mean(latencies_ms)
    avg_bytes = total_bytes / len(latencies_ms)

    print()
    print(f"iterations:    {len(latencies_ms)} ok, {errors} errors")
    print(f"wall time:     {elapsed:.2f}s  ({len(latencies_ms) / elapsed:.1f} req/s sustained)")
    print(f"latency p50:   {p50:.2f}ms")
    print(f"latency p95:   {p95:.2f}ms")
    print(f"latency p99:   {p99:.2f}ms")
    print(f"latency mean:  {mean:.2f}ms")
    print(f"latency min:   {latencies_ms[0]:.2f}ms")
    print(f"latency max:   {latencies_ms[-1]:.2f}ms")
    print(f"response size: {avg_bytes:.0f} bytes (avg)")
    print()
    print("verdict guidance:")
    print("  p99 < 20ms:   excellent — current code is cheap, no caching needed")
    print("  p99 < 50ms:   fine — 5s poll cadence is well within budget")
    print("  p99 < 200ms:  acceptable but worth profiling if 30fps polling were considered")
    print("  p99 > 200ms:  worth caching or breaking into separate endpoints")
    return 0


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    sys.exit(main(n))
