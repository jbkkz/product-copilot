"""LLM-authored prose cannot write a line of the terminal render path (#213).

The sibling of `tests/test_cli_untrusted_output.py`, and the gap it left. That file sweeps the
*diagnostic* verbs -- doctor, session verify, session show, artifact list, impact -- where the
untrusted string is a value read off disk. This one sweeps the **primary** render path, where the
untrusted string is the model's own reply: the questions, the challenges, the opportunities and the
brief prose a first-time user sees on `discover`, `status`, `brief`, `stories` and `estimate`.

The threat is neither hypothetical nor about a hostile model. SECURITY.md frames a *client request*
as untrusted business data the tool runs on, and the engine's whole job is turning that request into
prose. A request that steers the reply carries an embedded newline into `Question.q`, and the line
after it is a sentence Requivo appears to be saying -- a forged `Ready` verdict, a challenge hidden
behind a screen clear.

`streams.py` does not help here, and it is worth saying why, because it looks as though it should:
`errors="backslashreplace"` acts on characters the console *cannot encode*, and ESC is perfectly
encodable in UTF-8. It is emitted verbatim.

Two things are pinned. Each named field, so a regression names itself; and a **sweep** over every
LLM-authored string the terminal renders, which is what catches the field somebody adds next --
`display_text` at fifteen call sites is a discipline, and the sweep is what a discipline needs.

The control test is the other half. An escaper that made ordinary prose unreadable would be a worse
bug than the one it fixed, and it would ship green, because nobody re-reads output that looks busy.
"""
from __future__ import annotations

import io
import re
from contextlib import redirect_stdout

from _fakes import out, slot

from requivo.core.contracts import (
    Brief,
    Challenge,
    DesignDecision,
    EngineOutput,
    EstimateDraft,
    Leverage,
    Opportunity,
    Stories,
)
from requivo.core.dependencies import propagate
from requivo.render.terminal import (
    render_brief,
    render_dependency_map,
    render_estimate,
    render_impact,
    render_stories,
    render_turn,
)

# A newline, then a claim at column 0, then a screen clear. The three shapes together: the newline is
# what ends Requivo's line, the column-0 text is what reads as Requivo's own output, and the escape
# is what a terminal *executes* rather than displays.
FORGED = "Real text.\nFORGED AT COLUMN ZERO\x1b[2J"

# Every character that can move a cursor or end a line -- the same class `core/selectors.py` guards a
# selector token against, and deliberately no wider.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _render(fn, *args) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args)
    return buf.getvalue()


def _forged_lines(text: str) -> list:
    return [ln for ln in text.splitlines() if ln.startswith("FORGED")]


def _raw_controls(text: str) -> str:
    """The literal control characters still present, newlines excluded -- a renderer's own line
    breaks are not the threat. The forged-line check above catches a line; this catches an escape
    sequence that never needed one. A cursor move or a colour change is invisible to a
    `splitlines()` comparison and is the half of the threat that does not announce itself."""
    return "".join(c for c in _CONTROL.findall(text) if c != "\n")


def _model_with_question(q: str) -> EngineOutput:
    d = out({"problem": slot(80, "explicit", "high")}).model_dump()
    d["questions"] = [{"q": q, "slot": "problem", "why": "because"}]
    return EngineOutput.model_validate(d)


def _brief(**overrides) -> Brief:
    base = dict(
        problem="A problem.", solution="A solution.", complexity="medium",
        complexity_reasons=["A reason."], cost_driver="A driver.", risks=["A risk."],
        next_steps=["A step."], open_decisions=["An open decision."],
        challenges=[Challenge(headline="H", premise="P", alternative="A", consequence="C",
                              recommendation="R")],
        decisions=[DesignDecision(decision="D", why="W", alternative="Alt", tradeoff="T")],
        opportunities=[Opportunity(text="O", leverage=Leverage.high, modules=["m"])],
    )
    base.update(overrides)
    return Brief(**base)


def test_a_question_cannot_forge_a_line_of_the_turn_view():
    """`render_turn` is the first thing a user ever sees -- `discover`, `answer`, `status` and the
    offline `demo` all print it -- and `print(f"  {i}. {q.q}")` put the reply straight on the line.
    The readiness verdict it can forge sits three lines above it."""
    text = _render(render_turn, _model_with_question(FORGED))
    assert not _forged_lines(text), text
    assert _raw_controls(text) == ""
    # Must fire: neutralized means escaped and still readable, never dropped. Without this the
    # assertions above are satisfied by a renderer that prints nothing at all.
    assert "FORGED AT COLUMN ZERO" in text
    assert "\\x1b[2J" in text


def test_a_challenge_cannot_forge_a_line_of_the_decision_brief():
    """The brief is the deliverable and a challenge is the part a reader acts on. All five of its
    fields are rendered, so all five are checked -- a fix covering `headline` alone would look
    complete and leave four open."""
    model = out({"problem": slot(80, "explicit", "high")})
    for field in ("headline", "premise", "alternative", "consequence", "recommendation"):
        fields = {"headline": "H", "premise": "P", "alternative": "A", "consequence": "C",
                  "recommendation": "R", field: FORGED}
        challenge = Challenge(**fields)
        text = _render(render_brief, model, _brief(challenges=[challenge]))
        assert not _forged_lines(text), field
        assert _raw_controls(text) == "", field
        assert "FORGED AT COLUMN ZERO" in text, field


def test_every_llm_authored_string_the_terminal_renders_is_neutralized():
    """The sweep, and the reason this file exists rather than five tests beside five renderers.

    Every LLM-authored string field carries the payload at once and every terminal renderer runs, so
    a field added later and printed raw goes red here under its own renderer's name. That is the
    only guard that survives somebody adding a sixth field to `Challenge`."""
    model = out({"problem": slot(80, "explicit", "high")})
    d = model.model_dump()
    d["questions"] = [{"q": FORGED, "slot": "problem", "why": FORGED}]
    d["summary"]["objective"] = FORGED
    # `render_dependency_map` reads the *model's* reasoning layer, not the brief's, so a forged brief
    # alone leaves that renderer with nothing to render -- which the must-fire assertion below
    # correctly refused to call a pass.
    d["decisions"] = [{"decision": FORGED, "why": FORGED, "alternative": FORGED,
                       "tradeoff": FORGED, "derived_from": ["problem"]}]
    d["challenges"] = [{"headline": FORGED, "premise": FORGED, "alternative": FORGED,
                        "consequence": FORGED, "recommendation": FORGED, "contests": ["problem"]}]
    d["opportunities"] = [{"text": FORGED, "leverage": "high", "modules": [FORGED]}]
    forged_model = EngineOutput.model_validate(d)

    brief = _brief(
        problem=FORGED, solution=FORGED, complexity_reasons=[FORGED], cost_driver=FORGED,
        risks=[FORGED], next_steps=[FORGED], open_decisions=[FORGED],
        challenges=[Challenge(headline=FORGED, premise=FORGED, alternative=FORGED,
                              consequence=FORGED, recommendation=FORGED)],
        decisions=[DesignDecision(decision=FORGED, why=FORGED, alternative=FORGED,
                                  tradeoff=FORGED)],
        opportunities=[Opportunity(text=FORGED, leverage=Leverage.high, modules=[FORGED])],
    )
    stories = Stories(stories=[{"id": FORGED, "title": FORGED, "as_a": FORGED, "i_want": FORGED,
                                "so_that": FORGED, "acceptance": [FORGED], "slots": ["problem"]}])
    estimate = EstimateDraft(
        items=[{"story_id": "S1", "title": FORGED, "complexity": "M", "days_low": 1,
                "days_high": 2, "drives": [FORGED]}],
        risks=[FORGED])

    renders = {
        "render_turn": _render(render_turn, forged_model),
        "render_brief": _render(render_brief, forged_model, brief),
        "render_stories": _render(render_stories, stories),
        "render_estimate": _render(render_estimate, estimate, ["problem"], "low"),
        "render_dependency_map": _render(render_dependency_map, forged_model),
        "render_impact": _render(render_impact, propagate(forged_model, ["problem"])),
    }
    for name, text in renders.items():
        assert not _forged_lines(text), f"{name} let LLM text start a line: {_forged_lines(text)}"
        assert _raw_controls(text) == "", f"{name} emitted a raw control character"
        # Must fire: every one of these renderers must actually have printed the payload, or the
        # two assertions above are green on a renderer that emitted nothing.
        assert "FORGED AT COLUMN ZERO" in text, f"{name} rendered none of the forged fields"


def test_ordinary_prose_renders_byte_for_byte_unchanged():
    """The control, and the half a security fix ships without. An escaper that quoted every string
    would satisfy every assertion above, make the product unreadable, and ship green.

    Short strings on purpose: `textwrap.fill` wraps at 80 columns, so a long line would fail this
    for a reason with nothing to do with escaping."""
    text = _render(render_turn, _model_with_question("How are approvals routed today?"))
    assert "1. How are approvals routed today?" in text
    assert "\\" not in text

    brief_text = _render(render_brief, out({"problem": slot(80, "explicit", "high")}), _brief())
    for expected in ("A problem.", "A solution.", "A reason.", "A driver.", "A risk.", "A step."):
        assert expected in brief_text
    assert "\\" not in brief_text
