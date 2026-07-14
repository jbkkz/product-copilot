"""Unit tests for the pure logic — the parts that must be correct without an API call."""
import pytest
from pydantic import ValidationError

from src.engine import (
    PRD,
    AcceptanceCriteria,
    Confidence,
    EngineOutput,
    Slot,
    _extract_json,
    _readiness_blockers,
    _state_of,
    criteria_markdown,
    estimate_confidence,
    prd_markdown,
    soft_slots,
)


def slot(completeness, confidence, impact):
    return {"completeness": completeness, "confidence": confidence, "impact": impact}


def out(model):
    return EngineOutput.model_validate({"model": model, "questions": [], "summary": {}})


# ── JSON extraction ──────────────────────────────────────────────────────────


def test_extract_json_strips_fence():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_slices_surrounding_text():
    assert _extract_json('here it is: {"b": 2} — done') == {"b": 2}


def test_extract_json_raises_on_garbage():
    with pytest.raises(ValueError):
        _extract_json("no json object anywhere")


# ── Contract validation ──────────────────────────────────────────────────────


def test_output_rejects_out_of_range_completeness():
    with pytest.raises(ValidationError):
        out({"problem": slot(150, "explicit", "high")})


def test_output_rejects_unknown_confidence():
    with pytest.raises(ValidationError):
        out({"problem": slot(10, "maybe", "high")})


def test_output_requires_model():
    with pytest.raises(ValidationError):
        EngineOutput.model_validate({"questions": [], "summary": {}})


# ── The driver: uncertainty × impact ─────────────────────────────────────────


def test_soft_slots_are_medium_or_high_and_unresolved():
    model = out({
        "a": slot(90, "explicit", "high"),   # solid → not soft
        "b": slot(30, "inferred", "high"),   # uncertain + high → soft
        "c": slot(50, "inferred", "medium"), # uncertain + medium → soft
        "d": slot(10, "empty", "low"),       # low impact → never soft
    })
    assert soft_slots(model) == ["b", "c"]


def test_estimate_confidence_tiers():
    assert estimate_confidence(0) == "high"
    assert estimate_confidence(1) == "high"
    assert estimate_confidence(3) == "medium"
    assert estimate_confidence(5) == "low"


def test_readiness_blockers_are_high_impact_unconfirmed():
    model = out({
        "a": slot(90, "explicit", "high"),   # confirmed → not blocking
        "b": slot(80, "inferred", "high"),   # high but inferred → blocker
        "c": slot(0, "empty", "medium"),     # medium → not blocking
    })
    assert _readiness_blockers(model) == ["b"]


def test_state_of_maps_confidence():
    assert _state_of(Slot(completeness=90, confidence="explicit", impact="high")) == "confirmed"
    assert _state_of(Slot(completeness=50, confidence="inferred", impact="high")) == "inferred"
    assert _state_of(Slot(completeness=0, confidence="empty", impact="low")) == "unknown"


# ── Rendering ────────────────────────────────────────────────────────────────


def test_prd_markdown_renders_title_and_requirement_table():
    prd = PRD(
        title="Leave approval",
        requirements=[{"id": "FR-1", "requirement": "Submit a request", "priority": "must"}],
    )
    md = prd_markdown(prd)
    assert md.startswith("# Leave approval")
    assert "| FR-1 | Submit a request | Must |" in md


def test_criteria_markdown_renders_gherkin_checklist():
    ac = AcceptanceCriteria(
        title="Leave approval",
        features=[
            {
                "name": "Submitting a request",
                "scenarios": [
                    {
                        "id": "AC-1",
                        "title": "Valid request is accepted",
                        "kind": "happy_path",
                        "given": ["the employee is logged in", "they have enough balance"],
                        "when": "they submit a 3-day request",
                        "then": ["the request is created", "the manager is notified"],
                    }
                ],
            }
        ],
        open_questions=["Can a manager approve their own request?"],
    )
    md = criteria_markdown(ac)
    assert md.startswith("# Leave approval")
    assert "### [ ] AC-1 — Valid request is accepted  _Happy path_" in md
    # First given is "Given", subsequent ones fold to "And"; likewise Then → And.
    assert "- **Given** the employee is logged in" in md
    assert "- **And** they have enough balance" in md
    assert "- **When** they submit a 3-day request" in md
    assert "- **Then** the request is created" in md
    assert "- **And** the manager is notified" in md
    assert "## Open questions" in md
