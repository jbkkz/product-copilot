from __future__ import annotations

import json
import os
import re

from anthropic import Anthropic
from pydantic import ValidationError

from product_copilot.paths import ROOT


def load_context() -> str:
    cards = []
    for path in sorted((ROOT / "context").glob("*.md")):
        if path.name.startswith("_"):
            continue
        cards.append(f"## {path.stem}\n{path.read_text()}")
    return "\n\n".join(cards)


def build_prompt(name: str) -> str:
    """Load a prompt file and inject the schema + product context."""
    schema = (ROOT / "framework" / "model_schema.json").read_text()
    text = (ROOT / "prompts" / name).read_text()
    return text.replace("{{SCHEMA}}", schema).replace("{{CONTEXT}}", load_context())


def _first_text(resp) -> str:
    """First text block of the response — skips thinking/tool_use blocks."""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


def _extract_json(text: str) -> dict:
    """Best-effort JSON extraction: strip a ```json fence, else slice { … }."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("no JSON object found in the reply")
        text = text[start : end + 1]
    return json.loads(text)


def _complete(client: Anthropic, system: str, messages: list[dict], out_model, retries: int = 2):
    """One call → validated `out_model`. Retries with a nudge on malformed/non-conformant JSON.
    The nudge lives in a local copy so the caller's clean history is never polluted."""
    attempt = messages
    last_err = None
    for _ in range(retries + 1):
        resp = client.messages.create(
            model=os.getenv("MODEL", "claude-sonnet-5"),
            max_tokens=4000,
            system=system,
            messages=attempt,
        )
        raw = _first_text(resp)
        try:
            return out_model.model_validate(_extract_json(raw))
        except (json.JSONDecodeError, ValueError, ValidationError) as e:
            last_err = e
            attempt = attempt + [
                {"role": "assistant", "content": raw or "(empty)"},
                {"role": "user", "content": f"Your reply did not match the required schema ({e}). Reply with ONLY the JSON object, no prose, no code fence."},
            ]
    raise RuntimeError(f"No schema-valid JSON after {retries + 1} attempts: {last_err}")
