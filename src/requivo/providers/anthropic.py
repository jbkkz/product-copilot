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
import hashlib
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
    ModelProposal,
    ReleaseNotes,
    Stories,
)
from requivo.core.errors import ProviderOutputError, RequivoError
from requivo.core.validation import completeness_gap

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


def _system_blocks(system: str, reuse_system: bool) -> list[dict]:
    """The `system` argument for one request — carrying a cache breakpoint only when something will
    read it.

    A `cache_control` breakpoint bills the block at **1.25x** input to write and **0.1x** to read, so
    it saves money from the second send of a byte-identical prefix and loses ~25% if there is never a
    second send. Which of those it is depends entirely on the caller's loop, and this is a fixed cost
    the caller alone can predict — hence the parameter rather than a rule here.

    It genuinely pays *within* one operation: a JSON retry re-sends the identical system, `converse()`
    runs up to 8 discovery turns off one prompt, and a golden capture runs K of them.

    **The retry case is the accepted cost of `reuse_system=False`, and is stated here rather than
    glossed.** A one-shot generator that *does* retry now pays full price twice (2.0x the system block)
    where caching would have paid 1.25x + 0.1x = 1.35x. That is a real regression on that path, taken
    deliberately: with `p` the probability of a retry, not caching wins while `1 + p < 1.25 + 0.1p`,
    i.e. `p < ~0.28`, and a contract violation from these generators is far rarer than that. Caching
    only from the second attempt was considered and rejected — it costs 1.0 + 1.25 = 2.25x on two
    attempts, worse than the 2.0x above, and comes out ahead only past the same ~0.28 threshold at
    which simply caching everywhere would have been the right call anyway.

    It cannot pay *across* operations, and no breakpoint placement can change
    that: `build_prompt()` substitutes the shared schema + context cards into a **per-operation**
    template, and every template puts `{{SCHEMA}}`/`{{CONTEXT}}` near its end with an "Output format"
    section after them. The shared bulk is a *suffix*, caching is a *prefix* match, and a suffix has
    no prefix boundary to cache at — so a second operation could never hit a warm entry however many
    of the API's four breakpoints were spent on it. The comment that used to sit here claimed
    byte-identity "across the calls of a session" and was true only of the first list (#9).

    Making it pay across operations means moving the shared bulk to the **front** of all eight
    templates. That is a change to what the model reads, so it owes the golden harness a cycle
    (`docs/evaluations.md`) and is deliberately not bundled here.
    """
    block = {"type": "text", "text": system}
    if reuse_system:
        block["cache_control"] = {"type": "ephemeral"}
    return [block]


def _complete(client, system: str, messages: list[dict], out_model, retries: int = 2,
              validate=None, *, reuse_system: bool = True):
    """One call → validated `out_model`. Retries with a nudge on malformed/non-conformant JSON.
    The nudge lives in a local copy so the caller's clean history is never polluted.

    `validate` is an optional semantic post-check `(instance) -> None` that raises `ValueError` to
    reject an output Pydantic accepted but the caller still considers incomplete (e.g. a discovery
    model missing required slots). It rides the same retry loop, so the model self-corrects.

    `reuse_system` is the caller's answer to "will this exact system prompt be sent again inside the
    cache TTL?" — see `_system_blocks`. It defaults to True because that is the safe answer to an
    unknown: mistakenly caching costs 25% once, mistakenly not caching costs the full price of every
    repeat. Only a caller that *knows* it makes one call should say False."""
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
                system=_system_blocks(system, reuse_system),
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
    # A structured Requivo error, not a bare RuntimeError: exhausting the retry loop is a *known*
    # provider condition with an actionable cause, and every surface catches RequivoError. Raised as
    # anything else, it escaped the CLI's handler and reached the user as a traceback.
    raise ProviderOutputError(
        f"the provider returned output that did not match the {out_model.__name__} contract after "
        f"{retries + 1} attempts — the last failure was: {last_err}",
        details={"contract": out_model.__name__, "attempts": retries + 1, "last_error": str(last_err)},
    ) from last_err


# ── Discovery ─────────────────────────────────────────────────────────────────


def _require_complete_model(out: ModelProposal) -> None:
    """A discovery turn must return the whole required slot set, and must say what the thing is for.

    The rules themselves live in `core.validation.completeness_gap`, shared with the deterministic
    apply path so the two boundaries cannot drift. What is local here is the *shape* of the failure:
    a plain `ValueError`, which `_complete()`'s retry loop feeds back to the model as a corrective
    nudge, so it self-corrects instead of the turn dying. Neither rule is in the contract itself,
    because a partial model is a legitimate internal object (a diff basis, a projection) — it is only
    a *discovery reply* that owes completeness.
    """
    gap = completeness_gap(out)
    if gap is not None:
        raise ValueError(gap.message)


def run(client, messages: list[dict], retries: int = 2, only: list[str] | None = None,
        carry_from: EngineOutput | None = None, *, reuse_system: bool = True) -> EngineOutput:
    """Engine turn: request/answers → filled model. `only` restricts which context cards inform the
    turn (defaults to all); keep it constant across a session's turns so the prompt cache holds.

    The reply is parsed as a `ModelProposal`, not an `EngineOutput`, because `engine.md` asks for
    `model`/`questions`/`summary` and nothing else: a turn that says nothing about decisions or
    challenges is *quiet*, not deleting them. `carry_from` is the model being refined — the established
    reasoning is carried onto the reply, so what leaves this function is a complete model again.

    `reuse_system` is the one thing this function cannot decide, so it is the caller's (#58). The
    engine prompt is the one genuinely re-sent byte-identically — `converse()` runs up to 8 turns off
    it and a golden capture runs K — and the completeness `validate` hook below makes a corrective
    retry likelier here than anywhere else, so the breakpoint earns its 1.25x write on those paths.
    It does not on the single-call ones: `AnthropicProvider.analyze` is one call per service
    operation whichever branch it takes, and it now says so rather than inheriting a default written
    for a loop. The default stays True because that is the safe answer to an unknown — mistakenly
    caching costs 25% once, mistakenly not caching costs full price on every repeat (`_complete`)."""
    proposal = _complete(client, build_prompt("engine.md", only), messages, ModelProposal, retries,
                         validate=_require_complete_model, reuse_system=reuse_system)
    return proposal.resolve(carry_from)


def answer_turn(client, out: EngineOutput, request: str, answers: str,
                only: list[str] | None = None, *, reuse_system: bool = False) -> EngineOutput:
    """One stateless discovery turn: refine the model with new answers.

    The model IS the accumulated state, so a turn needs only the original request (for context),
    the current model, and the new answers — no live conversation loop. This is what lets any
    interface (Claude Code, an API, an MCP) drive discovery turn by turn instead of a blocking TTY.

    `only` is the context-card selection the original discovery used (from its session.json) — passing
    it keeps a refinement turn reasoning over the same cards, not silently the full set.

    **Single-call by construction, hence `reuse_system=False`** (#58). This function *is* the whole
    turn: it assembles a fresh message list, makes one call and returns — there is no loop here for a
    cached system block to be read back by, so the breakpoint was a flat ~25% surcharge on the write
    (#9). Checked rather than assumed, because the argument is about callers, not about this body:
    every surface that reaches it makes one call per operation — `requivo answer` per invocation,
    `POST /sessions/{slug}/answer` per request, one Claude Code turn — and the multi-turn caller,
    `converse()`, does not come through here at all. It calls `run()` directly and keeps the
    breakpoint. A caller that genuinely loops passes True and gets it back."""
    messages = [
        {"role": "user", "content": request},
        {"role": "assistant", "content": out.model_dump_json()},
        {"role": "user", "content": "Client answers:\n" + answers},
    ]
    return run(client, messages, only=only, carry_from=out, reuse_system=reuse_system)


# ── Generators (model → artifact) ───────────────────────────────────────────────
# Every generator threads `only` — the context-card selection its discovery ran against, read from
# session.json by the CLI — so an artifact is grounded in the same cards discovery used, not silently
# the full set. None means all cards (the default and the pre-0.6.1 behaviour).


# Every generator below is **one** `_complete` call, so its system prompt was being written to cache
# and never read back — `reuse_system=False` is the default here for that reason (#9). It stays a
# parameter rather than a constant because the same function is single-call in production and
# multi-call in the harness: `scripts/golden_run.py --brief` calls `advise()` K times off one prompt,
# and that caller should pass `reuse_system=True`. `AnthropicProvider.generate` threads it through
# `**kwargs`, so a future looping caller has the same escape hatch without another signature change.


def derive_stories(client, out: EngineOutput, only: list[str] | None = None, *,
                   reuse_system: bool = False) -> Stories:
    """Pipeline stage: a filled model → implementable user stories."""
    system = build_prompt("stories.md", only)
    user = "Completed requirements model to decompose into user stories:\n" + out.model_dump_json(indent=2)
    return _complete(client, system, [{"role": "user", "content": user}], Stories,
                     reuse_system=reuse_system)


def advise(client, out: EngineOutput, only: list[str] | None = None, *,
           reuse_system: bool = False) -> Brief:
    """Finalization stage: a completed model → design considerations, risks, opportunities."""
    system = build_prompt("brief.md", only)
    user = "Completed requirements model to advise on:\n" + out.model_dump_json(indent=2)
    return _complete(client, system, [{"role": "user", "content": user}], Brief,
                     reuse_system=reuse_system)


def generate_prd(client, out: EngineOutput, only: list[str] | None = None, *,
                 reuse_system: bool = False) -> PRD:
    """Artifact generator: a model → a Product Requirements Document."""
    system = build_prompt("prd.md", only)
    user = "Completed requirements model to turn into a PRD:\n" + out.model_dump_json(indent=2)
    return _complete(client, system, [{"role": "user", "content": user}], PRD,
                     reuse_system=reuse_system)


def generate_criteria(client, out: EngineOutput, only: list[str] | None = None, *,
                      reuse_system: bool = False) -> AcceptanceCriteria:
    """Artifact generator: a model → Given/When/Then acceptance criteria (the recette checklist)."""
    system = build_prompt("criteria.md", only)
    user = "Completed requirements model to turn into acceptance criteria:\n" + out.model_dump_json(indent=2)
    return _complete(client, system, [{"role": "user", "content": user}], AcceptanceCriteria,
                     reuse_system=reuse_system)


def generate_epic(client, out: EngineOutput, only: list[str] | None = None, *,
                  reuse_system: bool = False) -> Epic:
    """Artifact generator: a model → a delivery epic (work breakdown into trackable issues)."""
    system = build_prompt("epic.md", only)
    user = "Completed requirements model to turn into a delivery epic:\n" + out.model_dump_json(indent=2)
    return _complete(client, system, [{"role": "user", "content": user}], Epic,
                     reuse_system=reuse_system)


def generate_release(client, out: EngineOutput, version: str = "",
                     only: list[str] | None = None, *,
                     reuse_system: bool = False) -> ReleaseNotes:
    """Artifact generator: a model → client-facing release notes. The caller may stamp a version."""
    system = build_prompt("release.md", only)
    user = "Completed requirements model to turn into release notes:\n" + out.model_dump_json(indent=2)
    notes = _complete(client, system, [{"role": "user", "content": user}], ReleaseNotes,
                      reuse_system=reuse_system)
    if version:
        notes.version = version
    return notes


def estimate(client, out: EngineOutput, stories: Stories,
             only: list[str] | None = None, *,
             reuse_system: bool = False) -> tuple[EstimateDraft, list[str], str]:
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
    draft = _complete(client, system, [{"role": "user", "content": user}], EstimateDraft,
                      reuse_system=reuse_system)
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

# The prompt file behind each operation — what `prompt_version()` hashes to identify the reasoning that
# produced a revision. `analyze` is the discovery turn; the rest are the artifact types.
_OP_PROMPTS = {
    "analyze": "engine.md", "brief": "brief.md", "stories": "stories.md", "estimate": "estimate.md",
    "prd": "prd.md", "criteria": "criteria.md", "epic": "epic.md", "release": "release.md",
}


def prompt_version(op: str, only: list[str] | None = None) -> str:
    """`"sha256:…"` over the exact system prompt an operation sends — the prompt file, the schema, and
    the selected context cards, byte for byte.

    This is what makes a revision traceable rather than merely timestamped. Behaviour here is tuned by
    editing Markdown and JSON assets, so "which model produced this" answers half the question; the
    other half is "against which prompt and which context cards", and that is exactly what changes
    between two runs that look identical in the log. A card added to the set moves the hash, because it
    genuinely moved the reasoning."""
    return "sha256:" + hashlib.sha256(build_prompt(_OP_PROMPTS[op], only).encode("utf-8")).hexdigest()


class AnthropicProvider:
    """`ReasoningProvider` over the Anthropic SDK. Holds a client so the free functions above (which
    tests still exercise directly with a fake client) stay the single implementation — the object is
    a thin, uniform face over them for callers that want the provider seam."""

    name = "anthropic"

    def __init__(self, client=None):
        self.client = client or new_client()

    def analyze(self, request: str, *, current_model: EngineOutput | None = None,
                answers: str | None = None, only: list[str] | None = None) -> EngineOutput:
        """One reasoning turn, on either branch — and **one call**, which is why both say
        `reuse_system=False` (#58).

        This is where the caching question is actually decidable. `DiscoveryService` reaches this
        once per operation on every path it has — `start`, `run_discovery`, `answer` — so the system
        block was being written to cache at 1.25x and never read back. The free functions below
        cannot know that: `run()` is *also* called directly by `converse()`, which sends the
        identical prompt for up to 8 turns, and by the golden harness, which sends it K times. Both
        keep the breakpoint by keeping `run()`'s default.

        The issue that filed this named `converse()`'s `--once` branch as one of the two sites. That
        branch no longer calls `run()` — since the DiscoveryService seam it goes through `start()`
        and lands here — so the site moved while the fact about it did not."""
        if current_model is not None and answers is not None:
            return answer_turn(self.client, current_model, request, answers, only=only,
                               reuse_system=False)
        return run(self.client, [{"role": "user", "content": request}], only=only,
                   reuse_system=False)

    def generate(self, artifact_type: str, model: EngineOutput, *, only: list[str] | None = None,
                 **kwargs):
        """`**kwargs` carries the few per-artifact options a generator takes (release notes accept a
        `version` to stamp); an option a generator does not know is a TypeError, not a silent no-op."""
        try:
            fn = _GENERATORS[artifact_type]
        except KeyError as e:
            raise EngineError(f"unknown artifact type for the Anthropic provider: {artifact_type!r}") from e
        return fn(self.client, model, only=only, **kwargs)

    def model_name(self) -> str:
        return current_model_name()

    def provenance(self, op: str, *, only: list[str] | None = None) -> dict:
        """Who reasoned, with what, against which prompt — the fields a revision records. The service
        asks the provider for this instead of assembling it, so a second provider cannot silently
        stamp revisions as `anthropic`, and the prompt identity comes from the layer that owns it."""
        return {"provider": self.name, "model_name": self.model_name(),
                "prompt_version": prompt_version(op, only)}
