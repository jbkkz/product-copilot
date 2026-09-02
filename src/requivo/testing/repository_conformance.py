"""A pytest suite any `SessionRepository` implementation can run against itself (#424).

`SessionService` and `ArtifactService` are backing-agnostic by design (CLAUDE.md: "Both storage
... and reasoning ... are injected, so the orchestration is backing-agnostic -- a Postgres
repository reuses it verbatim"). The proof of that used to live entirely inside
`tests/test_sessions.py`, as one dict-backed `InMemorySessionRepository` and one test exercising it
-- real, but private: not importable, not runnable by an external implementation. This module lifts
the behavioural assertions behind that proof out into a factory-parametrised base class. Subclass it,
implement `make_repository()`, and pytest collects the rest.

    from requivo.testing.repository_conformance import SessionRepositoryConformance

    class TestMyPostgresRepository(SessionRepositoryConformance):
        def make_repository(self):
            return MyPostgresSessionRepository(dsn=TEST_DSN)

A repository that passes this suite has proven it honours the semantics the services assume:

- **Create claims the slug once** (invariant 11) -- a second `create()` on a slug already in the
  store raises `SessionExistsError` rather than silently overwriting it.
- **`expected_revision` is a real precondition** (invariant 2) -- `save_revision` refuses a write
  against a revision the session has moved past.
- **The lock is mutually exclusive across threads and re-entrant within one** (invariant 9) -- two
  threads never hold it at once, and a thread that already holds it may take it again without
  deadlocking (the service takes it once per compound operation and the calls inside take it again).
- **`list_slugs()` and `list_unexaminable()` partition, never overlap** (invariant 15's shape, at the
  repository's own layer) -- a name the backing knows is a session is never also reported as one it
  could not examine.
- **`load_artifact` returns `None` for a real absence** -- the *refuse loudly on an unsafe name*
  half of that rule is specific to a path-building backing (see the protocol's own docstring) and is
  not asserted here; what every backing owes is that "nobody has generated this yet" reads as `None`.
- **An unknown top-level key on the persisted model survives a save/load round trip** (invariants
  8/10) -- a field a *newer* Requivo wrote and this one does not recognise must not be silently
  dropped by the backing's own (de)serialisation.

What this suite deliberately does **not** assert: anything about *where* or *how* a backing stores
data (a Postgres row layout, a file's exact path) -- only the protocol-level behaviour the services
depend on. A backing is free to implement `SessionRepository` however it likes underneath that.
"""
from __future__ import annotations

import threading
import time

import pytest

from requivo.core.contracts import schema_slot_ids, schema_slots
from requivo.core.errors import RevisionConflictError, SessionExistsError
from requivo.core.persistence import PersistedEngineOutput
from requivo.services.repository import SessionRepository

__all__ = ["SessionRepositoryConformance", "full_model"]


def full_model(**overrides) -> PersistedEngineOutput:
    """A minimal but schema-complete model -- built from the same public schema surface
    (`schema_slots`/`schema_slot_ids`) prompts and validation read, not from a private helper, so an
    out-of-repo subclass of this suite can call it too."""
    _, required = schema_slot_ids()
    order = [s["id"] for s in schema_slots()]
    model = {sid: {"completeness": 0, "confidence": "empty", "impact": "low", "value": ""}
             for sid in order if sid in required}
    model.update(overrides.pop("model", {}))
    payload = {"model": model, "questions": [], "summary": {"objective": "A conformance-suite model"}}
    payload.update(overrides)
    return PersistedEngineOutput(**payload)


class SessionRepositoryConformance:
    """Subclass and implement `make_repository`. Not collected on its own: pytest's default
    `python_classes = Test*` never matches this name, so importing this module adds no tests until
    something actually subclasses it under a `Test*` name."""

    def make_repository(self) -> SessionRepository:
        raise NotImplementedError("subclasses must return a fresh, empty SessionRepository")

    @pytest.fixture
    def repo(self) -> SessionRepository:
        return self.make_repository()

    # -- invariant 11: create claims the slug once ------------------------------------------------

    def test_create_claims_the_slug_once(self, repo: SessionRepository):
        repo.create("s", "a request")
        with pytest.raises(SessionExistsError):
            repo.create("s", "a different request")

    def test_create_does_not_refuse_a_different_slug(self, repo: SessionRepository):
        """Positive control for the assertion above -- `create` must still succeed in the ordinary
        case, so the refusal above is about the *collision*, not about `create` itself."""
        repo.create("a", "req")
        repo.create("b", "req")
        assert {"a", "b"} <= set(repo.list_slugs())

    # -- invariant 2: expected_revision is a real precondition -------------------------------------

    def test_expected_revision_refuses_a_stale_write(self, repo: SessionRepository):
        repo.create("s", "req")
        repo.save_revision("s", full_model(), expected_revision=0)  # -> revision 1
        with pytest.raises(RevisionConflictError):
            repo.save_revision("s", full_model(), expected_revision=0)  # stale: session is at 1 now

    def test_expected_revision_accepts_a_correct_precondition(self, repo: SessionRepository):
        """Positive control: a correct precondition must not be refused."""
        repo.create("s", "req")
        rev, meta = repo.save_revision("s", full_model(), expected_revision=0)
        assert rev == 1
        assert meta.current_revision == 1

    # -- invariant 9: the lock is mutually exclusive and re-entrant --------------------------------

    def test_lock_serialises_two_concurrent_holders(self, repo: SessionRepository):
        """Provable, not merely likely: each holder records the wall-clock interval it held the lock
        for, and the two intervals must not overlap. A lock that let both threads run their critical
        section at once would show up as an overlap here, not as a flaky failure that only shows up
        under load."""
        repo.create("s", "req")
        start = threading.Barrier(2)
        intervals: list[tuple[float, float]] = []
        guard = threading.Lock()

        def hold() -> None:
            start.wait()
            with repo.lock("s"):
                t0 = time.monotonic()
                time.sleep(0.05)
                t1 = time.monotonic()
            with guard:
                intervals.append((t0, t1))

        threads = [threading.Thread(target=hold) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(intervals) == 2, "a holder did not finish -- the lock may have deadlocked"
        (a0, a1), (b0, b1) = intervals
        assert a1 <= b0 or b1 <= a0, f"the two holders overlapped: {intervals} -- lock is not exclusive"

    def test_lock_is_reentrant_within_one_thread(self, repo: SessionRepository):
        """A deadlock here must not hang the whole run: the nested acquire happens on a daemon
        thread, and a `join(timeout=...)` that does not observe completion is the failure, reported
        as a normal assertion rather than a wedged test process."""
        repo.create("s", "req")
        finished = threading.Event()

        def nested() -> None:
            with repo.lock("s"):
                with repo.lock("s"):
                    finished.set()

        t = threading.Thread(target=nested, daemon=True)
        t.start()
        t.join(timeout=10)
        assert finished.is_set(), "lock() is not re-entrant within one thread (invariant 9)"

    # -- invariant 15's shape at the repository layer: known vs. unexaminable never overlap --------

    def test_known_slugs_and_unexaminable_entries_do_not_overlap(self, repo: SessionRepository):
        repo.create("a", "req")
        repo.create("b", "req")
        slugs = set(repo.list_slugs())
        assert {"a", "b"} <= slugs
        unexaminable_names = {e.name for e in repo.list_unexaminable()}
        assert unexaminable_names.isdisjoint(slugs), (
            "a name reported as a known session must never also be reported as unexaminable"
        )

    # -- load_artifact: None means absent, and only that -------------------------------------------

    def test_load_artifact_is_none_for_a_real_absence(self, repo: SessionRepository):
        repo.create("s", "req")
        assert repo.load_artifact("s", "prd.md") is None

    def test_load_artifact_returns_what_was_saved(self, repo: SessionRepository):
        """Positive control: the None above must be about absence, not about `load_artifact` itself
        being unable to return content. Saved against revision 1, not 0 -- an artifact is always
        generated from a real model, and the file backing refuses a `source_revision` the session
        has not reached yet (`ArtifactRevisionOutOfRangeError`), which is the correct, stricter
        answer a backing may give even though this suite does not itself assert that refusal."""
        repo.create("s", "req")
        repo.save_revision("s", full_model(), expected_revision=0)
        repo.save_artifact("s", "prd", "prd.md", "# Hello", source_revision=1)
        assert repo.load_artifact("s", "prd.md") == "# Hello"

    # -- invariants 8/10: an unrecognised top-level key survives the round trip ---------------------

    def test_an_unknown_top_level_key_survives_a_save_and_load_round_trip(self, repo: SessionRepository):
        """A field a *newer* Requivo wrote and this one does not know about must not be silently
        dropped by the backing's own (de)serialisation -- the same promise `PersistedEngineOutput`
        makes for the file backing, which any other backing storing the same payload owes too."""
        repo.create("s", "req")
        model = full_model(a_field_from_a_newer_requivo="kept")
        repo.save_revision("s", model, expected_revision=0)
        loaded = repo.load_model("s")
        assert getattr(loaded, "a_field_from_a_newer_requivo", None) == "kept"
