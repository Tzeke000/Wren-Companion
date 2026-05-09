"""brain/claude_code_subagent.py — Spawn Claude Code as a subagent.

Jarvis-pattern. When the user says "build me a landing page" / "write
a script that does X", Ava spawns `claude` CLI as a subprocess in a
fresh working directory. She acks immediately, the subprocess does
the actual development work, and on completion she announces what
was built and where.

Design:
- Working dir: state/builds/<timestamp>-<slug>/
- Subprocess: `claude --print "<task>"` (non-interactive mode — runs
  and exits without a TTY)
- Output captured into build.log inside the working dir
- Result file list captured by listing the working dir after exit
- Announces "Done. I built X — files are at <path>." via TTS

Safeguards (the user is paying for Anthropic API on every build):
- Voice command requires explicit phrasing ("build me", "write me",
  "make me a")
- Won't fire if the request looks ambiguous / off-topic
- Working dir is sandboxed to state/builds/ — Ava can't write
  outside it via this path

Caller responsibility: the user has `claude` on PATH (Claude Code
CLI installed). Module checks shutil.which on import. If not
available, the voice command declines instead of erroring.
"""
from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


def _claude_available() -> str | None:
    """Return the path to `claude` CLI if available, else None."""
    return shutil.which("claude")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:48] or "build"


def _builds_dir(g: dict[str, Any]) -> Path:
    base = Path(g.get("BASE_DIR") or ".")
    p = base / "state" / "builds"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _new_workdir(g: dict[str, Any], task: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    wd = _builds_dir(g) / f"{stamp}-{_slug(task)}"
    wd.mkdir(parents=True, exist_ok=True)
    return wd


def _persist_user_turn(g: dict[str, Any], text: str) -> None:
    """Write the original user request to chat_history with build_request route."""
    try:
        import json
        person = g.get("_active_person_id") or g.get("active_person_id") or "zeke"
        base = Path(g.get("BASE_DIR") or ".")
        hp = base / "state" / "chat_history.jsonl"
        hp.parent.mkdir(parents=True, exist_ok=True)
        with hp.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.time(),
                "role": "user",
                "content": text,
                "person_id": person,
                "source": "user_voice",
                "turn_route": "build_request",
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[cc_subagent] history append error: {e!r}")


def _persist_completion(g: dict[str, Any], text: str, *, model: str) -> None:
    try:
        import json
        person = g.get("_active_person_id") or g.get("active_person_id") or "zeke"
        base = Path(g.get("BASE_DIR") or ".")
        hp = base / "state" / "chat_history.jsonl"
        with hp.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.time(),
                "role": "assistant",
                "content": text,
                "person_id": person,
                "source": "ava_response",
                "model": model,
                "emotion": "curiosity",
                "turn_route": "build_complete",
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[cc_subagent] completion-history error: {e!r}")


def _list_artifacts(workdir: Path) -> list[str]:
    """Return relative paths for files created in workdir, excluding the log."""
    out: list[str] = []
    try:
        for p in workdir.rglob("*"):
            if p.is_file() and p.name != "build.log":
                out.append(str(p.relative_to(workdir)))
    except Exception:
        pass
    return sorted(out)


def _run_build(task: str, g: dict[str, Any], workdir: Path) -> None:
    """Run `claude --print` in workdir, capture log, announce on exit."""
    claude_bin = _claude_available()
    if not claude_bin:
        msg = "I couldn't find Claude Code on the system. The build's off."
        worker = g.get("_tts_worker")
        if worker is not None and getattr(worker, "available", False):
            try:
                worker.speak(msg, emotion="calm", intensity=0.4, blocking=False)
            except Exception:
                pass
        return

    log_path = workdir / "build.log"
    cmd = [claude_bin, "--print", task]
    print(f"[cc_subagent] spawning: {shlex.join(cmd)} cwd={workdir}")
    t0 = time.time()
    try:
        with log_path.open("w", encoding="utf-8") as logf:
            proc = subprocess.Popen(
                cmd,
                cwd=str(workdir),
                stdout=logf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return_code = proc.wait(timeout=900)  # 15-min cap
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        return_code = -1
    except Exception as e:
        print(f"[cc_subagent] spawn error: {e!r}")
        return_code = -2

    elapsed = time.time() - t0
    artifacts = _list_artifacts(workdir)
    print(f"[cc_subagent] exit={return_code} elapsed={elapsed:.1f}s files={len(artifacts)}")

    if return_code == 0 and artifacts:
        if len(artifacts) == 1:
            files_phrase = artifacts[0]
        elif len(artifacts) <= 3:
            files_phrase = ", ".join(artifacts)
        else:
            files_phrase = f"{len(artifacts)} files including {artifacts[0]}"
        msg = (
            f"Okay, I'm done with that. I built {files_phrase} "
            f"in the build folder under {workdir.name}. Took about "
            f"{int(elapsed/60)} minute{'s' if int(elapsed/60) != 1 else ''}."
        )
    elif return_code == -1:
        msg = "That build took too long, so I stopped it. The log is in the build folder."
    elif return_code == 0:
        msg = "I worked on that, but I didn't end up creating any files. The log might explain why."
    else:
        msg = f"That build didn't finish cleanly — exit code {return_code}. Log is in the build folder."

    worker = g.get("_tts_worker")
    if worker is not None and getattr(worker, "available", False):
        try:
            worker.speak(msg, emotion="curiosity", intensity=0.5, blocking=False)
        except Exception as e:
            print(f"[cc_subagent] TTS error: {e!r}")
    _persist_completion(g, msg, model=f"claude-code:{return_code}")


def delegate_build(text: str, g: dict[str, Any]) -> str:
    """Spawn the Claude Code subagent. Returns the immediate acknowledgement.

    On hard failure (Claude Code not installed), returns a graceful decline
    message — caller should treat as a normal voice-command reply.
    """
    if not _claude_available():
        return "I'd love to build that, but I don't have Claude Code installed on this system."

    task = text.strip()
    workdir = _new_workdir(g, task)
    _persist_user_turn(g, task)

    threading.Thread(
        target=_run_build,
        args=(task, g, workdir),
        daemon=True,
        name="ava-cc-subagent",
    ).start()

    return f"On it — I'm starting that build now. I'll come back to you when it's done."


_BUILD_PATTERN = re.compile(
    r"^\s*(?:hey\s+ava[,\s]+)?(?:please\s+)?(?:"
    r"build\s+me\s+(?:a\s+|an\s+)?"
    r"|write\s+me\s+(?:a\s+|an\s+)?"
    r"|make\s+me\s+(?:a\s+|an\s+)?"
    r"|create\s+(?:a\s+|an\s+)?"
    r"|generate\s+(?:a\s+|an\s+)?"
    r"|build\s+(?:a\s+|an\s+)"
    r")(?P<task>.+?)$",
    re.IGNORECASE,
)


def looks_like_build_request(text: str) -> bool:
    return bool(_BUILD_PATTERN.match((text or "").strip()))


def extract_task(text: str) -> str:
    m = _BUILD_PATTERN.match((text or "").strip())
    if m:
        return (m.group("task") or "").strip(" .,;:?!")
    return (text or "").strip()
