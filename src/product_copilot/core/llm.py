from __future__ import annotations

import contextvars
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from anthropic import Anthropic, APIError
from pydantic import ValidationError

from product_copilot.paths import CONTEXT, FRAMEWORK, PROMPTS

MODEL_DEFAULT = "claude-sonnet-5"

# Output-token ceiling per call. Discovery emits a full slot model + questions + summary and runs
# right up against a 4k ceiling (a simple request already spends ~3.6k output tokens), so 4k left rich
# requests one variance spike away from truncation. 8k gives ~2x headroom; you pay only for tokens
# generated, not the ceiling, so raising it costs nothing on smaller outputs. A per-generator budget
# (the assessment needs less than an epic) is a later refinement — one safe ceiling first.
MAX_OUTPUT_TOKENS = 8000


class EngineError(RuntimeError):
    """A clean, user-facing failure (API unavailable, output truncated). The CLI catches this and
    prints the message without a traceback. A run that raises this never modifies the saved model —
    the call failed before any write."""


def current_model_name() -> str:
    """The model id this process will call — the env override or the default. Exposed so provenance
    (session.json) records the exact model a discovery ran against."""
    return os.getenv("MODEL", MODEL_DEFAULT)

# USD per 1M tokens (input, output), from the Anthropic pricing reference as of 2026-06-24. This
# yields an *estimate*, never a bill: prices drift and intro rates lapse, so the renderer stamps this
# date and labels the number an estimate. Tokens (below) are ground truth from the API; cost is the
# only thing here that can go stale — keep this table updateable and honest, not authoritative.
PRICING_AS_OF = "2026-06-24"
_PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


@dataclass
class CallRecord:
    """One `_complete()` call's usage — summed across its retry attempts (retries spend tokens too)."""
    model: str
    input_tokens: int = 0        # uncached, full-price input
    output_tokens: int = 0
    cache_read_tokens: int = 0   # served from cache (~0.1x input price)
    cache_write_tokens: int = 0  # written to cache (~1.25x input price)
    latency_ms: int = 0
    attempts: int = 1


@dataclass
class UsageLedger:
    """Accumulates the API usage of a session (one `pc` command). Presentation-free — the renderer
    turns it into a line; the cost estimate lives here because it is pure arithmetic over the records."""
    calls: list[CallRecord] = field(default_factory=list)

    def record(self, rec: CallRecord) -> None:
        self.calls.append(rec)

    @property
    def input_tokens(self) -> int:
        return sum(c.input_tokens for c in self.calls)

    @property
    def output_tokens(self) -> int:
        return sum(c.output_tokens for c in self.calls)

    @property
    def cache_read_tokens(self) -> int:
        return sum(c.cache_read_tokens for c in self.calls)

    @property
    def cache_write_tokens(self) -> int:
        return sum(c.cache_write_tokens for c in self.calls)

    @property
    def latency_ms(self) -> int:
        return sum(c.latency_ms for c in self.calls)

    @property
    def models(self) -> list[str]:
        seen = []
        for c in self.calls:
            if c.model not in seen:
                seen.append(c.model)
        return seen

    def cost_usd(self) -> float | None:
        """Estimated USD across all calls, or None if any model's price is unknown (never guess a
        price). Cache reads bill ~0.1x input, cache writes ~1.25x input."""
        total = 0.0
        for c in self.calls:
            price = _PRICE_PER_MTOK.get(c.model)
            if price is None:
                return None
            in_rate, out_rate = price
            total += (c.input_tokens * in_rate
                      + c.cache_read_tokens * in_rate * 0.1
                      + c.cache_write_tokens * in_rate * 1.25
                      + c.output_tokens * out_rate) / 1_000_000
        return total


# Session-scoped ledger. A ContextVar (not a module global) so it is isolated per call stack and
# trivially reset — cli.py opens `track_usage()` around a command; core just records if one is active.
_LEDGER: contextvars.ContextVar[UsageLedger | None] = contextvars.ContextVar("usage_ledger", default=None)


@contextmanager
def track_usage():
    """Scope a UsageLedger over a block. `_complete()` records into it; nothing else changes."""
    ledger = UsageLedger()
    token = _LEDGER.set(ledger)
    try:
        yield ledger
    finally:
        _LEDGER.reset(token)


def _record(rec: CallRecord) -> None:
    ledger = _LEDGER.get()
    if ledger is not None:
        ledger.record(rec)


def available_cards() -> list[str]:
    """Stems of the loadable context cards (non-`_`-prefixed), in load order — the vocabulary of the
    `--context` selector."""
    return [p.stem for p in sorted(CONTEXT.glob("*.md")) if not p.name.startswith("_")]


def load_context(only: list[str] | None = None) -> str:
    """Concatenate the context cards. `only` (card stems) restricts the set — this is how a session
    trims irrelevant cards so they don't dilute impact estimation (every card is loaded otherwise).
    Selection is per-session, so the assembled system stays byte-identical across a run's calls and
    the prompt cache still holds."""
    keep = None if only is None else {c.lower() for c in only}
    cards = []
    for path in sorted(CONTEXT.glob("*.md")):
        if path.name.startswith("_"):
            continue
        if keep is not None and path.stem.lower() not in keep:
            continue
        cards.append(f"## {path.stem}\n{path.read_text()}")
    return "\n\n".join(cards)


def build_prompt(name: str, only: list[str] | None = None) -> str:
    """Load a prompt file and inject the schema + product context (optionally a subset of cards)."""
    schema = (FRAMEWORK / "model_schema.json").read_text()
    text = (PROMPTS / name).read_text()
    return text.replace("{{SCHEMA}}", schema).replace("{{CONTEXT}}", load_context(only))


def _response_text(resp) -> str:
    """All text blocks of the response, concatenated — skips thinking/tool_use blocks. Joining
    (rather than taking only the first) means a reply split across text blocks isn't silently
    truncated to its opening fragment before JSON extraction."""
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


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


def _complete(client: Anthropic, system: str, messages: list[dict], out_model, retries: int = 2,
              validate=None):
    """One call → validated `out_model`. Retries with a nudge on malformed/non-conformant JSON.
    The nudge lives in a local copy so the caller's clean history is never polluted.

    `validate` is an optional semantic post-check `(instance) -> None` that raises `ValueError` to
    reject an output Pydantic accepted but the caller still considers incomplete (e.g. a discovery
    model missing required slots). It rides the same retry loop, so the model self-corrects."""
    attempt = messages
    last_err = None
    model = current_model_name()
    rec = CallRecord(model=model, attempts=0)
    started = time.perf_counter()

    def _stop(msg: str) -> EngineError:
        # Record the spend and stamp latency before surfacing a clean failure — a failed call still
        # billed for whatever it consumed, and the ledger should reflect it.
        rec.latency_ms = int((time.perf_counter() - started) * 1000)
        _record(rec)
        return EngineError(msg)

    for _ in range(retries + 1):
        rec.attempts += 1
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=MAX_OUTPUT_TOKENS,
                # The system prompt (template + schema + every context card) is byte-identical
                # across the calls of a session — the K runs of a golden capture, the up-to-8 turns
                # of converse(), each JSON retry. Caching its prefix makes those repeats cost ~0.1x
                # input instead of full price. No effect on output, so baselines are unaffected.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=attempt,
            )
        except APIError as e:
            # Network drop, timeout, rate limit, provider outage — anything from the transport. Turn
            # it into a clean, actionable message instead of a raw traceback. The saved model is
            # untouched (nothing was written yet), so retrying the command is always safe.
            raise _stop(
                "Anthropic API unavailable — the request could not be completed "
                f"({type(e).__name__}: {e}).\n"
                "The model on disk was not modified. Retry the command in a moment."
            ) from e
        # Accumulate usage across every attempt — a retry spends tokens too (fields absent on the
        # test fake, so default to 0 and this stays a no-op offline).
        u = getattr(resp, "usage", None)
        if u is not None:
            rec.input_tokens += getattr(u, "input_tokens", 0) or 0
            rec.output_tokens += getattr(u, "output_tokens", 0) or 0
            rec.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
            rec.cache_write_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0
        raw = _response_text(resp)
        truncated = getattr(resp, "stop_reason", None) == "max_tokens"
        try:
            result = out_model.model_validate(_extract_json(raw))
            if validate is not None:
                validate(result)
            rec.latency_ms = int((time.perf_counter() - started) * 1000)
            _record(rec)
            return result
        except (json.JSONDecodeError, ValueError, ValidationError) as e:
            last_err = e
            # A reply cut off at the token ceiling can't be salvaged by retrying (the same ceiling
            # truncates again), so surface it as a clean, specific failure. We check this *only on a
            # parse/validation failure*: a response can hit the ceiling yet still contain complete,
            # valid JSON — those must succeed above, not be rejected. (Rich discovery outputs run
            # right up against the ceiling; MAX_OUTPUT_TOKENS gives them headroom so this stays rare.)
            if truncated:
                raise _stop(
                    "The model's reply was cut off at the output limit "
                    f"(max_tokens={MAX_OUTPUT_TOKENS}) — the result would be incomplete, so it was "
                    "discarded. Narrow the request, or split it into fewer features per run."
                ) from e
            attempt = attempt + [
                {"role": "assistant", "content": raw or "(empty)"},
                {"role": "user", "content": f"Your reply did not match the required schema ({e}). Reply with ONLY the JSON object, no prose, no code fence."},
            ]
    rec.latency_ms = int((time.perf_counter() - started) * 1000)
    _record(rec)  # record the spend even on give-up — those tokens were still billed
    raise RuntimeError(f"No schema-valid JSON after {retries + 1} attempts: {last_err}")
