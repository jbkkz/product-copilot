"""The `requivo` subcommand surface, end to end and offline.

Split out of `test_engine.py` (#72). The modern surface is a thin layer over the same services;
`app()` takes an injected client so API-backed verbs run against a `FakeClient` and never reach the
network. What each test here pins is the *seam* — that the verb reaches the service, writes the
artifact where every other surface writes it, and records what it read.

The parser-shape tests (which verbs bind, which flags exist) live in `test_cli_flag_names.py`; the
no-LLM verbs live in `test_cli_deterministic.py`.
"""
import json
import shutil

import pytest
from _fakes import _ENGINE_REPLY, FakeClient, _model_in_out, _run_app, full_slots, slot

from requivo.cli import _is_file_arg, app
from requivo.core import persistence as store
from requivo.core.contracts import EngineOutput
from requivo.core.persistence import load_model
from requivo.services.artifacts import ArtifactService


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    """Every test in this module writes sessions/artifacts into an isolated temp workspace, never the
    real repo. Points both the canonical root (.requivo/sessions) and the legacy root (out/) at tmp."""
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))


def test_pc_status_runs_offline():
    with _model_in_out("clitest-status") as p:
        assert "UNDERSTANDING" in _run_app(["status", str(p)])  # no client built


def test_status_json_payload_is_rich_enough_for_a_client():
    # status(slug) must carry the full picture — understanding, questions, gaps, summary, context —
    # so Claude Code and a future Web client render it without rebuilding the presentation logic.
    from requivo.services.sessions import SessionService
    slug = "clitest-status-json"
    store.create_session(slug, "req")
    model = EngineOutput.model_validate({
        "model": full_slots(workflow=slot(90, "explicit", "high"),
                            business_rules=slot(30, "explicit", "high")),   # explicit but thin
        "questions": [{"q": "How are exceptions handled?", "slot": "business_rules", "why": "u×i"}],
        "summary": {"objective": "obj"},
    })
    SessionService().update_model(slug, model.model_dump())
    try:
        st = SessionService().status(slug)
        assert set(st) >= {"understanding", "questions", "summary", "remaining_gaps",
                           "context_cards", "artifacts", "readiness", "revision"}
        assert st["questions"][0]["slot"] == "business_rules" and st["questions"][0]["label"]
        assert st["summary"]["objective"] == "obj"
        # confirmed-but-thin high-impact slot: still a gap, and flagged `thin` in the understanding view
        assert "business_rules" in {g["slot"] for g in st["remaining_gaps"]}
        thin = [e for grp in st["understanding"].values() for e in grp if e["thin"]]
        assert any(e["slot"] == "business_rules" for e in thin)
    finally:
        shutil.rmtree(store.canonical_dir(slug), ignore_errors=True)


def test_pc_demo_runs_offline_from_saved_example():
    # The activation path: a visitor runs `requivo demo` with no key, no args, no network, and sees a
    # real run end to end. No client is passed and none is built.
    text = _run_app(["demo"])  # client=None
    assert "REQUIVO — DEMO" in text
    assert "freelancers to check guests in" in text     # the real request is shown
    assert "UNDERSTANDING" in text                       # status rendered live from the saved model
    assert "SOLUTION ASSESSMENT" in text                 # the assessment (the differentiator)
    assert "event-checkin-reconciliation/epic.md" in text  # the other artifacts are pointed to


def test_pc_brief_uses_injected_client():
    with _model_in_out("clitest-brief") as p:
        text = _run_app(["brief", str(p)], client=FakeClient(json.dumps({"complexity": "low", "solution": "S"})))
        assert "SOLUTION ASSESSMENT" in text


def test_demo_payload_matches_the_browsable_example():
    # `requivo demo` reads a frozen payload bundled in the package (so it works from a wheel); examples/
    # holds the browsable copy at the repo root. Guard against silent drift between the two: every
    # bundled demo file must byte-match its counterpart under examples/event-checkin-reconciliation/.
    from requivo.paths import DEMO

    repo_root = DEMO.parents[3]  # assets/demo → assets → requivo → src → repo
    browsable = repo_root / "examples" / "event-checkin-reconciliation"
    bundled = sorted(DEMO.glob("*"))
    assert bundled, "demo payload is empty"
    for f in bundled:
        assert f.read_text(encoding="utf-8") == (browsable / f.name).read_text(encoding="utf-8"), f"demo payload drifted from examples/: {f.name}"


def test_pc_brief_persists_reasoning_into_model():
    # Keystone: advise()'s reasoning is absorbed into the model and saved (backfill),
    # so downstream generators inherit it instead of it being regenerated and discarded.
    with _model_in_out("clitest-brief-persist") as p:
        brief_json = json.dumps({
            "complexity": "high",
            "decisions": [{"decision": "draft-first", "tradeoff": "review step"}],
            "challenges": [{
                "headline": "Archive vs delete", "premise": "pr",
                "alternative": "al", "consequence": "co", "recommendation": "re",
            }],
            "opportunities": [{"text": "reuse engine", "leverage": "high", "modules": ["Invoicing"]}],
        })
        _run_app(["brief", str(p)], client=FakeClient(brief_json))
        reloaded = load_model(p)  # the saved model now carries the reasoning
        assert reloaded.challenges[0].headline == "Archive vs delete"
        assert reloaded.decisions[0].decision == "draft-first"
        assert reloaded.opportunities[0].modules == ["Invoicing"]


def test_pc_stories_renders():
    with _model_in_out("clitest-stories") as p:
        text = _run_app(["stories", str(p)], client=FakeClient(json.dumps({"stories": [{"id": "S1", "title": "T"}]})))
        assert "=== USER STORIES ===" in text and "[S1] T" in text


def test_pc_estimate_renders():
    with _model_in_out("clitest-estimate") as p:
        fake = FakeClient(
            json.dumps({"stories": [{"id": "S1", "title": "T"}]}),
            json.dumps({"items": [{"story_id": "S1", "title": "T", "complexity": "S", "days_low": 1, "days_high": 2}]}),
        )
        assert "=== ESTIMATE" in _run_app(["estimate", str(p)], client=fake)


def test_pc_brief_writes_the_artifact_like_every_other_surface():
    # The terminal used to render the assessment and keep it: the Web and Claude Code saved a tracked
    # artifact, the CLI saved nothing. A generation now produces the same document wherever it was
    # asked for — same file, same provenance, same staleness tracking.
    with _model_in_out("clitest-brief-artifact") as p:
        _run_app(["brief", str(p)], client=FakeClient(json.dumps({"complexity": "low", "solution": "S"})))
        assert (p.parent / "artifacts" / "solution-assessment.md").exists()
        listed = ArtifactService().list(p.parent.name)["brief"]
        assert listed["stale"] is False and listed["revision"] >= 1


def test_pc_generators_record_which_prompt_reasoned(tmp_path):
    # A revision log that cannot say what produced it cannot reproduce it. Behaviour is tuned by
    # editing prompts and context cards, so the prompt hash is half the provenance.
    with _model_in_out("clitest-provenance") as p:
        _run_app(["brief", str(p)], client=FakeClient(json.dumps({"complexity": "low"})))
        rec = store.read_meta(p.parent.name).revisions[-1]
        assert rec.surface == "cli-brief" and rec.provider == "anthropic"
        assert rec.prompt_version and rec.prompt_version.startswith("sha256:")


# Minimal-but-legal artifact replies. The contracts require what makes each artifact *be* that
# artifact — a PRD states a problem, a scenario has a `when` and at least one `then`, an epic
# decomposes into at least one issue — so a stub reply has to carry those and nothing more.
_CRITERIA = {"title": "X", "features": [
    {"name": "Requesting leave", "scenarios": [
        {"id": "SC-1", "title": "Manager approves", "when": "the manager approves",
         "then": ["the request is marked approved"]}]}]}
_EPIC = {"title": "X", "issues": [{"id": "I-1", "title": "Build the request form"}]}


def test_pc_prd_writes_artifact():
    with _model_in_out("clitest-prd") as p:
        _run_app(["prd", str(p)], client=FakeClient(json.dumps({"title": "X", "problem": "P"})))
        assert (p.parent / "artifacts" / "prd.md").read_text(encoding="utf-8").startswith("# X")


def test_pc_criteria_writes_artifact():
    with _model_in_out("clitest-criteria") as p:
        _run_app(["criteria", str(p)], client=FakeClient(json.dumps(_CRITERIA)))
        assert (p.parent / "artifacts" / "acceptance-criteria.md").exists()


def test_pc_epic_writes_all_views():
    with _model_in_out("clitest-epic") as p:
        _run_app(["epic", str(p), "--export-json", "--github", "--gitlab"],
                 client=FakeClient(json.dumps(_EPIC)))
        for name in ("epic.md", "epic.json", "epic.github.json", "epic.gitlab.json"):
            assert (p.parent / "artifacts" / name).exists()


def test_pc_release_stamps_version():
    with _model_in_out("clitest-release") as p:
        _run_app(["release", str(p), "v1.0"], client=FakeClient(json.dumps({"title": "X"})))
        assert "v1.0" in (p.parent / "artifacts" / "release-notes.md").read_text(encoding="utf-8")


def test_pc_discover_once_saves_model():
    slug = "clitest-discover-probe-xyz"
    _run_app(["discover", "clitest discover probe xyz", "--once"], client=FakeClient(_ENGINE_REPLY))
    folder = store.canonical_dir(slug)
    assert (folder / "model.json").exists()
    assert (folder / "request.md").exists()   # saved so `requivo answer` can resume
    assert store.read_meta(slug).current_revision == 1
    assert store.read_meta(slug).provider == "anthropic"


def test_discover_file_check_survives_a_real_length_request():
    # A real client request is a paragraph — longer than the OS filename limit. The file-vs-text
    # heuristic must treat that as text, not crash (Path.exists() raises OSError above the limit).
    long_request = "When a contract is signed we want everything to reconcile. " * 20
    assert _is_file_arg(long_request) is False


def test_discover_file_check_rejects_blank_arg():
    # Path("") resolves to the current directory, which exists — so a naive .exists() check would
    # treat a blank request as a readable file and then blow up on read_text. Blank must read as text.
    assert _is_file_arg("") is False
    assert _is_file_arg("   \n\t ") is False


def test_discover_file_check_rejects_a_directory(tmp_path):
    # A directory `exists()` too. Accepting one means calling read_text() on it a line later, which
    # raises IsADirectoryError as a traceback instead of treating the argument as a request.
    assert _is_file_arg(str(tmp_path)) is False
    f = tmp_path / "request.md"
    f.write_text("Build a leave approval system.")
    assert _is_file_arg(str(f)) is True


def test_discover_from_a_file_slugifies_its_name(tmp_path, monkeypatch):
    # A filename is a suggestion for the slug, not a slug. Slugs name a directory in the session store
    # and are validated strictly, so passing the raw stem through turned an ordinary input file
    # ("Leave Approval v2.md") into an invalid_slug error.
    from requivo.services.sessions import SessionService

    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    req = tmp_path / "Leave Approval v2.md"
    req.write_text("We would like a leave approval system.")
    _run_app(["discover", str(req), "--once"], client=FakeClient(_ENGINE_REPLY))
    assert [m.slug for m in SessionService().list_sessions()] == ["leave-approval-v2"]


def test_pc_discover_rejects_empty_request():
    # An empty/whitespace request should fail fast with a clear message, not crash or fire an
    # empty-content API call. The FakeClient would raise if reached — SystemExit means we never did.
    for blank in ("", "   "):
        with pytest.raises(SystemExit):
            _run_app(["discover", blank], client=FakeClient(_ENGINE_REPLY))


def test_pc_answer_refines_the_model():
    # A stateless discovery turn: answers + the current model → a refined model.
    with _model_in_out("clitest-answer") as p:
        turn2 = json.dumps({
            "model": full_slots(problem=slot(95, "explicit", "high")),
            "questions": [],
            # A discovery reply owes an objective — a session of slots with nothing naming what they
            # are for renders as a blank heading everywhere. The boundary check rejects it otherwise.
            "summary": {"objective": "A leave approval system"},
        })
        fake = FakeClient(turn2)
        _run_app(["answer", str(p), "The approver is HR, and the circuit is per-client."], client=fake)
        # the answers + the prior model reached the engine turn
        sent = fake.calls[0]["messages"]
        assert "The approver is HR" in sent[-1]["content"]
        assert "problem" in sent[1]["content"]  # prior model carried as assistant turn
        # and the saved model is refined (inferred/80 → explicit/95)
        reloaded = load_model(p)
        assert reloaded.model["problem"].completeness == 95
        assert reloaded.model["problem"].confidence.value == "explicit"


# ── Rename: Product Copilot → Requivo (identity is correct and complete) ───────


def test_requivo_package_is_importable_and_versioned():
    import requivo

    assert requivo.__version__  # a real version string
    from requivo.cli import app  # the entry point resolves
    assert callable(app)


def test_old_package_name_is_gone():
    # The package was renamed, not shimmed — the old import must fail so nothing silently depends on it.
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("product_copilot")


def test_cli_help_exits_cleanly():
    # `requivo --help` (and `pc --help`, same entry point) prints usage and exits 0 via argparse.
    with pytest.raises(SystemExit) as ei:
        app(["--help"])
    assert ei.value.code == 0
