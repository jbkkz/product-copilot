"""Core + services tests for the versioned session store, validation, and the apply pipeline.

All offline — no API, no provider. A temp workspace is pointed at with REQUIVO_WORKSPACE so the
canonical `.requivo/sessions/` layout is exercised in isolation.
"""
from __future__ import annotations

import json

import pytest

from requivo.core import persistence as store
from requivo.core.contracts import EngineOutput, _schema_order, schema_slot_ids
from requivo.core.errors import MissingRequiredSlotError, RequivoError, SessionNotFoundError, UnknownSlotError
from requivo.core.validation import validate_proposal
from requivo.services.artifacts import ArtifactService
from requivo.services.sessions import SessionService


def _slot(completeness=0, confidence="empty", impact="low", value=""):
    return {"completeness": completeness, "confidence": confidence, "impact": impact, "value": value}


def _full_model(**overrides) -> dict:
    _, required = schema_slot_ids()
    model = {sid: _slot() for sid in _schema_order() if sid in required}
    model.update(overrides)
    return {"model": model, "questions": [], "summary": {}}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))  # isolate legacy root too
    return tmp_path


# ── validation ────────────────────────────────────────────────────────────────


def test_validate_accepts_a_complete_model():
    out = validate_proposal(_full_model())
    assert isinstance(out, EngineOutput)


def test_validate_rejects_unknown_slot():
    bad = _full_model()
    bad["model"]["not_a_real_slot"] = _slot()
    with pytest.raises(UnknownSlotError) as e:
        validate_proposal(bad)
    assert e.value.code == "unknown_slot"
    assert "not_a_real_slot" in e.value.details["slots"]


def test_validate_rejects_missing_required_slot():
    partial = _full_model()
    a_required = next(iter(partial["model"]))
    del partial["model"][a_required]
    with pytest.raises(MissingRequiredSlotError) as e:
        validate_proposal(partial)
    assert e.value.code == "missing_required_slot"
    assert a_required in e.value.details["slots"]


def test_validate_allows_partial_when_not_required():
    partial = _full_model()
    del partial["model"][next(iter(partial["model"]))]
    out = validate_proposal(partial, require_complete=False)  # no raise
    assert isinstance(out, EngineOutput)


def test_validate_rejects_non_json_string():
    with pytest.raises(RequivoError) as e:
        validate_proposal("{not json")
    assert e.value.code == "invalid_model"


def test_error_to_dict_is_serializable():
    err = UnknownSlotError("bad", path="model.x", details={"slots": ["x"]})
    d = err.to_dict()
    assert d == {"code": "unknown_slot", "message": "bad", "path": "model.x", "details": {"slots": ["x"]}}
    json.dumps(d)  # must round-trip


# ── store: revisions + artifacts ────────────────────────────────────────────────


def test_store_creates_session_and_revisions(workspace):
    store.create_session("s1", "Build a leave system.", provider="claude-code")
    assert store.read_meta("s1").current_revision == 0
    out = EngineOutput.model_validate(_full_model())
    rev1, _ = store.save_revision("s1", out)
    rev2, meta = store.save_revision("s1", out)
    assert (rev1, rev2, meta.current_revision) == (1, 2, 2)
    d = store.canonical_dir("s1")
    assert (d / "model.json").exists()
    assert (d / "revisions" / "0001-model.json").exists()
    assert (d / "revisions" / "0002-model.json").exists()
    assert store.list_session_slugs() == ["s1"]


def test_store_migrate_session_rejects_a_future_format(workspace):
    from requivo.core.errors import InvalidSessionError
    with pytest.raises(InvalidSessionError):
        store.migrate_session({"format_version": 999, "session_id": "x", "slug": "s",
                               "created_at": "t", "updated_at": "t"})


# ── services: the apply pipeline ─────────────────────────────────────────────────


def test_session_service_create_and_apply(workspace):
    svc = SessionService()
    meta = svc.create_session("Build a leave approval system.", slug="leave", provider="claude-code")
    assert meta.slug == "leave" and meta.current_revision == 0

    # A high-impact slot left unconfirmed must block readiness.
    result = svc.update_model("leave", _full_model(**{"problem": _slot(0, "empty", "high")}))
    assert result.status == "applied"
    assert result.revision == 1
    assert set(result.changed_slots)  # every slot present counts as changed on the first apply
    assert result.readiness.ready is False
    assert "problem" in result.readiness.blocking_slots


def test_apply_diff_reports_changed_slots_and_readiness(workspace):
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    # First model: everything empty.
    svc.update_model("s", _full_model())
    # Second: fill one slot explicitly → it should be the changed slot.
    changed_model = _full_model(**{"problem": _slot(90, "explicit", "high", "A real problem")})
    result = svc.update_model("s", changed_model)
    assert result.revision == 2
    assert "problem" in result.changed_slots


def test_diff_does_not_write(workspace):
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())
    before = store.read_meta("s").current_revision
    plan = svc.diff("s", _full_model(**{"problem": _slot(90, "explicit", "high", "X")}))
    assert plan.status == "planned"
    assert store.read_meta("s").current_revision == before  # unchanged — no write


def test_apply_flags_generated_artifact_stale(workspace):
    svc = SessionService()
    art = ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())
    art.save("s", "prd", "# PRD\n")  # generated at revision 1
    assert art.list("s")["prd"]["stale"] is False
    # Change a slot the PRD consumes (workflow) → PRD goes stale.
    result = svc.update_model("s", _full_model(**{"workflow": _slot(80, "explicit", "high", "new flow")}))
    assert "prd" in result.stale_artifacts
    assert art.list("s")["prd"]["stale"] is True


def _with_reasoning(model: dict) -> dict:
    """A full model that also carries baked-in reasoning: a decision on `permissions`, a challenge
    contesting `workflow`."""
    model["decisions"] = [{"decision": "Draft-first", "derived_from": ["permissions"]}]
    model["challenges"] = [{
        "headline": "Archive vs delete", "premise": "p", "alternative": "a",
        "consequence": "c", "recommendation": "r", "contests": ["workflow"],
    }]
    return model


def test_propagate_reports_challenges_via_contests():
    from requivo.core.dependencies import propagate
    out = EngineOutput.model_validate(_with_reasoning(_full_model()))
    hit = propagate(out, ["workflow"])
    assert [c.headline for c in hit.challenges] == ["Archive vs delete"]
    assert hit.reasoning_hit is True
    # A change that touches neither derived_from nor contests unseats no reasoning.
    miss = propagate(out, ["success_metrics"])
    assert not miss.challenges and not miss.decisions and not miss.reasoning_hit


def test_apply_flags_assessment_stale_when_reasoning_is_unseated(workspace):
    svc = SessionService()
    art = ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _with_reasoning(_full_model()))
    art.save("s", "brief", "# Assessment\n")  # the saved assessment renders that reasoning
    assert art.list("s")["brief"]["stale"] is False

    # Change `workflow` — a challenge contests it → the assessment on disk no longer holds.
    changed = _with_reasoning(_full_model())
    changed["model"]["workflow"] = _slot(80, "explicit", "high", "new flow")
    result = svc.update_model("s", changed)
    assert "Archive vs delete" in result.invalidated_challenges
    assert "brief" in result.stale_artifacts
    assert art.list("s")["brief"]["stale"] is True
    # The decision (on `permissions`) was untouched, so it is not reported.
    assert result.invalidated_decisions == []
    assert "invalidated_challenges" in result.to_dict()


def test_changing_the_problem_marks_a_saved_assessment_stale(workspace):
    # The assessment used to sit outside the artifact→slot map entirely, on the grounds that it was the
    # live analysis layer rather than a deliverable. Once it is saved to disk that stops holding: an
    # assessment whose problem statement has since been rewritten is not "fresh", it is out of date.
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())          # no decisions, no challenges — nothing to unseat
    art.save("s", "brief", "# Assessment\n")
    assert art.list("s")["brief"]["stale"] is False

    result = svc.update_model("s", _full_model(**{"problem": _slot(80, "explicit", "high", "reframed")}))
    assert "brief" in result.stale_artifacts
    assert art.list("s")["brief"]["stale"] is True


def test_artifact_cannot_be_recorded_against_an_impossible_revision(workspace):
    # Provenance that cannot be true is worse than none: every freshness answer downstream is read off
    # this number, so a revision from the future is refused rather than stored.
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())          # session is at revision 1
    with pytest.raises(RequivoError) as ei:
        art.save("s", "prd", "# PRD\n", source_revision=999)
    assert ei.value.code == "invalid_session"
    with pytest.raises(RequivoError):
        art.save("s", "prd", "# PRD\n", source_revision=0)
    assert "prd" not in art.list("s")            # nothing was recorded


# ── generation vs. concurrent writes ──────────────────────────────────────────
# A provider call runs for seconds to minutes, and the session can move underneath it (a second browser
# tab, a CLI apply, a Claude Code turn). These two tests pin the behaviour at that seam: the model the
# generator read is the revision it writes against, and a change that lands mid-flight is never lost
# and never silently inherited.

class _RacingClient:
    """A provider whose reply arrives only after someone else has already moved the session."""

    def __init__(self, reply: str, on_call):
        self._reply, self._on_call = reply, on_call
        self.messages = self

    def create(self, **kwargs):
        self._on_call()          # the concurrent write lands while "reasoning" is in flight
        return _Reply(self._reply)


class _Reply:
    def __init__(self, text):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = "end_turn"
        self.usage = None


def test_generation_that_races_a_concurrent_apply_does_not_lose_it(workspace):
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())          # revision 1 — what the generator will read

    def concurrent_answer():
        svc.update_model("s", _full_model(**{"business_rules": _slot(90, "explicit", "high", "HR signs off")}))

    brief_reply = json.dumps({"complexity": "medium", "problem": "P", "solution": "S",
                              "risks": [], "next_steps": []})
    disco = DiscoveryService(client=_RacingClient(brief_reply, concurrent_answer))
    with pytest.raises(RevisionConflictError):
        disco.generate("s", "brief")

    # The rule that landed mid-flight is still there — the assessment's apply did not write over it.
    assert svc.load_model("s").model["business_rules"].value == "HR signs off"


def test_an_answers_turn_holds_the_revision_it_read(workspace):
    # A turn has the same seam as a generation, so a caller that passes no expectation still gets one:
    # the revision the turn actually read. Without it, the CLI's `answer` would quietly overwrite a
    # change made in a browser tab between the read and the apply.
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())

    def concurrent_apply():
        svc.update_model("s", _full_model(**{"risks": _slot(70, "explicit", "high", "rollout risk")}))

    disco = DiscoveryService(client=_RacingClient(json.dumps(_full_model()), concurrent_apply))
    with pytest.raises(RevisionConflictError):
        disco.answer("s", "here are my answers")
    assert svc.load_model("s").model["risks"].value == "rollout risk"


def test_an_artifact_generated_from_a_superseded_revision_is_born_stale(workspace):
    from requivo.services.discovery import DiscoveryService

    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())          # revision 1 — the PRD's actual source

    def concurrent_answer():
        svc.update_model("s", _full_model(**{"workflow": _slot(90, "explicit", "high", "new flow")}))

    prd_reply = json.dumps({"title": "PRD", "summary": "…"})
    DiscoveryService(client=_RacingClient(prd_reply, concurrent_answer)).generate("s", "prd")

    saved = art.list("s")["prd"]
    assert saved["revision"] == 1        # recorded against the revision it was written from…
    assert saved["stale"] is True        # …and the workflow change it never saw makes it stale


def test_update_missing_session_raises(workspace):
    with pytest.raises(SessionNotFoundError):
        SessionService().update_model("ghost", _full_model())


# ── legacy migration on first mutation ──────────────────────────────────────────


def test_legacy_session_is_migrated_on_first_apply(workspace):
    # Seed a legacy out/<slug>/ session by hand (model + request + a generated artifact).
    legacy = store.legacy_dir("old")
    (legacy).mkdir(parents=True)
    (legacy / "model.json").write_text(json.dumps(_full_model()))
    (legacy / "request.txt").write_text("Legacy request.")
    (legacy / "prd.md").write_text("# Legacy PRD\n")

    svc = SessionService()
    assert svc.exists("old")  # visible via the legacy root
    # A read does not migrate.
    svc.load_model("old")
    assert not store.session_exists("old")

    # First mutation migrates in place, preserving the originals.
    result = svc.update_model("old", _full_model(**{"problem": _slot(90, "explicit", "high", "P")}))
    assert store.session_exists("old")
    assert (legacy / "model.json").exists()  # legacy left untouched
    # The migrated model became revision 1, and the apply is revision 2.
    assert result.revision == 2
    d = store.canonical_dir("old")
    assert (d / "revisions" / "0001-model.json").exists()
    assert (d / "artifacts" / "prd.md").read_text() == "# Legacy PRD\n"  # artifact carried over
