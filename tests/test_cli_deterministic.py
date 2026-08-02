"""End-to-end tests of the deterministic CLI surface — doctor / session / model / artifact.

Every command here must run with no LLM and no API key. Output is captured through `app()` (the real
entry point) against a temp workspace; a `--json` variant is asserted where the spec fixes a machine
format, so Claude Code can rely on it.
"""
from __future__ import annotations

import io
import json
import zipfile
from contextlib import redirect_stdout

import pytest

from requivo.cli import _build_parser, app
from requivo.core import persistence as store
from requivo.core.contracts import _schema_order, schema_slot_ids


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path


def _slot(c=0, cf="empty", im="low", v=""):
    return {"completeness": c, "confidence": cf, "impact": im, "value": v}


def _full_model(**overrides):
    _, required = schema_slot_ids()
    model = {sid: _slot() for sid in _schema_order() if sid in required}
    model.update(overrides)
    return {"model": model, "questions": [], "summary": {}}


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        app(argv, client=None)  # client=None → any accidental API use would blow up
    return buf.getvalue()


def _run_json(argv):
    return json.loads(_run(argv))


# ── doctor ──────────────────────────────────────────────────────────────────────


def test_doctor_runs_without_api_key(workspace, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = _run_json(["doctor", "--json"])
    assert r["schema"]["ok"] and r["schema"]["slots"] > 0
    # Missing key / SDK must never be reported as a hard failure.
    assert r["provider_anthropic"]["api_key_present"] is False
    assert "sessions" in r["workspace"]


# ── the acceptance scenario ─────────────────────────────────────────────────────


def test_session_init_creates_a_session(workspace):
    r = _run_json(["session", "init", "Build a leave approval system.", "--slug", "leave", "--json"])
    assert r["slug"] == "leave"
    assert store.session_exists("leave")
    assert store.read_meta("leave").current_revision == 0


def test_model_validate_ok_and_invalid_exit(workspace, tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_full_model()))
    assert _run_json(["model", "validate", str(good), "--json"])["status"] == "valid"

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"model": {"nope": _slot()}, "summary": {}}))
    with pytest.raises(SystemExit) as e:
        _run(["model", "validate", str(bad), "--json"])
    assert e.value.code == 1


def test_model_apply_and_status_and_artifact_flow(workspace, tmp_path):
    _run(["session", "init", "Reconcile event check-ins.", "--slug", "event"])
    proposal = tmp_path / "p.json"
    proposal.write_text(json.dumps(_full_model(**{"workflow": _slot(70, "inferred", "high", "scan")})))

    applied = _run_json(["model", "apply", "event", str(proposal), "--json"])
    assert applied["status"] == "applied" and applied["revision"] == 1
    assert "workflow" in applied["readiness"]["blocking_slots"]  # inferred high-impact blocks

    status = _run_json(["status", "event", "--json"])
    assert status["revision"] == 1 and status["readiness"]["ready"] is False

    brief = tmp_path / "brief.md"
    brief.write_text("# Assessment\n")
    _run(["artifact", "save", "event", "--type", "brief", "--file", str(brief)])
    listed = _run_json(["artifact", "list", "event", "--json"])
    assert listed["brief"]["revision"] == 1 and listed["brief"]["stale"] is False


def test_session_show_reads_freshness_from_the_dependency_graph_not_the_revision(workspace, tmp_path):
    # `session show` used to call an artifact stale whenever the session had moved past its source
    # revision — which contradicted `artifact list` and the status JSON in the same binary, and made
    # every artifact look out of date after any unrelated change. The stale flag is the whole rule.
    _run(["session", "init", "X.", "--slug", "s"])
    proposal = tmp_path / "m.json"
    proposal.write_text(json.dumps(_full_model()))
    _run(["model", "apply", "s", str(proposal)])
    prd = tmp_path / "prd.md"
    prd.write_text("# PRD\n")
    _run(["artifact", "save", "s", "--type", "prd", "--file", str(prd)])   # generated at revision 1

    # Move the session on via a slot the PRD does not consume: revision 2, PRD inputs untouched.
    proposal.write_text(json.dumps(_full_model(
        **{"current_process": _slot(80, "explicit", "high", "as-is described")})))
    _run(["model", "apply", "s", str(proposal)])

    out = _run(["session", "show", "s"])
    assert "revision 2" in out and "rev 1" in out   # provenance still says where it came from…
    assert "STALE" not in out                       # …but it is not stale, and both views agree
    assert _run_json(["artifact", "list", "s", "--json"])["prd"]["stale"] is False


def test_model_validate_has_no_flag_it_does_not_honour():
    # `--session` was declared and read by nothing. A flag that parses and changes nothing is worse
    # than a missing one: the caller believes a check ran. `model diff` is the real answer.
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["model", "validate", "p.json", "--session", "s"])
    assert _build_parser().parse_args(["model", "diff", "s", "p.json"]).func.__name__ == "_cmd_model_diff"


def test_apply_invalid_proposal_emits_error_envelope(workspace, tmp_path):
    _run(["session", "init", "X.", "--slug", "s"])
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"model": {"ghost": _slot()}, "summary": {}}))
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit):
        app(["model", "apply", "s", str(bad), "--json"], client=None)
    env = json.loads(buf.getvalue())
    assert env["code"] == "unknown_slot" and env["details"]["slots"] == ["ghost"]


def test_model_diff_does_not_write(workspace, tmp_path):
    _run(["session", "init", "X.", "--slug", "s"])
    p = tmp_path / "p.json"
    p.write_text(json.dumps(_full_model()))
    _run(["model", "apply", "s", str(p)])
    before = store.read_meta("s").current_revision
    r = _run_json(["model", "diff", "s", str(p), "--json"])
    assert r["status"] == "planned"
    assert store.read_meta("s").current_revision == before


def test_session_list_and_show(workspace, tmp_path):
    _run(["session", "init", "First.", "--slug", "one"])
    p = tmp_path / "p.json"
    p.write_text(json.dumps(_full_model()))
    _run(["model", "apply", "one", str(p)])
    listing = _run_json(["session", "list", "--json"])
    assert any(s["slug"] == "one" and s["revision"] == 1 for s in listing)
    shown = _run_json(["session", "show", "one", "--json"])
    assert shown["slug"] == "one" and shown["format_version"] == 1


def test_session_migrate_moves_legacy_sessions(workspace, tmp_path):
    # Seed a legacy out/<slug>/ session, then bulk-migrate it into the canonical store.
    legacy = store.legacy_dir("legacy-one")
    legacy.mkdir(parents=True)
    legacy.joinpath("model.json").write_text(json.dumps(_full_model()))
    legacy.joinpath("request.txt").write_text("Legacy request.")

    r = _run_json(["session", "migrate", "--json"])
    assert "legacy-one" in r["migrated"]
    assert store.session_exists("legacy-one")
    assert store.read_meta("legacy-one").current_revision == 1
    assert legacy.joinpath("model.json").exists()  # originals preserved


def test_new_verbs_are_bound_in_the_parser():
    cases = [
        (["doctor"], "_cmd_doctor"),
        (["session", "init", "r"], "_cmd_session_init"),
        (["session", "migrate"], "_cmd_session_migrate"),
        (["model", "apply", "s", "p.json"], "_cmd_model_apply"),
        (["model", "validate", "p.json"], "_cmd_model_validate"),
        (["artifact", "save", "s", "--type", "prd", "--file", "f"], "_cmd_artifact_save"),
    ]
    for argv, fname in cases:
        assert _build_parser().parse_args(argv).func.__name__ == fname


# ── the revision contract on the CLI surface ────────────────────────────────────
# These are the primitives the Claude Code skills drive, so their JSON shape is part of the contract:
# a skill reads `revision`, reasons, then hands it back on apply and on save.


def test_session_init_json_reports_the_revision(workspace, tmp_path):
    r = _run_json(["session", "init", "Build a leave approval system.", "--json"])
    assert r["revision"] == 0  # a fresh session has no model yet

    (tmp_path / "p.json").write_text(json.dumps(_full_model()))
    _run(["model", "apply", r["slug"], str(tmp_path / "p.json"), "--json"])
    # `init` is idempotent: re-running it on the same request returns the session as it now stands,
    # so a caller about to apply learns it is no longer at revision 0.
    again = _run_json(["session", "init", "Build a leave approval system.", "--json"])
    assert again["slug"] == r["slug"]
    assert again["revision"] == 1


def test_model_apply_honours_the_expected_revision_precondition(workspace, tmp_path):
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    p = tmp_path / "p.json"
    p.write_text(json.dumps(_full_model()))
    _run(["model", "apply", "s", str(p), "--expected-revision", "0", "--json"])  # fresh: asserts 0

    p2 = tmp_path / "p2.json"
    p2.write_text(json.dumps(_full_model(**{"workflow": _slot(80, "explicit", "high", "new")})))
    _run(["model", "apply", "s", str(p2), "--expected-revision", "1", "--json"])

    # Applying again from the same base is refused with a structured, actionable error.
    with pytest.raises(SystemExit) as exc:
        _run(["model", "apply", "s", str(p2), "--expected-revision", "1", "--json"])
    assert exc.value.code != 0


def test_artifact_save_reports_staleness_at_save_time(workspace, tmp_path):
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    p = tmp_path / "p.json"
    p.write_text(json.dumps(_full_model()))
    _run(["model", "apply", "s", str(p), "--json"])                       # revision 1
    p2 = tmp_path / "p2.json"
    p2.write_text(json.dumps(_full_model(**{"workflow": _slot(80, "explicit", "high", "new")})))
    _run(["model", "apply", "s", str(p2), "--json"])                      # revision 2

    doc = tmp_path / "prd.md"
    doc.write_text("# PRD\n")
    # Reasoned from revision 1, saved once the session is at 2: the answer is knowable, so it is given
    # here rather than only on a later `artifact list`.
    r = _run_json(["artifact", "save", "s", "--type", "prd", "--file", str(doc),
                   "--revision", "1", "--json"])
    assert r["revision"] == 1 and r["stale"] is True
    assert _run_json(["artifact", "list", "s", "--json"])["prd"]["stale"] is True

    fresh = _run_json(["artifact", "save", "s", "--type", "prd", "--file", str(doc), "--json"])
    assert fresh["revision"] == 2 and fresh["stale"] is False


# ── documents on stdin ──────────────────────────────────────────────────────────
# `-` exists so a caller holding content does not have to invent a file for it. The Claude Code skills
# used to write `/tmp/requivo:prd.md` — a shared path, illegal on Windows, needing `rm` to clean up.


def _run_stdin(argv, text, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    return _run(argv)


def test_a_proposal_can_be_applied_from_stdin(workspace, monkeypatch):
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    proposal = json.dumps(_full_model())
    r = json.loads(_run_stdin(["model", "validate", "-", "--json"], proposal, monkeypatch))
    assert r["status"] == "valid"
    applied = json.loads(_run_stdin(["model", "apply", "s", "-", "--expected-revision", "0", "--json"],
                                    proposal, monkeypatch))
    assert applied["revision"] == 1


def test_an_artifact_can_be_saved_from_stdin(workspace, monkeypatch):
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _run_stdin(["model", "apply", "s", "-", "--json"], json.dumps(_full_model()), monkeypatch)
    r = json.loads(_run_stdin(["artifact", "save", "s", "--type", "prd", "--file", "-", "--json"],
                              "# PRD\nwritten straight to stdin\n", monkeypatch))
    assert r["revision"] == 1 and r["stale"] is False
    assert "straight to stdin" in _run(["artifact", "show", "s", "--type", "prd"])


def test_a_request_can_be_created_from_stdin(workspace, monkeypatch):
    r = json.loads(_run_stdin(["session", "init", "-", "--slug", "s", "--json"],
                              "We need a leave approval system.\n", monkeypatch))
    assert r["slug"] == "s"
    assert "leave approval" in store.session_request("s")


def test_a_missing_document_path_is_an_error_not_content(workspace):
    # `model apply <session> <path>` takes a path. Treating an unreadable one as the proposal itself
    # would turn a typo into a confusing schema error about a body that happens to be a filename.
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    with pytest.raises(SystemExit) as exc:
        _run(["model", "apply", "s", "no-such-file.json", "--json"])
    assert exc.value.code != 0


def test_stdin_is_refused_when_it_is_a_terminal(workspace, monkeypatch):
    class _Tty(io.StringIO):
        def isatty(self):
            return True

    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    monkeypatch.setattr("sys.stdin", _Tty(""))
    # Without this guard the command blocks forever waiting for input nobody meant to type.
    with pytest.raises(SystemExit) as exc:
        _run(["model", "apply", "s", "-", "--json"])
    assert exc.value.code != 0


def test_context_can_be_asked_for_by_session(workspace):
    # A session's card selection is held constant across its turns; a later turn that reads every card
    # reasons from a wider context than the model was built on. Asking by session makes that unmissable.
    _run(["session", "init", "Something.", "--slug", "narrow", "--context", "b2b-platform", "--json"])
    _run(["session", "init", "Something else.", "--slug", "wide", "--json"])
    narrow = _run(["context", "--session", "narrow"])
    wide = _run(["context", "--session", "wide"])
    assert "## b2b-platform" in narrow
    assert len(narrow) < len(wide)          # the subset really is a subset
    assert narrow == _run(["context", "--cards", "b2b-platform"])

    with pytest.raises(SystemExit):         # the two selectors are alternatives
        _run(["context", "--session", "narrow", "--cards", "b2b-platform"])


# ── session import ──────────────────────────────────────────────────────────────
# Import takes a file from outside the workspace and turns it into a session, so it is the one command
# whose input is genuinely untrusted. Nothing may land in the store before the archive has been checked.


def _zip(path, entries: dict) -> None:
    with zipfile.ZipFile(path, "w") as z:
        for name, content in entries.items():
            z.writestr(name, content)


def _good_entries(slug="imported", revision=0):
    meta = {"format_version": 1, "session_id": "abc", "slug": slug, "created_at": "t",
            "updated_at": "t", "current_revision": revision}
    entries = {f"{slug}/session.json": json.dumps(meta), f"{slug}/request.md": "A request."}
    if revision:
        entries[f"{slug}/model.json"] = json.dumps(_full_model())
    return entries


def test_export_import_round_trip(workspace, tmp_path, monkeypatch):
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _run_stdin(["model", "apply", "s", "-", "--json"], json.dumps(_full_model()), monkeypatch)
    _run(["session", "export", "s", "-o", str(tmp_path / "s.zip"), "--json"])

    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path / "elsewhere"))
    r = _run_json(["session", "import", str(tmp_path / "s.zip"), "--json"])
    assert r["imported"] == "s" and r["replaced"] is False
    assert store.read_meta("s").current_revision == 1


def test_import_refuses_a_directory_name_that_is_not_a_valid_slug(workspace, tmp_path):
    """The reviewer's case: an archive whose folder is `bad slug` unpacked happily and then broke every
    later `session list`. A directory name becomes a slug, so it faces the same validation as any."""
    _zip(tmp_path / "bad.zip", _good_entries("bad slug"))
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "bad.zip"), "--json"])
    assert store.list_session_slugs() == []          # and nothing was written
    assert _run_json(["session", "list", "--json"]) == []


@pytest.mark.parametrize("entry", [
    "../escape/session.json",          # traversal via a parent segment
    "/absolute/session.json",          # an absolute path
    "..\\windows\\session.json",       # a Windows separator zipfile does not treat as a boundary
    "loose.json",                      # not inside a session directory at all
])
def test_import_refuses_unsafe_entries(workspace, tmp_path, entry):
    _zip(tmp_path / "evil.zip", {entry: "{}"})
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "evil.zip"), "--json"])
    assert store.list_session_slugs() == []


def test_import_refuses_an_archive_holding_more_than_one_session(workspace, tmp_path):
    _zip(tmp_path / "two.zip", {**_good_entries("one"), **_good_entries("two")})
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "two.zip"), "--json"])
    assert store.list_session_slugs() == []


def test_import_refuses_an_archive_that_is_too_large_or_too_many_files(workspace, tmp_path):
    from requivo.deterministic import MAX_ARCHIVE_FILES

    many = {f"s/artifacts/f{i}.md": "x" for i in range(MAX_ARCHIVE_FILES + 1)}
    _zip(tmp_path / "many.zip", many)
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "many.zip"), "--json"])

    # A zip bomb compresses to nothing and expands past the ceiling; the cap is on the expanded size.
    _zip(tmp_path / "big.zip", {"s/session.json": "0" * (64 * 1024 * 1024 + 1)})
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "big.zip"), "--json"])
    assert store.list_session_slugs() == []


def test_import_refuses_an_archive_that_is_not_a_session(workspace, tmp_path):
    # Extraction succeeding is not the same as having imported a session. Import used to declare
    # success on the strength of the extraction alone.
    _zip(tmp_path / "nometa.zip", {"s/notes.md": "hello"})
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "nometa.zip"), "--json"])

    _zip(tmp_path / "badjson.zip", {"s/session.json": "{not json"})
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "badjson.zip"), "--json"])

    # A session that disagrees with itself about its own identity.
    _zip(tmp_path / "mismatch.zip", {**_good_entries("claimed")})
    with zipfile.ZipFile(tmp_path / "mismatch2.zip", "w") as z:
        meta = json.loads(_good_entries("claimed")["claimed/session.json"])
        meta["slug"] = "something-else"
        z.writestr("claimed/session.json", json.dumps(meta))
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "mismatch2.zip"), "--json"])

    # A session claiming a model it does not carry.
    _zip(tmp_path / "noModel.zip", {"s/session.json": json.dumps(
        {"format_version": 1, "session_id": "a", "slug": "s", "created_at": "t", "updated_at": "t",
         "current_revision": 3})})
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "noModel.zip"), "--json"])
    assert store.list_session_slugs() == []


def test_import_refuses_a_collision_unless_forced(workspace, tmp_path, monkeypatch):
    _run(["session", "init", "The original.", "--slug", "dup", "--json"])
    _run_stdin(["model", "apply", "dup", "-", "--json"], json.dumps(_full_model()), monkeypatch)
    _zip(tmp_path / "dup.zip", _good_entries("dup"))

    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "dup.zip"), "--json"])
    assert store.read_meta("dup").current_revision == 1        # the original is untouched
    assert "The original." in store.session_request("dup")

    r = _run_json(["session", "import", str(tmp_path / "dup.zip"), "--force", "--json"])
    assert r["replaced"] is True
    assert store.read_meta("dup").current_revision == 0        # genuinely replaced, not merged
    assert "A request." in store.session_request("dup")


def test_a_refused_import_leaves_no_scratch_directory(workspace, tmp_path):
    _zip(tmp_path / "bad.zip", _good_entries("bad slug"))
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "bad.zip"), "--json"])
    _zip(tmp_path / "nometa.zip", {"s/notes.md": "hello"})
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "nometa.zip"), "--json"])
    assert list((workspace / ".requivo").glob(".import-*")) == []
