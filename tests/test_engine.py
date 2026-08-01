"""Unit tests for the pure logic — the parts that must be correct without an API call."""
import io
import json
import shutil
from contextlib import contextmanager, redirect_stdout
from datetime import date as _date

import anthropic
import httpx
import pytest
from pydantic import ValidationError

from requivo.cli import _build_parser, _is_file_arg, app
from requivo.core import persistence as store
from requivo.core.dependencies import artifact_slots, diff_models, propagate, resolve_slots
from requivo.core.persistence import _atomic_write, load_session, save_session, session_cards
from requivo.paths import output_root
from requivo.providers.anthropic import EngineError, _complete, _response_text, advise, answer_turn, current_model_name
from requivo.services.artifacts import ArtifactService
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


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    """Every test in this module writes sessions/artifacts into an isolated temp workspace, never the
    real repo. Points both the canonical root (.requivo/sessions) and the legacy root (out/) at tmp."""
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))


def slot(completeness, confidence, impact):
    return {"completeness": completeness, "confidence": confidence, "impact": impact}


def full_slots(**overrides):
    """A complete required-slot model (every required slot present, empty/low by default) with
    per-slot overrides — mirrors what a real discovery turn emits, so models that go through run()
    satisfy the completeness invariant."""
    from requivo.core.contracts import _schema_order, schema_slot_ids

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


def test_output_rejects_a_decision_derived_from_an_unknown_slot():
    # A DAG edge into a slot the schema doesn't define would make the dependency graph look rigorous
    # while pointing at nothing — rejected at the contract, same as an unknown slot in the model.
    with pytest.raises(ValidationError):
        EngineOutput.model_validate({
            "model": {"workflow": slot(60, "inferred", "high")},
            "questions": [], "summary": {},
            "decisions": [{"decision": "X", "derived_from": ["not_a_slot"]}],
        })


def test_output_rejects_a_challenge_contesting_an_unknown_slot():
    with pytest.raises(ValidationError):
        EngineOutput.model_validate({
            "model": {"workflow": slot(60, "inferred", "high")},
            "questions": [], "summary": {},
            "challenges": [{"headline": "h", "premise": "p", "alternative": "a", "consequence": "c",
                            "recommendation": "r", "contests": ["not_a_slot"]}],
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
    from requivo.core.contracts import schema_slot_ids

    _, required = schema_slot_ids()
    model_dict = {sid: slot(90, "explicit", "high") for sid in required}
    del model_dict["business_rules"]  # a high-impact dimension goes missing
    model = EngineOutput.model_validate({"model": model_dict, "questions": [], "summary": {}})
    # business_rules is absent, high-impact by default → it is still a blocker, not invisible.
    assert "business_rules" in _readiness_blockers(model)


def test_readiness_blocks_a_thin_high_impact_slot_even_when_explicit():
    # Provenance is not coverage: a high-impact slot stated in one word (completeness below the soft
    # boundary) is NOT resolved, even if its confidence is explicit — it must still block, not read as
    # confirmed. Guards the readiness fix that gates on completeness, not confidence alone.
    model = out({
        "business_rules": slot(5, "explicit", "high"),   # explicit but thin → still a blocker
        "problem": slot(90, "explicit", "high"),         # explicit AND covered → not blocking
    })
    blockers = _readiness_blockers(model)
    assert "business_rules" in blockers
    assert "problem" not in blockers


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
    assert payload["format"] == "requivo-epic" and payload["version"] == 1
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
    label = "requivo-epic:leave-approval"
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
    label = "requivo-epic:leave-approval"
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
    assert "generated by Requivo" in md


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
    p = save_model(out({"problem": slot(80, "explicit", "high")}), "chartest-slug")
    try:
        assert p.name == "model.json"
        assert p.parent.name == "chartest-slug"
        assert p.parent.parent.name == "out"
        a = write_artifact("chartest-slug", "prd.md", "# X\n")
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
    # load_context reads the CONTEXT anchor from its own module (core.context); point it at an empty dir,
    # and point the user-cards dir at a nonexistent path so the machine's real one can't leak in.
    from requivo.core import context as llm

    ctx_dir = tmp_path / "context"
    ctx_dir.mkdir()
    (ctx_dir / "_only_template.md").write_text("skip me")
    monkeypatch.setattr(llm, "CONTEXT", ctx_dir)
    monkeypatch.setenv("REQUIVO_CONTEXT_DIR", str(tmp_path / "no-user-cards"))
    assert load_context() == ""


def test_user_context_cards_merge_and_override_bundled(tmp_path, monkeypatch):
    # A pip-installed user extends discovery by dropping cards in REQUIVO_CONTEXT_DIR: new stems are added,
    # and a stem matching a bundled card overrides it (tweak a built-in without editing the package).
    from requivo.core import context as llm

    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "b2b-platform.md").write_text("BUNDLED b2b")
    (bundled / "shared.md").write_text("BUNDLED shared")
    monkeypatch.setattr(llm, "CONTEXT", bundled)

    user = tmp_path / "user"
    user.mkdir()
    (user / "my-product.md").write_text("USER product")
    (user / "shared.md").write_text("USER shared override")
    monkeypatch.setenv("REQUIVO_CONTEXT_DIR", str(user))

    assert llm.available_cards() == ["b2b-platform", "my-product", "shared"]  # merged, sorted
    ctx = load_context()
    assert "BUNDLED b2b" in ctx            # bundled-only card kept
    assert "USER product" in ctx           # user-only card added
    assert "USER shared override" in ctx   # user card wins on stem clash
    assert "BUNDLED shared" not in ctx      # ...replacing the bundled version


# ── The `pc` subcommand CLI ───────────────────────────────────────────────────
# The modern surface is a thin layer over the same core; app() takes an injected
# client so API-backed verbs run offline against a FakeClient.


@contextmanager
def _model_in_out(slug):
    """A canonical .requivo/sessions/<slug>/ session with a model the subcommands can load and mutate.
    Yields the path to model.json (the subcommands accept a slug OR a model.json path)."""
    store.create_session(slug, f"request for {slug}")
    store.save_revision(slug, out({"problem": slot(80, "explicit", "high")}))
    p = store.canonical_dir(slug) / "model.json"
    try:
        yield p
    finally:
        shutil.rmtree(store.canonical_dir(slug), ignore_errors=True)


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


def test_documented_cli_commands_exist():
    # Guard doc/CLI drift: every top-level command the README and docs/cli.md promise must be a real
    # subcommand, so a rename or removal can't leave the docs pointing at a command that doesn't exist.
    import argparse

    parser = _build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    documented = {"discover", "answer", "status", "impact", "brief", "prd", "stories", "estimate",
                  "criteria", "epic", "release", "web", "demo", "doctor", "schema", "context",
                  "session", "model", "artifact"}
    missing = documented - set(sub.choices)
    assert not missing, f"documented CLI commands missing from the parser: {sorted(missing)}"


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
        assert f.read_text() == (browsable / f.name).read_text(), f"demo payload drifted from examples/: {f.name}"


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


def test_pc_prd_writes_artifact():
    with _model_in_out("clitest-prd") as p:
        _run_app(["prd", str(p)], client=FakeClient(json.dumps({"title": "X"})))
        assert (p.parent / "artifacts" / "prd.md").read_text().startswith("# X")


def test_pc_criteria_writes_artifact():
    with _model_in_out("clitest-criteria") as p:
        _run_app(["criteria", str(p)], client=FakeClient(json.dumps({"title": "X"})))
        assert (p.parent / "artifacts" / "acceptance-criteria.md").exists()


def test_pc_epic_writes_all_views():
    with _model_in_out("clitest-epic") as p:
        _run_app(["epic", str(p), "--json", "--github", "--gitlab"], client=FakeClient(json.dumps({"title": "X"})))
        for name in ("epic.md", "epic.json", "epic.github.json", "epic.gitlab.json"):
            assert (p.parent / "artifacts" / name).exists()


def test_pc_release_stamps_version():
    with _model_in_out("clitest-release") as p:
        _run_app(["release", str(p), "v1.0"], client=FakeClient(json.dumps({"title": "X"})))
        assert "v1.0" in (p.parent / "artifacts" / "release-notes.md").read_text()


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


def test_propagate_reaches_only_the_assessment_for_an_otherwise_isolated_slot():
    # current_process feeds no buildable deliverable and no decision rests on it. The solution
    # assessment is the exception by design: it is a judgment over the whole model, so it rests on
    # every slot — describing the as-is process differently does change the assessment on disk.
    rep = propagate(_out_with_decisions(), ["current_process"])
    assert rep.artifacts == ["brief"]
    assert not rep.decisions and not rep.challenges and not rep.empty


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
    from requivo.core.analysis import _slot_meta
    valid = set(_slot_meta()[1])
    for name, slots in artifact_slots().items():
        assert slots <= valid, f"{name} references unknown slot ids: {slots - valid}"


def test_pc_impact_reports_blast_radius_offline():
    # No client needed — impact is a pure DAG query.
    with _model_in_out("clitest-impact") as p:
        out_ = _out_with_decisions(
            DesignDecision(decision="Draft-first invoices", derived_from=["permissions"]))
        store.save_revision(p.parent.name, out_)
        text = _run_app(["impact", str(p), "permissions"])
        assert "Draft-first invoices" in text and "prd" in text


def test_pc_impact_no_slots_prints_the_full_map():
    with _model_in_out("clitest-impact-map") as p:
        store.save_revision(p.parent.name, _out_with_decisions())
        text = _run_app(["impact", str(p)])
        assert "DEPENDENCY MAP" in text


# ── Tier 2 (B): change-detection — stale artifacts on disk ────────────────────


def test_stale_on_disk_only_flags_present_files_that_consume_a_changed_slot():
    from requivo.core.dependencies import stale_on_disk
    out_ = _out_with_decisions()
    # workflow is consumed by prd, stories, estimate, criteria, epic, release
    pairs = stale_on_disk(out_, ["workflow"], present={"prd.md", "epic.md", "model.json"})
    names = {n for n, _f in pairs}
    assert names == {"prd", "epic"}  # only the ones with a file present
    # stories consumes workflow but has no file → never flagged even if "present"
    assert "stories" not in names


def test_unrelated_slot_change_keeps_artifact_fresh():
    # The freshness fix: an artifact goes stale only when the change reaches a slot it consumes — not
    # on every revision bump. criteria consumes {workflow, business_rules, permissions, edge_cases,
    # acceptance}; success_metrics is outside that set, so a material change to it leaves criteria fresh.
    from requivo.services.sessions import SessionService
    svc = SessionService()
    slug = "clitest-fresh-unrelated"
    store.create_session(slug, "req")
    svc.update_model(slug, out({"success_metrics": slot(40, "inferred", "high")}).model_dump())
    ArtifactService().save(slug, "criteria", "# criteria")  # generated at revision 1
    try:
        # confidence moves inferred → explicit on an UNRELATED slot: a real change, new revision.
        svc.update_model(slug, out({"success_metrics": slot(90, "explicit", "high")}).model_dump())
        items = ArtifactService().list(slug)
        assert items["criteria"]["stale"] is False        # revision advanced, but criteria is untouched
    finally:
        shutil.rmtree(store.canonical_dir(slug), ignore_errors=True)


def test_completeness_only_change_keeps_artifact_fresh():
    # A completeness-only bump on a CONSUMED slot is not a material change (diff_models ignores
    # completeness), so it must not invalidate the artifact — completeness is progress noise, not signal.
    from requivo.services.sessions import SessionService
    svc = SessionService()
    slug = "clitest-fresh-completeness"
    store.create_session(slug, "req")
    svc.update_model(slug, out({"workflow": slot(50, "explicit", "high")}).model_dump())
    ArtifactService().save(slug, "criteria", "# criteria")  # criteria consumes workflow
    try:
        svc.update_model(slug, out({"workflow": slot(95, "explicit", "high")}).model_dump())  # only %
        items = ArtifactService().list(slug)
        assert items["criteria"]["stale"] is False
    finally:
        shutil.rmtree(store.canonical_dir(slug), ignore_errors=True)


def test_related_slot_change_marks_artifact_stale():
    # The other side: a material change to a slot the artifact DOES consume flags it stale.
    from requivo.services.sessions import SessionService
    svc = SessionService()
    slug = "clitest-stale-related"
    store.create_session(slug, "req")
    svc.update_model(slug, out({"workflow": slot(50, "inferred", "high")}).model_dump())
    ArtifactService().save(slug, "criteria", "# criteria")  # criteria consumes workflow
    try:
        wf = {**slot(95, "explicit", "high"), "value": "draft → issued → archived"}
        svc.update_model(slug, out({"workflow": wf}).model_dump())
        items = ArtifactService().list(slug)
        assert items["criteria"]["stale"] is True
    finally:
        shutil.rmtree(store.canonical_dir(slug), ignore_errors=True)


def test_first_apply_does_not_invalidate_its_own_reasoning():
    # A first apply of a model that already carries decisions/challenges must NOT report them as
    # invalidated: they were proposed FOR this state, there is no prior reasoning to unseat. The old
    # code propagated over `new` when there was no `current`, flagging a model's own reasoning stale.
    from requivo.services.sessions import SessionService
    svc = SessionService()
    slug = "clitest-first-apply"
    store.create_session(slug, "req")
    model = EngineOutput.model_validate({
        "model": full_slots(workflow=slot(80, "explicit", "high"),
                            permissions=slot(75, "explicit", "high")),
        "questions": [], "summary": {},
        "decisions": [DesignDecision(decision="Draft-first invoices reviewed by Finance",
                                     derived_from=["workflow", "permissions"]).model_dump()],
        "challenges": [Challenge(headline="Invoice at signature", premise="p", alternative="a",
                                 consequence="c", recommendation="r",
                                 contests=["workflow"]).model_dump()],
    })
    try:
        result = svc.update_model(slug, model.model_dump())
        assert result.invalidated_decisions == []      # its own reasoning is fresh, not stale
        assert result.invalidated_challenges == []
    finally:
        shutil.rmtree(store.canonical_dir(slug), ignore_errors=True)


def test_second_apply_invalidates_prior_reasoning_a_change_unseats():
    # The other side: once reasoning is established, a later change that reaches a slot it rests on
    # DOES invalidate it — the behaviour the first-apply guard must not suppress.
    from requivo.services.sessions import SessionService
    svc = SessionService()
    slug = "clitest-second-apply"
    store.create_session(slug, "req")
    first = EngineOutput.model_validate({
        "model": full_slots(workflow=slot(80, "inferred", "high")),
        "questions": [], "summary": {},
        "decisions": [DesignDecision(decision="Draft-first invoices reviewed by Finance",
                                     derived_from=["workflow"]).model_dump()],
    })
    svc.update_model(slug, first.model_dump())
    try:
        # a material change to workflow, and the refinement turn drops the decision from its reply
        second = out({"workflow": {**slot(95, "explicit", "high"), "value": "draft → issued → archived"}})
        result = svc.update_model(slug, second.model_dump())
        assert "Draft-first invoices reviewed by Finance" in result.invalidated_decisions
    finally:
        shutil.rmtree(store.canonical_dir(slug), ignore_errors=True)


def test_expected_revision_precondition_blocks_a_stale_write():
    # Optimistic locking: a writer that expects an out-of-date revision is rejected rather than landing
    # silently on top of another update — the guarantee a concurrent Web service needs.
    from requivo.core.errors import RevisionConflictError
    from requivo.services.sessions import SessionService
    svc = SessionService()
    slug = "clitest-lock"
    store.create_session(slug, "req")
    svc.update_model(slug, out({"workflow": slot(60, "inferred", "high")}).model_dump())  # → revision 1
    try:
        with pytest.raises(RevisionConflictError):   # a racer still thinks it is at revision 0
            svc.update_model(slug, out({"workflow": slot(80, "explicit", "high")}).model_dump(),
                             expected_revision=0)
        r = svc.update_model(slug, out({"workflow": slot(80, "explicit", "high")}).model_dump(),
                             expected_revision=1)     # the right expectation applies cleanly
        assert r.revision == 2
    finally:
        shutil.rmtree(store.canonical_dir(slug), ignore_errors=True)


def test_each_revision_records_its_provenance():
    # Provenance is per-revision: a session's model is moved by more than one surface over its life, so
    # each revision records who produced it, what it succeeded, and a content hash.
    from requivo.services.sessions import SessionService
    svc = SessionService()
    slug = "clitest-provenance"
    store.create_session(slug, "req")
    svc.update_model(slug, out({"workflow": slot(60, "inferred", "high")}).model_dump(),
                     provenance={"provider": "anthropic", "surface": "cli-discover", "model_name": "claude-x"})
    svc.update_model(slug, out({"workflow": {**slot(90, "explicit", "high"), "value": "a → b"}}).model_dump(),
                     provenance={"provider": "claude-code", "surface": "cli-apply"})
    try:
        revs = store.read_meta(slug).revisions
        assert [r.revision for r in revs] == [1, 2]
        assert revs[0].previous_revision is None and revs[1].previous_revision == 1
        assert revs[0].surface == "cli-discover" and revs[0].provider == "anthropic"
        assert revs[1].surface == "cli-apply" and revs[1].provider == "claude-code"
        assert all(r.model_hash.startswith("sha256:") for r in revs)
    finally:
        shutil.rmtree(store.canonical_dir(slug), ignore_errors=True)


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


def test_pc_answer_warns_when_a_turn_makes_a_generated_artifact_stale():
    with _model_in_out("clitest-stale") as p:
        slug = p.parent.name
        # a real slot an artifact consumes, and an already-generated PRD tracked in the session
        wf = {**slot(60, "inferred", "high"), "value": "draft → issued"}
        store.save_revision(slug, out({"workflow": wf}))
        ArtifactService().save(slug, "prd", "# stale PRD")  # generated at the current revision
        turn2 = json.dumps({
            "model": full_slots(workflow={"completeness": 95, "confidence": "explicit",
                                          "impact": "high", "value": "draft → issued → paid → archived"}),
            "questions": [], "summary": {},
        })
        text = _run_app(["answer", str(p), "It also has an archived state."],
                        client=FakeClient(turn2))
        assert "STALE" in text and "prd.md" in text
        assert "Workflow" in text  # the changed slot is named in the warning


# ── Tier 3: API usage tracking (tokens / cost / latency) ──────────────────────
# Tokens are ground truth from the response; cost is a labelled estimate. The ledger accumulates
# per-call usage; the renderer turns it into a line; the CLI prints it after an API-backed command.

from requivo.providers.anthropic import CallRecord, UsageLedger, track_usage  # noqa: E402
from requivo.render.terminal import render_usage  # noqa: E402


class _FakeUsage:
    def __init__(self, i, o, cache_read=0, cache_write=0):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_read_input_tokens = cache_read
        self.cache_creation_input_tokens = cache_write


class _UsageResponse:
    def __init__(self, text, usage):
        self.content = [_FakeBlock(text)]
        self.usage = usage


class UsageFakeClient:
    """Like FakeClient but every reply carries a usage object, so `_complete` records real numbers."""

    def __init__(self, text, usage):
        self._text, self._usage = text, usage
        self.messages = self
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _UsageResponse(self._text, self._usage)


def test_usage_ledger_totals_and_cost():
    ledger = UsageLedger()
    ledger.record(CallRecord(model="claude-sonnet-5", input_tokens=1_000_000))
    ledger.record(CallRecord(model="claude-sonnet-5", output_tokens=1_000_000))
    assert ledger.input_tokens == 1_000_000 and ledger.output_tokens == 1_000_000
    # Standard rates: 1M input @ $3 + 1M output @ $15 = $18.00. Pinned to a day after the launch
    # window so the assertion states one rate rather than whichever is live when the suite runs.
    assert abs(ledger.cost_usd(on=_date(2026, 9, 1)) - 18.0) < 1e-6


def test_usage_ledger_applies_launch_pricing_until_it_lapses():
    # Sonnet 5 runs on launch pricing ($2/$10) through 2026-08-31, then reverts to $3/$15. A dated
    # table with no expiry gets exactly one of those two days right.
    ledger = UsageLedger()
    ledger.record(CallRecord(model="claude-sonnet-5", input_tokens=1_000_000, output_tokens=1_000_000))
    assert abs(ledger.cost_usd(on=_date(2026, 8, 31)) - 12.0) < 1e-6   # launch: 2 + 10
    assert abs(ledger.cost_usd(on=_date(2026, 9, 1)) - 18.0) < 1e-6    # standard: 3 + 15


def test_usage_ledger_cost_counts_cache_tiers():
    ledger = UsageLedger()
    # cache read ≈ 0.1× input rate, cache write ≈ 1.25× input rate (Sonnet standard input $3/Mtok)
    ledger.record(CallRecord(model="claude-sonnet-5", cache_read_tokens=1_000_000,
                             cache_write_tokens=1_000_000))
    assert abs(ledger.cost_usd(on=_date(2026, 9, 1)) - (0.3 + 3.75)) < 1e-6


def test_usage_ledger_cost_is_none_for_unpriced_model():
    ledger = UsageLedger()
    ledger.record(CallRecord(model="some-future-model", input_tokens=10))
    assert ledger.cost_usd() is None


def test_render_usage_shows_tokens_cache_latency_and_estimate():
    ledger = UsageLedger()
    ledger.record(CallRecord(model="claude-sonnet-5", input_tokens=1000, output_tokens=200,
                             cache_read_tokens=500, latency_ms=1500))
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_usage(ledger)
    text = buf.getvalue()
    assert "API USAGE" in text
    assert "1,500 tokens" in text          # processed = input + cache_read = 1000 + 500
    assert "500 served from cache" in text
    assert "1.5 s" in text
    assert "Est. cost" in text and "estimate" in text


def test_render_usage_silent_without_tokens():
    # No call, or usage absent (offline fake) → nothing printed.
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_usage(UsageLedger())                                   # empty
        render_usage(UsageLedger(calls=[CallRecord(model="claude-sonnet-5")]))  # a call, zero tokens
    assert buf.getvalue() == ""


def test_render_usage_flags_unpriced_model_but_keeps_tokens():
    ledger = UsageLedger()
    ledger.record(CallRecord(model="some-future-model", input_tokens=1000, output_tokens=200))
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_usage(ledger)
    text = buf.getvalue()
    assert "1,200 tokens" in text or "1,000 tokens" in text  # tokens still reported
    assert "no price on file" in text                         # cost honestly withheld


def test_complete_records_usage_into_the_active_ledger():
    # run() → _complete records one CallRecord, summing the response's usage fields.
    client = UsageFakeClient(_ENGINE_REPLY, _FakeUsage(1000, 200, cache_read=500))
    with track_usage() as ledger:
        run(client, [{"role": "user", "content": "leave approval"}])
    assert len(ledger.calls) == 1
    assert ledger.input_tokens == 1000 and ledger.output_tokens == 200
    assert ledger.cache_read_tokens == 500
    assert ledger.cost_usd() is not None


def test_pc_status_reports_no_usage_offline():
    # An offline verb makes no call → no ledger records → no usage line.
    with _model_in_out("clitest-usage") as p:
        text = _run_app(["status", str(p)])
    assert "API USAGE" not in text


# ── Tier 4: finishing polish (slug collisions, table escaping, card selection) ─


def test_resolve_slug_avoids_silent_overwrite():
    from requivo.core.persistence import resolve_slug, save_request

    base = "slugtest-collide"
    folder = output_root() /  base
    try:
        assert resolve_slug(base, "first request") == base          # free → clean slug
        save_request(base, "first request")                         # base now owned by this request
        assert resolve_slug(base, "first request") == base          # same request re-run → reuse
        suffixed = resolve_slug(base, "a different request entirely")
        assert suffixed != base and suffixed.startswith(base + "-")  # collision → hash suffix
        assert resolve_slug(base, "a different request entirely") == suffixed  # deterministic
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_prd_markdown_escapes_pipes_in_table_cells():
    # A requirement containing a literal | would otherwise split the Markdown table row.
    prd = PRD(title="X", requirements=[
        {"id": "FR-1", "requirement": "Export as CSV | XLSX | PDF", "priority": "must"}])
    md = prd_markdown(prd)
    assert "| FR-1 | Export as CSV \\| XLSX \\| PDF | Must |" in md


def test_available_cards_lists_real_non_underscore_cards():
    from requivo.core.context import available_cards

    cards = available_cards()
    assert "b2b-platform" in cards and "financial-reporting" in cards
    assert all(not c.startswith("_") for c in cards)


def test_load_context_only_filters_to_selected_cards():
    from requivo.core.context import load_context

    ctx = load_context(only=["b2b-platform"])
    assert "## b2b-platform" in ctx
    assert "## financial-reporting" not in ctx  # a non-selected card is excluded
    assert load_context() != ctx                # default still loads everything


def test_run_restricts_context_cards_when_only_given():
    # The --context selection threads run() → build_prompt() → load_context(): the assembled system
    # carries only the chosen card, so it can't dilute impact estimation with the others.
    fake = FakeClient(_ENGINE_REPLY)
    run(fake, [{"role": "user", "content": "leave approval"}], only=["b2b-platform"])
    system = fake.calls[0]["system"][0]["text"]
    assert "## b2b-platform" in system
    assert "## financial-reporting" not in system


def test_resolve_cards_maps_stems_and_flags_unknown():
    from requivo.cli import _resolve_cards

    picked, unknown = _resolve_cards("b2b-platform, nope, financial-reporting")
    assert picked == ["b2b-platform", "financial-reporting"]
    assert unknown == ["nope"]


# ── 0.6.1: durable writes, provenance, API-boundary robustness, context continuity ─


def test_atomic_write_persists_content_and_leaves_no_tmp(tmp_path):
    dest = tmp_path / "model.json"
    _atomic_write(dest, '{"ok": true}')
    assert dest.read_text() == '{"ok": true}'
    # The temp sidecar is renamed onto the target, never left behind.
    assert not (tmp_path / ".model.json.tmp").exists()
    assert list(tmp_path.iterdir()) == [dest]


def test_session_roundtrips_provenance_and_cards():
    slug = "clitest-session"
    p = save_model(out({"problem": slot(80, "explicit", "high")}), slug)
    try:
        save_session(slug, request="build me a thing", model_name="claude-sonnet-5",
                     context_cards=["financial-reporting"])
        session = load_session(p)
        assert session["requivo_version"]           # stamped from the package version
        assert session["model_name"] == "claude-sonnet-5"
        assert session["context_cards"] == ["financial-reporting"]
        assert len(session["request_sha256"]) == 64          # a real sha256 hex digest
        assert session_cards(p) == ["financial-reporting"]
    finally:
        shutil.rmtree(p.parent, ignore_errors=True)


def test_session_cards_is_none_without_a_session_file():
    # Pre-0.6.1 models have no session.json — readers must tolerate its absence and mean "all cards".
    slug = "clitest-nosession"
    p = save_model(out({"problem": slot(80, "explicit", "high")}), slug)
    try:
        assert load_session(p) == {}
        assert session_cards(p) is None
    finally:
        shutil.rmtree(p.parent, ignore_errors=True)


def test_response_text_concatenates_text_blocks_and_skips_others():
    class _Block:
        def __init__(self, type_, text=""):
            self.type = type_
            self.text = text

    class _Resp:
        content = [_Block("thinking", "IGNORE"), _Block("text", "abc"), _Block("text", "def")]

    assert _response_text(_Resp()) == "abcdef"


class _RaisingClient:
    """A client whose create() raises — to exercise the API-error boundary in _complete()."""

    def __init__(self, exc):
        self._exc = exc
        self.messages = self

    def create(self, **kwargs):
        raise self._exc


def test_complete_wraps_api_errors_as_a_clean_engine_error():
    exc = anthropic.APIConnectionError(message="boom", request=httpx.Request("POST", "https://api.anthropic.com"))
    with pytest.raises(EngineError) as ei:
        _complete(_RaisingClient(exc), "sys", [{"role": "user", "content": "x"}], EngineOutput)
    assert "not modified" in str(ei.value)  # the reassurance that nothing was written


class _MaxTokensClient:
    """Returns a reply flagged as cut off at the token ceiling (stop_reason == 'max_tokens'),
    carrying whatever text it is given — so we can exercise both the broken- and complete-JSON cases."""

    def __init__(self, text):
        self._text = text
        self.messages = self

    def create(self, **kwargs):
        text = self._text

        class _Resp:
            stop_reason = "max_tokens"
            content = [_FakeBlock(text)]
        return _Resp()


def test_complete_rejects_a_truncated_reply_that_fails_to_parse():
    # Genuine truncation: the JSON is cut off mid-object, so parsing fails and the ceiling is the
    # named cause — retrying at the same ceiling wouldn't help, so it fails fast and cleanly.
    client = _MaxTokensClient('{"model": {"problem":')
    with pytest.raises(EngineError) as ei:
        _complete(client, "sys", [{"role": "user", "content": "x"}], EngineOutput)
    assert "max_tokens" in str(ei.value)


def test_complete_accepts_a_max_tokens_reply_whose_json_is_complete():
    # Parse-first: rich discovery outputs run right against the ceiling and can be flagged max_tokens
    # while still carrying complete, valid JSON. That must succeed — not be rejected as truncated.
    complete = json.dumps({"model": full_slots(problem=slot(80, "explicit", "high")),
                           "questions": [], "summary": {}})
    result = _complete(_MaxTokensClient(complete), "sys", [{"role": "user", "content": "x"}], EngineOutput)
    assert result.model["problem"].completeness == 80


def test_answer_turn_threads_the_discovery_context_cards():
    # A refinement turn must reason over the same cards the original discovery used, not silently all.
    fake = FakeClient(_ENGINE_REPLY)
    answer_turn(fake, out({"problem": slot(80, "explicit", "high")}), "req", "answers",
                only=["event-ops"])
    system = fake.calls[0]["system"][0]["text"]
    assert "## event-ops" in system
    assert "## financial-reporting" not in system


def test_generators_thread_the_context_selection():
    # A generator grounds its artifact in the discovery's card subset, not the full set.
    fake = FakeClient(json.dumps({"complexity": "low", "solution": "S"}))
    advise(fake, out({"problem": slot(80, "explicit", "high")}), only=["financial-reporting"])
    system = fake.calls[0]["system"][0]["text"]
    assert "## financial-reporting" in system
    assert "## b2b-platform" not in system


def test_current_model_name_reads_env_override(monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    assert current_model_name() == "claude-sonnet-5"
    monkeypatch.setenv("MODEL", "claude-opus-4-8")
    assert current_model_name() == "claude-opus-4-8"


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


def test_console_scripts_alias_shares_one_entry_point():
    # `requivo` is primary; `pc` is a temporary alias. Both must map to the exact same entry point.
    from pathlib import Path

    import requivo

    repo_root = Path(requivo.__file__).resolve().parents[2]  # src/requivo/__init__.py → repo
    pyproject = (repo_root / "pyproject.toml").read_text()
    assert 'requivo = "requivo.cli:app"' in pyproject
    assert 'pc = "requivo.cli:app"' in pyproject


# ── SessionRepository: the storage seam (proves the service is backing-agnostic) ──
# The point of the seam is requivo-cloud: the same SessionService orchestration must run on a Postgres
# backing, not just files. This in-memory repository stands in for that non-file backing — if the
# service works against it with zero filesystem, the orchestration is genuinely storage-agnostic.
from requivo.core.errors import RevisionConflictError as _RevConflict  # noqa: E402
from requivo.core.errors import SessionNotFoundError as _NotFound  # noqa: E402
from requivo.core.persistence import ArtifactStatus, RevisionRecord, SessionMeta  # noqa: E402
from requivo.services.repository import SessionRepository  # noqa: E402


class InMemorySessionRepository:
    """A dict-backed SessionRepository — no filesystem, no `.requivo/` directory. A faithful stand-in
    for a Postgres backing (everything is mutation-backed, so has_meta == exists, ensure_writable is a
    no-op check)."""

    def __init__(self):
        self._meta: dict = {}
        self._model: dict = {}
        self._revs: dict = {}      # (slug, revision) → model, the history a file backing keeps on disk
        self._req: dict = {}
        self._art: dict = {}

    def exists(self, slug): return slug in self._meta
    def has_meta(self, slug): return slug in self._meta

    def ensure_writable(self, slug):
        if slug not in self._meta:
            raise _NotFound(f"no session '{slug}'", details={"slug": slug})

    def create(self, slug, request, *, provider=None, model_name=None, context_cards=None):
        meta = SessionMeta(session_id="mem-" + slug, slug=slug, created_at="t", updated_at="t",
                           provider=provider, model_name=model_name, context_cards=context_cards)
        self._meta[slug] = meta
        self._req[slug] = request
        self._art[slug] = {}
        return meta

    def read_meta(self, slug):
        if slug not in self._meta:
            raise _NotFound(f"no session '{slug}'", details={"slug": slug})
        return self._meta[slug]

    def write_meta(self, slug, meta): self._meta[slug] = meta
    def list_slugs(self): return sorted(self._meta)

    def load_model(self, slug):
        if slug not in self._model:
            raise _NotFound(f"no model '{slug}'", details={"slug": slug})
        return self._model[slug]

    def load_revision(self, slug, revision):
        if (slug, revision) not in self._revs:
            raise _NotFound(f"no revision {revision}", details={"slug": slug, "revision": revision})
        return self._revs[(slug, revision)]

    def save_revision(self, slug, model, *, expected_revision=None, provenance=None):
        meta = self.read_meta(slug)
        if expected_revision is not None and meta.current_revision != expected_revision:
            raise _RevConflict("conflict", details={"expected": expected_revision,
                                                    "actual": meta.current_revision})
        rev = meta.current_revision + 1
        prov = dict(provenance or {})
        meta.revisions.append(RevisionRecord(
            revision=rev, created_at="t", previous_revision=meta.current_revision or None,
            model_hash="sha256:mem", provider=prov.get("provider"), model_name=prov.get("model_name"),
            surface=prov.get("surface"), prompt_version=prov.get("prompt_version")))
        meta.current_revision = rev
        self._model[slug] = model
        self._revs[(slug, rev)] = model
        return rev, meta

    def request_text(self, slug): return self._req.get(slug, "")
    def context_cards(self, slug): return self._meta[slug].context_cards if slug in self._meta else None

    def save_artifact(self, slug, artifact_type, filename, content, *, source_revision):
        self._art[slug][filename] = content
        st = ArtifactStatus(revision=source_revision, filename=filename, updated_at="t", stale=False)
        self._meta[slug].artifact_status[artifact_type] = st
        return st

    def load_artifact(self, slug, filename): return self._art.get(slug, {}).get(filename)


def test_session_service_runs_unchanged_on_a_non_file_repository():
    from requivo.services.sessions import SessionService
    repo = InMemorySessionRepository()
    assert isinstance(repo, SessionRepository)          # satisfies the protocol (runtime-checkable)
    svc = SessionService(repo)

    svc.create_session("a leave request", slug="leave-mem")
    r1 = svc.update_model("leave-mem", out({"workflow": slot(60, "inferred", "high")}).model_dump(),
                          provenance={"provider": "anthropic", "surface": "cli-discover"})
    assert r1.revision == 1

    # artifact tracking + dependency-graph staleness, entirely in memory
    ArtifactService(repo).save("leave-mem", "criteria", "# c")   # criteria consumes workflow
    r2 = svc.update_model(
        "leave-mem", out({"workflow": {**slot(95, "explicit", "high"), "value": "a → b"}}).model_dump())
    assert r2.revision == 2
    assert ArtifactService(repo).list("leave-mem")["criteria"]["stale"] is True

    # optimistic locking is enforced by the backing, not the file layout
    with pytest.raises(_RevConflict):
        svc.update_model("leave-mem", out({"workflow": slot(95, "explicit", "high")}).model_dump(),
                         expected_revision=0)

    # the rich status projection needs no filesystem either
    st = svc.status("leave-mem")
    assert st["revision"] == 2 and "understanding" in st
    # provenance is recorded per revision on the non-file backing
    assert [rr.surface for rr in svc.meta("leave-mem").revisions] == ["cli-discover", None]
