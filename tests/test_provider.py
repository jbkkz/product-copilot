"""The provider call: `providers/anthropic.py`, driven offline against a canned client.

Split out of `test_engine.py` (#72). Everything here reaches the one place an LLM is called — the JSON
extraction and retry loop, the discovery turn and its completeness self-heal, the generators, the
context-card threading, and the prompt-cache breakpoints of #9. No real network: `FakeClient` returns
canned replies in order and records the request that came out, so each test asserts on what would have
been sent.
"""
import io
import json
from contextlib import redirect_stdout

import anthropic
import httpx
import pytest
from _fakes import _ENGINE_REPLY, FakeClient, _FakeBlock, full_slots, out, slot

from requivo.core.contracts import PRD, Brief, EngineOutput, Stories
from requivo.core.persistence import load_model
from requivo.providers.anthropic import (
    CallRecord,
    EngineError,
    UsageLedger,
    _complete,
    _extract_json,
    _response_text,
    advise,
    answer_turn,
    current_model_name,
    derive_stories,
    generate_prd,
    run,
)
from requivo.render.markdown import prd_markdown
from requivo.render.terminal import render_stories

# ── JSON extraction ──────────────────────────────────────────────────────────


def test_extract_json_strips_fence():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_slices_surrounding_text():
    assert _extract_json('here it is: {"b": 2} — done') == {"b": 2}


def test_extract_json_raises_on_garbage():
    with pytest.raises(ValueError):
        _extract_json("no json object anywhere")


# ── Characterization: discovery, generators, errors, context ─────────────────
# These pin CURRENT behavior (shapes, formats, error surfaces). They are not quality tests.


def test_run_returns_engine_output_and_wires_schema_and_context():
    # The --once discovery pass is a single run() call. Characterize its result
    # AND that the engine turn is driven by prompts/engine.md with schema + context injected.
    fake = FakeClient(_ENGINE_REPLY)
    result = run(fake, [{"role": "user", "content": "leave approval"}])
    assert isinstance(result, EngineOutput)
    assert result.model["problem"].completeness == 80
    # system is a cache-controlled text block so its stable prefix is cached across calls.
    block = fake.calls[0]["system"][0]
    assert block["cache_control"] == {"type": "ephemeral"}
    system = block["text"]
    assert "slots" in system              # framework/model_schema.json injected ({{SCHEMA}})
    assert "## b2b-platform" in system    # context card injected ({{CONTEXT}})


def test_run_rejects_a_model_missing_required_slots():
    # A discovery reply missing a required slot is refused: the completeness invariant is enforced at
    # the boundary. The FakeClient returns the same incomplete reply every retry, so run() gives up.
    from requivo.core.errors import ProviderOutputError
    incomplete = json.dumps({
        "model": {"problem": slot(80, "explicit", "high")},  # 1 of 15 required
        "questions": [], "summary": {"objective": "o"},
    })
    fake = FakeClient(incomplete, incomplete, incomplete)  # every retry attempt
    # A RequivoError with a stable code, not a bare RuntimeError: the CLI's handler catches the former
    # and prints a clean message, and lets the latter through as a traceback.
    with pytest.raises(ProviderOutputError, match="missing required slots") as exc:
        run(fake, [{"role": "user", "content": "leave approval"}])
    assert exc.value.to_dict()["code"] == "provider_output_invalid"
    assert exc.value.details["attempts"] == 3


def test_run_self_heals_when_a_retry_completes_the_model():
    # The completeness check rides the existing retry loop: a first incomplete reply nudges the model,
    # and a complete reply on the next attempt is accepted. This is why the invariant is safe to
    # enforce on a non-deterministic model — an omission is corrected, not fatal.
    incomplete = json.dumps({
        "model": {"problem": slot(80, "explicit", "high")},
        "questions": [], "summary": {"objective": "o"},
    })
    fake = FakeClient(incomplete, _ENGINE_REPLY)  # 1st attempt short, 2nd complete
    result = run(fake, [{"role": "user", "content": "leave approval"}])
    assert result.model["problem"].completeness == 80
    assert len(fake.calls) == 2  # it took a retry
    # the nudge names the missing slots so the model knows what to add
    nudge = fake.calls[1]["messages"][-1]["content"]
    assert "missing required slots" in nudge


def test_generate_prd_from_saved_model_roundtrip(tmp_path):
    # The --from path: reload a saved model and regenerate an artifact, no discovery.
    model = out({"problem": slot(80, "explicit", "high")})
    path = tmp_path / "model.json"
    path.write_text(model.model_dump_json())

    loaded = load_model(path)
    assert loaded.model["problem"].completeness == 80

    prd = generate_prd(FakeClient(json.dumps({"title": "Leave approval", "problem": "Approvals are lost in email."})), loaded)
    assert isinstance(prd, PRD) and prd.title == "Leave approval"
    md = prd_markdown(prd)
    assert md.startswith("# Leave approval")
    assert "generated by Requivo" in md


def test_derive_stories_returns_structured_stories():
    reply = json.dumps({"stories": [{"id": "S1", "title": "Submit a leave request"}]})
    stories = derive_stories(FakeClient(reply), out({"problem": slot(80, "explicit", "high")}))
    assert isinstance(stories, Stories)
    assert [s.id for s in stories.stories] == ["S1"]
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_stories(stories)
    text = buf.getvalue()
    assert "=== USER STORIES ===" in text and "[S1] Submit a leave request" in text


def test_run_restricts_context_cards_when_only_given():
    # The --context selection threads run() → build_prompt() → load_context(): the assembled system
    # carries only the chosen card, so it can't dilute impact estimation with the others.
    fake = FakeClient(_ENGINE_REPLY)
    run(fake, [{"role": "user", "content": "leave approval"}], only=["b2b-platform"])
    system = fake.calls[0]["system"][0]["text"]
    assert "## b2b-platform" in system
    assert "## financial-reporting" not in system


def test_answer_turn_threads_the_discovery_context_cards():
    # A refinement turn must reason over the same cards the original discovery used, not silently all.
    fake = FakeClient(_ENGINE_REPLY)
    answer_turn(fake, out({"problem": slot(80, "explicit", "high")}), "req", "answers",
                only=["event-ops"])
    system = fake.calls[0]["system"][0]["text"]
    assert "## event-ops" in system
    assert "## financial-reporting" not in system


def test_generators_thread_the_context_selection():
    # A generator grounds its artifact in the discovery's card subset, not the full set.
    fake = FakeClient(json.dumps({"complexity": "low", "solution": "S"}))
    advise(fake, out({"problem": slot(80, "explicit", "high")}), only=["financial-reporting"])
    system = fake.calls[0]["system"][0]["text"]
    assert "## financial-reporting" in system
    assert "## b2b-platform" not in system


def test_response_text_concatenates_text_blocks_and_skips_others():
    class _Block:
        def __init__(self, type_, text=""):
            self.type = type_
            self.text = text

    class _Resp:
        content = [_Block("thinking", "IGNORE"), _Block("text", "abc"), _Block("text", "def")]

    assert _response_text(_Resp()) == "abcdef"


class _RaisingClient:
    """A client whose create() raises — to exercise the API-error boundary in _complete()."""

    def __init__(self, exc):
        self._exc = exc
        self.messages = self

    def create(self, **kwargs):
        raise self._exc


def test_complete_wraps_api_errors_as_a_clean_engine_error():
    exc = anthropic.APIConnectionError(message="boom", request=httpx.Request("POST", "https://api.anthropic.com"))
    with pytest.raises(EngineError) as ei:
        _complete(_RaisingClient(exc), "sys", [{"role": "user", "content": "x"}], EngineOutput)
    assert "not modified" in str(ei.value)  # the reassurance that nothing was written


class _MaxTokensClient:
    """Returns a reply flagged as cut off at the token ceiling (stop_reason == 'max_tokens'),
    carrying whatever text it is given — so we can exercise both the broken- and complete-JSON cases."""

    def __init__(self, text):
        self._text = text
        self.messages = self

    def create(self, **kwargs):
        text = self._text

        class _Resp:
            stop_reason = "max_tokens"
            content = [_FakeBlock(text)]
        return _Resp()


def test_complete_rejects_a_truncated_reply_that_fails_to_parse():
    # Genuine truncation: the JSON is cut off mid-object, so parsing fails and the ceiling is the
    # named cause — retrying at the same ceiling wouldn't help, so it fails fast and cleanly.
    client = _MaxTokensClient('{"model": {"problem":')
    with pytest.raises(EngineError) as ei:
        _complete(client, "sys", [{"role": "user", "content": "x"}], EngineOutput)
    assert "max_tokens" in str(ei.value)


def test_complete_accepts_a_max_tokens_reply_whose_json_is_complete():
    # Parse-first: rich discovery outputs run right against the ceiling and can be flagged max_tokens
    # while still carrying complete, valid JSON. That must succeed — not be rejected as truncated.
    complete = json.dumps({"model": full_slots(problem=slot(80, "explicit", "high")),
                           "questions": [], "summary": {}})
    result = _complete(_MaxTokensClient(complete), "sys", [{"role": "user", "content": "x"}], EngineOutput)
    assert result.model["problem"].completeness == 80


# ── Prompt-cache breakpoints: paid for only where the prefix is re-read (#9) ──
#
# `cache_control` costs 1.25x input to write and pays back at 0.1x on a read, so it is a saving only
# when the *same* system prompt is sent again inside the cache TTL. It is, across the calls of one
# operation — a JSON retry, converse()'s turns, a golden capture's K runs. It is not, across
# operations: `build_prompt()` substitutes the shared schema + context cards into a *per-operation*
# template, and every template places {{SCHEMA}}/{{CONTEXT}} near its end with an "Output format"
# section after them. The shared bulk is therefore a suffix, and a cache is a prefix match — no
# breakpoint placement can let a second operation hit a warm one. A one-shot generator was writing a
# cache it could never read, a flat ~25% premium on the largest part of its input.
#
# Every "must not fire" assertion below is paired with a "must fire" control in the same fixture: a
# fix that strips the directive everywhere breaks the operations where caching genuinely pays, and
# these tests fail on that too.


def _system_block(fake, i: int) -> dict:
    return fake.calls[i]["system"][0]


_BRIEF_REPLY = json.dumps({"complexity": "low", "solution": "S"})


def test_cache_breakpoint_rides_a_reused_prefix_and_not_a_single_call():
    # Both halves in ONE fixture. Discovery keeps the breakpoint (converse() loops it, the golden
    # harness loops it, a retry re-sends it); a one-shot generator does not.
    fake = FakeClient(_ENGINE_REPLY, _BRIEF_REPLY)
    run(fake, [{"role": "user", "content": "leave approval"}])
    advise(fake, out({"problem": slot(80, "explicit", "high")}))
    assert _system_block(fake, 0)["cache_control"] == {"type": "ephemeral"}  # must fire
    assert "cache_control" not in _system_block(fake, 1)                     # must not fire


def test_the_provider_seam_is_single_call_on_both_analyze_branches():
    """#58: `AnthropicProvider.analyze` is one call per service operation, so it must not write a
    cache entry nothing reads — on *either* branch.

    Driven through the real object rather than by reading a signature, because the defect this
    replaces was a function that took `reuse_system` and dropped it on the floor: `analyze` could
    declare False, pass nothing down, and inherit `run()`'s True.

    **What this test isolates, stated precisely because the first draft of it overclaimed.** Only the
    `run()` arm is pinned here: dropping `analyze`'s explicit `reuse_system=False` on that arm makes
    this red, because `run()`'s own default is True. Dropping it on the `answer_turn` arm does *not*,
    because `answer_turn` already defaults to False — the explicit keyword there is a call-site
    declaration (the design asks for one) sitting on top of a default that agrees with it, not a
    second guard. What pins the `answer_turn` arm is the neighbouring
    `test_a_looping_caller_can_still_ask_for_the_breakpoint_back`, which fails if that default is
    flipped. Both branches are still driven here so the assertion covers the observable behaviour of
    each; the claim about which mutation each one catches is the part that has to be exact.

    The control is in the same fixture and it is the point: `run()` called directly — which is what
    `converse()` and the golden harness do — must still carry the directive. A change that strips
    the breakpoint from `run()` fails here rather than looking like this fix."""
    from requivo.providers.anthropic import AnthropicProvider

    model = out({"problem": slot(80, "explicit", "high")})
    fake = FakeClient(_ENGINE_REPLY, _ENGINE_REPLY, _ENGINE_REPLY)
    provider = AnthropicProvider(fake)

    provider.analyze("leave approval")                                     # first discovery
    provider.analyze("leave approval", current_model=model, answers="A")   # a refinement turn
    run(fake, [{"role": "user", "content": "leave approval"}])             # the multi-call caller

    assert "cache_control" not in _system_block(fake, 0), "a first discovery pays for a cache nothing reads"
    assert "cache_control" not in _system_block(fake, 1), "a refinement turn pays for a cache nothing reads"
    assert _system_block(fake, 2)["cache_control"] == {"type": "ephemeral"}, "converse() lost its breakpoint"
    # MUST FIRE: all three sent the same engine prompt, so the assertions above are about the
    # directive and not about three different system blocks.
    assert _system_block(fake, 0)["text"] == _system_block(fake, 1)["text"] == _system_block(fake, 2)["text"]


def test_a_looping_caller_can_still_ask_for_the_breakpoint_back():
    """The escape hatch, kept honest. `reuse_system` is a per-call-site decision, not a per-function
    one — the same `run()` is single-call under the provider seam and multi-call under `converse()`.
    A future surface that genuinely loops `answer_turn` passes True and gets the directive."""
    model = out({"problem": slot(80, "explicit", "high")})
    fake = FakeClient(_ENGINE_REPLY, _ENGINE_REPLY)
    answer_turn(fake, model, "leave approval", "A")
    answer_turn(fake, model, "leave approval", "A", reuse_system=True)
    assert "cache_control" not in _system_block(fake, 0)                     # must not fire
    assert _system_block(fake, 1)["cache_control"] == {"type": "ephemeral"}  # must fire


# A minimal contract-valid reply per generator, so the assertions below can drive the *real* call
# rather than reading a signature. An earlier version of this test checked
# `inspect.signature(fn).parameters["reuse_system"].default is False` and nothing else, which both
# reviewers independently called vacuous and they were right: a generator that declared the parameter
# and then ignored it — passing nothing to `_complete`, falling back to its `True` default — satisfied
# every assertion while writing exactly the cache entry #9 is about. A signature is not a behaviour.
_GENERATOR_REPLIES = {
    "brief": _BRIEF_REPLY,
    "stories": json.dumps({"stories": [{"id": "S1", "title": "T"}]}),
    "prd": json.dumps({"title": "T", "problem": "P"}),
    "criteria": json.dumps({"title": "T", "features": [
        {"name": "F", "scenarios": [{"id": "SC1", "title": "T", "when": "w", "then": ["t"]}]}]}),
    "epic": json.dumps({"title": "T", "issues": [{"id": "E1", "title": "T"}]}),
    "release": json.dumps({"title": "T"}),
}


@pytest.mark.parametrize("artifact_type", sorted(_GENERATOR_REPLIES))
def test_every_generator_drives_a_real_call_without_a_cache_write(artifact_type):
    # Drives each registered generator for real and reads the request that came out, so a generator
    # that takes `reuse_system` and drops it on the floor fails here. Both arms in one test: the
    # default must not carry the directive, and `reuse_system=True` must — so "deleted it everywhere"
    # fails too, per generator rather than only for `brief`.
    from requivo.providers.anthropic import _GENERATORS

    reply = _GENERATOR_REPLIES[artifact_type]
    model = out({"problem": slot(80, "explicit", "high")})
    fake = FakeClient(reply, reply)
    _GENERATORS[artifact_type](fake, model)
    _GENERATORS[artifact_type](fake, model, reuse_system=True)
    assert "cache_control" not in _system_block(fake, 0), f"{artifact_type} pays for a cache nothing reads"
    assert _system_block(fake, 1)["cache_control"] == {"type": "ephemeral"}, f"{artifact_type} lost its opt-in"


def test_estimate_drives_a_real_call_without_a_cache_write():
    # `estimate` is not in `_GENERATORS` — the CLI calls it directly, past the provider seam — so it
    # needs its own case or it is the one single-call verb nothing covers.
    from requivo.core.contracts import Story
    from requivo.providers.anthropic import estimate

    reply = json.dumps({"items": [
        {"story_id": "S1", "title": "T", "complexity": "S", "days_low": 1, "days_high": 2}]})
    model = out({"problem": slot(80, "explicit", "high")})
    stories = Stories(stories=[Story(id="S1", title="T")])
    fake = FakeClient(reply, reply)
    estimate(fake, model, stories)
    estimate(fake, model, stories, reuse_system=True)
    assert "cache_control" not in _system_block(fake, 0)                     # must not fire
    assert _system_block(fake, 1)["cache_control"] == {"type": "ephemeral"}  # must fire


def test_complete_still_defaults_to_caching_for_an_undeclared_caller():
    # The control for every assertion above. "Will this be sent again?" is the caller's question, and
    # a caller that has not considered it should pay the safe answer — 25% once — rather than silently
    # lose a real cache worth up to 90% per repeat. If this default ever flips, the generators' saving
    # stops being a decision and becomes the accident of a global.
    import inspect

    assert inspect.signature(_complete).parameters["reuse_system"].default is True
    fake = FakeClient(_BRIEF_REPLY)
    _complete(fake, "SYSTEM", [{"role": "user", "content": "u"}], Brief)
    assert _system_block(fake, 0)["cache_control"] == {"type": "ephemeral"}


def test_a_generator_can_opt_back_in_when_its_caller_loops_it():
    # scripts/golden_run.py --brief calls advise() K times with one system prompt, so the harness is a
    # genuine re-reader. The escape hatch has to actually reach the request, not just exist.
    fake = FakeClient(_BRIEF_REPLY, _BRIEF_REPLY)
    model = out({"problem": slot(80, "explicit", "high")})
    advise(fake, model)                       # production: one call
    advise(fake, model, reuse_system=True)    # harness: K calls, same prompt
    assert "cache_control" not in _system_block(fake, 0)                     # must not fire
    assert _system_block(fake, 1)["cache_control"] == {"type": "ephemeral"}  # must fire


def test_skipping_the_breakpoint_does_not_change_the_system_prompt_bytes():
    # The cheap fix must stay a cheap fix. Moving the shared bulk to the front of every template is
    # the other way to make this pay, and it changes what the model reads — a behaviour change that
    # owes the golden harness a cycle. This pins that no such reordering rode along: the text sent is
    # still exactly what build_prompt() assembles.
    from requivo.core.context import build_prompt

    fake = FakeClient(_BRIEF_REPLY)
    advise(fake, out({"problem": slot(80, "explicit", "high")}))
    assert _system_block(fake, 0)["text"] == build_prompt("brief.md", None)


def test_retry_resends_a_byte_identical_system_whether_or_not_it_is_cached():
    # The intra-operation invariant, on both arms: a retry must re-send the same bytes, or the cache
    # is lost exactly where it does pay. Asserted for the cached arm too, so a future edit that makes
    # the directive conditional on the attempt number fails here.
    for reuse, expect_directive in ((True, True), (False, False)):
        fake = FakeClient("not json at all", _BRIEF_REPLY)
        _complete(fake, "SYSTEM PROMPT", [{"role": "user", "content": "u"}], Brief,
                  reuse_system=reuse)
        assert len(fake.calls) == 2, "expected one retry"
        assert _system_block(fake, 0)["text"] == _system_block(fake, 1)["text"] == "SYSTEM PROMPT"
        for i in (0, 1):
            assert ("cache_control" in _system_block(fake, i)) is expect_directive


def test_cost_estimate_bills_a_write_premium_and_plain_input_differently():
    # Not a new-behaviour test — a guard that the fix's whole point survives in the rendered number.
    # The ledger prices what the API *reported*, so dropping the directive moves those tokens from
    # cache_write (1.25x) to input (1.0x) with no arithmetic change here. If these two ever bill the
    # same, the saving becomes invisible and the issue's "the number rendered is correct" stops
    # holding.
    from datetime import date

    on = date(2026, 9, 1)  # past the sonnet-5 launch-price expiry, so the rate is the plain 3.00
    cached = UsageLedger()
    cached.record(CallRecord(model="claude-sonnet-5", cache_write_tokens=1_000_000))
    plain = UsageLedger()
    plain.record(CallRecord(model="claude-sonnet-5", input_tokens=1_000_000))
    assert cached.cost_usd(on) == pytest.approx(3.75)
    assert plain.cost_usd(on) == pytest.approx(3.00)
    assert cached.cost_usd(on) > plain.cost_usd(on)


def test_current_model_name_reads_env_override(monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    assert current_model_name() == "claude-sonnet-5"
    monkeypatch.setenv("MODEL", "claude-opus-4-8")
    assert current_model_name() == "claude-opus-4-8"
