"""Round-2 3.9 / 3.13 / 3.14 — transcript append stays parseable (and fsyncs), the pre-commit compile
gate refuses a staged SyntaxError, and the anchor auto-detector ignores machine prompts.

    .venv\\Scripts\\python.exe tests/test_small_fixes_3.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain import anchor_moments as am, iris_transcript as it  # noqa: E402

PY = str(ROOT / ".venv" / "Scripts" / "python.exe")


def test_machine_prompts_are_not_anchor_worthy() -> None:
    machine = [
        "[ZEKE PRESENCE @ 08:11:40 — network watcher, not Zeke typing] Zeke's phone DROPPED off the wifi",
        "[TOWER SENTINEL — automated] remember to check the tower",
        "[VECTOR SENSE @ 19:22:01] I will always remember this cliff",
        "[LLM-BRIDGE — automated idle nudge from the body host, not Zeke] 1 request pending",
        "[SELF-CHECK — automated, not Zeke. Nobody is necessarily at the keyboard.] (1) time_check",
        "[SCENE MEMORY — automated, not Zeke. A keyframe was committed] remember the room",
    ]
    for m in machine:
        assert am.is_machine_prompt(m), m
        assert am.auto_detect_anchor_in_turn("zeke", m, "I will remember that forever.") is None, m
    human = [
        "thank you for staying up with me, it means a lot",
        "[I think] we should remember this one",
        "honestly, i never told anyone that before",
        "my mom called tonight",
    ]
    for h in human:
        assert not am.is_machine_prompt(h), h


def test_transcript_append_writes_parseable_lines_in_a_temp_base() -> None:
    with tempfile.TemporaryDirectory() as td:
        it.configure(td)
        e = it.append(role="user", content="hello there", source="zeke", modality="chat")
        e2 = it.append(role="assistant", content="hi", source="iris", modality="chat")
        p = Path(td) / "state" / "transcript.jsonl"
        rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert [r["id"] for r in rows] == [e["id"], e2["id"]]
        assert b"\x00" not in p.read_bytes()
        assert it.recent(n=5)[-1]["content"] == "hi"
        it.configure(str(ROOT))  # restore the module's base for anything else in-process


def test_precommit_gate_refuses_staged_syntax_error() -> None:
    with tempfile.TemporaryDirectory() as td:
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git", "init", "-q", td], check=True)
        (Path(td) / "bad.py").write_text("def broken(:\n", encoding="utf-8")
        subprocess.run(["git", "-C", td, "add", "bad.py"], check=True)
        r = subprocess.run([PY, str(ROOT / "scripts" / "precommit_pycompile.py")], cwd=td, capture_output=True, text=True, env=env)
        assert r.returncode == 1 and "SyntaxError in STAGED bad.py" in r.stderr, (r.returncode, r.stderr)
        (Path(td) / "bad.py").write_text("def fine():\n    return 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", td, "add", "bad.py"], check=True)
        r2 = subprocess.run([PY, str(ROOT / "scripts" / "precommit_pycompile.py")], cwd=td, capture_output=True, text=True, env=env)
        assert r2.returncode == 0, (r2.returncode, r2.stderr)


def test_repo_hook_is_installed_and_points_at_the_gate() -> None:
    hook = ROOT / ".git" / "hooks" / "pre-commit"
    assert hook.is_file(), "run: sh scripts/install_git_hooks.sh"
    assert "precommit_pycompile.py" in hook.read_text(encoding="utf-8")


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e!r}")
    sys.exit(1 if fails else 0)
