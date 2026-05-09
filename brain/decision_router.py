"""brain/decision_router.py — Decision Router scaffolding (Wave 2 start).

Architecture sweep #2 from designs/ava-roadmap-personhood.md.

CURRENT STATE: this module provides the PROTOCOL + base classes that
future-Wave-2 work will use to migrate `run_ava`'s 200+-line
branching sequence into a clean dispatcher. NO handlers are wired yet.
NO calls into this module from run_ava yet.

The reason: actually refactoring run_ava without runtime testing is
high-risk. Every turn flows through there. Subtle state-side-effect
bugs would be hard to detect statically. So this is the design scaffold
landed first, with the migration happening incrementally in subsequent
Zeke-awake sessions.

When the migration happens, it'll look like:
    router = DecisionRouter()
    router.register(VoiceCommandHandler())   # current branch ~line 200 of run_ava
    router.register(ActionTagHandler())      # current branch ~line 300
    router.register(SubagentHandler())       # current branch ~line 350
    router.register(IntrospectionHandler())  # currently inlined in _cmd_mood
    router.register(FastPathHandler())       # current branch ~line 420
    router.register(DeepPathHandler())       # current branch ~line 800

    result = router.dispatch(turn_ctx)
    if result is not None:
        return result.reply, result.visual, result.profile, ...

Each handler is responsible for:
- Detecting whether it should handle this turn (try_handle returns
  None to defer, HandlerResult to claim)
- Running the action through the safety layer (#7)
- Recording telemetry stages (#9)
- Persisting to chat_history if it claims the turn
- Updating canonical history

Today: define the protocol + TurnContext + base helpers. Migration
happens in subsequent Wave-2 sessions.

Migration plan (one PR per step, each Zeke-awake-tested):
  W2-1: Extract VoiceCommandHandler (smallest, lowest-risk). Swap
        run_ava's voice_command_router block to use it. Verify Phase A
        (8/8 baseline) still passes.
  W2-2: Extract ActionTagHandler. Verify Phase B compounds still work.
  W2-3: Extract SubagentHandler. Verify polar bear delegation still
        produces the ack + delayed answer.
  W2-4: Extract IntrospectionHandler (move out of voice_commands._cmd_mood).
  W2-5: Extract FastPathHandler (simple-question check).
  W2-6: Extract DeepPathHandler (everything else).
  W2-7: run_ava becomes a 30-line orchestrator instead of a 1200-line
        sequence. Done.

After W2-7, adding a new intent type (e.g. play_mode, build_request,
research_query) is one new handler file plus router.register(). That's
the Decision-Router-as-additive-extension principle.
"""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any, Optional


# ── TurnContext ───────────────────────────────────────────────────────────


@dataclass
class TurnContext:
    """Everything a handler needs to evaluate + execute a turn.

    Bundle pattern so handlers don't reach into a generic dict by
    convention. Easier to test (construct a TurnContext in a test;
    all handlers see consistent shape) and easier to reason about
    (the data dependency surface is explicit in this dataclass).
    """

    # Inputs
    user_input: str  # raw user text (post-STT if voice path)
    g: dict[str, Any]  # global avaagent state — handlers may need it for cross-cutting concerns
    active_person_id: str  # who's talking ("zeke" | "claude_code" | etc.)

    # Telemetry
    turn_id: str = ""
    started_ts: float = field(default_factory=time.time)

    # Source
    source: str = "unknown"  # "voice" | "inject_transcript" | "text" etc.

    # Convenience accessors
    @property
    def base_dir(self) -> str:
        return str(self.g.get("BASE_DIR") or ".")

    @property
    def normalized_input(self) -> str:
        """Lower-cased, stripped, trailing-punctuation-removed."""
        t = (self.user_input or "").lower().strip().rstrip("?!.,")
        return t


# ── HandlerResult ─────────────────────────────────────────────────────────


@dataclass
class HandlerResult:
    """What a handler returns when it CLAIMS a turn.

    Mirrors the tuple shape that run_ava's various branches currently
    return: (reply_text, visual_payload, active_profile, action_log,
    metadata).
    """

    reply: str
    visual: dict[str, Any] | None = None
    profile: dict[str, Any] | None = None
    actions_taken: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    route: str = ""  # which handler claimed it (for telemetry)
    persisted_to_history: bool = False  # did this handler write to chat_history.jsonl?


# ── Handler protocol ──────────────────────────────────────────────────────


class Handler(abc.ABC):
    """One specialized turn handler.

    Subclasses implement:
    - name (string identifier for telemetry / logging)
    - try_handle(ctx) → HandlerResult or None

    Returning None means "I don't claim this turn, try the next handler."
    Returning a HandlerResult means "I've handled this turn; here's the
    reply." The router stops dispatching as soon as one handler claims.

    Handlers should:
    - Be FAST in the negative case (returning None should be ~milliseconds
      so the router can fall through to the next handler quickly).
    - Run the safety layer for any actions they execute (architecture #7).
    - Record telemetry stages on the ctx.turn_id.
    - Persist to chat_history if and only if they claim the turn.
    - Not modify ctx.g state in ways that survive a None return (i.e.
      no side-effects on rejection).
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        ...

    @abc.abstractmethod
    def try_handle(self, ctx: TurnContext) -> Optional[HandlerResult]:
        """Return HandlerResult if this handler claims the turn, else None."""
        ...

    # Optional: handlers can override if they want a specific priority hint
    # Lower numbers = higher priority. Default ordering is registration order.
    @property
    def priority(self) -> int:
        return 0


# ── DecisionRouter ────────────────────────────────────────────────────────


class DecisionRouter:
    """Dispatches a turn through registered handlers in order.

    Today: not yet wired into run_ava. Future Wave-2 sessions migrate
    each branch of run_ava into a Handler and register it here.
    """

    def __init__(self) -> None:
        self._handlers: list[Handler] = []

    def register(self, handler: Handler) -> None:
        if not isinstance(handler, Handler):
            raise TypeError(f"register expects a Handler subclass, got {type(handler)!r}")
        self._handlers.append(handler)

    def dispatch(self, ctx: TurnContext) -> Optional[HandlerResult]:
        """Run the chain. First handler whose try_handle returns non-None
        wins. Returns None if no handler claims (caller should fall back
        to whatever the legacy path is — typically deep-path)."""
        for handler in self._handlers:
            try:
                result = handler.try_handle(ctx)
            except Exception as e:
                print(f"[decision_router] handler {handler.name!r} raised: {e!r}")
                continue
            if result is not None:
                # Tag the route for telemetry consumers.
                if not result.route:
                    result.route = handler.name
                return result
        return None

    def list_handlers(self) -> list[str]:
        return [h.name for h in self._handlers]


# ── Singleton (will be populated by future Wave-2 migration steps) ───────


router = DecisionRouter()


# ── Migration helper: build a TurnContext from current run_ava state ─────


def build_turn_context_from_run_ava(
    user_input: str,
    g: dict[str, Any],
    active_person_id: str,
    *,
    turn_id: str = "",
    source: str = "unknown",
) -> TurnContext:
    """Construct a TurnContext from the inputs run_ava receives today.

    When run_ava is migrated, it'll call this helper at the top, then
    pass the ctx to router.dispatch(). For now this exists as the
    interface contract for future migration.
    """
    return TurnContext(
        user_input=user_input,
        g=g,
        active_person_id=str(active_person_id or ""),
        turn_id=turn_id,
        source=source,
    )
