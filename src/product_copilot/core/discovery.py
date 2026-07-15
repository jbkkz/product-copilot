from __future__ import annotations

from anthropic import Anthropic

from product_copilot.core.contracts import EngineOutput
from product_copilot.core.llm import _complete, build_prompt


def run(client: Anthropic, messages: list[dict], retries: int = 2) -> EngineOutput:
    """Engine turn: request/answers → filled model."""
    return _complete(client, build_prompt("engine.md"), messages, EngineOutput, retries)


def answer_turn(client: Anthropic, out: EngineOutput, request: str, answers: str) -> EngineOutput:
    """One stateless discovery turn: refine the model with new answers.

    The model IS the accumulated state, so a turn needs only the original request (for context),
    the current model, and the new answers — no live conversation loop. This is what lets any
    interface (Claude Code, an API, an MCP) drive discovery turn by turn instead of a blocking TTY."""
    messages = [
        {"role": "user", "content": request},
        {"role": "assistant", "content": out.model_dump_json()},
        {"role": "user", "content": "Client answers:\n" + answers},
    ]
    return run(client, messages)
