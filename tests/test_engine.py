"""Unit tests for the pure logic — the parts that must be correct without an API call."""
import io
import json
import shutil
from contextlib import contextmanager, redirect_stdout

import pytest
from pydantic import ValidationError

from product_copilot.cli import _build_parser, _is_file_arg, app
from product_copilot.core.dependencies import artifact_slots, diff_models, propagate, resolve_slots
from product_copilot.paths import ROOT
from src.engine import (
    PRD,
    AcceptanceCriteria,
    Brief,
    Challenge,
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


def slot(completeness, confidence, impact):
    return {"completeness": completeness, "confidence": confidence, "impact": impact}


def full_slots(**overrides):
    """A complete required-slot model (every required slot present, empty/low by default) with
    per-slot overrides — mirrors what a real discovery turn emits, so models that go through run()
    satisfy the completeness invariant."""
    from product_copilot.core.contracts import _schema_order, schema_slot_ids

    _, required = schema_slot_ids()
    # Schema order, mirroring a real reply (the LLM emits slots in schema order; Pydantic preserves
    # it). `required` is an unordered set, so iterate the ordered id list and keep the required ones.
    model = {sid: slot(0, "empty", "low") for sid in _schema_order() if sid in required}
    model.update(overrides)
    return model


def out(model):
    # Pad to the full required slot set: a real EngineOutput always carries every slot, and the
    # discovery boundary enforces it. Tests that care about one slot just override that one.
    return EngineOutput.model_validate(
        {"model": full_slots(**model), "questions": [], "summary": {}}
    )


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


def test_output_rejects_unknown_slots():
    # A slot id the schema doesn't define (typo / hallucination) is rejected at the contract, so it
    # can never sit in the model unseen by the schema-driven views. `real_problem` is the classic typo.
    with pytest.raises(ValidationError):
        EngineOutput.model_validate({
            "model": {"real_problem": slot(80, "explicit", "high")},
            "questions": [], "summary": {},
        })


def test_output_allows_a_partial_but_known_model():
    # Completeness (the full required set) is enforced at the discovery boundary, NOT the contract —
    # internal projections (diff/propagate) legitimately carry a subset of *known* slots.
    part = EngineOutput.model_validate({
        "model": {"workflow": slot(60, "inferred", "high")},
        "questions": [], "summary": {},
    })
    assert set(part.model) == {"workflow"}


def _q(slot_id, i=0):
    return {"q": f"question {i}", "slot": slot_id, "why": "uncertainty × impact"}


def test_output_caps_questions_at_six():
    # The engine asks at most 6, sorted by information value; a 7th means it stopped prioritising.
    base = {"model": {"workflow": slot(60, "inferred", "high")}, "summary": {}}
    ok = EngineOutput.model_validate({**base, "questions": [_q("workflow", i) for i in range(6)]})
    assert len(ok.questions) == 6
    with pytest.raises(ValidationError):
        EngineOutput.model_validate({**base, "questions": [_q("workflow", i) for i in range(7)]})


def test_output_rejects_a_question_targeting_an_unknown_slot():
    # A question must point at a slot the schema defines — a question about a non-existent slot is as
    # malformed as an unknown slot in the model.
    with pytest.raises(ValidationError):
        EngineOutput.model_validate({
            "model": {"workflow": slot(60, "inferred", "high")},
            "questions": [_q("not_a_slot")], "summary": {},
        })


# ── The driver: uncertainty × impact ─────────────────────────────────────────


def test_soft_slots_are_medium_or_high_and_unresolved():
    # Real slot ids: the model must speak the schema's vocabulary (padded slots stay empty/low → not
    # soft). business_objects precedes business_rules in schema order, so soft comes back in that order.
    model = out({
        "problem": slot(90, "explicit", "high"),           # solid → not soft
        "business_rules": slot(30, "inferred", "high"),    # uncertain + high → soft
        "business_objects": slot(50, "inferred", "medium"),# uncertain + medium → soft
        "reporting": slot(10, "empty", "low"),             # low impact → never soft
    })
    assert soft_slots(model) == ["business_objects", "business_rules"]


def test_estimate_confidence_tiers():
    assert estimate_confidence(0) == "high"
    assert estimate_confidence(1) == "high"
    assert estimate_confidence(3) == "medium"
    assert estimate_confidence(5) == "low"


def test_readiness_blockers_are_high_impact_unconfirmed():
    # Padded slots are low-impact → never blockers; only the high-impact-unconfirmed override is.
    model = out({
        "problem": slot(90, "explicit", "high"),         # confirmed → not blocking
        "business_rules": slot(80, "inferred", "high"),  # high but inferred → blocker
        "success_metrics": slot(0, "empty", "medium"),   # medium → not blocking
    })
    assert _readiness_blockers(model) == ["business_rules"]


def test_readiness_flags_a_missing_high_impact_slot_as_blocker():
    # The north-star guard: a required high-impact slot the model omitted entirely must NOT vanish
    # from readiness. Build a model with everything explicit EXCEPT business_rules, then drop it.
    from product_copilot.core.contracts import schema_slot_ids

    _, required = schema_slot_ids()
    model_dict = {sid: slot(90, "explicit", "high") for sid in required}
    del model_dict["business_rules"]  # a high-impact dimension goes missing
    model = EngineOutput.model_validate({"model": model_dict, "questions": [], "summary": {}})
    # business_rules is absent, high-impact by default → it is still a blocker, not invisible.
    assert "business_rules" in _readiness_blockers(model)


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
    model = {"problem": slot(80, "explicit", "high")}
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
    model = {"problem": slot(80, "explicit", "high")}
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
        # A run() reply must carry the whole required slot set (the completeness invariant) — a
        # single-slot reply would now be rejected and retried, exhausting the FakeClient.
        "model": full_slots(problem=slot(80, "explicit", "high")),
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
    assert result.model["problem"].completeness == 80
    # system is a cache-controlled text block so its stable prefix is cached across calls.
    block = fake.calls[0]["system"][0]
    assert block["cache_control"] == {"type": "ephemeral"}
    system = block["text"]
    assert "slots" in system              # framework/model_schema.json injected ({{SCHEMA}})
    assert "## b2b-platform" in system    # context card injected ({{CONTEXT}})


def test_run_rejects_a_model_missing_required_slots():
    # A discovery reply missing a required slot is refused: the completeness invariant is enforced at
    # the boundary. The FakeClient returns the same incomplete reply every retry, so run() gives up.
    incomplete = json.dumps({
        "model": {"problem": slot(80, "explicit", "high")},  # 1 of 15 required
        "questions": [], "summary": {"objective": "o"},
    })
    fake = FakeClient(incomplete, incomplete, incomplete)  # every retry attempt
    with pytest.raises(RuntimeError, match="missing required slots"):
        run(fake, [{"role": "user", "content": "leave approval"}])


def test_run_self_heals_when_a_retry_completes_the_model():
    # The completeness check rides the existing retry loop: a first incomplete reply nudges the model,
    # and a complete reply on the next attempt is accepted. This is why the invariant is safe to
    # enforce on a non-deterministic model — an omission is corrected, not fatal.
    incomplete = json.dumps({
        "model": {"problem": slot(80, "explicit", "high")},
        "questions": [], "summary": {"objective": "o"},
    })
    fake = FakeClient(incomplete, _ENGINE_REPLY)  # 1st attempt short, 2nd complete
    result = run(fake, [{"role": "user", "content": "leave approval"}])
    assert result.model["problem"].completeness == 80
    assert len(fake.calls) == 2  # it took a retry
    # the nudge names the missing slots so the model knows what to add
    nudge = fake.calls[1]["messages"][-1]["content"]
    assert "missing required slots" in nudge


def test_generate_prd_from_saved_model_roundtrip(tmp_path):
    # The --from path: reload a saved model and regenerate an artifact, no discovery.
    model = out({"problem": slot(80, "explicit", "high")})
    path = tmp_path / "model.json"
    path.write_text(model.model_dump_json())

    loaded = load_model(path)
    assert loaded.model["problem"].completeness == 80

    prd = generate_prd(FakeClient(json.dumps({"title": "Leave approval"})), loaded)
    assert isinstance(prd, PRD) and prd.title == "Leave approval"
    md = prd_markdown(prd)
    assert md.startswith("# Leave approval")
    assert "generated by Product Copilot" in md


def test_derive_stories_returns_structured_stories():
    reply = json.dumps({"stories": [{"id": "S1", "title": "Submit a leave request"}]})
    stories = derive_stories(FakeClient(reply), out({"problem": slot(80, "explicit", "high")}))
    assert isinstance(stories, Stories)
    assert [s.id for s in stories.stories] == ["S1"]
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_stories(stories)
    text = buf.getvalue()
    assert "=== USER STORIES ===" in text and "[S1] Submit a leave request" in text


def test_artifact_paths_and_names():
    # Guards R1: save_model / write_artifact resolve to out/<slug>/<file> and round-trip.
    p = save_model(out({"problem": slot(80, "explicit", "high")}), "_chartest_slug")
    try:
        assert p.name == "model.json"
        assert p.parent.name == "_chartest_slug"
        assert p.parent.parent.name == "out"
        a = write_artifact("_chartest_slug", "prd.md", "# X\n")
        assert a.parent == p.parent and a.name == "prd.md"
        assert a.read_text() == "# X\n"
        assert load_model(p).model["problem"].completeness == 80
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
    p = save_model(out({"problem": slot(80, "explicit", "high")}), slug)
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
        ("impact", "m.json"): "_cmd_impact",
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
        assert (folder / "request.txt").exists()  # saved so `pc answer` can resume
    finally:
        shutil.rmtree(folder, ignore_errors=True)


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


def test_pc_discover_rejects_empty_request():
    # An empty/whitespace request should fail fast with a clear message, not crash or fire an
    # empty-content API call. The FakeClient would raise if reached — SystemExit means we never did.
    for blank in ("", "   "):
        with pytest.raises(SystemExit):
            _run_app(["discover", blank], client=FakeClient(_ENGINE_REPLY))


def test_pc_answer_refines_the_model():
    # A stateless discovery turn: answers + the current model → a refined model.
    with _model_in_out("_clitest_answer") as p:
        (p.parent / "request.txt").write_text("original leave-approval request")
        turn2 = json.dumps({
            "model": full_slots(problem=slot(95, "explicit", "high")),
            "questions": [],
            "summary": {},
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


# ── Tier 2: the dependency DAG (impact propagation) ───────────────────────────
# Pure logic — no API. A change to a slot propagates to the decisions that rest on
# it and the artifacts that consume it.


def _out_with_decisions(*decisions):
    return EngineOutput.model_validate({
        "model": {
            "current_process": slot(80, "explicit", "high"),
            "permissions": slot(60, "inferred", "high"),
            "workflow": slot(70, "inferred", "high"),
            "business_objects": slot(50, "inferred", "medium"),
        },
        "questions": [], "summary": {},
        "decisions": [d.model_dump() for d in decisions],
    })


def test_propagate_flags_dependent_decisions_and_artifacts():
    out_ = _out_with_decisions(
        DesignDecision(decision="Draft-first invoices reviewed by Finance",
                       derived_from=["permissions", "workflow"]),
        DesignDecision(decision="Amount sourced from the Contract",
                       derived_from=["business_objects"]),
    )
    rep = propagate(out_, ["permissions"])
    # only the decision resting on permissions, and it names the changed slot it rests on
    assert [d.decision for d in rep.decisions] == ["Draft-first invoices reviewed by Finance"]
    assert rep.decisions[0].rests_on == ["Permissions"]
    # artifacts consuming permissions go stale; release (no permissions) does not
    assert "prd" in rep.artifacts and "criteria" in rep.artifacts
    assert "release" not in rep.artifacts
    assert not rep.empty


def test_propagate_is_empty_for_an_isolated_slot():
    # current_process feeds no buildable artifact and no decision rests on it → safe in isolation
    rep = propagate(_out_with_decisions(), ["current_process"])
    assert rep.empty


def test_resolve_slots_accepts_ids_and_label_words_and_flags_unknowns():
    assert resolve_slots(["permissions"]) == (["permissions"], [])
    assert resolve_slots(["permission"]) == (["permissions"], [])  # label substring
    ids, unmatched = resolve_slots(["workflow", "zzz"])
    assert ids == ["workflow"] and unmatched == ["zzz"]
    # returned in schema order regardless of input order
    assert resolve_slots(["risks", "problem"])[0] == ["problem", "risks"]


def test_diff_models_flags_material_change_but_ignores_completeness_noise():
    base = {"workflow": {"completeness": 60, "confidence": "inferred", "impact": "high",
                         "value": "draft → issued", "evidence": ""}}
    old = EngineOutput.model_validate({"model": base, "questions": [], "summary": {}})
    # completeness alone moving is not a material change
    bumped = json.loads(json.dumps(base))
    bumped["workflow"]["completeness"] = 90
    same = EngineOutput.model_validate({"model": bumped, "questions": [], "summary": {}})
    assert diff_models(old, same) == []
    # a value change is
    changed = json.loads(json.dumps(base))
    changed["workflow"]["value"] = "draft → issued → paid"
    newv = EngineOutput.model_validate({"model": changed, "questions": [], "summary": {}})
    assert diff_models(old, newv) == ["workflow"]


def test_diff_models_flags_a_removed_slot():
    # A slot present before and gone after must register as a change — otherwise a decision or artifact
    # resting on it could go stale silently. (In practice the completeness invariant prevents a real
    # discovery from dropping a slot, but the diff must not depend on that upstream guarantee.)
    both = {"workflow": {"completeness": 60, "confidence": "inferred", "impact": "high",
                         "value": "draft → issued", "evidence": ""},
            "permissions": {"completeness": 70, "confidence": "explicit", "impact": "high",
                            "value": "HR only", "evidence": ""}}
    old = EngineOutput.model_validate({"model": both, "questions": [], "summary": {}})
    dropped = {"workflow": both["workflow"]}  # permissions removed
    new = EngineOutput.model_validate({"model": dropped, "questions": [], "summary": {}})
    assert diff_models(old, new) == ["permissions"]


def test_artifact_slots_reference_only_real_slot_ids():
    from product_copilot.core.analysis import _slot_meta
    valid = set(_slot_meta()[1])
    for name, slots in artifact_slots().items():
        assert slots <= valid, f"{name} references unknown slot ids: {slots - valid}"


def test_pc_impact_reports_blast_radius_offline():
    # No client needed — impact is a pure DAG query.
    with _model_in_out("_clitest_impact") as p:
        out_ = _out_with_decisions(
            DesignDecision(decision="Draft-first invoices", derived_from=["permissions"]))
        save_model(out_, p.parent.name)
        text = _run_app(["impact", str(p), "permissions"])
        assert "Draft-first invoices" in text and "prd" in text


def test_pc_impact_no_slots_prints_the_full_map():
    with _model_in_out("_clitest_impact_map") as p:
        save_model(_out_with_decisions(), p.parent.name)
        text = _run_app(["impact", str(p)])
        assert "DEPENDENCY MAP" in text


# ── Tier 2 (B): change-detection — stale artifacts on disk ────────────────────


def test_stale_on_disk_only_flags_present_files_that_consume_a_changed_slot():
    from product_copilot.core.dependencies import stale_on_disk
    out_ = _out_with_decisions()
    # workflow is consumed by prd, stories, estimate, criteria, epic, release
    pairs = stale_on_disk(out_, ["workflow"], present={"prd.md", "epic.md", "model.json"})
    names = {n for n, _f in pairs}
    assert names == {"prd", "epic"}  # only the ones with a file present
    # stories consumes workflow but has no file → never flagged even if "present"
    assert "stories" not in names


def test_pc_answer_warns_when_a_turn_makes_a_generated_artifact_stale():
    with _model_in_out("_clitest_stale") as p:
        # a real slot an artifact consumes, and an already-generated PRD on disk
        wf = {**slot(60, "inferred", "high"), "value": "draft → issued"}
        save_model(out({"workflow": wf}), p.parent.name)
        (p.parent / "request.txt").write_text("original request")
        (p.parent / "prd.md").write_text("# stale PRD")
        turn2 = json.dumps({
            "model": full_slots(workflow={"completeness": 95, "confidence": "explicit",
                                          "impact": "high", "value": "draft → issued → paid → archived"}),
            "questions": [], "summary": {},
        })
        text = _run_app(["answer", str(p), "It also has an archived state."],
                        client=FakeClient(turn2))
        assert "STALE" in text and "prd.md" in text
        assert "Workflow" in text  # the changed slot is named in the warning
