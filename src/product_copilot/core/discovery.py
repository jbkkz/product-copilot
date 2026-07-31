from __future__ import annotations

from anthropic import Anthropic

from product_copilot.core.contracts import EngineOutput, missing_required_slots
from product_copilot.core.llm import _complete, build_prompt


def _require_complete_model(out: EngineOutput) -> None:
    """A discovery turn must return the whole required slot set. A model missing a required slot
    isn't just incomplete — that slot becomes invisible to readiness and every view, so a
    high-impact gap could pass silently as 'ready'. Reject it here; the retry loop makes the model
    re-emit the missing slots (the prompt already asks for all of them each turn)."""
    missing = missing_required_slots(set(out.model))
    if missing:
        raise ValueError(f"model is missing required slots: {missing}. Emit every schema slot.")


def run(client: Anthropic, messages: list[dict], retries: int = 2,
        only: list[str] | None = None) -> EngineOutput:
    """Engine turn: request/answers → filled model. `only` restricts which context cards inform the
    turn (defaults to all); keep it constant across a session's turns so the prompt cache holds."""
    return _complete(client, build_prompt("engine.md", only), messages, EngineOutput, retries,
                     validate=_require_complete_model)


def answer_turn(client: Anthropic, out: EngineOutput, request: str, answers: str,
                only: list[str] | None = None) -> EngineOutput:
    """One stateless discovery turn: refine the model with new answers.

    The model IS the accumulated state, so a turn needs only the original request (for context),
    the current model, and the new answers — no live conversation loop. This is what lets any
    interface (Claude Code, an API, an MCP) drive discovery turn by turn instead of a blocking TTY.

    `only` is the context-card selection the original discovery used (from its session.json) — passing
    it keeps a refinement turn reasoning over the same cards, not silently the full set."""
    messages = [
        {"role": "user", "content": request},
        {"role": "assistant", "content": out.model_dump_json()},
        {"role": "user", "content": "Client answers:\n" + answers},
    ]
    return run(client, messages, only=only)
