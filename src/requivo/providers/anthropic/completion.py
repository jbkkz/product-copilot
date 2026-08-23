"""One call to the model: the request, the retry loop, the JSON extraction, the truncation check.

Everything here is about a single `_complete()` — what is sent, what comes back, and what is billed
for it. The discovery turn and the generators are `generators.py`; they all funnel through here.

**The one constraint this module owes another one.** `_complete` records the spend into
`requivo.usage`, and it must do so **before** it surfaces a clean failure, on every exit — a failed
call is still billed for whatever it consumed. That used to be two adjacent lines in one file and is
now a cross-module contract, which is exactly what #74 flagged as the thing most likely to break
quietly in the split. There are four exits and all four record: the success return, the transport
failure and the truncation refusal (both through `_stop()`, which exists so the two clean failures
have one place to get it right), and the retry give-up. Only `_stop()` reads as obviously about
billing, which is why the give-up carries its own line saying so.
`test_a_failed_call_is_still_recorded_on_every_exit` goes red when any exit stops recording.
"""

from __future__ import annotations

import json
import re
import time

from pydantic import ValidationError

from requivo.core.errors import ProviderOutputError
from requivo.providers.anthropic.client import APIError, current_model_name
from requivo.providers.anthropic.pricing import price_call
from requivo.providers.errors import EngineError
from requivo.usage import CallRecord, record_call

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


def _record(rec: CallRecord) -> None:
    """Price the call at Anthropic's rates, then file it against whatever ledger is active.

    The one place this provider meets `requivo.usage`, so the vendor's price table is consulted here
    and the ledger stays neutral (#167). Called exactly once per `CallRecord` -- `_complete` builds
    one and reaches one exit with it -- which is why `price_call` needs no first-write-wins guard and
    deliberately has none.
    """
    record_call(price_call(rec))


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
    repeat. Only a caller that *knows* it makes one call should say False.

    Every exit records the spend first — see this module's docstring, and `_stop()` below."""
    attempt = messages
    last_err = None
    model = current_model_name()
    rec = CallRecord(model=model, attempts=0)
    started = time.perf_counter()

    def _stop(msg: str) -> EngineError:
        # Record the spend and stamp latency before surfacing a clean failure — a failed call still
        # billed for whatever it consumed, and the ledger should reflect it. Both *clean* failure
        # exits go through here so there is one place to get it right; the retry give-up below is
        # the third and records inline. See the module docstring, pinned by
        # `test_a_failed_call_is_still_recorded_on_every_exit`.
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
