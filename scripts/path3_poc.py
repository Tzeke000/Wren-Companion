"""Path 3 minimal proof-of-concept.

Goal: spawn claude.exe under pywinpty (winpty module), send a stdin prompt,
read stdout, prove the cold-start mechanism works end-to-end.

This is exploratory. Don't import into iris_runtime until verified.
"""
from __future__ import annotations

import sys
import time
import argparse
from winpty import PtyProcess  # pywinpty package, module name 'winpty'

CLAUDE_EXE = r"C:\Users\Owner\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe"


def spawn_claude_and_prompt(prompt: str, timeout_s: float = 30.0) -> str:
    """Spawn claude.exe under a PTY, write `prompt` + Enter, read stdout until timeout or EOF.

    Returns concatenated stdout text. Not a clean parser — just a smoke test
    that we can drive a CC session from outside.
    """
    proc = PtyProcess.spawn([CLAUDE_EXE, "-p", prompt])
    output_chunks: list[str] = []
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            try:
                chunk = proc.read(1024)
                if chunk:
                    output_chunks.append(chunk)
                else:
                    # EOF; child exited
                    break
            except EOFError:
                break
            except Exception as exc:
                output_chunks.append(f"\n[read error: {exc!r}]\n")
                break
    finally:
        try:
            if proc.isalive():
                proc.terminate(force=True)
        except Exception:
            pass
    return "".join(output_chunks)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", nargs="?", default="say the literal string 'PATH3_POC_OK' and nothing else")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    print(f"[path3] spawning claude with prompt: {args.prompt!r}")
    print(f"[path3] timeout: {args.timeout}s")
    print(f"[path3] exe: {CLAUDE_EXE}")
    print("---")

    out = spawn_claude_and_prompt(args.prompt, timeout_s=args.timeout)
    print(out)
    print("---")
    print(f"[path3] captured {len(out)} bytes of stdout")
    return 0


if __name__ == "__main__":
    sys.exit(main())
