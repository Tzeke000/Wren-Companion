"""wren-voice MCP — THIN client to the voice daemon (wren_voice_daemon.py).
Tools forward to the daemon over a socket (wren_voice_client) so voice fixes reload
the daemon with NO Claude Code relaunch, and the daemon can stream speech. (GOSE pattern.)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wren_voice_client import client
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("wren-voice")


def _fmt(r: dict) -> str:
    # Render a daemon response for the model.
    if not r.get("ok"):
        return f"[voice] {r.get('error', 'daemon error')}"
    res = r.get("result")
    return res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)


@mcp.tool()
def voice_speak(text: str) -> str:
    """Enqueue `text` for speech on the daemon's play-queue and return after queueing.
    The daemon plays sentences in order, so call this sentence-by-sentence as you think
    — speech streams out while you continue reasoning. Forwarded to the daemon.
    """
    return _fmt(client().call("speak", text=text))


@mcp.tool()
def voice_listen(
    timeout_seconds: float = 45.0,
    max_utterance_seconds: float = 20.0,
    end_silence_seconds: float = 0.0,
) -> str:
    """Listen for Zeke to speak and return the transcript. Blocks until he starts
    talking and finishes a thought, then transcribes. Forwarded to the daemon.

    Args:
        timeout_seconds: how long to wait for speech to start before giving up.
        max_utterance_seconds: hard cap on a single utterance once speech begins.
        end_silence_seconds: trailing silence that ends a turn (<=0 uses daemon default).
    """
    return _fmt(
        client().call(
            "listen",
            timeout_seconds=timeout_seconds,
            max_utterance_seconds=max_utterance_seconds,
            end_silence_seconds=end_silence_seconds,
        )
    )


@mcp.tool()
def voice_speak_interruptible(
    text: str,
    timeout_seconds: float = 45.0,
    max_utterance_seconds: float = 20.0,
) -> str:
    """Speak `text` while watching for a barge-in (Zeke cutting in mid-sentence).
    If he interrupts, stops speech and returns his words. Forwarded to the daemon.

    Args:
        text: what to say.
        timeout_seconds: after a barge-in, how long to wait for his words.
        max_utterance_seconds: hard cap on the captured interruption.
    """
    return _fmt(
        client().call(
            "speak_interruptible",
            text=text,
            timeout_seconds=timeout_seconds,
            max_utterance_seconds=max_utterance_seconds,
        )
    )


@mcp.tool()
def voice_call_start() -> str:
    """Warm the voice pipeline for an active call (mouth, whisper, smart-turn, prosody)
    and confirm readiness by speaking through the live mouth. Forwarded to the daemon.
    """
    return _fmt(client().call("call_start"))


@mcp.tool()
def voice_call_end() -> str:
    """Cold the full voice pipeline after a call — free VRAM, unload models.
    Forwarded to the daemon.
    """
    return _fmt(client().call("call_end"))


@mcp.tool()
def voice_status() -> str:
    """Report whether the voice daemon is up and the current voice state
    (idle/listening/thinking/speaking/muted). Forwarded to the daemon.
    """
    return _fmt(client().call("status"))


@mcp.tool()
def voice_backend(name: str = "", action: str = "switch") -> str:
    """Pick / cold-start / stop which TTS engine the mouth uses (xtts, chatterbox,
    styletts2). Use action='list' to see engines and which are up. Forwarded to daemon.

    Args:
        name: engine name (e.g. 'xtts', 'chatterbox', 'styletts2').
        action: 'switch', 'start', 'stop', or 'list'.
    """
    return _fmt(client().call("backend", name=name, action=action))


@mcp.tool()
def voice_reload(include_listen: bool = False) -> str:
    """Trigger a hot-reload of the daemon's voice logic — swaps prosody, smart-turn,
    and other modules with models still warm, NO Claude Code relaunch required.
    The daemon decides what to reload; `include_listen` is accepted for compatibility.
    """
    return _fmt(client().call("reload"))


if __name__ == "__main__":
    mcp.run()
