"""The interactive `discover` loop, driven end to end over a stub provider.

`converse()` is the CLI's TTY loop and, until #77, was also a second orchestration of discovery: it
called `run()` and then `advise()` itself and used `DiscoveryService` only for the final write. The
seam guard in `tests/test_boundaries.py` pins that those imports are gone. This file pins the other
half -- that the loop still does the same work through the service, because "the import list is
clean" is a claim about the file, not about the behaviour.

The provider is a stub implementing `ReasoningProvider`, injected into `DiscoveryService`, so nothing
here needs the Anthropic SDK, a key, or the network. `input()` is patched, since the whole point of
this path is that a human is on the other end of it.
"""
from __future__ import annotations

import builtins
import inspect
import io
from contextlib import redirect_stdout

import pytest

from requivo.cli import MAX_TURNS, converse
from requivo.core.contracts import Brief, EngineOutput, Question, Slot, Summary
from requivo.providers.base import ReasoningProvider
from requivo.services.discovery import DiscoveryService

ARROW = "→"  # the separator converse() puts between a question and its answer


def _model(*, objective: str, questions: list[Question] | None = None) -> EngineOutput:
    """A model complete enough to be a real turn's output. The loop reads `questions` and hands the
    whole object back on the next turn, so the slot is there to make it a plausible `EngineOutput`
    rather than a shell that would pass an identity check and nothing else."""
    return EngineOutput(
        model={"problem": Slot(completeness=80, confidence="explicit", impact="high", value="v")},
        summary=Summary(objective=objective),
        questions=questions or [],
    )


def _question() -> Question:
    return Question(q="Who approves?", slot="permissions", why="approval routing drives the workflow")


class StubProvider:
    """Records every call and replays a scripted list of turns.

    Deliberately not a Mock: the assertions below are about *what the loop handed the seam* -- the
    request, the carried model, the answers, the cards -- and a recorded call list says that in one
    place. `name`/`model_name`/`provenance` are present because the protocol declares them.
    """

    name = "stub"

    def __init__(self, *turns: EngineOutput, brief: Brief | None = None):
        self.turns = list(turns)
        self.brief = brief or Brief(complexity="low", solution="a solution")
        self.analyze_calls: list[dict] = []
        self.generate_calls: list[dict] = []

    def analyze(self, request, *, current_model=None, answers=None, only=None, reuse_system=False):
        self.analyze_calls.append({
            "request": request, "current_model": current_model, "answers": answers,
            "only": only, "reuse_system": reuse_system,
        })
        if not self.turns:
            raise AssertionError("the loop asked for more turns than the stub was scripted with")
        return self.turns.pop(0)

    def generate(self, artifact_type, model, *, only=None, **kwargs):
        self.generate_calls.append(
            {"artifact_type": artifact_type, "model": model, "only": only, **kwargs})
        return self.brief

    def model_name(self) -> str:
        return "stub-model"

    def provenance(self, op, *, only=None) -> dict:
        return {"provider": self.name, "model_name": self.model_name(), "prompt_version": "sha256:0"}


def _service(provider: StubProvider) -> DiscoveryService:
    return DiscoveryService(provider)


def _converse(disco, request, answers=(), only=None):
    """Run the loop with `answers` fed to `input()` in order, capturing stdout."""
    supplied = iter(answers)
    buf = io.StringIO()
    real_input = builtins.input
    builtins.input = lambda _prompt="": next(supplied)
    try:
        with redirect_stdout(buf):
            out = converse(disco, request, only=only)
    finally:
        builtins.input = real_input
    return out, buf.getvalue()


def test_the_stub_satisfies_the_provider_protocol():
    """The control for every test in this file. A stub that had drifted from `ReasoningProvider`
    would let the loop pass against a seam the real provider does not offer -- the failure this
    fixture exists to catch, wearing a green tick.

    `isinstance` alone does not catch it, and that limit is `docs/providers.md`'s own: a
    `@runtime_checkable` protocol checks that a member is **present**, never that it has the right
    signature. The parameters are therefore compared as well, because the drift this file is exposed
    to is exactly a signature one -- `analyze` grew `reuse_system` in this very change, and a stub
    that had not grown it would have made every assertion below true about a seam that does not
    exist.

    The `Drifted` class is the must-fire control for the control: it passes `isinstance` and fails
    the signature comparison, so this test goes red if the second half is ever deleted as redundant.
    """
    assert isinstance(StubProvider(), ReasoningProvider)
    for name in ("analyze", "generate"):
        declared = set(inspect.signature(getattr(ReasoningProvider, name)).parameters)
        offered = set(inspect.signature(getattr(StubProvider, name)).parameters)
        assert declared <= offered, (
            f"StubProvider.{name} is missing {sorted(declared - offered)} -- the fixture has drifted "
            f"from the protocol, so the tests below assert against a seam the real provider is not"
        )

    class Drifted:
        """Present on every member, wrong on every signature."""

        name = "drifted"

        def analyze(self, request): ...
        def generate(self, artifact_type, model): ...
        def model_name(self): ...
        def provenance(self, op): ...

    assert isinstance(Drifted(), ReasoningProvider), (
        "this control assumes isinstance passes on a signature-drifted stub -- that is the whole "
        "limit the parameter comparison above exists to cover"
    )
    assert not set(inspect.signature(ReasoningProvider.analyze).parameters) <= set(
        inspect.signature(Drifted.analyze).parameters
    ), "the parameter comparison cannot fire, so it is not a check"


def test_the_loop_reasons_through_the_service_and_carries_the_model_not_a_transcript():
    """#77's behavioural half. Each turn goes through `DiscoveryService.draft_turn`, which hands the
    provider the request, the model so far and the answers just given.

    The first turn carries no model and no answers -- it is a first discovery, reasoning from the
    request alone. Every later turn carries the previous turn's model, which is what makes the CLI's
    loop the same turn operation `requivo answer` and the Web form use rather than a second one."""
    first = _model(objective="one", questions=[_question()])
    second = _model(objective="two")
    provider = StubProvider(first, second)

    out, _ = _converse(_service(provider), "a leave approval system", ["the line manager"])

    assert out is second
    assert len(provider.analyze_calls) == 2
    opening, refinement = provider.analyze_calls
    assert opening["current_model"] is None and opening["answers"] is None
    assert opening["request"] == "a leave approval system"
    assert refinement["current_model"] is first, "the refinement turn did not carry the prior model"
    assert refinement["answers"] == f"[slot: permissions] Q: Who approves? {ARROW} A: the line manager"
    assert refinement["request"] == "a leave approval system", "the request is context on every turn"


def test_the_loop_declares_its_repeated_prompt_at_the_seam():
    """A drafting loop sends one system prompt several times, so the breakpoint is genuinely read
    back and is worth its write. It used to be `converse()` passing `reuse_system=True` to `run()`
    directly; it has to survive the move to the seam or the interactive path silently pays full
    price on every turn after the first (#9, #58).

    MUST-FIRE control in the same fixture: a single-call operation must still say the opposite, or
    "declared it everywhere" would pass this too."""
    provider = StubProvider(_model(objective="done"), _model(objective="done"))
    disco = _service(provider)

    _converse(disco, "a leave approval system")
    assert provider.analyze_calls[0]["reuse_system"] is True, "the drafting loop lost its breakpoint"

    disco._need_provider().analyze("a leave approval system")  # any one-shot operation
    assert provider.analyze_calls[1]["reuse_system"] is False, (
        "a single-call operation pays for a cache nothing reads"
    )


def test_the_context_cards_are_held_constant_across_every_turn():
    """A card selection is what the impact estimates are read against, so a turn that quietly widened
    to the full set would reason a different session from the one before it."""
    provider = StubProvider(
        _model(objective="one", questions=[_question()]),
        _model(objective="two"),
    )
    _converse(_service(provider), "a request", ["a manager"], only=["financial-reporting"])
    assert [c["only"] for c in provider.analyze_calls] == [["financial-reporting"]] * 2


def test_the_assessment_is_reasoned_through_the_service_too():
    """`advise(client, ...)` was the CLI's second direct provider call. `draft_assessment` is the
    same work through the seam, with the cards the discovery ran against."""
    provider = StubProvider()
    model = _model(objective="done")
    brief = _service(provider).draft_assessment(model, cards=["financial-reporting"])
    assert brief is provider.brief
    assert provider.generate_calls == [
        {"artifact_type": "brief", "model": model, "only": ["financial-reporting"]}
    ]


@pytest.mark.parametrize("answers, expected", [
    (["q"], "Stopped."),   # the user quits at the first question
    ([""], "No answer provided"),   # every question skipped -- nothing to feed back
])
def test_stopping_early_returns_nothing_and_makes_no_further_call(answers, expected):
    """A stop is a stop: the loop returns None, `_cmd_discover` returns before finalizing, and no
    session is claimed. The call count is asserted because "returned None" would also be true of a
    loop that kept reasoning and then threw the result away -- and that one costs money."""
    provider = StubProvider(_model(objective="one", questions=[_question()]))
    out, printed = _converse(_service(provider), "a request", answers)
    assert out is None
    assert len(provider.analyze_calls) == 1
    assert expected in printed


def test_the_turn_limit_still_bounds_the_loop():
    """`MAX_TURNS` is the only thing between a model that keeps asking questions and an unbounded
    spend. Every scripted turn carries a question, so nothing but the limit can end this."""
    asking = [_model(objective=f"turn {i}", questions=[_question()]) for i in range(MAX_TURNS)]
    provider = StubProvider(*asking)
    out, printed = _converse(_service(provider), "a request", ["an answer"] * MAX_TURNS)
    assert len(provider.analyze_calls) == MAX_TURNS
    assert out is asking[-1]
    assert f"{MAX_TURNS}-turn limit" in printed


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, EOFError])
def test_an_interrupt_at_the_prompt_stops_rather_than_traces_back(interrupt):
    """Ctrl-C and EOF at a question are how a real user leaves this loop, so they end it cleanly."""
    provider = StubProvider(_model(objective="one", questions=[_question()]))

    def raiser(_prompt=""):
        raise interrupt

    real_input, builtins.input = builtins.input, raiser
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            out = converse(_service(provider), "a request")
    finally:
        builtins.input = real_input
    assert out is None, f"{interrupt.__name__} did not stop the loop"
    assert "Stopped." in buf.getvalue()
