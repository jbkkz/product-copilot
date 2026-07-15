"""Unit tests for the pure logic — the parts that must be correct without an API call."""
import pytest
from pydantic import ValidationError

import io
import json
import shutil
from contextlib import contextmanager, redirect_stdout

from src.engine import (
    PRD,
    AcceptanceCriteria,
    Brief,
    Challenge,
    Confidence,
    DesignDecision,
    EngineOutput,
    Epic,
    Leverage,
    Opportunity,
    ReleaseNotes,
    Slot,
    Stories,
    _extract_json,
    _readiness_blockers,
    _slug,
    _state_of,
    criteria_markdown,
    derive_stories,
    epic_export,
    epic_export_json,
    epic_markdown,
    estimate_confidence,
    generate_prd,
    load_context,
    load_model,
    prd_markdown,
    release_markdown,
    render_brief,
    render_stories,
    run,
    save_model,
    soft_slots,
    to_github,
    to_gitlab,
    write_artifact,
)
from product_copilot.cli import _build_parser, app
from product_copilot.paths import ROOT


def slot(completeness, confidence, impact):
    return {"completeness": completeness, "confidence": confidence, "impact": impact}


def out(model):
    return EngineOutput.model_validate({"model": model, "questions": [], "summary": {}})


# ── Characterization harness (commit 0: safety net before the refactor) ───────
# A stub Anthropic client so generator functions run offline. It mimics the one
# call shape _complete() relies on: client.messages.create(...) returning
# resp.content = [block] where block.type == "text".


class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]


class FakeClient:
    """Returns canned JSON replies in order; records each create() call's kwargs."""

    def __init__(self, *replies):
        self._replies = list(replies)
        self.calls = []
        self.messages = self  # so client.messages.create resolves to self.create

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._replies.pop(0))


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


def test_epic_markdown_renders_issues_with_labels_and_deps():
    epic = Epic(
        title="Leave approval",
        milestone="Pilot",
        goal="Let employees request leave and managers approve it.",
        issues=[
            {"id": "#1", "title": "Model the leave object", "description": "Fields and states.",
             "labels": ["backend"]},
            {"id": "#2", "title": "Build approval circuit", "description": "Route to manager.",
             "labels": ["feature", "backend"], "depends_on": ["#1"]},
        ],
        open_questions=["Half-day support?"],
    )
    md = epic_markdown(epic)
    assert md.startswith("# Epic: Leave approval")
    assert "**Milestone:** Pilot" in md
    assert "### [ ] #1 — Model the leave object" in md
    assert "**Labels:** `feature`, `backend` · **Depends on:** #1" in md
    assert "## Open questions" in md


def test_epic_export_is_neutral_and_maps_issues():
    epic = Epic(
        title="Leave approval",
        milestone="Pilot",
        goal="Employees request leave, managers approve.",
        business_value="Removes email/Excel churn.",
        in_scope=["Submission"],
        issues=[
            {"id": "#1", "title": "Model the leave object", "labels": ["backend"]},
            {"id": "#2", "title": "Approval circuit", "labels": ["feature"], "depends_on": ["#1"]},
        ],
    )
    payload = epic_export(epic)
    assert payload["format"] == "product-copilot-epic" and payload["version"] == 1
    assert payload["epic"]["labels"] == ["epic"] and payload["epic"]["milestone"] == "Pilot"
    # goal + business value + scope fold into one importable description body.
    assert "Business value" in payload["epic"]["description"]
    assert "In scope" in payload["epic"]["description"]
    # Each issue carries its ref, the shared milestone, and dependencies as refs.
    assert payload["issues"][0]["ref"] == "#1" and payload["issues"][0]["milestone"] == "Pilot"
    assert payload["issues"][1]["depends_on"] == ["#1"]
    # The JSON writer emits valid, parseable JSON.
    assert json.loads(epic_export_json(epic)) == payload


def test_to_github_plan_degrades_honestly_and_is_idempotent():
    epic = Epic(
        title="Leave approval",
        milestone="Pilot",
        goal="Employees request leave.",
        issues=[
            {"id": "#1", "title": "Model the leave object", "description": "Fields.", "labels": ["backend"]},
            {"id": "#2", "title": "Approval circuit", "labels": ["feature"], "depends_on": ["#1"]},
        ],
    )
    plan = to_github(epic_export(epic), "leave-approval")
    assert plan["target"] == "github"
    # Every issue carries the idempotency label so a re-run can find-then-skip.
    label = "pc-epic:leave-approval"
    assert plan["idempotency_label"] == label
    assert all(label in issue["labels"] for issue in plan["issues"])
    assert label in plan["tracking_issue"]["labels"]
    # The epic degrades to a tracking issue with a task list (GitHub has no native epic).
    assert "- [ ] Model the leave object" in plan["tracking_issue"]["body"]
    # depends_on has no native GitHub concept — stated in the body, resolved to the issue's title.
    assert "**Depends on:** Model the leave object" in plan["issues"][1]["body"]
    assert "_Part of epic: Leave approval_" in plan["issues"][0]["body"]


def test_to_gitlab_wires_depends_on_as_issue_links():
    epic = Epic(
        title="Leave approval",
        milestone="Pilot",
        issues=[
            {"id": "#1", "title": "Model the leave object", "labels": ["backend"]},
            {"id": "#2", "title": "Approval circuit", "labels": ["feature"], "depends_on": ["#1"]},
            {"id": "#3", "title": "UI", "labels": ["frontend"], "depends_on": ["#1", "#2"]},
        ],
    )
    plan = to_gitlab(epic_export(epic), "leave-approval")
    assert plan["target"] == "gitlab"
    label = "pc-epic:leave-approval"
    assert all(label in issue["labels"] for issue in plan["issues"])
    # GitLab maps depends_on to structured issue links (the dependency blocks the dependent), not text.
    assert {"source_ref": "#1", "target_ref": "#2", "type": "blocks"} in plan["links"]
    assert {"source_ref": "#2", "target_ref": "#3", "type": "blocks"} in plan["links"]
    assert len(plan["links"]) == 3
    # No dependency text in the body — the relationship is structured.
    assert "Depends on" not in plan["issues"][1]["description"]


def test_release_markdown_stamps_version_and_sections():
    rn = ReleaseNotes(
        title="Leave approval",
        version="v1.0",
        summary="Your team can now request and approve leave online.",
        highlights=["Submit a request in a few clicks"],
        known_limitations=["Payroll export is not included yet"],
        notes=["An administrator sets the approval circuit first"],
    )
    md = release_markdown(rn)
    assert md.startswith("# Leave approval — v1.0")
    assert "Your team can now request and approve leave online." in md
    assert "## What's new" in md
    assert "## Not included yet" in md
    assert "## Before you start" in md


def test_release_markdown_omits_version_when_empty():
    md = release_markdown(ReleaseNotes(title="Leave approval", highlights=["A"]))
    assert md.startswith("# Leave approval\n")
    assert "—" not in md.splitlines()[0]


def test_render_brief_titles_solution_assessment_and_shows_challenges():
    model = {"real_problem": slot(80, "explicit", "high")}
    brief = Brief(
        problem="P",
        solution="S",
        complexity="high",
        decisions=[
            DesignDecision(
                decision="Draft-first invoices reviewed before issuance",
                why="Finance sign-off is required.",
                alternative="Immediate issuance.",
                tradeoff="Extra step, lower compliance risk.",
            ),
            DesignDecision(decision="Amount sourced from the Contract"),  # bare fact, no fork
        ],
        challenges=[
            Challenge(
                headline="Invoice at signature",
                premise="Invoices are generated the moment a contract is signed.",
                alternative="Many teams invoice at the contract start date or on a billing schedule.",
                consequence="Signature-triggered invoicing multiplies credit-note handling.",
                recommendation="Validate the billing trigger with Finance first.",
            )
        ],
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_brief(out(model), brief)
    text = buf.getvalue()
    assert "SOLUTION ASSESSMENT" in text and "DISCOVERY BRIEF" not in text
    assert "CHALLENGES" in text
    # the top challenge surfaces in the executive summary, detail in the full analysis
    assert "Challenge Invoice at signature" in text
    assert "⚑ Invoice at signature" in text  # full-analysis section
    assert "Premise" in text and "Alternative" in text and "Recommend" in text
    # Design decisions: the forked one shows its reasoning, the bare fact stays a single line.
    assert "DESIGN DECISIONS" in text and "DECISION LOG" not in text
    assert "✓ Draft-first invoices reviewed before issuance" in text
    assert "Why" in text and "Tradeoff" in text
    assert "✓ Amount sourced from the Contract" in text


def test_render_brief_opportunity_names_reached_modules():
    model = {"real_problem": slot(80, "explicit", "high")}
    brief = Brief(
        problem="P",
        solution="S",
        complexity="high",
        opportunities=[
            Opportunity(
                text="Generalize the approval circuit.",
                leverage=Leverage.high,
                modules=["Absence", "Contracts", "Missions"],
            ),
            Opportunity(text="Add a dashboard later.", leverage=Leverage.future),  # no modules
        ],
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_brief(out(model), brief)
    text = buf.getvalue()
    # a grounded opportunity names the modules it reaches; an ungrounded one shows no ↳ line
    assert "↳ reaches: Absence, Contracts, Missions" in text
    assert "Add a dashboard later." in text
    assert text.count("↳ reaches:") == 1


# ── Characterization: discovery, generators, artifacts, errors, context ───────
# These pin CURRENT behavior (shapes, paths, formats, error surfaces) so the
# upcoming package split stays comportement-constant. They are not quality tests.

_ENGINE_REPLY = json.dumps(
    {
        "model": {"real_problem": {"completeness": 80, "confidence": "explicit", "impact": "high"}},
        "questions": [],
        "summary": {"objective": "o"},
    }
)


def test_run_returns_engine_output_and_wires_schema_and_context():
    # The --once discovery pass is a single run() call. Characterize its result
    # AND that the engine turn is driven by prompts/engine.md with schema + context injected.
    fake = FakeClient(_ENGINE_REPLY)
    result = run(fake, [{"role": "user", "content": "leave approval"}])
    assert isinstance(result, EngineOutput)
    assert result.model["real_problem"].completeness == 80
    system = fake.calls[0]["system"]
    assert "slots" in system              # framework/model_schema.json injected ({{SCHEMA}})
    assert "## b2b-platform" in system    # context card injected ({{CONTEXT}})


def test_generate_prd_from_saved_model_roundtrip(tmp_path):
    # The --from path: reload a saved model and regenerate an artifact, no discovery.
    model = out({"real_problem": slot(80, "explicit", "high")})
    path = tmp_path / "model.json"
    path.write_text(model.model_dump_json())

    loaded = load_model(path)
    assert loaded.model["real_problem"].completeness == 80

    prd = generate_prd(FakeClient(json.dumps({"title": "Leave approval"})), loaded)
    assert isinstance(prd, PRD) and prd.title == "Leave approval"
    md = prd_markdown(prd)
    assert md.startswith("# Leave approval")
    assert "generated by Product Copilot" in md


def test_derive_stories_returns_structured_stories():
    reply = json.dumps({"stories": [{"id": "S1", "title": "Submit a leave request"}]})
    stories = derive_stories(FakeClient(reply), out({"real_problem": slot(80, "explicit", "high")}))
    assert isinstance(stories, Stories)
    assert [s.id for s in stories.stories] == ["S1"]
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_stories(stories)
    text = buf.getvalue()
    assert "=== USER STORIES ===" in text and "[S1] Submit a leave request" in text


def test_artifact_paths_and_names():
    # Guards R1: save_model / write_artifact resolve to out/<slug>/<file> and round-trip.
    p = save_model(out({"real_problem": slot(80, "explicit", "high")}), "_chartest_slug")
    try:
        assert p.name == "model.json"
        assert p.parent.name == "_chartest_slug"
        assert p.parent.parent.name == "out"
        a = write_artifact("_chartest_slug", "prd.md", "# X\n")
        assert a.parent == p.parent and a.name == "prd.md"
        assert a.read_text() == "# X\n"
        assert load_model(p).model["real_problem"].completeness == 80
    finally:
        shutil.rmtree(p.parent, ignore_errors=True)


def test_slug_is_first_five_word_tokens():
    assert _slug("We'd like an invoice created automatically when signed") == "we-d-like-an-invoice"
    assert _slug("!!!") == "discovery"


def test_load_model_rejects_invalid_model(tmp_path):
    bad = tmp_path / "model.json"
    bad.write_text(json.dumps({"questions": [], "summary": {}}))  # required `model` missing
    with pytest.raises(ValidationError):
        load_model(bad)


def test_load_context_includes_real_cards_and_skips_underscore():
    ctx = load_context()
    assert "## b2b-platform" in ctx    # committed context card is included
    assert "## _template" not in ctx   # underscore-prefixed card is skipped


def test_load_context_empty_when_no_cards(tmp_path, monkeypatch):
    # load_context reads ROOT from its own module (core.llm after the split).
    from product_copilot.core import llm

    monkeypatch.setattr(llm, "ROOT", tmp_path)
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "_only_template.md").write_text("skip me")
    assert load_context() == ""


# ── The `pc` subcommand CLI ───────────────────────────────────────────────────
# The modern surface is a thin layer over the same core; app() takes an injected
# client so API-backed verbs run offline against a FakeClient.


@contextmanager
def _model_in_out(slug):
    """A real out/<slug>/model.json the model-taking subcommands can load."""
    p = save_model(out({"real_problem": slot(80, "explicit", "high")}), slug)
    try:
        yield p
    finally:
        shutil.rmtree(p.parent, ignore_errors=True)


def _run_app(argv, client=None):
    buf = io.StringIO()
    with redirect_stdout(buf):
        app(argv, client=client)
    return buf.getvalue()


def test_pc_parser_binds_every_subcommand():
    cases = {
        ("discover", "req"): "_cmd_discover",
        ("status", "m.json"): "_cmd_status",
        ("brief", "m.json"): "_cmd_brief",
        ("prd", "m.json"): "_cmd_prd",
        ("stories", "m.json"): "_cmd_stories",
        ("estimate", "m.json"): "_cmd_estimate",
        ("criteria", "m.json"): "_cmd_criteria",
        ("epic", "m.json"): "_cmd_epic",
        ("release", "m.json"): "_cmd_release",
    }
    for argv, fname in cases.items():
        assert _build_parser().parse_args(list(argv)).func.__name__ == fname
    assert _build_parser().parse_args(["epic", "m", "--github", "--gitlab"]).github
    assert _build_parser().parse_args(["release", "m", "v1.0"]).version == "v1.0"


def test_pc_unknown_command_errors():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["bogus"])


def test_pc_status_runs_offline():
    with _model_in_out("_clitest_status") as p:
        assert "UNDERSTANDING" in _run_app(["status", str(p)])  # no client built


def test_pc_brief_uses_injected_client():
    with _model_in_out("_clitest_brief") as p:
        text = _run_app(["brief", str(p)], client=FakeClient(json.dumps({"complexity": "low", "solution": "S"})))
        assert "SOLUTION ASSESSMENT" in text


def test_pc_brief_persists_reasoning_into_model():
    # Keystone: advise()'s reasoning is absorbed into the model and saved (backfill),
    # so downstream generators inherit it instead of it being regenerated and discarded.
    with _model_in_out("_clitest_brief_persist") as p:
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
    with _model_in_out("_clitest_stories") as p:
        text = _run_app(["stories", str(p)], client=FakeClient(json.dumps({"stories": [{"id": "S1", "title": "T"}]})))
        assert "=== USER STORIES ===" in text and "[S1] T" in text


def test_pc_estimate_renders():
    with _model_in_out("_clitest_estimate") as p:
        fake = FakeClient(
            json.dumps({"stories": [{"id": "S1", "title": "T"}]}),
            json.dumps({"items": [{"story_id": "S1", "title": "T", "complexity": "S", "days_low": 1, "days_high": 2}]}),
        )
        assert "=== ESTIMATE" in _run_app(["estimate", str(p)], client=fake)


def test_pc_prd_writes_artifact():
    with _model_in_out("_clitest_prd") as p:
        _run_app(["prd", str(p)], client=FakeClient(json.dumps({"title": "X"})))
        assert (p.parent / "prd.md").read_text().startswith("# X")


def test_pc_criteria_writes_artifact():
    with _model_in_out("_clitest_criteria") as p:
        _run_app(["criteria", str(p)], client=FakeClient(json.dumps({"title": "X"})))
        assert (p.parent / "acceptance-criteria.md").exists()


def test_pc_epic_writes_all_views():
    with _model_in_out("_clitest_epic") as p:
        _run_app(["epic", str(p), "--json", "--github", "--gitlab"], client=FakeClient(json.dumps({"title": "X"})))
        for name in ("epic.md", "epic.json", "epic.github.json", "epic.gitlab.json"):
            assert (p.parent / name).exists()


def test_pc_release_stamps_version():
    with _model_in_out("_clitest_release") as p:
        _run_app(["release", str(p), "v1.0"], client=FakeClient(json.dumps({"title": "X"})))
        assert "v1.0" in (p.parent / "release-notes.md").read_text()


def test_pc_discover_once_saves_model():
    slug = "clitest-discover-probe-xyz"
    folder = ROOT / "out" / slug
    try:
        _run_app(["discover", "clitest discover probe xyz", "--once"], client=FakeClient(_ENGINE_REPLY))
        assert (folder / "model.json").exists()
    finally:
        shutil.rmtree(folder, ignore_errors=True)
