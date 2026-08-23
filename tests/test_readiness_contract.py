"""Readiness is one boolean, and every surface has to render it as one boolean.

`core/analysis.py` publishes exactly `{"ready": not blockers}` — there is no third value anywhere in
the Core, and `docs/requirements-model.md` says so normatively: *Requivo does not invent graded
"nearly ready" levels; it shows what blocks.* The Web view model and the Claude Code status skill
each restate that rule in their own source.

The terminal did not, for a whole release (#165). `render_readiness` and `render_turn` both branched
on `len(blockers) <= 2` and produced a third state — `Nearly ready`, `⚠ Nearly — N to confirm` — so a
session with two unresolved high-impact topics was told *Nearly ready* by `requivo status` and *Not
ready to produce a reliable scope* by the Web, off the same `model.json` at the same revision.
Nothing went red, because nothing pinned the contract anywhere: `grep -rn "Nearly" tests/` returned
nothing.

So the guard lives here rather than in any one surface's test module, and it is stated as a property
rather than as a set of expected strings: **the readiness verdict a surface renders is a function of
`bool(blockers)` and of nothing else.** A surface may word it however suits its reader; it may not
have a third answer, and it may not have only one.
"""
import io
import re
from contextlib import redirect_stdout

import pytest
from _fakes import out, slot

from requivo.core.analysis import model_status
from requivo.core.contracts import Brief, _schema_order, schema_slot_ids
from requivo.render.markdown import brief_markdown
from requivo.render.terminal import render_readiness, render_turn
from requivo.web.viewmodels.status import readiness_view

# 0 is ready; 1 and 2 are what the deleted `<= 2` arm used to call "nearly"; 3 and 5 sit the other
# side of it. A test using only 0 and 3 is green on the defect.
BLOCKER_COUNTS = (0, 1, 2, 3, 5)

# The one question the brief document asks, in one place, so a deliberate caption change is one edit
# here rather than a hunt. `test_every_surface_asks_the_same_readiness_question` is what pins it.
BRIEF_READINESS_HEADING = "## Are we ready?"


def _model_with(n_blockers: int):
    """A complete model whose only unresolved high-impact topics are the first `n_blockers` ones.

    Every required slot is high-impact so that the count is exactly what the caller asked for:
    `_readiness_blockers` blocks on a high-impact slot that is not both `explicit` and covered.
    """
    _, required = schema_slot_ids()
    ordered = [sid for sid in _schema_order() if sid in required]
    assert len(ordered) > max(BLOCKER_COUNTS), "the schema no longer has enough required slots"
    model = {sid: slot(90, "explicit", "high") for sid in ordered}
    for sid in ordered[:n_blockers]:
        model[sid] = slot(0, "empty", "high")
    return out(model)


def _brief() -> Brief:
    return Brief(complexity="low")


def _printed(fn, model) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(model)
    return buf.getvalue()


def _status_cell(model) -> str:
    line = next(ln for ln in _printed(render_readiness, model).splitlines()
                if ln.strip().startswith("Status"))
    return line.split("Status", 1)[1].strip()


def _turn_verdict(model) -> str:
    line = next(ln for ln in _printed(render_turn, model).splitlines() if "Ready?" in ln)
    return line.split("Ready?", 1)[1].split("→")[0].strip()


def _brief_verdict(model) -> str:
    body = brief_markdown(model, _brief()).split(BRIEF_READINESS_HEADING, 1)[1]
    match = re.search(r"\*\*(.+?)\*\*", body)
    assert match, "the decision brief's readiness section states no verdict"
    return match.group(1)


def _web_headline(model) -> str:
    return readiness_view(model_status(model))["headline"]


SURFACES = [
    ("terminal status", _status_cell),
    ("terminal turn", _turn_verdict),
    ("decision brief", _brief_verdict),
    ("web", _web_headline),
]


def test_the_core_answers_readiness_with_one_boolean():
    # The reference the surfaces are graded against, and the positive control for `_model_with`: if
    # the fixture stopped producing blockers, every surface below would agree for the wrong reason.
    ready = {n: model_status(_model_with(n))["readiness"]["ready"] for n in BLOCKER_COUNTS}
    assert ready == {0: True, 1: False, 2: False, 3: False, 5: False}


@pytest.mark.parametrize("surface,extract", SURFACES, ids=[s for s, _ in SURFACES])
def test_readiness_renders_as_one_boolean_on_every_surface(surface, extract):
    verdicts = {n: extract(_model_with(n)) for n in BLOCKER_COUNTS}
    blocked = {v for n, v in verdicts.items() if n}
    # Exactly one way of saying "not ready", however many topics block ...
    assert len(blocked) == 1, f"{surface} grades readiness by blocker count: {verdicts}"
    # ... and it is not the same thing it says when nothing blocks. Without this second half the
    # first passes on a surface that renders nothing at all.
    assert verdicts[0] not in blocked, f"{surface} says the same thing ready and not: {verdicts}"


@pytest.mark.parametrize("surface,extract", SURFACES, ids=[s for s, _ in SURFACES])
def test_no_surface_invents_a_middle_readiness_state(surface, extract):
    # The wording half of the same rule, and the one a reader recognises. "Nearly" is the word
    # `docs/requirements-model.md` names; a synonym would still be caught by the test above.
    for n in BLOCKER_COUNTS:
        verdict = extract(_model_with(n))
        assert not re.search(r"(?i)nearly", verdict), f"{surface} invents a middle state: {verdict!r}"


def test_every_surface_asks_the_same_readiness_question():
    # One boolean was being asked as three different questions — `READY FOR IMPLEMENTATION?` in the
    # terminal, `Ready to estimate?` in the decision brief, `Ready for a first decision brief` on the
    # Web — which invites a reader to believe they are three thresholds. They are one.
    model = _model_with(2)
    terminal = _printed(render_readiness, model)
    markdown = brief_markdown(model, _brief())

    assert "ARE WE READY?" in terminal
    assert "READY FOR IMPLEMENTATION?" not in terminal
    assert BRIEF_READINESS_HEADING in markdown
    assert "Ready to estimate?" not in markdown
