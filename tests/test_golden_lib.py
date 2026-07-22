"""Unit tests for the golden harness's own logic.

The harness decides what counts as a regression, so a bug here is worse than a bug in a generator: it
would let a real change through, or invent one that isn't there. None of this needs an API call — the
captures are fixtures, and every function below is a pure read over them.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from golden_lib import (_cluster_headlines, brief_consensus, brief_movements,  # noqa: E402
                        consensus, movements, stability)

from src.engine import (Brief, Challenge, Confidence, EngineOutput, Impact,  # noqa: E402
                        Level, Slot, Summary)


# ── builders ─────────────────────────────────────────────────────────────────────────────────────

def _slot(value="v", impact=Impact.medium, confidence=Confidence.explicit, completeness=80):
    return Slot(value=value, completeness=completeness, confidence=confidence,
                impact=impact, evidence="e")


def _model(**impacts) -> EngineOutput:
    """An EngineOutput carrying the named slots at the given impacts, no questions."""
    return EngineOutput(model={sid: _slot(impact=imp) for sid, imp in impacts.items()},
                        questions=[], summary=Summary())


def _challenge(headline):
    return Challenge(headline=headline, premise="p", alternative="a",
                     consequence="c", recommendation="r")


def _brief(headlines, complexity=Level.high):
    return Brief(challenges=[_challenge(h) for h in headlines], complexity=complexity)


# ── the noise floor ──────────────────────────────────────────────────────────────────────────────

def test_consensus_reports_modal_value_and_agreement():
    runs = [_model(a=Impact.high), _model(a=Impact.high), _model(a=Impact.low)]
    con = consensus(runs)
    assert con["slots"]["a"]["impact"] == ("high", 2)   # modal value, 2 of 3 runs
    assert con["n"] == 3


def test_stability_separates_unanimous_slots_from_jittery_ones():
    runs = [_model(steady=Impact.high, jumpy=Impact.high),
            _model(steady=Impact.high, jumpy=Impact.low),
            _model(steady=Impact.high, jumpy=Impact.medium)]
    st = stability(runs)
    assert st["unanimous"]["impact"] == 1
    assert st["jitter"]["impact"] == 1


# ── strong vs weak, the rule the whole lens rests on ─────────────────────────────────────────────

def test_unanimous_before_and_after_is_a_strong_move():
    old = [_model(a=Impact.low)] * 3
    new = [_model(a=Impact.high)] * 3
    m = movements(old, new)
    assert len(m["strong"]) == 1 and not m["weak"]
    assert (m["strong"][0]["from"], m["strong"][0]["to"]) == ("low", "high")


def test_bare_majority_is_only_a_weak_move():
    """At K=3 a majority is 2 of 3 — one run flipping. That must not read as signal."""
    old = [_model(a=Impact.low)] * 3
    new = [_model(a=Impact.high), _model(a=Impact.high), _model(a=Impact.low)]
    m = movements(old, new)
    assert len(m["weak"]) == 1 and not m["strong"]


def test_a_jittery_old_baseline_is_never_a_reference():
    """If the old runs disagreed, there is nothing reliable to have moved away from."""
    old = [_model(a=Impact.low), _model(a=Impact.low), _model(a=Impact.high)]
    new = [_model(a=Impact.medium)] * 3
    assert not movements(old, new)["moved"]


def test_no_movement_when_the_value_holds():
    runs = [_model(a=Impact.high)] * 3
    assert not movements(runs, runs)["moved"]


# ── the assessment lens ──────────────────────────────────────────────────────────────────────────

def test_headlines_cluster_across_phrasing_variants():
    """The engine never repeats a headline verbatim, so themes are matched on content-word overlap."""
    clusters = _cluster_headlines([
        ["Signature as billing trigger"],
        ["Billing trigger at signature"],
        ["Signature is the billing trigger"],
    ])
    assert list(clusters.values()) == [3]      # one theme, seen in all three runs


def test_a_theme_from_a_single_run_stays_below_the_majority():
    briefs = [_brief(["Offline capability assumed"]),
              _brief(["Offline capability assumed"]),
              _brief(["Retention clock on delete"])]
    themes = brief_consensus(briefs)["themes"]
    assert "Offline capability assumed" in themes
    assert "Retention clock on delete" not in themes


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
