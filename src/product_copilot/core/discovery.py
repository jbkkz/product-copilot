from __future__ import annotations

from anthropic import Anthropic

from product_copilot.core.contracts import EngineOutput
from product_copilot.core.llm import _complete, build_prompt


def run(client: Anthropic, messages: list[dict], retries: int = 2) -> EngineOutput:
    """Engine turn: request/answers → filled model."""
    return _complete(client, build_prompt("engine.md"), messages, EngineOutput, retries)
