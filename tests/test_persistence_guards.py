"""Guards in `core/persistence.py` that lived one layer above the function that needed them.

Not one subject but two, and the file is worth reading as two. `#4` is the session store's atomic
slug claim: `migrate_legacy` must lose to a live session rather than overwrite it, which is
invariant 11 and touches no filename at all. Everything after it is the artifact-path chokepoint,
which began as two write paths whose *sibling* argument was unvalidated and has since grown a read
(#23), the end-of-line anchor a recorded name can carry (#40), and the two display-only joins that
printed a path they never opened (#36).

What the second group shares is the chokepoint rather than the direction of travel, and what both
groups share is the shape of the defect: a rule stated at the callers that happened to be careful,
in a store whose threat model is the caller that is not one of them. Offline, like the rest of the
session tests: a temp workspace via REQUIVO_WORKSPACE.

A third group joined at the foot of the file with the `test_engine.py` split (#72): the slug and the
model loader. `validate_slug` is a guard of the same family, and `_slug` is the shape it has to keep
accepting — a test of the guard and a test of what feeds it read better together than apart.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

# The one control in this repo that can actually move the ambient default encoding, measured rather
# than assumed. Borrowed rather than restated: two copies of a probe like this drift, and the copy
# that drifts is the one that silently stops firing.
from test_boundaries import _force_default_encoding

from requivo.cli import _build_parser, _wrote
from requivo.core import persistence as store
from requivo.core.contracts import _schema_order, schema_slot_ids
from requivo.core.dependencies import ARTIFACT_FILENAMES
from requivo.core.errors import RequivoError
from requivo.core.integrity import check_session
from requivo.core.persistence import _slug, load_model
from requivo.services.artifacts import ArtifactService
from requivo.services.repository import FileSessionRepository
from requivo.services.sessions import SessionService


def _slot(completeness=0, confidence="empty", impact="low", value=""):
    return {"completeness": completeness, "confidence": confidence, "impact": impact, "value": value}


def _full_model(**overrides) -> dict:
    _, required = schema_slot_ids()
    model = {sid: _slot() for sid in _schema_order() if sid in required}
    model.update(overrides)
    return {"model": model, "questions": [], "summary": {"objective": "A leave approval system"}}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))  # isolate the legacy root too
    return tmp_path


def _legacy(slug: str, marker: str) -> None:
    """A legacy out/<slug>/ session whose `problem` slot is identifiable."""
    d = store.legacy_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.json").write_text(json.dumps(
        _full_model(**{"problem": _slot(10, "inferred", "low", marker)})))


def _problem(slug: str, revision: int | None = None) -> str:
    out = (store.load_session_model(slug) if revision is None
           else store.load_revision_model(slug, revision))
    return out.model["problem"].value


# ── #4: migrate_legacy() must not overwrite a live session ──────────────────────


def test_migrating_onto_a_live_session_is_refused_rather_than_overwriting_it(workspace):
    """`migrate_legacy` checked only that the *legacy* model existed. Pointed at a slug a real session
    already occupies, it rewrote session.json at current_revision 0 and then wrote the legacy model
    over revisions/0001-model.json — and revisions/ is the only durable copy, so revision 1 was gone
    with no copy anywhere. The refusal belongs inside the function: the single in-repo caller guarded
    with a preceding existence check, which invariant 11 forbids precisely because it is not held
    across the write, and every other caller had no guard at all."""
    svc = SessionService()
    svc.create_session("A real request.", slug="dup")
    svc.update_model("dup", _full_model(**{"problem": _slot(80, "explicit", "high", "REAL v1")}))
    svc.update_model("dup", _full_model(**{"problem": _slot(90, "explicit", "high", "REAL v2")}))
    _legacy("dup", "LEGACY")

    with pytest.raises(RequivoError) as ei:
        store.migrate_legacy("dup")
    assert ei.value.code == "session_exists"

    # The live session is untouched: the revision count, both revision files, and the current model.
    meta = store.read_meta("dup")
    assert meta.current_revision == 2
    assert [r.revision for r in meta.revisions] == [1, 2]
    assert _problem("dup", 1) == "REAL v1"
    assert _problem("dup", 2) == "REAL v2"
    assert _problem("dup") == "REAL v2"
    # …and it still tells the truth about itself — the overwrite left an orphan_revision_file behind.
    assert [p.code for p in check_session("dup")] == []
    # The legacy originals are preserved on the refusal, as they are on the success path.
    assert (store.legacy_dir("dup") / "model.json").exists()


def test_migrating_onto_a_slug_claimed_at_revision_zero_is_refused_too(workspace):
    """The other half of the claim. A session created but never analysed holds no revision to destroy,
    yet it is still somebody's session — its id, provider and context cards were claimed by a
    `create_session` that won the slug. A refusal keyed on `current_revision > 0` would take the slug
    out from under it, which is the bug invariant 11 already describes for two concurrent creations."""
    SessionService().create_session("A real request.", slug="fresh", provider="claude-code")
    claimed = store.read_meta("fresh").session_id
    _legacy("fresh", "LEGACY")

    with pytest.raises(RequivoError) as ei:
        store.migrate_legacy("fresh")
    assert ei.value.code == "session_exists"
    meta = store.read_meta("fresh")
    assert meta.session_id == claimed
    assert meta.current_revision == 0
    assert meta.provider == "claude-code"


def test_migrating_a_free_slug_still_works(workspace):
    """The positive control for both refusals above: without it, a `migrate_legacy` that raised
    unconditionally would satisfy every assertion in this section."""
    _legacy("free", "LEGACY")
    legacy = store.legacy_dir("free")
    (legacy / "request.txt").write_text("Legacy request.")
    (legacy / "prd.md").write_text("# Legacy PRD\n")
    (legacy / "session.json").write_text(json.dumps(
        {"created_at": "2026-01-02T03:04:05Z", "provider": "anthropic", "model_name": "claude-x"}))

    meta = store.migrate_legacy("free")
    assert meta.current_revision == 1
    assert _problem("free", 1) == "LEGACY"
    assert _problem("free") == "LEGACY"
    assert meta.artifact_status["prd"].revision == 1
    assert (store.canonical_dir("free") / "artifacts" / "prd.md").read_text(encoding="utf-8") == "# Legacy PRD\n"
    assert (store.canonical_dir("free") / "request.md").read_text(encoding="utf-8") == "Legacy request."
    assert [p.code for p in check_session("free")] == []
    assert (legacy / "model.json").exists()   # the originals are preserved

    # Provenance recovered from the legacy session.json, not invented.
    assert meta.created_at == "2026-01-02T03:04:05Z"
    assert (meta.provider, meta.model_name) == ("anthropic", "claude-x")
    # The session id stays derived from the slug, so a migrated session has a stable identity.
    assert meta.session_id == store.read_meta("free").session_id


def test_the_bulk_migrate_command_skips_a_slug_that_is_already_taken(workspace, capsys):
    """The sweep reports `migrated` and `skipped_already_present`, so a refusal has to degrade that one
    row rather than abort the pass — the rule invariant 15 states for a listing, applied to a loop."""
    from requivo.deterministic import _cmd_session_migrate

    svc = SessionService()
    svc.create_session("A real request.", slug="aaa-taken")
    svc.update_model("aaa-taken", _full_model(**{"problem": _slot(80, "explicit", "high", "REAL")}))
    _legacy("aaa-taken", "LEGACY")
    _legacy("zzz-free", "LEGACY")

    _cmd_session_migrate(type("Args", (), {"json": True})(), None)
    out = json.loads(capsys.readouterr().out)
    assert out["migrated"] == ["zzz-free"]
    assert out["skipped_already_present"] == ["aaa-taken"]
    assert _problem("aaa-taken", 1) == "REAL"
    assert _problem("zzz-free", 1) == "LEGACY"


# ── #5: filename is a write target, so it is validated like its slug sibling ─────


# Every shape that is not a filename: traversal, a bare dot segment, both separators, an absolute
# path, a dot-prefixed name (which the staging convention and `list_session_slugs` reserve), and the
# empty string. A backslash is a separator on Windows and an ordinary character on POSIX, so it is
# refused on both rather than only on the one where it happens to escape.
ESCAPES = [
    "../../../../ESCAPED.md",
    "..",
    "sub/nested.md",
    "sub\\nested.md",
    "/etc/passwd",
    ".hidden.md",
    "",
]


def test_write_artifact_file_refuses_a_filename_that_is_not_a_filename(workspace):
    """`slug` is validated at this chokepoint so that "every surface inherits the same
    directory-traversal guard, not just FastAPI" — and the sibling parameter on the same mutating call
    had none. Nothing in-repo can reach it (every caller passes a literal or an ARTIFACT_FILENAMES
    lookup), so the test drives the function directly, which is exactly invariant 14's threat model:
    the external consumer calling the service, not the CLI being careful."""
    svc = SessionService()
    svc.create_session("Something.", slug="trav")
    svc.update_model("trav", _full_model())
    artifacts = store.canonical_dir("trav") / "artifacts"

    # Positive control first: an ordinary export name still lands where it should. Without it, a
    # `write_artifact_file` that refused everything would satisfy every assertion below.
    assert store.write_artifact_file("trav", "epic.github.json", "{}") == artifacts / "epic.github.json"

    for name in ESCAPES:
        with pytest.raises(RequivoError) as ei:
            store.write_artifact_file("trav", name, "pwned")
        assert ei.value.code == "invalid_filename", name
    assert not (workspace / "ESCAPED.md").exists()
    assert sorted(p.name for p in artifacts.iterdir()) == ["epic.github.json"]


def test_save_session_artifact_refuses_it_too_and_records_nothing(workspace):
    """The recorded filename is read back by `integrity.py` and by the artifact-show paths, so a
    poisoned value persists and is re-consumed. The refusal has to land before session.json is
    rewritten, not after."""
    svc = SessionService()
    svc.create_session("Something.", slug="trav2")
    svc.update_model("trav2", _full_model())

    st = store.save_session_artifact("trav2", "brief", "solution-assessment.md", "# A\n",
                                     source_revision=1)
    assert st.revision == 1

    for name in ESCAPES:
        with pytest.raises(RequivoError) as ei:
            store.save_session_artifact("trav2", "prd", name, "pwned", source_revision=1)
        assert ei.value.code == "invalid_filename", name

    assert not (workspace / "ESCAPED.md").exists()
    meta = store.read_meta("trav2")
    assert set(meta.artifact_status) == {"brief"}   # nothing recorded for the refused writes
    assert [p.code for p in check_session("trav2")] == []


def test_a_too_long_filename_is_refused_at_the_boundary(workspace):
    """Length is part of validity for a slug for a stated reason: the filesystem refuses an over-long
    name deep inside a write as a bare OSError instead of at the boundary. The same holds one argument
    over, and it is the one vector the traversal pattern alone does not cover."""
    svc = SessionService()
    svc.create_session("Something.", slug="trav3")
    svc.update_model("trav3", _full_model())
    with pytest.raises(RequivoError) as ei:
        store.write_artifact_file("trav3", "a" * 300 + ".md", "x")
    assert ei.value.code == "invalid_filename"


# ── #40 (adjacent): the end-of-line anchor is not the end of the string ──────────


def test_both_name_guards_anchor_at_the_end_of_the_string_not_before_a_newline(workspace):
    """Found while fixing #40, and outside its footprint — called out rather than slipped in.

    Both `_SLUG_RE` and `_FILENAME_RE` ended in the end-of-line anchor, which in Python matches at
    the end of the string **or just before a trailing newline**. So a slug and a filename each ending
    in one newline were returned unchanged: two guards whose whole job is to make a separator or a
    control character unrepresentable, admitting one. The end-of-string anchor is what both
    docstrings already claim.

    One character in each pattern, and the same defect class as #40 — untrusted text carrying a line
    break past a guard — which is why it is fixed here rather than filed.
    """
    # must fire: every name the store actually writes still passes both guards
    assert store.validate_slug("leave-approval") == "leave-approval"
    for name in sorted(ARTIFACT_FILENAMES.values()) + ["epic.github.json"]:
        assert store.validate_filename(name) == name

    # must not fire: a trailing newline is not a valid name, and never was meant to be
    for bad in ("ok\n", "ok\r", "leave-approval\n"):
        with pytest.raises(RequivoError) as ei:
            store.validate_slug(bad)
        assert ei.value.code == "invalid_slug", repr(bad)
    for bad in ("prd.md\n", "prd.md\r"):
        with pytest.raises(RequivoError) as ei:
            store.validate_filename(bad)
        assert ei.value.code == "invalid_filename", repr(bad)


def test_integrity_cannot_be_made_to_print_a_line_break_by_a_recorded_filename(workspace):
    """The reachable consequence of the anchor above, and why it earns a test rather than a note.

    `integrity.py` renders the recorded filename with `!r` on three of its four lines and **bare** on
    the fourth — the one that says `artifacts/<name> is missing`. That line is guarded: it sits on
    the `elif` behind `validate_filename`, so it is only reachable by a name the guard accepted,
    which is exactly what the end-of-line anchor allowed. One trailing newline is limited leverage,
    but a receipt line that splits in two is a line the program did not write.
    """
    svc = SessionService()
    svc.create_session("Something.", slug="anch")
    svc.update_model("anch", _full_model())
    store.save_session_artifact("anch", "prd", "prd.md", "# P\n", source_revision=1)

    # must fire: a genuinely missing artifact is still reported, on one line
    (store.canonical_dir("anch") / "artifacts" / "prd.md").unlink()
    problems = check_session("anch")
    assert [p.code for p in problems] == ["missing_artifact_file"]
    assert "\n" not in problems[0].message

    # must not fire: a recorded name carrying a newline cannot split that line
    p = store.canonical_dir("anch") / "session.json"
    meta = json.loads(p.read_text(encoding="utf-8"))
    meta["artifact_status"]["prd"]["filename"] = "prd.md\n"
    p.write_text(json.dumps(meta), encoding="utf-8")
    reported = check_session("anch")
    assert reported, "must fire: the tampered name is still reported"
    for problem in reported:
        assert "\n" not in problem.message, problem.code


# ── #23: the same filename is a read target, and a refusal is not an absence ─────


def _session(slug: str) -> FileSessionRepository:
    """A session at revision 1, plus the repository an external consumer would hold."""
    svc = SessionService()
    svc.create_session("Something.", slug=slug)
    svc.update_model(slug, _full_model())
    return FileSessionRepository()


def test_load_artifact_refuses_a_traversal_rather_than_disclosing_the_file(workspace):
    """The read-side sibling of the two write paths above, and a different question: the write fix
    answers what this code may *create*, a read traversal answers what it may *disclose*.

    `FileSessionRepository.load_artifact` re-joined `canonical_dir(slug) / "artifacts" / filename`
    inline rather than going through `artifact_path`, one layer above the chokepoint — which is
    exactly why the sweep that closed the writes in #21 did not reach it.

    Driven through the repository directly, which is invariant 14's threat model verbatim: every
    in-repo caller arrives via `ArtifactService.show` with an `ARTIFACT_FILENAMES` lookup, and
    `requivo-cloud` reuses Core as a dependency and is the consumer that does not."""
    repo = _session("read-trav")

    # ESCAPES[0] resolves four levels up from artifacts/, i.e. to <workspace>. Put a real, readable
    # file exactly there: without it, a `load_artifact` that merely failed to *find* anything would
    # satisfy every assertion below, and the test would prove nothing about refusal.
    (workspace / "ESCAPED.md").write_text("TOP SECRET", encoding="utf-8")
    assert (workspace / "ESCAPED.md").read_text(encoding="utf-8") == "TOP SECRET"

    # Positive control: a legitimate ARTIFACT_FILENAMES value still loads, byte for byte.
    store.save_session_artifact("read-trav", "brief", ARTIFACT_FILENAMES["brief"], "# A brief\n",
                                source_revision=1)
    assert repo.load_artifact("read-trav", ARTIFACT_FILENAMES["brief"]) == "# A brief\n"

    # The over-long name rides the same guard here as on the write side: it is the one vector the
    # traversal shapes do not cover, and a read of it fails as a bare OSError without the boundary.
    for name in ESCAPES + ["a" * 300 + ".md"]:
        with pytest.raises(RequivoError) as ei:
            repo.load_artifact("read-trav", name)
        assert ei.value.code == "invalid_filename", name
        # The refusal has to name what it refused, or a caller holding several names cannot tell
        # which one was rejected. Read off `details` rather than the message: the length branch of
        # `validate_filename` states the count and not the name, and truncates the one it records.
        # Asserting instead that the secret is absent from the message would be unfalsifiable — the
        # raise happens before any read, so no content is ever in scope for the message to leak.
        assert name.startswith(ei.value.details["filename"]), name


def test_a_refused_read_raises_where_a_missing_artifact_returns_none(workspace):
    """The judgment this issue turned on. `artifact_path()` raises and `load_artifact` returns None,
    so routing one through the other forces a choice, and the tempting one is the quiet answer:
    returning None for a rejected traversal too would make it indistinguishable from an artifact
    nobody has generated yet. That is not hypothetical — reproducing the defect, a traversal that
    resolved to no file returned None exactly as an ungenerated artifact does, and only the depth of
    the `..` chain separated disclosure from a plausible-looking absence.

    So all three states are asserted together, because each only means anything against the other
    two: content for a saved artifact, None for a legitimate name with no file behind it, and a
    raise for a name that is not a filename."""
    repo = _session("read-3state")
    store.save_session_artifact("read-3state", "brief", ARTIFACT_FILENAMES["brief"], "# A brief\n",
                                source_revision=1)

    assert repo.load_artifact("read-3state", ARTIFACT_FILENAMES["brief"]) == "# A brief\n"
    assert repo.load_artifact("read-3state", ARTIFACT_FILENAMES["prd"]) is None   # never generated

    with pytest.raises(RequivoError) as ei:
        repo.load_artifact("read-3state", "../../../../ESCAPED.md")
    assert ei.value.code == "invalid_filename"


def test_core_owns_the_read_guard_so_the_next_reader_cannot_forget_it(workspace):
    """#21 put the write guard at `artifact_path()` in Core rather than at its callers, for the
    reason `_child_of` gives: a rule applied per-caller is a rule the next caller forgets. The read
    side is that sentence's own proof, so the fix goes to Core too and this drives Core directly
    rather than through the adapter — a guard that lived only in `FileSessionRepository` would leave
    Core with a write-only chokepoint and the next reader re-joining the path a third time."""
    _session("read-core")
    (workspace / "ESCAPED.md").write_text("TOP SECRET", encoding="utf-8")

    store.write_artifact_file("read-core", "epic.github.json", "{}")
    assert store.read_artifact_file("read-core", "epic.github.json") == "{}"
    assert store.read_artifact_file("read-core", "epic.json") is None   # a real name, no file

    for name in ESCAPES:
        with pytest.raises(RequivoError) as ei:
            store.read_artifact_file("read-core", name)
        assert ei.value.code == "invalid_filename", name


def test_an_artifact_round_trips_non_ascii_content(workspace, monkeypatch):
    """The other half of the line this change rewrites. `_atomic_write` passes `encoding="utf-8"`
    explicitly and the read beside it passed none, so it decoded with the *locale's* — `LC_ALL=C`, or
    a DBCS Windows shell, and a generated artifact dies on its first em-dash. Every artifact this
    engine writes is full of them.

    The plain round trip below is only a regression pin: on a UTF-8 locale it passes with or without
    the explicit `encoding=`, which is to say the control cannot fire. So the fallback is forced and
    *measured* first, reusing `test_boundaries`' helper rather than restating it — the same shape,
    and the same loud skip where the force does not take, because a control that cannot fail is worse
    than no control."""
    repo = _session("read-utf8")
    body = "# Brief\n\nAn em-dash — a café — and a curly quote: “ready”.\n"
    store.save_session_artifact("read-utf8", "brief", ARTIFACT_FILENAMES["brief"], body,
                                source_revision=1)
    assert repo.load_artifact("read-utf8", ARTIFACT_FILENAMES["brief"]) == body

    # Everything above is set up under the ambient encoding; only the read is forced, so a session.json
    # or model_schema.json read cannot fail for reasons that have nothing to do with the artifact.
    with monkeypatch.context() as m:
        if not _force_default_encoding(m, workspace, "ascii"):
            pytest.skip(
                "the ambient default encoding could not be forced on this interpreter (CPython "
                "dropped _bootlocale in 3.10 and resolves the locale encoding in C), so this control "
                "cannot fire here. UNTESTED ON THIS INTERPRETER: that read_artifact_file passes an "
                "explicit encoding rather than taking the locale's. The 3.9 leg of the CI matrix "
                "does test it."
            )
        p = store.artifact_path("read-utf8", ARTIFACT_FILENAMES["brief"])
        with pytest.raises(UnicodeDecodeError):
            # Deliberately bare: this read IS the thing under test, performing the defect so the
            # assertion can catch it. Passing `encoding=` here would bypass the forced locale
            # entirely and the `raises` could never fire -- which is exactly what a mechanical sweep
            # did to it, invisibly on 3.10+ (where the force does not take and the test skips) and
            # fatally on the 3.9 leg. Registered in `_LOCALE_DEFAULT_BY_DESIGN` in test_encoding.py.
            p.read_text()   # what the repository's own line did, meeting the locale it would meet
        assert repo.load_artifact("read-utf8", ARTIFACT_FILENAMES["brief"]) == body


# ── #22: session_lock() must not materialise the session it guards ──────────────


def _ghost_locking_calls() -> dict:
    """Every route that takes the session lock on a slug the caller has not proven exists.

    Named as a table rather than tested one by one because the defect was never in any of them: it
    was in the lock, and each of these is only a way to reach it. A route added later that locks
    before it reads belongs here, not in a test of its own."""
    from requivo.services.artifacts import ArtifactService

    def take_the_lock(slug):
        with store.session_lock(slug):
            pass

    # The keys become slugs, so they are hyphenated: an underscore is not a legal slug character and
    # `validate_slug` would refuse the name before the lock could be reached at all.
    return {
        "session-lock": take_the_lock,
        "save-revision": lambda slug: store.save_revision(slug, _engine_output()),
        "save-session-artifact": lambda slug: store.save_session_artifact(
            slug, "brief", ARTIFACT_FILENAMES["brief"], "# Brief\n", source_revision=1),
        "mark-stale": lambda slug: ArtifactService(FileSessionRepository()).mark_stale(slug, ["problem"]),
    }


def _engine_output(**overrides):
    from requivo.core.contracts import EngineOutput
    return EngineOutput.model_validate(_full_model(**overrides))


def test_a_lock_on_a_slug_with_no_session_leaves_no_trace(workspace):
    """The lock created `canonical_dir(slug)` before opening `.lock` inside it, so taking it on a slug
    with no session left a directory behind holding nothing else. That directory is invisible to
    `list_session_slugs` (no session.json) and non-empty, so `create_session`'s rename — the *only*
    claim on a slug under invariant 11 — lost to a session nobody had created, and the user was told
    one already existed that neither they nor the tool could see.

    The property pinned here is the general one, not the symptom: a lock that fails, on a slug that
    has no session, leaves the store exactly as it found it. `real` is the positive control — without
    a session the same fixture *does* put on disk, every assertion below is also satisfied by a
    workspace pointed somewhere nothing is ever written."""
    SessionService().create_session("A real request.", slug="real")
    before = sorted(p.name for p in store.session_root().iterdir())
    assert before == ["real"], "the control session is not where this test is looking"

    for name, call in _ghost_locking_calls().items():
        with pytest.raises(RequivoError) as ei:
            call(f"ghost-{name}")
        assert ei.value.code == "session_not_found", name
        assert not store.canonical_dir(f"ghost-{name}").exists(), name

    assert sorted(p.name for p in store.session_root().iterdir()) == before
    assert store.list_session_slugs() == ["real"]


def test_a_session_deleted_between_the_check_and_the_open_is_refused_not_recreated(workspace,
                                                                                    monkeypatch):
    """The race the existence check cannot close, and the reason the fix is not that check alone. The
    session is gone by the time `os.open` runs, and the old code would simply have made the directory
    again. `session_exists` is forced rather than raced, so the arm is *executed* on every leg of the
    matrix instead of being asserted about: the errno-to-exception mapping for opening a file whose
    parent directory is missing is the platform-specific part of this, and a reasoned claim about
    Windows is worth less than one CI can falsify."""
    monkeypatch.setattr(store, "session_exists", lambda slug: True)

    with pytest.raises(RequivoError) as ei:
        with store.session_lock("vanished"):
            pass                                    # pragma: no cover - the lock must not be granted
    assert ei.value.code == "session_not_found"
    assert not store.canonical_dir("vanished").exists()
    root = store.session_root()
    assert not root.exists() or list(root.iterdir()) == []


def test_a_slug_a_failed_lock_touched_can_still_be_created(workspace):
    """The reproduction from the issue, end to end. `list_session_slugs` and `create_session` have to
    agree about whether a slug is taken — the refusal was false precisely because they did not."""
    with pytest.raises(RequivoError):
        store.save_session_artifact("later", "brief", ARTIFACT_FILENAMES["brief"], "x", source_revision=1)

    assert "later" not in store.list_session_slugs()
    meta = store.create_session("later", "A request that arrives afterwards.")
    assert meta.current_revision == 0
    assert store.list_session_slugs() == ["later"]
    assert store.session_request("later") == "A request that arrives afterwards."


def test_a_migration_onto_such_a_slug_is_performed_not_reported_as_skipped(workspace, capsys):
    """Why this is more than a misleading message. `migrate_legacy` claims its slug through
    `create_session`, and the bulk sweep turns `SessionExistsError` into `skipped_already_present` —
    a row that reads as a decision. A ghost directory made the sweep report a session it had refused
    to migrate as one that was already there, and the legacy work silently never landed."""
    from requivo.deterministic import _cmd_session_migrate

    _legacy("stale-lock", "LEGACY")
    with pytest.raises(RequivoError):
        store.save_revision("stale-lock", _engine_output())

    _cmd_session_migrate(type("Args", (), {"json": True})(), None)
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["migrated"] == ["stale-lock"]
    assert receipt["skipped_already_present"] == []
    assert _problem("stale-lock") == "LEGACY"


def test_the_lock_still_guards_a_session_that_exists(workspace):
    """The other direction, and the one a fix here can break silently. The lock's job is the compound
    mutations on sessions that *do* exist: `save_revision` and `save_session_artifact` write files
    under a session directory while holding it, the service layer nests it around several core calls,
    and `.lock` still belongs inside the session it locks (`session export` excludes it by name)."""
    svc = SessionService()
    svc.create_session("A real request.", slug="live")
    svc.update_model("live", _full_model(**{"problem": _slot(80, "explicit", "high", "REAL")}))

    lock_file = store.canonical_dir("live") / ".lock"
    assert lock_file.exists(), "a writer that holds the lock leaves the lockfile in the session"

    with store.session_lock("live"):
        # Re-entrant within the thread: the service takes it around a whole update and every core
        # call inside takes it again. A guard that refused the second acquisition would deadlock.
        store.save_session_artifact("live", "brief", ARTIFACT_FILENAMES["brief"], "# Brief\n",
                                    source_revision=1)
        rev, meta = store.save_revision(
            "live", _engine_output(**{"problem": _slot(90, "explicit", "high", "REAL v2")}))

    assert (rev, meta.current_revision) == (2, 2)
    assert _problem("live") == "REAL v2"
    assert store.read_meta("live").artifact_status["brief"].revision == 1
    assert [p.code for p in check_session("live")] == []
# ── #36: a path that is only printed is still a path this code built ─────────────
#
# `deterministic.py`'s `artifact save` and `cli.py`'s `_wrote` each re-joined
# `canonical_dir(slug) / "artifacts" / <recorded filename>` inline, so the chokepoint the two
# writes (#5) and the read (#23) were routed through was closed in three places and open in two.
# Neither of the two opens the file, which is exactly how they survived both sweeps — "it only
# prints it" reads as harmless. It is a different harm, not an absent one: a printed path is a
# disclosure in the plainest form there is, and the join is the same join.
#
# Nothing in-repo reaches either site with a name that is not an `ARTIFACT_FILENAMES` value, so both
# tests hand the site what a `SessionRepository` that is not this file backing would hand it. That is
# invariant 14's threat model verbatim, and the same reason
# `test_write_artifact_file_refuses_a_filename_that_is_not_a_filename` above drives Core directly.
#
# `session import` is NOT that route, and the difference is worth stating because the invariant's own
# sentence is about `context_cards` and reads as though it covered this field too. It does not:
# `check_session_dir` pins each recorded filename to its `ARTIFACT_FILENAMES` value and to
# containment, and import refuses the whole archive on either. A `session.json` edited in place is a
# live route — nothing re-validates the field when `read_meta` loads it back — but that is a
# different door, and naming the shut one as the open one is how a docstring stops being evidence.


def _recorded(filename: str) -> store.ArtifactStatus:
    """The `ArtifactStatus` a display site is handed — `filename` is an unconstrained `str` on it."""
    return store.ArtifactStatus(revision=1, filename=filename, updated_at="2026-08-19T00:00:00Z")


def _run_command(argv: list) -> str:
    """Run one deterministic verb through the real parser and command function, capturing stdout.

    Deliberately not through `app()`: its `except RequivoError` turns a refusal into a printed
    envelope and `SystemExit`, and what this test needs to see is which of the two the site produced.
    """
    ns = _build_parser().parse_args(argv)
    buf = io.StringIO()
    with redirect_stdout(buf):
        ns.func(ns, None)
    return buf.getvalue()


def test_artifact_save_reports_where_it_wrote_through_the_chokepoint(workspace, tmp_path, monkeypatch):
    """`artifact save`'s human branch printed the join itself. Routing it through `artifact_path`
    costs nothing on the ordinary path and refuses a name that is not a filename.

    The absence/refusal distinction #23 turned on survives here because there is nothing to confuse
    it with: this line runs immediately after the write, states where the content went, and never
    asks whether the file is there. `artifact_path` does not stat either, so a session with nothing
    generated is not newly an error — it never reached this line in the first place.
    """
    _session("say-where")
    (workspace / "ESCAPED.md").write_text("TOP SECRET", encoding="utf-8")
    doc = tmp_path / "brief.md"
    doc.write_text("# A brief\n", encoding="utf-8")
    argv = ["artifact", "save", "say-where", "--type", "brief", "--file", str(doc), "--revision", "1"]

    # Positive control first, and it is the load-bearing half: the ordinary save must still name the
    # real file under artifacts/. A site that raised on everything, or printed nothing at all, would
    # satisfy the refusal assertions below without ever having said anything true.
    out = _run_command(argv)
    assert str(store.artifact_path("say-where", ARTIFACT_FILENAMES["brief"])) in out

    # And the refusal. `ArtifactService.save` is the layer that hands this line a filename; a
    # repository that is not this repo's file backing is what can hand it one of these.
    for name in ESCAPES:
        monkeypatch.setattr(ArtifactService, "save", lambda *a, _n=name, **k: _recorded(_n))
        with pytest.raises(RequivoError) as ei:
            _run_command(argv)
        assert ei.value.code == "invalid_filename", name


def test_a_generated_document_reports_its_path_through_the_chokepoint(workspace):
    """`cli.py::_wrote` is the same join, and it is the one of the two that is shared: five generator
    verbs say where their document went through it, so one guard here covers all five.

    Driven directly rather than through a generator, for the reason the write-side test gives — every
    in-repo caller arrives with an `ARTIFACT_FILENAMES` value, and the caller that does not is the
    external consumer holding the services. `result` is a stand-in because `_wrote` reads exactly one
    field off it; a real generation result would only make the fixture longer.
    """
    _session("wrote-where")
    (workspace / "ESCAPED.md").write_text("TOP SECRET", encoding="utf-8")

    # Positive control: an ordinary generated document still names its real file.
    out = io.StringIO()
    with redirect_stdout(out):
        _wrote("wrote-where", SimpleNamespace(status=_recorded(ARTIFACT_FILENAMES["prd"])), "PRD")
    assert str(store.artifact_path("wrote-where", ARTIFACT_FILENAMES["prd"])) in out.getvalue()

    for name in ESCAPES:
        with pytest.raises(RequivoError) as ei:
            _wrote("wrote-where", SimpleNamespace(status=_recorded(name)), "PRD")
        assert ei.value.code == "invalid_filename", name


def test_neither_display_site_can_be_made_to_print_a_path_outside_the_session(workspace, tmp_path,
                                                                             monkeypatch):
    """The consequence the two tests above are guards for, asserted as the thing a reader cares about
    rather than as an exception type: whatever these lines print stays under this session's
    `artifacts/`.

    **Both** sites, because the name says both. Each of the two above pins one, and a test whose name
    claims a pair while driving one of them is the overclaim this file exists to catch, one layer
    down in its own fixture. The shapes here are the ones the shared `ESCAPES` list does not carry.

    A backslash separator and a drive-letter path are in the list on every platform rather than
    behind a platform branch. On POSIX a backslash is an ordinary character, so there this asserts
    that the *name* is refused; on Windows it additionally asserts that the path could not have
    escaped — and the leg most likely to be handed one is the leg that could not have said so if the
    list were POSIX-only. The over-long name rides the same guard: it is the one vector the traversal
    shapes do not cover, and it fails as a bare OSError without the boundary. The uppercase name is
    here because `_FILENAME_RE` is deliberately lowercase-only, which is a refusal a reader is more
    likely to mistake for a bug than for the guard it is.
    """
    _session("stay-inside")
    artifacts = store.canonical_dir("stay-inside") / "artifacts"
    doc = tmp_path / "brief.md"
    doc.write_text("# A brief\n", encoding="utf-8")
    argv = ["artifact", "save", "stay-inside", "--type", "brief", "--file", str(doc), "--revision", "1"]

    for name in [r"..\..\ESCAPED.md", r"c:\windows\win.ini", "a" * 300 + ".md", "PRD.MD"]:
        with pytest.raises(RequivoError):
            _wrote("stay-inside", SimpleNamespace(status=_recorded(name)), "PRD")
        with monkeypatch.context() as m:
            m.setattr(ArtifactService, "save", lambda *a, _n=name, **k: _recorded(_n))
            with pytest.raises(RequivoError):
                _run_command(argv)

    # must fire, on both sites: each still prints, and prints inside artifacts/, for a real name.
    # Without this the block above is satisfied by two sites that refuse everything.
    out = io.StringIO()
    with redirect_stdout(out):
        _wrote("stay-inside", SimpleNamespace(status=_recorded(ARTIFACT_FILENAMES["epic"])), "epic")
    assert str(artifacts / ARTIFACT_FILENAMES["epic"]) in out.getvalue()
    assert str(artifacts / ARTIFACT_FILENAMES["brief"]) in _run_command(argv)


# ── the slug, and the loader that reads a model back ─────────────────────────


def test_slug_is_first_five_word_tokens():
    assert _slug("We'd like an invoice created automatically when signed") == "we-d-like-an-invoice"
    assert _slug("!!!") == "discovery"


def test_invalid_slug_is_rejected_before_touching_the_filesystem():
    # The traversal guard: an explicit slug that could escape the session root must raise in Core,
    # never build a path. Covers the separator, the dot segment, an absolute root, and the empty string.
    from requivo.core.errors import InvalidSlugError
    from requivo.core.persistence import canonical_dir, validate_slug
    for bad in ("../../escaped", "a/b", "..", ".", "", "/abs", "Upper", "under_score"):
        with pytest.raises(InvalidSlugError):
            validate_slug(bad)
        with pytest.raises(InvalidSlugError):
            canonical_dir(bad)
    assert validate_slug("leave-approval") == "leave-approval"   # the shape _slug() always emits


def test_load_model_rejects_invalid_model(tmp_path):
    bad = tmp_path / "model.json"
    bad.write_text(json.dumps({"questions": [], "summary": {}}))  # required `model` missing
    with pytest.raises(ValidationError):
        load_model(bad)
