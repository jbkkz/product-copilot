"""Unit tests for the golden harness's own logic.

The harness decides what counts as a regression, so a bug here is worse than a bug in a generator: it
would let a real change through, or invent one that isn't there. None of this needs an API call — the
captures are fixtures, and every function below is a pure read over them.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from golden_lib import (  # noqa: E402
    AnswerSheet,
    Turn,
    _cluster_headlines,
    answers_for_turn,
    brief_consensus,
    brief_movements,
    consensus,
    is_interactive,
    load_runs,
    load_turns,
    movements,
    parse_requests,
    stability,
    turn_envelope,
    turn_lens,
    turn_movements,
)

from requivo.core.contracts import (  # noqa: E402
    Brief,
    Challenge,
    Confidence,
    EngineOutput,
    Impact,
    Level,
    Question,
    Slot,
    Summary,
)

# ── builders ─────────────────────────────────────────────────────────────────────────────────────

def _slot(value="v", impact=Impact.medium, confidence=Confidence.explicit, completeness=80):
    return Slot(value=value, completeness=completeness, confidence=confidence,
                impact=impact, evidence="e")


def _model(**impacts) -> EngineOutput:
    """An EngineOutput carrying the named slots at the given impacts, no questions. Slot names must be
    real schema ids — the contract rejects unknown slots — so these tests use `problem`/`workflow` as
    stand-ins; the statistical logic under test is indifferent to which id it is."""
    return EngineOutput(model={sid: _slot(impact=imp) for sid, imp in impacts.items()},
                        questions=[], summary=Summary())


def _challenge(headline, contests=()):
    return Challenge(headline=headline, premise="p", alternative="a",
                     consequence="c", recommendation="r", contests=list(contests))


def _brief(challenges, complexity=Level.high):
    """`challenges` is a list of headlines, or of (headline, contested slot ids) pairs."""
    built = [_challenge(*c) if isinstance(c, tuple) else _challenge(c) for c in challenges]
    return Brief(challenges=built, complexity=complexity)


# ── the noise floor ──────────────────────────────────────────────────────────────────────────────

def test_consensus_reports_modal_value_and_agreement():
    runs = [_model(problem=Impact.high), _model(problem=Impact.high), _model(problem=Impact.low)]
    con = consensus(runs)
    assert con["slots"]["problem"]["impact"] == ("high", 2)   # modal value, 2 of 3 runs
    assert con["n"] == 3


def test_stability_separates_unanimous_slots_from_jittery_ones():
    runs = [_model(problem=Impact.high, workflow=Impact.high),
            _model(problem=Impact.high, workflow=Impact.low),
            _model(problem=Impact.high, workflow=Impact.medium)]
    st = stability(runs)
    assert st["unanimous"]["impact"] == 1
    assert st["jitter"]["impact"] == 1


# ── strong vs weak, the rule the whole lens rests on ─────────────────────────────────────────────

def test_unanimous_before_and_after_is_a_strong_move():
    old = [_model(problem=Impact.low)] * 3
    new = [_model(problem=Impact.high)] * 3
    m = movements(old, new)
    assert len(m["strong"]) == 1 and not m["weak"]
    assert (m["strong"][0]["from"], m["strong"][0]["to"]) == ("low", "high")


def test_bare_majority_is_only_a_weak_move():
    """At K=3 a majority is 2 of 3 — one run flipping. That must not read as signal."""
    old = [_model(problem=Impact.low)] * 3
    new = [_model(problem=Impact.high), _model(problem=Impact.high), _model(problem=Impact.low)]
    m = movements(old, new)
    assert len(m["weak"]) == 1 and not m["strong"]


def test_a_jittery_old_baseline_is_never_a_reference():
    """If the old runs disagreed, there is nothing reliable to have moved away from."""
    old = [_model(problem=Impact.low), _model(problem=Impact.low), _model(problem=Impact.high)]
    new = [_model(problem=Impact.medium)] * 3
    assert not movements(old, new)["moved"]


def test_no_movement_when_the_value_holds():
    runs = [_model(problem=Impact.high)] * 3
    assert not movements(runs, runs)["moved"]


# ── the assessment lens ──────────────────────────────────────────────────────────────────────────

def test_headlines_cluster_across_phrasing_variants():
    """The word-overlap fallback, used for captures taken before `contests` existed. It handles
    reordering, which is the easy case."""
    clusters = _cluster_headlines([
        ["Signature as billing trigger"],
        ["Billing trigger at signature"],
        ["Signature is the billing trigger"],
    ])
    assert list(clusters.values()) == [3]      # one theme, seen in all three runs


def test_the_same_challenge_reworded_beyond_recognition_still_groups():
    """The case that broke the first version of this lens, taken verbatim from a doc-reapproval
    capture: three runs raised the same challenge — what happens to the published version during
    re-approval — with almost no shared vocabulary. Word overlap read that as two challenges lost and
    two gained. Grouping on the contested slots gets it right."""
    briefs = [
        _brief([("Visibility of the superseded signed copy", ["edge_cases", "permissions"])]),
        _brief([("Published-document blast radius ignored", ["edge_cases"])]),
        _brief([("Old version stays live mid-re-approval", ["edge_cases", "workflow"])]),
    ]
    con = brief_consensus(briefs)
    assert con["all_themes"]["Edge cases"] == 3      # the slot all three runs really contested
    assert con["themes"] == {"Edge cases"}           # the secondary slots stay below the majority


def test_challenges_contesting_unrelated_slots_stay_apart():
    """The grouping must not collapse everything into one theme either."""
    briefs = [_brief([("Auto-issued invoice, no review", ["workflow"]),
                      ("One contract, one invoice", ["business_objects"])])] * 3
    assert len(brief_consensus(briefs)["themes"]) == 2


def test_a_challenge_only_some_runs_raise_is_not_stable():
    """Challenge themes need every run, not a majority: challenges name several slots each, so a
    majority bar marks almost everything stable and the readout stops discriminating."""
    briefs = [_brief([("Offline capability assumed", ["constraints"])]),
              _brief([("Offline capability assumed", ["constraints"])]),
              _brief([("Retention clock on delete", ["business_rules"])])]
    assert brief_consensus(briefs)["themes"] == set()

    everywhere = [_brief([("Offline capability assumed", ["constraints"])])] * 3
    assert brief_consensus(everywhere)["themes"] == {"Constraints"}


def test_a_headline_used_as_a_theme_label_cannot_forge_a_line():
    """The other half of #137's sweep, and the one the print sites could not cover.

    A theme label is normally `_label(slot_id)` — a validated slot id through the schema's table, so
    it cannot carry anything. The fallback path for a capture predating `contests` is different: it
    keys themes on the **challenge headline**, which is provider-written prose, and `golden_diff`
    prints those labels straight (`assessment + challenge(s) now raised: …`). Squashed here rather
    than at each print, because a label reaches four of them and a consumer should inherit the
    guarantee instead of remembering it — which is the reasoning `_log_safe` in
    `scripts/plugin_cli_drift.py` already applies to its own sink. `_log_safe` rather than the
    `_one_line` it is built on: whitespace alone is not enough at a sink a CI runner parses, which
    is #176.
    """
    forged = [_brief(["benign headline\n  assessment + challenge(s) now raised: FORGED"])] * 3
    ((label, _), ) = brief_consensus(forged)["all_themes"].items()
    assert "\n" not in label
    assert "FORGED" in label and "\\n" in label

    # must not fire: an ordinary headline is its own label, byte for byte. A squash that quoted
    # every headline would make the assessment readout unreadable and get itself deleted.
    plain = [_brief(["Signature as billing trigger"])] * 3
    assert set(brief_consensus(plain)["all_themes"]) == {"Signature as billing trigger"}


def test_a_challenge_the_engine_stopped_raising_is_reported():
    old = [_brief(["Signature as billing trigger", "Offline capability assumed"])] * 3
    new = [_brief(["Offline capability assumed", "Rounding convention"])] * 3
    b = brief_movements(old, new)
    assert b["themes_removed"] == ["Signature as billing trigger"]
    assert b["themes_added"] == ["Rounding convention"]


def test_complexity_verdict_is_graded_like_a_slot():
    old = [_brief([], Level.high)] * 3
    unanimous = [_brief([], Level.medium)] * 3
    assert brief_movements(old, unanimous)["complexity"]["strong"] is True

    split = [_brief([], Level.medium), _brief([], Level.medium), _brief([], Level.high)]
    assert brief_movements(old, split)["complexity"]["strong"] is False


def test_a_held_verdict_and_challenge_set_reports_nothing():
    runs = [_brief(["Offline capability assumed"], Level.medium)] * 3
    b = brief_movements(runs, runs)
    assert b["complexity"] is None and not b["themes_added"] and not b["themes_removed"]


# ── the multi-turn lens ──────────────────────────────────────────────────────────────────────────
#
# #77 moved the interactive `discover` loop onto `DiscoveryService.draft_turn`, and from turn 3 the
# loop is grounded on the carried model alone where the old one re-sent the whole transcript. Turns 1
# and 2 were verified byte-identical to the old loop in #77 itself, so a two-turn capture measures
# nothing about that change: the lens below only counts from turn 3, and every assertion about a
# thing that must NOT happen is paired with a fixture where it does — a lens reporting a clean run
# and a lens that cannot see produce the same empty finding set otherwise (#137).

def _q(slot: str) -> Question:
    return Question(q=f"tell me about {slot}", slot=slot, why="it drives the shape")


def _turn(index: int, answered: list[str], *, asks: tuple = (),
          states: dict | None = None) -> Turn:
    """One captured turn: what the sheet answered, and the model that came back.

    `states` maps a slot id to (confidence, completeness) so a test can say "this slot was confirmed
    at turn 2 and unknown at turn 5" — the information-loss case the whole lens exists for."""
    model = {sid: _slot(confidence=conf, completeness=comp)
             for sid, (conf, comp) in (states or {}).items()}
    return Turn(index=index, answered=list(answered),
                model=EngineOutput(model=model, questions=[_q(s) for s in asks],
                                   summary=Summary()))


def test_parse_requests_collects_a_layered_answer_sheet(tmp_path):
    """A slot may be answered more than once — each line is the next layer a client volunteers when
    the engine comes back to that slot. Ordering is the point, so the layers stay a list."""
    p = tmp_path / "requests.md"
    p.write_text("\n".join(["### s", "form: f", "card: c", "request: r",
                            "answer.problem: first layer", "answer.actors: who",
                            "answer.problem: second layer"]), encoding="utf-8")
    req = parse_requests(p)[0]
    assert req["answers"] == {"problem": ["first layer", "second layer"], "actors": ["who"]}
    assert is_interactive(req) is True


def test_a_request_without_an_answer_sheet_is_single_pass(tmp_path):
    p = tmp_path / "requests.md"
    p.write_text("### s\nform: f\ncard: c\nrequest: r\n", encoding="utf-8")
    req = parse_requests(p)[0]
    assert req["answers"] == {} and is_interactive(req) is False


def test_the_answer_sheet_hands_each_layer_out_once():
    """Consumed FIFO, so a client never repeats themselves and the loop keeps finding new ground —
    which is what drives the capture past turn 2 in the first place."""
    sheet = AnswerSheet({"problem": ["first", "second"]})
    assert sheet.reply_for("problem") == "first"
    assert sheet.reply_for("problem") == "second"
    assert sheet.reply_for("problem") is None      # exhausted
    assert sheet.reply_for("actors") is None       # never had anything to say


def test_a_turn_answers_only_the_questions_the_sheet_can_speak_to():
    """The skip is the fixture's version of a user pressing Enter, and it has to be visible: the
    answered slots are what the re-ask metric is measured against."""
    sheet = AnswerSheet({"problem": ["the real problem"]})
    block, answered = answers_for_turn([_q("problem"), _q("risks")], sheet)
    assert answered == ["problem"]
    assert "[slot: problem]" in block and "risks" not in block


def test_a_turn_the_sheet_cannot_speak_to_at_all_ends_the_capture():
    """`converse()` stops when no question got an answer, and so must the harness — otherwise it
    keeps paying for turns that carry no new client input."""
    block, answered = answers_for_turn([_q("risks")], AnswerSheet({}))
    assert block is None and answered == []


def test_load_turns_says_it_could_not_look_at_a_single_pass_capture():
    """Third state. A single-pass capture has nothing to say about turn 3, and saying nothing must
    not read as saying nothing is wrong."""
    text = json.dumps({"request": "r", "runs": [_model(problem=Impact.high).model_dump()]})
    assert load_turns(text) is None


def test_load_runs_reads_the_last_turn_of_each_run_when_there_is_no_runs_key():
    """The multi-turn envelope does not duplicate the final models under `runs` — every existing
    consumer of a baseline (consensus, movements, --questions) reads them back through here."""
    runs = [[_turn(1, ["problem"], states={"problem": ("inferred", 40)}),
             _turn(2, [], states={"problem": ("explicit", 90)})]]
    loaded = load_runs(turn_envelope("r", {"problem": ["p"]}, runs))
    assert len(loaded) == 1
    assert loaded[0].model["problem"].completeness == 90


def test_a_question_re_asked_after_the_client_answered_it_is_counted():
    """The failure mode the whole issue is about: the transcript is gone from turn 3, so the engine
    can come back to ground the client already covered."""
    run = [_turn(1, ["problem"], asks=("problem",), states={"problem": ("explicit", 80)}),
           _turn(2, ["actors"], asks=("actors",), states={"problem": ("explicit", 80)}),
           _turn(3, [], asks=("problem",), states={"problem": ("explicit", 80)})]
    lens = turn_lens([run])
    assert lens["measured"] is True
    assert lens["reasked"] == {"Real problem": 1}


def test_an_engine_that_moves_on_reports_no_re_ask():
    """The positive control's twin. Without it, a lens that never fires and a lens that is broken
    produce the same empty dict."""
    run = [_turn(1, ["problem"], asks=("problem",), states={"problem": ("explicit", 80)}),
           _turn(2, ["actors"], asks=("actors",), states={"problem": ("explicit", 80)}),
           _turn(3, ["risks"], asks=("risks",), states={"problem": ("explicit", 80)})]
    assert turn_lens([run])["reasked"] == {}


def test_a_re_ask_before_turn_three_is_not_counted():
    """Turns 1 and 2 send exactly what the old loop sent, so a repeat there is the engine's own
    behaviour and not evidence about the grounding change."""
    run = [_turn(1, ["problem"], asks=("problem",), states={"problem": ("explicit", 80)}),
           _turn(2, ["problem"], asks=("problem",), states={"problem": ("explicit", 80)})]
    assert turn_lens([run])["reasked"] == {}


def test_a_slot_the_client_confirmed_early_and_the_model_later_forgot_is_reported():
    run = [_turn(1, ["problem"], asks=("problem",), states={"problem": ("explicit", 80)}),
           _turn(2, [], states={"problem": ("explicit", 80)}),
           _turn(3, [], states={"problem": ("empty", 10)})]
    assert turn_lens([run])["lost"] == {"Real problem": 1}


def test_a_slot_that_stayed_confirmed_is_not_reported_as_lost():
    run = [_turn(1, ["problem"], asks=("problem",), states={"problem": ("explicit", 80)}),
           _turn(2, [], states={"problem": ("explicit", 80)}),
           _turn(3, [], states={"problem": ("explicit", 95)})]
    assert turn_lens([run])["lost"] == {}


def test_completeness_falling_back_across_a_deep_turn_is_reported():
    run = [_turn(1, ["problem"], asks=("problem",), states={"problem": ("explicit", 80)}),
           _turn(2, [], states={"problem": ("explicit", 90)}),
           _turn(3, [], states={"problem": ("explicit", 55)})]
    assert turn_lens([run])["regressed"] == {"Real problem": 1}


def test_a_finding_in_every_run_is_the_strong_tier():
    """The same rule the slot lens already applies: unanimous is what you act on, one run is noise."""
    reasks = [_turn(1, ["problem"], asks=("problem",), states={"problem": ("explicit", 80)}),
              _turn(2, ["actors"], asks=("actors",), states={"problem": ("explicit", 80)}),
              _turn(3, [], asks=("problem",), states={"problem": ("explicit", 80)})]
    clean = [_turn(1, ["problem"], asks=("problem",), states={"problem": ("explicit", 80)}),
             _turn(2, ["actors"], asks=("actors",), states={"problem": ("explicit", 80)}),
             _turn(3, ["risks"], asks=("risks",), states={"problem": ("explicit", 80)})]
    assert turn_lens([reasks, reasks])["unanimous"]["reasked"] == ["Real problem"]
    assert turn_lens([reasks, clean])["unanimous"]["reasked"] == []
    assert turn_lens([reasks, clean])["reasked"] == {"Real problem": 1}


def test_the_lens_reports_how_deep_each_run_actually_got():
    """A capture that stopped at turn 2 measures nothing about this issue, and the reader has to see
    that rather than read an empty finding set as a clean bill of health."""
    lens = turn_lens([[_turn(1, ["problem"], asks=("problem",)), _turn(2, [])]])
    assert lens["depths"] == [2]
    assert lens["deep_enough"] is False


def test_the_lens_says_it_could_not_look_rather_than_reporting_nothing():
    lens = turn_lens(None)
    assert lens["measured"] is False and lens["reason"]
    assert "reasked" not in lens        # no empty finding set to misread as clean


def test_turn_movements_reports_that_it_could_not_compare_a_single_pass_baseline():
    run = [_turn(1, ["problem"], asks=("problem",)), _turn(2, [])]
    m = turn_movements(None, [run])
    assert m["measured"] is False and m["reason"]


def test_turn_movements_reports_a_re_ask_the_engine_gained_or_dropped():
    before = [_turn(1, ["problem"], asks=("problem",), states={"problem": ("explicit", 80)}),
              _turn(2, ["actors"], asks=("actors",), states={"problem": ("explicit", 80)}),
              _turn(3, [], asks=("problem",), states={"problem": ("explicit", 80)})]
    after = [_turn(1, ["actors"], asks=("actors",), states={"actors": ("explicit", 80)}),
             _turn(2, ["problem"], asks=("problem",), states={"actors": ("explicit", 80)}),
             _turn(3, [], asks=("actors",), states={"actors": ("explicit", 80)})]
    m = turn_movements([before], [after])
    assert m["measured"] is True
    assert m["reasked_added"] == ["Actors & roles"]
    assert m["reasked_removed"] == ["Real problem"]
