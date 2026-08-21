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
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from _fakes import _ENGINE_REPLY, FakeClient, _run_app, full_slots, slot

from requivo.cli import MAX_TURNS, app, converse
from requivo.core.contracts import Brief, EngineOutput, Question, Slot, Summary
from requivo.providers.base import ReasoningProvider
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService

ARROW = "→"  # the separator converse() puts between a question and its answer


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    """An isolated temp workspace for every test here, never the real repo.

    The `converse()` tests write nothing, so for them this is protection rather than a requirement.
    The `app()`-driven ones at the foot of the file really do claim sessions on disk.
    """
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))


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
    """A stop is a stop: the loop returns None and `_cmd_discover` returns before finalizing. The
    call count is asserted because "returned None" would also be true of a loop that kept reasoning
    and then threw the result away -- and that one costs money.

    This used to say "and no session is claimed", which stopped being true in #133: the revision-zero
    gate now claims the slug *before* the loop, so an abandoned discovery leaves the request captured
    at revision 0. `test_stopping_early_leaves_the_claimed_session_and_says_where` pins that half."""
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

# ── the entry-point gate, driven through `app()` (#133) ────────────────────────────────────────────
# Everything above injects a stub provider into the service and drives `converse()` directly. The
# three below drive the real `requivo discover` over a `FakeClient`, because what they pin is the
# *position* of a precondition relative to the first billed call — and a position is only visible
# from the whole verb.

_REQUEST = "a leave approval system, discovered twice"
_BRIEF_REPLY = json.dumps({"complexity": "low", "solution": "S"})
_ASKING_REPLY = json.dumps({
    "model": full_slots(problem=slot(80, "explicit", "high")),
    "questions": [{"q": "Who approves?", "slot": "permissions", "why": "approval routing drives it"}],
    "summary": {"objective": "A leave approval system"},
})


def _at_a_terminal(monkeypatch) -> None:
    """`_cmd_discover` picks its branch on `--once` *or* the absence of a TTY, and under pytest stdin
    is never one. Patched for both legs of the tests below, so the flag is the only difference between
    them — which is what "the two entry points refuse identically" has to mean."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)


@pytest.mark.parametrize("argv_tail", [["--once"], []], ids=["once", "interactive"])
def test_both_discover_entry_points_refuse_a_refined_session_before_paying(monkeypatch, capsys, argv_tail):
    """Invariant 13's revision-zero gate is taken before the first billed call on *both* paths (#133).

    `--once` claimed the session inside `start()` and refused for free. The interactive branch reached
    the provider through `converse()` and met the gate only inside `finalize_discovery`, after the
    reasoning: two calls in this reproduction, up to nine in the field — eight turns plus the
    assessment — every one of them paid for and then thrown away by a refusal that was correct and in
    the wrong place. CLAUDE.md's own text for that invariant says a rule enforced by an interface is
    not enforced; this was the same shape one surface along.

    **The assertion is the call count, not the refusal.** The refusal already happened before the fix,
    so a test asserting only `revision_conflict` was green on the defect. The `--once` leg is the
    control: it passed before this change and must keep passing, or the two paths have merely swapped
    which one is wrong.
    """
    _at_a_terminal(monkeypatch)
    _run_app(["discover", _REQUEST, "--once"], client=FakeClient(_ENGINE_REPLY))  # → revision 1

    # Scripted with a turn *and* an assessment, so an ungated run gets all the way to the old refusal
    # point and the count below reports how much it spent rather than dying on an exhausted stub.
    fake = FakeClient(_ENGINE_REPLY, _BRIEF_REPLY)
    with pytest.raises(SystemExit) as exit_:
        app(["discover", _REQUEST, *argv_tail], client=fake)

    assert exit_.value.code == 1
    assert "already carries a model" in capsys.readouterr().err
    assert fake.calls == [], (
        f"{len(fake.calls)} provider call(s) were billed before the refusal — the gate is downstream "
        f"of the reasoning on this path"
    )


@pytest.mark.parametrize("argv_tail, calls", [(["--once"], 1), ([], 2)], ids=["once", "interactive"])
def test_a_first_discovery_still_reaches_the_provider_on_both_paths(monkeypatch, argv_tail, calls):
    """The must-fire half of the test above. `fake.calls == []` is also true of a verb that never ran,
    a stub that was never reached and a harness that broke — so a gate refusing *everything* would
    pass that test and fail this one. The interactive path pays for two calls (the turn, then the
    assessment); `--once` pays for one, since it does not finalize."""
    _at_a_terminal(monkeypatch)
    fake = FakeClient(_ENGINE_REPLY, _BRIEF_REPLY)
    _run_app(["discover", _REQUEST, *argv_tail], client=fake)
    assert len(fake.calls) == calls
    assert [m.current_revision for m in SessionService().list_sessions()] == [1]


def test_stopping_early_leaves_the_claimed_session_and_says_where(monkeypatch, capsys):
    """The cost of taking the gate first, stated as a test rather than left to be discovered.

    Claiming the slug before the loop means a discovery the user abandons leaves the request captured
    at revision 0 instead of nothing at all. That is not a new state — it is exactly what
    `create_only` persists on the "capture the request now, discover later" path, and it is what
    `--once` already leaves behind when the provider call fails after the claim.

    It is *printed* because the alternative is a session directory appearing with no line accounting
    for it, and a directory nobody was told about is a directory nobody deletes.
    """
    _at_a_terminal(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": "q")
    fake = FakeClient(_ASKING_REPLY)

    printed = _run_app(["discover", _REQUEST], client=fake)

    sessions = SessionService().list_sessions()
    assert [m.current_revision for m in sessions] == [0], "the abandoned discovery wrote a model"
    assert len(fake.calls) == 1, "the loop kept reasoning after the user stopped"
    assert "Stopped." in printed
    assert sessions[0].slug in printed, (
        "the claimed session is on disk and nothing said so — see this test's docstring"
    )


def test_the_golden_harness_answers_a_turn_in_exactly_the_words_this_loop_does():
    """The golden harness's multi-turn capture drives `draft_turn` off a scripted answer sheet rather
    than a TTY, and it is only a measurement of this loop for as long as it hands the seam the same
    bytes (#137). The answer block is the one thing it has to reproduce and the one thing that can
    silently drift, because a differently-shaped `Client answers:` body still reasons and still
    produces a plausible model — the capture would just be of a shape no user ever meets.

    So the comparison is behavioural, not a shared constant: this drives `converse()` with a known
    answer and asserts the harness's `answers_for_turn` builds character-for-character what the loop
    passed to `draft_turn`. Either side changing its wording goes red here.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from golden_lib import AnswerSheet, answers_for_turn

    question = _question()
    provider = StubProvider(_model(objective="one", questions=[question]),
                            _model(objective="two"))
    _converse(_service(provider), "a request", ["a scripted reply"])
    from_the_loop = provider.analyze_calls[1]["answers"]

    from_the_harness, answered = answers_for_turn(
        [question], AnswerSheet({question.slot: ["a scripted reply"]}))
    assert from_the_harness == from_the_loop
    assert answered == [question.slot]
