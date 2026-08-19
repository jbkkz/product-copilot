"""#6 — an artifact's provenance is stated by the caller or the save is refused.

Invariant 2 ends "Never record `stale=False` because the caller didn't say otherwise", and
`ArtifactService.save` was doing exactly that: an omitted `source_revision` was read as *the current
revision*, which made `_stale_since` return False without consulting the dependency graph at all. The
recorded number was the session's real current revision, so the fabrication was undetectable
downstream — `artifact list`, `session show` and the Web all reported a stale document fresh.

Every assertion here is paired. The "must not happen" half of a guard passes just as happily when the
fixture is broken and nothing happens at all, so each refusal test sits beside a save that must still
be accepted and must still record the flag it always recorded.
"""

from __future__ import annotations

import json

import pytest

from requivo.core import persistence as store
from requivo.core.contracts import _schema_order, schema_slot_ids
from requivo.core.errors import InvalidSessionError, RequivoError
from requivo.services.artifacts import ArtifactService, UnstatedSourceRevisionError
from requivo.services.sessions import SessionService


def _slot(completeness=0, confidence="empty", impact="low", value=""):
    return {"completeness": completeness, "confidence": confidence, "impact": impact, "value": value}


def _full_model(**overrides) -> dict:
    _, required = schema_slot_ids()
    model = {sid: _slot() for sid in _schema_order() if sid in required}
    model.update(overrides)
    return {"model": model, "questions": [], "summary": {"objective": "A leave approval system"}}


@pytest.fixture
def moved(tmp_path, monkeypatch):
    """A session at revision 2 whose `workflow` slot — which `prd` and `criteria` both rest on —
    materially changed between revision 1 and revision 2.

    So: an artifact reasoned from revision 1 IS stale, and one reasoned from revision 2 is NOT. The
    two answers are different, which is what makes an assertion about either of them mean something.
    """
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    slug = "prov-moved"
    store.create_session(slug, "a leave approval system")
    svc = SessionService()
    svc.update_model(slug, json.dumps(_full_model(workflow=_slot(50, "inferred", "high", "draft"))))
    svc.update_model(slug, json.dumps(_full_model(
        workflow=_slot(90, "explicit", "high", "draft -> issued -> archived"))))
    assert store.read_meta(slug).current_revision == 2, "fixture did not reach revision 2"
    return slug


# ── F1: an omitted source revision is unknown provenance, not "now" ───────────────


def test_a_stated_source_revision_still_records_the_flag_it_always_did(moved):
    """MUST FIRE. The positive control for every refusal below: with the revision stated, the two
    answers are still computed from the dependency graph and they still differ from each other."""
    svc = ArtifactService()
    stale = svc.save(moved, "prd", "# PRD reasoned from revision 1", source_revision=1)
    assert stale.revision == 1 and stale.stale is True

    fresh = svc.save(moved, "criteria", "# criteria reasoned from revision 2", source_revision=2)
    assert fresh.revision == 2 and fresh.stale is False


def test_an_omitted_source_revision_is_refused_rather_than_read_as_now(moved):
    """#6 F1. The save that used to be recorded `revision: 2, stale: false` about content the caller
    never said anything about."""
    with pytest.raises(UnstatedSourceRevisionError) as e:
        ArtifactService().save(moved, "prd", "# PRD reasoned from revision 1")
    assert isinstance(e.value, RequivoError), "must reach a surface as a structured failure"
    assert e.value.details["source_revision"] is None
    assert e.value.details["current_revision"] == 2


def test_the_refusal_says_what_to_pass(moved):
    """A refusal a caller cannot act on is a worse outcome than the wrong answer it replaced: the
    message has to name the flag and the range of revisions that exist."""
    with pytest.raises(UnstatedSourceRevisionError) as e:
        ArtifactService().save(moved, "prd", "# PRD")
    msg = e.value.message
    assert "--revision" in msg and "source_revision" in msg
    assert "1" in msg and "2" in msg, f"the revision range is not in the message: {msg!r}"


def test_a_refused_save_writes_nothing_at_all(moved):
    """The refusal happens before the artifact file and before `session.json` is touched, so a
    rejected save cannot leave content on disk that no status row describes."""
    svc = ArtifactService()
    with pytest.raises(UnstatedSourceRevisionError):
        svc.save(moved, "prd", "# PRD")
    assert not (store.canonical_dir(moved) / "artifacts" / "prd.md").exists()
    assert "prd" not in store.read_meta(moved).artifact_status

    # must fire: the same call with the revision stated does write both.
    svc.save(moved, "prd", "# PRD", source_revision=1)
    assert (store.canonical_dir(moved) / "artifacts" / "prd.md").exists()
    assert "prd" in store.read_meta(moved).artifact_status


# ── F2: the honesty guard is as wide as the failure set it was written for ────────


def _revision_file(slug: str, revision: int):
    return store.canonical_dir(slug) / "revisions" / f"{revision:04d}-model.json"


def test_a_corrupt_revision_file_is_refused_as_a_structured_error(moved):
    """#6 F2. `except RequivoError` only caught a *missing* revision. A file that is present but
    truncated — an interrupted sync — reaches `PersistedEngineOutput.model_validate_json`, which raises
    pydantic's `ValidationError`: a `ValueError`, so the guard never fired and a raw traceback came
    out of a service call, from inside the session lock.
    """
    _revision_file(moved, 1).write_text('{"model": {"workflow": ', encoding="utf-8")
    with pytest.raises(InvalidSessionError) as e:
        ArtifactService().save(moved, "prd", "# PRD", source_revision=1)
    assert e.value.code == "invalid_session"
    assert e.value.details["source_revision"] == 1
    assert "ValidationError" in json.dumps(e.value.details), "the cause is not recorded"


def test_an_unreadable_revision_file_is_refused_as_a_structured_error(moved):
    """The `OSError` half of the same widening. A directory where the revision file belongs is the
    portable way to make the read fail rather than the parse: POSIX raises `IsADirectoryError` and
    Windows raises `PermissionError`, and both are `OSError`, so this asserts the same refusal on
    every leg instead of branching on the platform.
    """
    p = _revision_file(moved, 1)
    p.unlink()
    p.mkdir()
    with pytest.raises(InvalidSessionError) as e:
        ArtifactService().save(moved, "prd", "# PRD", source_revision=1)
    assert e.value.code == "invalid_session"


def test_a_missing_revision_file_is_still_refused_the_way_it_always_was(moved):
    """The case the original guard did catch, kept as a control: widening it must not have moved
    the answer for the failure it already handled."""
    _revision_file(moved, 1).unlink()
    with pytest.raises(InvalidSessionError):
        ArtifactService().save(moved, "prd", "# PRD", source_revision=1)


def test_both_invalid_session_refusals_carry_one_details_shape(moved):
    """`invalid_session` now carries two conditions from this service — provenance not stated, and
    provenance not readable — and `docs/compatibility.md` holds every code to one `details` shape.

    A key on one payload and not the other is the failure #35 measured: a consumer follows the
    documented advice (match the code), reads a key out of `details`, and gets a `KeyError` from a
    payload that correctly carried the code it matched. Asserted on the *key sets* rather than on
    either one alone, so adding a field to one raise site and not its sibling fails here rather than
    in somebody's consumer. The claim was prose in two places and checked by nothing until this test.
    """
    svc = ArtifactService()
    with pytest.raises(InvalidSessionError) as unstated:
        svc.save(moved, "prd", "# PRD")

    _revision_file(moved, 1).write_text("{", encoding="utf-8")
    with pytest.raises(InvalidSessionError) as unreadable:
        svc.save(moved, "prd", "# PRD", source_revision=1)

    assert unstated.value.code == unreadable.value.code == "invalid_session"
    assert set(unstated.value.details) == set(unreadable.value.details)
    # must fire: the shared shape is the real one, not two empty dicts agreeing with each other.
    assert {"slug", "type", "source_revision", "current_revision", "cause"} == set(unstated.value.details)
    # and the two payloads still say different things — one shape is not one fact.
    assert unstated.value.details["cause"] is None
    assert unreadable.value.details["cause"] is not None


def test_an_intact_history_is_still_diffed_rather_than_refused(moved):
    """MUST FIRE for the whole F2 block. Every test above breaks a revision file and asserts a
    refusal; if the fixture stopped producing a readable history, they would all still pass. This
    one fails instead."""
    st = ArtifactService().save(moved, "prd", "# PRD", source_revision=1)
    assert st.stale is True
