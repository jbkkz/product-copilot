"""End-to-end tests of the deterministic CLI surface — doctor / session / model / artifact.

Every command here must run with no LLM and no API key. Output is captured through `app()` (the real
entry point) against a temp workspace; a `--json` variant is asserted where the spec fixes a machine
format, so Claude Code can rely on it.
"""
from __future__ import annotations

import io
import json
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
