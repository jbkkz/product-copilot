"""Two guards that lived one layer above the function that needed them.

Both are `core/persistence.py` write paths whose *sibling* argument was already validated, and both
were reachable by any caller that is not the one in-repo caller that happened to be careful. Offline,
like the rest of the session tests: a temp workspace via REQUIVO_WORKSPACE.
"""
from __future__ import annotations

import json

import pytest

# The one control in this repo that can actually move the ambient default encoding, measured rather
# than assumed. Borrowed rather than restated: two copies of a probe like this drift, and the copy
# that drifts is the one that silently stops firing.
from test_boundaries import _force_default_encoding

from requivo.core import persistence as store
from requivo.core.contracts import _schema_order, schema_slot_ids
from requivo.core.dependencies import ARTIFACT_FILENAMES
from requivo.core.errors import RequivoError
from requivo.core.integrity import check_session
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
    assert (store.canonical_dir("free") / "artifacts" / "prd.md").read_text() == "# Legacy PRD\n"
    assert (store.canonical_dir("free") / "request.md").read_text() == "Legacy request."
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
        assert "TOP SECRET" not in ei.value.message, name


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
            p.read_text()   # what the repository's own line did, meeting the locale it would meet
        assert repo.load_artifact("read-utf8", ARTIFACT_FILENAMES["brief"]) == body
