"""The Anthropic reasoning provider — the only module that calls the Anthropic API.

Everything provider-specific lives here: the SDK client, the single-call/retry loop (`_complete`),
JSON extraction, the usage ledger and cost estimate, and the discovery/generation calls that turn a
request into a model and a model into an artifact. `requivo.core` imports none of this. `anthropic`
is an **optional** dependency (`requivo[anthropic]`); importing this module without the SDK installed
raises a clean, actionable error rather than an ImportError deep in a call stack.

Prompt assembly (schema + context injection) is deterministic and lives in `core.context`; this
module only *feeds* the assembled prompt to the model.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date

from pydantic import ValidationError

from requivo.core.analysis import estimate_confidence, soft_slots
from requivo.core.context import build_prompt
from requivo.core.contracts import (
    PRD,
    AcceptanceCriteria,
    Brief,
    EngineOutput,
    Epic,
    EstimateDraft,
    ReleaseNotes,
    Stories,
    missing_required_slots,
)
from requivo.core.errors import RequivoError

try:  # The SDK is an optional extra: the deterministic core + CLI work without it (Claude Code mode).
    from anthropic import Anthropic, APIError
except ImportError as _e:  # pragma: no cover - exercised only in a no-SDK install
    Anthropic = None  # type: ignore[assignment,misc]
    APIError = Exception  # type: ignore[assignment,misc]
    _IMPORT_ERROR = _e
else:
    _IMPORT_ERROR = None

MODEL_DEFAULT = "claude-sonnet-5"

# Output-token ceiling per call. Discovery emits a full slot model + questions + summary; on a rich
# multi-feature request that JSON exceeds 8k output tokens and the whole reply is discarded as
# truncated (observed: a messy 5-feature request truncated at 8k). claude-sonnet-5 actually caps at
# 128k output, but this path is a *non-streaming* client.messages.create(), and the SDK raises / risks
# HTTP timeouts above ~16k without streaming — so 16k is the safe ceiling here. It fits a rich
# discovery run with headroom, and you pay only for tokens generated, so raising it costs nothing on
# smaller outputs (and never changes an output that already fit — golden baselines are unaffected).
# Going higher (32k–128k) needs the call switched to streaming; a per-generator budget (the assessment
# needs less than an epic) is a further refinement. One safe ceiling first.
MAX_OUTPUT_TOKENS = 16000


class EngineError(RequivoError):
    """A clean, provider-transport failure (API unavailable, output truncated). The CLI catches this
    and prints the message without a traceback. A run that raises this never modifies the saved model —
    the call failed before any write. It is a `RequivoError` so a single `except RequivoError` at the
    CLI boundary catches both reasoning-transport and core-validation failures."""

    code = "provider_unavailable"


def new_client() -> Anthropic:
    """Construct an Anthropic client, or raise a clean error if the optional SDK is not installed.
    Every provider-backed CLI verb funnels through here so the 'install requivo[anthropic]' guidance
    is stated once, not scattered."""
    if Anthropic is None:
        raise EngineError(
            "The Anthropic provider is not installed. Install it with `pip install 'requivo[anthropic]'` "
            "(or `uv tool install 'requivo[anthropic]'`). You do NOT need it to use Requivo inside "
            f"Claude Code — that mode uses no API key. (import error: {_IMPORT_ERROR})"
        )
    return Anthropic()


def current_model_name() -> str:
    """The model id this process will call — the env override or the default. Exposed so provenance
    (session.json) records the exact model a discovery ran against."""
    return os.getenv("MODEL", MODEL_DEFAULT)


# USD per 1M tokens (input, output), from the Anthropic pricing reference as of 2026-08-01. This
# yields an *estimate*, never a bill: prices drift and intro rates lapse, so the renderer stamps this
# date and labels the number an estimate. Tokens (below) are ground truth from the API; cost is the
# only thing here that can go stale — keep this table updateable and honest, not authoritative.
PRICING_AS_OF = "2026-08-01"
_PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Launch pricing that lapses on a known date: model → (input, output, last day inclusive). A dated
# table with no notion of expiry is wrong twice — it over-reports while an intro rate is live, then
# under-reports the day someone edits the rate in and forgets the lapse. Encoding the end date lets
# the estimate be right on both sides of it without another edit. `claude-sonnet-5` (the default
# model) is on launch pricing through 2026-08-31, reverting to the standard 3.00/15.00 above.
_LAUNCH_PRICE_PER_MTOK: dict[str, tuple[float, float, str]] = {
    "claude-sonnet-5": (2.00, 10.00, "2026-08-31"),
}


def price_per_mtok(model: str, on: date | None = None) -> tuple[float, float] | None:
    """The (input, output) USD rate per million tokens for `model` on a given day, or None when the
    model's price is unknown — never guess a price. `on` defaults to today, so a running estimate
    follows a launch rate over its expiry without a code change."""
    launch = _LAUNCH_PRICE_PER_MTOK.get(model)
    if launch is not None:
        in_rate, out_rate, until = launch
        if (on or date.today()) <= date.fromisoformat(until):
            return in_rate, out_rate
    return _PRICE_PER_MTOK.get(model)


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
    """Accumulates the API usage of a session (one `requivo` command). Presentation-free — the renderer
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

    def cost_usd(self, on: date | None = None) -> float | None:
        """Estimated USD across all calls, or None if any model's price is unknown (never guess a
        price). Cache reads bill ~0.1x input, cache writes ~1.25x input. `on` fixes the day the rates
        are read for (launch pricing lapses); it defaults to today and exists so a test can assert both
        sides of an expiry without waiting for it."""
        total = 0.0
        for c in self.calls:
            price = price_per_mtok(c.model, on)
            if price is None:
                return None
            in_rate, out_rate = price
            total += (c.input_tokens * in_rate
                      + c.cache_read_tokens * in_rate * 0.1
                      + c.cache_write_tokens * in_rate * 1.25
                      + c.output_tokens * out_rate) / 1_000_000
        return total


# Session-scoped ledger. A ContextVar (not a module global) so it is isolated per call stack and
# trivially reset — cli.py opens `track_usage()` around a command; the provider just records if one is
# active.
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


def _complete(client, system: str, messages: list[dict], out_model, retries: int = 2,
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


# ── Discovery ─────────────────────────────────────────────────────────────────


def _require_complete_model(out: EngineOutput) -> None:
    """A discovery turn must return the whole required slot set. A model missing a required slot
    isn't just incomplete — that slot becomes invisible to readiness and every view, so a
    high-impact gap could pass silently as 'ready'. Reject it here; the retry loop makes the model
    re-emit the missing slots (the prompt already asks for all of them each turn)."""
    missing = missing_required_slots(set(out.model))
    if missing:
        raise ValueError(f"model is missing required slots: {missing}. Emit every schema slot.")


def run(client, messages: list[dict], retries: int = 2,
        only: list[str] | None = None) -> EngineOutput:
    """Engine turn: request/answers → filled model. `only` restricts which context cards inform the
    turn (defaults to all); keep it constant across a session's turns so the prompt cache holds."""
    return _complete(client, build_prompt("engine.md", only), messages, EngineOutput, retries,
                     validate=_require_complete_model)


def answer_turn(client, out: EngineOutput, request: str, answers: str,
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


# ── Generators (model → artifact) ───────────────────────────────────────────────
# Every generator threads `only` — the context-card selection its discovery ran against, read from
# session.json by the CLI — so an artifact is grounded in the same cards discovery used, not silently
# the full set. None means all cards (the default and the pre-0.6.1 behaviour).


def derive_stories(client, out: EngineOutput, only: list[str] | None = None) -> Stories:
    """Pipeline stage: a filled model → implementable user stories."""
    system = build_prompt("stories.md", only)
    user = "Completed requirements model to decompose into user stories:\n" + out.model_dump_json(indent=2)
    return _complete(client, system, [{"role": "user", "content": user}], Stories)


def advise(client, out: EngineOutput, only: list[str] | None = None) -> Brief:
    """Finalization stage: a completed model → design considerations, risks, opportunities."""
    system = build_prompt("brief.md", only)
    user = "Completed requirements model to advise on:\n" + out.model_dump_json(indent=2)
    return _complete(client, system, [{"role": "user", "content": user}], Brief)


def generate_prd(client, out: EngineOutput, only: list[str] | None = None) -> PRD:
    """Artifact generator: a model → a Product Requirements Document."""
    system = build_prompt("prd.md", only)
    user = "Completed requirements model to turn into a PRD:\n" + out.model_dump_json(indent=2)
    return _complete(client, system, [{"role": "user", "content": user}], PRD)


def generate_criteria(client, out: EngineOutput, only: list[str] | None = None) -> AcceptanceCriteria:
    """Artifact generator: a model → Given/When/Then acceptance criteria (the recette checklist)."""
    system = build_prompt("criteria.md", only)
    user = "Completed requirements model to turn into acceptance criteria:\n" + out.model_dump_json(indent=2)
    return _complete(client, system, [{"role": "user", "content": user}], AcceptanceCriteria)


def generate_epic(client, out: EngineOutput, only: list[str] | None = None) -> Epic:
    """Artifact generator: a model → a delivery epic (work breakdown into trackable issues)."""
    system = build_prompt("epic.md", only)
    user = "Completed requirements model to turn into a delivery epic:\n" + out.model_dump_json(indent=2)
    return _complete(client, system, [{"role": "user", "content": user}], Epic)


def generate_release(client, out: EngineOutput, version: str = "",
                     only: list[str] | None = None) -> ReleaseNotes:
    """Artifact generator: a model → client-facing release notes. The caller may stamp a version."""
    system = build_prompt("release.md", only)
    user = "Completed requirements model to turn into release notes:\n" + out.model_dump_json(indent=2)
    notes = _complete(client, system, [{"role": "user", "content": user}], ReleaseNotes)
    if version:
        notes.version = version
    return notes


def estimate(client, out: EngineOutput, stories: Stories,
             only: list[str] | None = None) -> tuple[EstimateDraft, list[str], str]:
    """Pipeline stage: stories + the model's soft slots → a day-based estimate.
    Returns (draft, soft_slots, confidence) — the latter two are Python-authoritative."""
    soft = soft_slots(out)
    system = build_prompt("estimate.md", only)
    user = (
        "User stories to estimate:\n"
        + stories.model_dump_json(indent=2)
        + "\n\nUnresolved (soft) slots — widen the range for any story that depends on one:\n"
        + (", ".join(soft) if soft else "(none — the model is solid)")
    )
    draft = _complete(client, system, [{"role": "user", "content": user}], EstimateDraft)
    return draft, soft, estimate_confidence(len(soft))


# ── Provider object ─────────────────────────────────────────────────────────────

_GENERATORS = {
    "brief": advise,
    "stories": derive_stories,
    "prd": generate_prd,
    "criteria": generate_criteria,
    "epic": generate_epic,
    "release": generate_release,
}


class AnthropicProvider:
    """`ReasoningProvider` over the Anthropic SDK. Holds a client so the free functions above (which
    tests still exercise directly with a fake client) stay the single implementation — the object is
    a thin, uniform face over them for callers that want the provider seam."""

    name = "anthropic"

    def __init__(self, client=None):
        self.client = client or new_client()

    def analyze(self, request: str, *, current_model: EngineOutput | None = None,
                answers: str | None = None, only: list[str] | None = None) -> EngineOutput:
        if current_model is not None and answers is not None:
            return answer_turn(self.client, current_model, request, answers, only=only)
        return run(self.client, [{"role": "user", "content": request}], only=only)

    def generate(self, artifact_type: str, model: EngineOutput, *, only: list[str] | None = None):
        try:
            fn = _GENERATORS[artifact_type]
        except KeyError as e:
            raise EngineError(f"unknown artifact type for the Anthropic provider: {artifact_type!r}") from e
        return fn(self.client, model, only=only)

    def model_name(self) -> str:
        return current_model_name()
