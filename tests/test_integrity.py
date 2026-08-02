"""Integrity tests: the guarantees the session store makes about concurrency, provenance and bounds.

Every test here reproduces a way the store could lie or crash rather than a feature it offers — two
writers racing, an artifact recorded fresh when it isn't, a future field destroyed by an older
reader, a slug the filesystem refuses. They are grouped in one file because they share a subject:
*the session on disk is trustworthy*. All offline — no API, no provider.
"""
from __future__ import annotations

import json
import threading

import pytest

from requivo.core import persistence as store
from requivo.core.contracts import _schema_order, schema_slot_ids
from requivo.core.errors import InvalidSlugError, RevisionConflictError
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
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path


# ── concurrency ───────────────────────────────────────────────────────────────


def test_racing_applies_conflict_cleanly_instead_of_crashing(workspace):
    """Two writers starting from the same revision: one lands, the other is told it lost.

    `expected_revision` was checked and then acted on, with the writes in between unguarded, so both
    writers passed the check. What surfaced was not a `RevisionConflictError` but a `FileNotFoundError`
    from `Path.replace` — the two had also collided on one shared temp filename, and the second
    renamed a scratch file the first had already moved away. A lost update is bad; a lost update
    reported as a missing file is worse, because it reads as a bug in Requivo rather than a conflict
    the caller must resolve."""
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())  # revision 1 — the shared base

    # A dozen writers rather than two: the unguarded window was narrow, and a regression test that
    # only sometimes reopens it is not a regression test. Against the pre-0.9.4 store this reliably
    # produced both symptoms at once — several FileNotFoundError crashes, and *two* writers passing
    # the same `expected_revision=1` precondition.
    n = 12
    start = threading.Barrier(n)
    outcomes: list[str] = []
    guard = threading.Lock()

    def apply(value: str) -> None:
        start.wait()
        try:
            svc.update_model("s", _full_model(**{"workflow": _slot(80, "explicit", "high", value)}),
                             expected_revision=1)
            outcome = "applied"
        except RevisionConflictError:
            outcome = "conflict"
        except BaseException as e:  # noqa: BLE001 - the point is to catch anything else
            outcome = f"crash:{type(e).__name__}"
        with guard:
            outcomes.append(outcome)

    threads = [threading.Thread(target=apply, args=(str(i),)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert outcomes.count("applied") == 1                    # exactly one writer may win
    assert outcomes.count("conflict") == n - 1               # the rest are told so, and told why
    assert [o for o in outcomes if o.startswith("crash:")] == []
    # Exactly one revision was created, and the session is internally consistent afterwards.
    meta = store.read_meta("s")
    assert meta.current_revision == 2
    assert len(meta.revisions) == 2
    assert (store.canonical_dir("s") / "revisions" / "0002-model.json").exists()


def test_concurrent_atomic_writes_do_not_collide_on_a_temp_file(workspace):
    """`_atomic_write` is called from every write path; its scratch file must be private to the call.

    With a fixed `.model.json.tmp`, concurrent writers interleaved write-then-rename and the loser hit
    `FileNotFoundError`. The content afterwards must be one writer's payload in full — never a mix."""
    d = workspace / "scratch"
    d.mkdir()
    target = d / "model.json"
    errors: list[BaseException] = []

    def write(n: int) -> None:
        try:
            store._atomic_write(target, f"payload-{n}\n" * 200)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    lines = set(target.read_text().splitlines())
    assert len(lines) == 1                                    # one writer's payload, not a blend
    assert not list(d.glob(".*tmp"))                          # no scratch left behind


def test_a_failed_atomic_write_leaves_no_scratch_file(workspace):
    d = workspace / "scratch"
    d.mkdir()
    with pytest.raises(TypeError):
        store._atomic_write(d / "model.json", None)  # type: ignore[arg-type]
    assert list(d.iterdir()) == []


# ── forward compatibility of the session file ─────────────────────────────────


def test_a_field_from_a_future_requivo_survives_a_round_trip(workspace):
    """`docs/compatibility.md` promises that adding a field is a compatible change. Under
    `extra="ignore"` an older reader honoured that only until it wrote the file back, at which point
    the unknown field was silently dropped — so the first mutation by an older Requivo destroyed it."""
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    p = store.canonical_dir("s") / "session.json"

    raw = json.loads(p.read_text())
    raw["future_field"] = {"added_by": "requivo 1.4", "keep": True}
    p.write_text(json.dumps(raw, indent=2))

    svc.update_model("s", _full_model())        # a real mutation: reads, then rewrites session.json
    after = json.loads(p.read_text())
    assert after["future_field"] == {"added_by": "requivo 1.4", "keep": True}
    assert after["current_revision"] == 1       # and the known fields still moved


def test_a_retired_key_is_dropped_rather_than_carried_forever(workspace):
    """The mirror image: `extra="allow"` must not resurrect keys a past Requivo retired. Retirement is
    explicit in `migrate_session`, so a key from the *past* is dropped and one from the *future* kept."""
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    p = store.canonical_dir("s") / "session.json"

    raw = json.loads(p.read_text())
    raw["prompt_versions"] = {"engine.md": "sha256:dead"}   # declared once, never written, now retired
    p.write_text(json.dumps(raw, indent=2))

    svc.update_model("s", _full_model())
    assert "prompt_versions" not in json.loads(p.read_text())


# ── slug bounds ───────────────────────────────────────────────────────────────


def test_a_long_request_yields_a_slug_the_filesystem_accepts(workspace):
    """A request is arbitrary user text. One 300-character word made a 300-character directory name
    and the write failed deep inside with a bare `OSError: File name too long`."""
    long_word = "a" * 300
    slug = store._slug(f"{long_word} system")
    assert len(slug) <= store.MAX_SLUG_LENGTH
    store.validate_slug(slug)                    # still a well-formed kebab-case token

    # Truncation must not merge two different requests into one session directory.
    other = store._slug(f"{long_word} platform")
    assert slug != other

    svc = SessionService()
    meta = svc.create_session(f"{long_word} system")   # and the whole path is writable
    assert store.canonical_dir(meta.slug).is_dir()


def test_an_explicit_over_long_slug_is_refused_at_the_boundary(workspace):
    with pytest.raises(InvalidSlugError) as e:
        store.validate_slug("x" * (store.MAX_SLUG_LENGTH + 1))
    assert e.value.code == "invalid_slug"
    assert e.value.details["max_length"] == store.MAX_SLUG_LENGTH


# ── artifact freshness ────────────────────────────────────────────────────────


def _with_decision(model: dict, why: str) -> dict:
    model["decisions"] = [{"decision": "Draft-first", "why": why, "derived_from": ["permissions"]}]
    return model


def test_an_artifact_saved_against_an_older_revision_is_recorded_stale(workspace):
    """Saving is not the same moment as reasoning. A provider call takes minutes and the session can
    move under it; Claude Code can save a file it produced several turns ago. The source revision was
    recorded faithfully and the freshness beside it was simply assumed `False`, so a PRD reasoned from
    a superseded model sat on disk marked fresh."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())                                        # revision 1
    svc.update_model("s", _full_model(**{"workflow": _slot(80, "explicit", "high", "new")}))  # 2

    st = art.save("s", "prd", "# PRD\n", source_revision=1)   # reasoned from 1, saved at 2
    assert st.revision == 1
    assert st.stale is True
    assert art.list("s")["prd"]["stale"] is True


def test_an_older_revision_that_missed_the_artifact_leaves_it_fresh(workspace):
    """The control. Staleness is the dependency graph, not revision drift: an artifact whose slots
    were untouched stays fresh however far the session has moved on."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())
    # `current_process` is in no artifact's slot set except the assessment's `*`.
    svc.update_model("s", _full_model(**{"current_process": _slot(80, "explicit", "high", "email")}))

    assert art.save("s", "prd", "# PRD\n", source_revision=1).stale is False
    assert art.save("s", "brief", "# Assessment\n", source_revision=1).stale is True


# ── the reasoning layer as a dependency ───────────────────────────────────────


def test_reasoning_that_changes_without_a_slot_moving_still_invalidates(workspace):
    """Every generator is prompted with the whole model, reasoning included, so a rewritten design
    decision can change the PRD with no slot touched. `diff_models` sees only slots, so this reported
    `changed_slots: []` and left the PRD marked fresh."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _with_decision(_full_model(), "drafts are cheap"))
    art.save("s", "prd", "# PRD\n")
    assert art.list("s")["prd"]["stale"] is False

    result = svc.update_model("s", _with_decision(_full_model(), "reviewers were the bottleneck"))
    assert result.changed_slots == []                       # the facts did not move
    assert len(result.changed_decisions) == 1               # the judgment over them did
    assert "prd" in result.stale_artifacts
    assert art.list("s")["prd"]["stale"] is True
    assert "changed_decisions" in result.to_dict()


def test_reasoning_merely_omitted_by_a_turn_is_not_a_change(workspace):
    """A refinement turn answers a question; it does not re-derive the brief, so its reply routinely
    arrives with no decisions at all. Reading that silence as a deletion would mark every artifact
    stale on nearly every turn — a freshness signal that fires constantly says nothing."""
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _with_decision(_full_model(), "drafts are cheap"))
    art.save("s", "prd", "# PRD\n")

    result = svc.update_model("s", _full_model())           # same slots, reasoning simply absent
    assert result.changed_decisions == []
    assert result.stale_artifacts == []
    assert art.list("s")["prd"]["stale"] is False
