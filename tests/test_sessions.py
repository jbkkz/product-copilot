"""Core + services tests for the versioned session store, validation, and the apply pipeline.

All offline — no API, no provider. A temp workspace is pointed at with REQUIVO_WORKSPACE so the
canonical `.requivo/sessions/` layout is exercised in isolation.
"""
from __future__ import annotations

import json

import pytest

from requivo.core import persistence as store
from requivo.core.contracts import EngineOutput, _schema_order, schema_slot_ids
from requivo.core.errors import MissingRequiredSlotError, RequivoError, SessionNotFoundError, UnknownSlotError
from requivo.core.validation import validate_proposal
from requivo.services.artifacts import ArtifactService
from requivo.services.sessions import SessionService


def _slot(completeness=0, confidence="empty", impact="low", value=""):
    return {"completeness": completeness, "confidence": confidence, "impact": impact, "value": value}


def _full_model(**overrides) -> dict:
    _, required = schema_slot_ids()
    model = {sid: _slot() for sid in _schema_order() if sid in required}
    model.update(overrides)
    # A complete model owes an objective as much as it owes its slots (see `completeness_gap`),
    # so the shared fixture carries one.
    return {"model": model, "questions": [], "summary": {"objective": "A leave approval system"}}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))  # isolate legacy root too
    return tmp_path


# ── validation ────────────────────────────────────────────────────────────────


def test_validate_accepts_a_complete_model():
    out = validate_proposal(_full_model())
    assert isinstance(out, EngineOutput)


def test_validate_rejects_unknown_slot():
    bad = _full_model()
    bad["model"]["not_a_real_slot"] = _slot()
    with pytest.raises(UnknownSlotError) as e:
        validate_proposal(bad)
    assert e.value.code == "unknown_slot"
    assert "not_a_real_slot" in e.value.details["slots"]


def test_validate_rejects_missing_required_slot():
    partial = _full_model()
    a_required = next(iter(partial["model"]))
    del partial["model"][a_required]
    with pytest.raises(MissingRequiredSlotError) as e:
        validate_proposal(partial)
    assert e.value.code == "missing_required_slot"
    assert a_required in e.value.details["slots"]


def test_validate_rejects_a_complete_model_with_no_objective():
    """Completeness is the full slot set *and* an objective. The provider's retry hook required both;
    the deterministic path required only the slots, so the same model was complete when Anthropic
    produced it and complete-enough when Claude Code applied it — and a session of fifteen filled
    slots with nothing naming what they are for renders as a blank heading in every view. Both
    boundaries now read the one definition (`completeness_gap`)."""
    from requivo.core.errors import InvalidModelError

    with pytest.raises(InvalidModelError) as e:
        validate_proposal({**_full_model(), "summary": {"objective": "   "}})
    assert e.value.path == "summary.objective"
    # A projection is a different claim — it never promised completeness in the first place.
    validate_proposal({**_full_model(), "summary": {}}, require_complete=False)


def test_validate_allows_partial_when_not_required():
    partial = _full_model()
    del partial["model"][next(iter(partial["model"]))]
    out = validate_proposal(partial, require_complete=False)  # no raise
    assert isinstance(out, EngineOutput)


def test_validate_rejects_non_json_string():
    with pytest.raises(RequivoError) as e:
        validate_proposal("{not json")
    assert e.value.code == "invalid_model"


def test_error_to_dict_is_serializable():
    err = UnknownSlotError("bad", path="model.x", details={"slots": ["x"]})
    d = err.to_dict()
    assert d == {"code": "unknown_slot", "message": "bad", "path": "model.x", "details": {"slots": ["x"]}}
    json.dumps(d)  # must round-trip


# ── store: revisions + artifacts ────────────────────────────────────────────────


def test_store_creates_session_and_revisions(workspace):
    store.create_session("s1", "Build a leave system.", provider="claude-code")
    assert store.read_meta("s1").current_revision == 0
    out = EngineOutput.model_validate(_full_model())
    rev1, _ = store.save_revision("s1", out)
    rev2, meta = store.save_revision("s1", out)
    assert (rev1, rev2, meta.current_revision) == (1, 2, 2)
    d = store.canonical_dir("s1")
    assert (d / "model.json").exists()
    assert (d / "revisions" / "0001-model.json").exists()
    assert (d / "revisions" / "0002-model.json").exists()
    assert store.list_session_slugs() == ["s1"]


def test_store_migrate_session_rejects_a_future_format(workspace):
    from requivo.core.errors import InvalidSessionError
    with pytest.raises(InvalidSessionError):
        store.migrate_session({"format_version": 999, "session_id": "x", "slug": "s",
                               "created_at": "t", "updated_at": "t"})


# ── services: the apply pipeline ─────────────────────────────────────────────────


def test_session_service_create_and_apply(workspace):
    svc = SessionService()
    meta = svc.create_session("Build a leave approval system.", slug="leave", provider="claude-code")
    assert meta.slug == "leave" and meta.current_revision == 0

    # A high-impact slot left unconfirmed must block readiness.
    result = svc.update_model("leave", _full_model(**{"problem": _slot(0, "empty", "high")}))
    assert result.status == "applied"
    assert result.revision == 1
    assert set(result.changed_slots)  # every slot present counts as changed on the first apply
    assert result.readiness.ready is False
    assert "problem" in result.readiness.blocking_slots


def test_apply_diff_reports_changed_slots_and_readiness(workspace):
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    # First model: everything empty.
    svc.update_model("s", _full_model())
    # Second: fill one slot explicitly → it should be the changed slot.
    changed_model = _full_model(**{"problem": _slot(90, "explicit", "high", "A real problem")})
    result = svc.update_model("s", changed_model)
    assert result.revision == 2
    assert "problem" in result.changed_slots


def test_diff_does_not_write(workspace):
    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())
    before = store.read_meta("s").current_revision
    plan = svc.diff("s", _full_model(**{"problem": _slot(90, "explicit", "high", "X")}))
    assert plan.status == "planned"
    assert store.read_meta("s").current_revision == before  # unchanged — no write


def test_apply_flags_generated_artifact_stale(workspace):
    svc = SessionService()
    art = ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())
    art.save("s", "prd", "# PRD\n")  # generated at revision 1
    assert art.list("s")["prd"]["stale"] is False
    # Change a slot the PRD consumes (workflow) → PRD goes stale.
    result = svc.update_model("s", _full_model(**{"workflow": _slot(80, "explicit", "high", "new flow")}))
    assert "prd" in result.stale_artifacts
    assert art.list("s")["prd"]["stale"] is True


def _with_reasoning(model: dict) -> dict:
    """A full model that also carries baked-in reasoning: a decision on `permissions`, a challenge
    contesting `workflow`."""
    model["decisions"] = [{"decision": "Draft-first", "derived_from": ["permissions"]}]
    model["challenges"] = [{
        "headline": "Archive vs delete", "premise": "p", "alternative": "a",
        "consequence": "c", "recommendation": "r", "contests": ["workflow"],
    }]
    return model


def test_propagate_reports_challenges_via_contests():
    from requivo.core.dependencies import propagate
    out = EngineOutput.model_validate(_with_reasoning(_full_model()))
    hit = propagate(out, ["workflow"])
    assert [c.headline for c in hit.challenges] == ["Archive vs delete"]
    assert hit.reasoning_hit is True
    # A change that touches neither derived_from nor contests unseats no reasoning.
    miss = propagate(out, ["success_metrics"])
    assert not miss.challenges and not miss.decisions and not miss.reasoning_hit


def test_apply_flags_assessment_stale_when_reasoning_is_unseated(workspace):
    svc = SessionService()
    art = ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _with_reasoning(_full_model()))
    art.save("s", "brief", "# Assessment\n")  # the saved assessment renders that reasoning
    assert art.list("s")["brief"]["stale"] is False

    # Change `workflow` — a challenge contests it → the assessment on disk no longer holds.
    changed = _with_reasoning(_full_model())
    changed["model"]["workflow"] = _slot(80, "explicit", "high", "new flow")
    result = svc.update_model("s", changed)
    assert "Archive vs delete" in result.invalidated_challenges
    assert "brief" in result.stale_artifacts
    assert art.list("s")["brief"]["stale"] is True
    # The decision (on `permissions`) was untouched, so it is not reported.
    assert result.invalidated_decisions == []
    assert "invalidated_challenges" in result.to_dict()


def test_changing_the_problem_marks_a_saved_assessment_stale(workspace):
    # The assessment used to sit outside the artifact→slot map entirely, on the grounds that it was the
    # live analysis layer rather than a deliverable. Once it is saved to disk that stops holding: an
    # assessment whose problem statement has since been rewritten is not "fresh", it is out of date.
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())          # no decisions, no challenges — nothing to unseat
    art.save("s", "brief", "# Assessment\n")
    assert art.list("s")["brief"]["stale"] is False

    result = svc.update_model("s", _full_model(**{"problem": _slot(80, "explicit", "high", "reframed")}))
    assert "brief" in result.stale_artifacts
    assert art.list("s")["brief"]["stale"] is True


def test_artifact_cannot_be_recorded_against_an_impossible_revision(workspace):
    # Provenance that cannot be true is worse than none: every freshness answer downstream is read off
    # this number, so a revision from the future is refused rather than stored.
    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())          # session is at revision 1
    with pytest.raises(RequivoError) as ei:
        art.save("s", "prd", "# PRD\n", source_revision=999)
    assert ei.value.code == "invalid_session"
    with pytest.raises(RequivoError):
        art.save("s", "prd", "# PRD\n", source_revision=0)
    assert "prd" not in art.list("s")            # nothing was recorded


# ── generation vs. concurrent writes ──────────────────────────────────────────
# A provider call runs for seconds to minutes, and the session can move underneath it (a second browser
# tab, a CLI apply, a Claude Code turn). These two tests pin the behaviour at that seam: the model the
# generator read is the revision it writes against, and a change that lands mid-flight is never lost
# and never silently inherited.

class _RacingClient:
    """A provider whose reply arrives only after someone else has already moved the session."""

    def __init__(self, reply: str, on_call):
        self._reply, self._on_call = reply, on_call
        self.messages = self

    def create(self, **kwargs):
        self._on_call()          # the concurrent write lands while "reasoning" is in flight
        return _Reply(self._reply)


class _Reply:
    def __init__(self, text):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = "end_turn"
        self.usage = None


def test_generation_that_races_a_concurrent_apply_does_not_lose_it(workspace):
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())          # revision 1 — what the generator will read

    def concurrent_answer():
        svc.update_model("s", _full_model(**{"business_rules": _slot(90, "explicit", "high", "HR signs off")}))

    brief_reply = json.dumps({"complexity": "medium", "problem": "P", "solution": "S",
                              "risks": [], "next_steps": []})
    disco = DiscoveryService(client=_RacingClient(brief_reply, concurrent_answer))
    with pytest.raises(RevisionConflictError):
        disco.generate("s", "brief")

    # The rule that landed mid-flight is still there — the assessment's apply did not write over it.
    assert svc.load_model("s").model["business_rules"].value == "HR signs off"


def test_an_answers_turn_holds_the_revision_it_read(workspace):
    # A turn has the same seam as a generation, so a caller that passes no expectation still gets one:
    # the revision the turn actually read. Without it, the CLI's `answer` would quietly overwrite a
    # change made in a browser tab between the read and the apply.
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())

    def concurrent_apply():
        svc.update_model("s", _full_model(**{"risks": _slot(70, "explicit", "high", "rollout risk")}))

    reply = _full_model()
    reply["summary"] = {"objective": "A leave approval system"}   # a discovery reply owes an objective
    disco = DiscoveryService(client=_RacingClient(json.dumps(reply), concurrent_apply))
    with pytest.raises(RevisionConflictError):
        disco.answer("s", "here are my answers")
    assert svc.load_model("s").model["risks"].value == "rollout risk"


def test_an_answers_turn_that_says_nothing_about_reasoning_keeps_it(workspace):
    """The full user journey the tri-state exists for: discovery → assessment → an ordinary answer.

    `engine.md` asks a turn for model/questions/summary only, so a refinement reply carries no
    decisions — and this whole path (provider parse → apply → diff → freshness) used to read that as a
    deletion, wiping the reasoning the assessment had just established while reporting no change and
    leaving the PRD marked fresh. The reply below is exactly what the engine returns; nothing about
    the reasoning is mentioned in it."""
    from requivo.services.discovery import DiscoveryService

    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", {**_full_model(), "decisions": [
        {"decision": "Managers approve in-app", "derived_from": ["permissions"]}]})
    art.save("s", "prd", "# PRD\n")

    reply = {**_full_model(**{"workflow": _slot(90, "explicit", "high", "request → approve")}),
             "summary": {"objective": "A leave approval system"}}
    DiscoveryService(client=_RacingClient(json.dumps(reply), lambda: None)).answer("s", "in-app")

    after = svc.load_model("s")
    assert [d.decision for d in after.decisions] == ["Managers approve in-app"]
    assert after.model["workflow"].value == "request → approve"   # the facts did move
    assert art.list("s")["prd"]["stale"] is True                  # …and that alone marks the PRD stale


def test_the_same_request_under_different_cards_is_a_different_session(workspace):
    """Context cards are provenance, not decoration: the same request read against `b2b-platform` and
    against `event-ops` gets different impact estimates, so different questions. Creation keyed on the
    request alone, so the second call silently handed back the first session — with a card selection
    the caller had not asked for and no way to notice."""
    svc = SessionService()
    first = svc.create_session("Same request.", context_cards=["b2b-platform"])
    again = svc.create_session("Same request.", context_cards=["b2b-platform"])
    other = svc.create_session("Same request.", context_cards=["event-ops"])

    assert again.slug == first.slug                       # same discovery: still idempotent
    assert other.slug != first.slug
    assert svc.cards(other.slug) == ["event-ops"]         # and it got the cards it asked for


def test_a_fresh_discovery_refuses_to_replace_a_model_that_already_exists(workspace):
    """Session creation is idempotent, so re-running `discover` on the same request lands on the same
    session — and used to overwrite whatever it held, replacing a model refined over several turns
    with a naive first-turn one. A conflict is recoverable; a silent replacement is not."""
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    disco = DiscoveryService(_FakeProvider())
    slug = disco.start("A leave approval system.", slug="dup")
    SessionService().update_model(slug, _full_model(**{"workflow": _slot(90, "explicit", "high", "kept")}))

    with pytest.raises(RevisionConflictError):
        disco.start("A leave approval system.", slug="dup")
    assert SessionService().load_model(slug).model["workflow"].value == "kept"


def test_run_discovery_refuses_a_session_that_already_has_a_model(workspace):
    """`run_discovery` reasons from the request alone — it never sees the current model — so on a
    refined session it does not improve the understanding, it discards it. The optimistic lock does
    not catch this: the call reads revision N and writes against revision N, so the precondition is
    satisfied while the content is a regression. `POST /sessions/{slug}/discover` reaches this
    directly; the Web only shows the button at revision 0, but a rule enforced by a hidden button is
    not enforced. The refusal is also *before* the call — reasoning that can only be thrown away
    should not be paid for."""
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model(**{"workflow": _slot(90, "explicit", "high", "refined")}))

    provider = _CountingProvider()
    with pytest.raises(RevisionConflictError) as e:
        DiscoveryService(provider).run_discovery("s")

    assert e.value.details["actual"] == 1 and e.value.details["expected"] == 0
    assert provider.calls == 0                                    # refused before the paid call
    assert svc.load_model("s").model["workflow"].value == "refined"


def test_a_repeat_discovery_is_refused_before_the_provider_is_paid(workspace):
    """Same rule, the other entry point. `start()` used to reason first and discover the conflict
    afterwards, so an accidental re-run bought a discovery turn — and, when finalizing, an assessment
    too — purely to throw both away."""
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    provider = _CountingProvider()
    disco = DiscoveryService(provider)
    disco.start("A leave approval system.", slug="dup")
    assert provider.calls == 1

    with pytest.raises(RevisionConflictError):
        disco.start("A leave approval system.", slug="dup")
    assert provider.calls == 1                                    # the second run never reasoned


def test_the_artifact_service_defaults_to_the_session_service_s_storage(workspace):
    """Two services, one backing. On files the default and the injected repository resolve to the same
    workspace, so a split was invisible — but `DiscoveryService(sessions=SessionService(postgres))`
    sent sessions to Postgres and artifacts to the local filesystem, and every call succeeded. This is
    the shape requivo-cloud constructs, so the default has to follow the session service."""
    from requivo.services.discovery import DiscoveryService
    from requivo.services.repository import FileSessionRepository

    repo = FileSessionRepository()
    disco = DiscoveryService(_FakeProvider(), sessions=SessionService(repo))
    assert disco.artifacts.repo is repo
    assert DiscoveryService(_FakeProvider(), repo=repo).sessions.repo is repo


def test_the_service_refuses_a_context_card_that_does_not_exist(workspace):
    """The CLI and the Web both resolve cards before they get here, which made the service look safe.
    It is not a boundary until it holds the rule itself: an unknown card recorded on a session is read
    back by every later turn, and an empty resolved selection means *every* card — so a bad name
    silently widens the context instead of narrowing it. requivo-cloud calls exactly this layer."""
    from requivo.core.errors import UnknownContextCardError

    with pytest.raises(UnknownContextCardError):
        SessionService().create_session("Something.", context_cards=["made-up"])
    assert SessionService().create_session(
        "Something.", context_cards=["b2b-platform"]).context_cards == ["b2b-platform"]


def test_an_artifact_is_refused_when_its_freshness_cannot_be_established(workspace):
    """`False` is not "I don't know" — it is the claim that the artifact is up to date. It was being
    returned for a session whose history could not be read at all, which is the one case where the
    answer is genuinely unavailable. Refusing the save is the honest outcome: the provenance it would
    record cannot be verified."""
    from requivo.core.errors import RequivoError

    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())                                   # revision 1
    svc.update_model("s", _full_model(**{"workflow": _slot(90, "explicit", "high", "moved")}))  # 2
    (store.canonical_dir("s") / "revisions" / "0001-model.json").unlink()  # the history is now a lie

    with pytest.raises(RequivoError) as e:
        art.save("s", "prd", "# PRD\n", source_revision=1)
    assert e.value.code == "invalid_session"
    assert "prd" not in art.list("s")                                      # nothing was recorded


def test_a_first_discovery_that_races_a_concurrent_write_conflicts(workspace):
    """`run_discovery` reasons from revision N and applies; the call takes minutes, so it captures the
    revision it read and holds the write to it — the same precondition every other provider-backed
    operation carries. Without it the concurrent model was replaced by one reasoned from the older
    state, which is exactly the case optimistic locking exists for."""
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    svc = SessionService()
    svc.create_session("Something.", slug="s")

    def concurrent_apply():
        svc.update_model("s", _full_model(**{"risks": _slot(70, "explicit", "high", "rollout risk")}))

    reply = {**_full_model(), "summary": {"objective": "A leave approval system"}}
    disco = DiscoveryService(client=_RacingClient(json.dumps(reply), concurrent_apply))
    with pytest.raises(RevisionConflictError):
        disco.run_discovery("s")
    assert svc.load_model("s").model["risks"].value == "rollout risk"


def test_an_artifact_generated_from_a_superseded_revision_is_born_stale(workspace):
    from requivo.services.discovery import DiscoveryService

    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())          # revision 1 — the PRD's actual source

    def concurrent_answer():
        svc.update_model("s", _full_model(**{"workflow": _slot(90, "explicit", "high", "new flow")}))

    prd_reply = json.dumps({"title": "PRD", "problem": "Approvals are lost in email."})
    DiscoveryService(client=_RacingClient(prd_reply, concurrent_answer)).generate("s", "prd")

    saved = art.list("s")["prd"]
    assert saved["revision"] == 1        # recorded against the revision it was written from…
    assert saved["stale"] is True        # …and the workflow change it never saw makes it stale


# ── the provider seam ─────────────────────────────────────────────────────────
# The point of the protocol is that the orchestration is not Anthropic-shaped. These two tests are the
# proof: one drives a whole discovery through a provider that has never heard of Anthropic, the other
# checks that what lands in the revision log is enough to reproduce the run.

class _FakeProvider:
    """A `ReasoningProvider` with no vendor behind it — the stand-in for a second implementation."""

    name = "fake"

    def analyze(self, request, *, current_model=None, answers=None, only=None):
        return EngineOutput.model_validate({**_full_model(), "summary": {"objective": "A leave system"}})

    def generate(self, artifact_type, model, *, only=None):
        raise AssertionError("not needed for this test")

    def model_name(self):
        return "fake-model-1"

    def provenance(self, op, *, only=None):
        return {"provider": self.name, "model_name": self.model_name(), "prompt_version": "sha256:fake"}


class _CountingProvider(_FakeProvider):
    """A provider that records whether it was asked to reason — the point of a pre-flight check."""

    def __init__(self):
        self.calls = 0

    def analyze(self, request, *, current_model=None, answers=None, only=None):
        self.calls += 1
        return super().analyze(request, current_model=current_model, answers=answers, only=only)


def test_discovery_runs_on_a_provider_that_is_not_anthropic(workspace):
    from requivo.services.discovery import DiscoveryService

    slug = DiscoveryService(_FakeProvider()).start("A leave approval system.", slug="fake-prov")
    meta = SessionService().meta(slug)
    # Nothing hard-codes "anthropic": the session and its revision are stamped by the provider itself.
    assert meta.provider == "fake" and meta.model_name == "fake-model-1"
    assert [(r.provider, r.model_name) for r in meta.revisions] == [("fake", "fake-model-1")]


def test_a_revision_records_the_prompt_it_was_reasoned_against(workspace):
    # A revision log that is only "anthropic, at 14:02" cannot reproduce anything: behaviour here is
    # tuned by editing prompts and context cards, so the prompt identity is half the provenance.
    from requivo.providers.anthropic import prompt_version
    from requivo.services.discovery import DiscoveryService

    reply = {**_full_model(), "summary": {"objective": "A leave approval system"}}
    slug = DiscoveryService(client=_RacingClient(json.dumps(reply), lambda: None)).start(
        "A leave approval system.", slug="prov")

    rec = SessionService().meta(slug).revisions[-1]
    assert rec.provider == "anthropic" and rec.model_name
    assert rec.prompt_version and rec.prompt_version.startswith("sha256:")
    # It follows the context-card selection, because a different card set is different reasoning.
    assert prompt_version("analyze") != prompt_version("analyze", only=[])


# ── the session format is public ──────────────────────────────────────────────
# `.requivo/sessions/` is the interface between the CLI, the Claude Code plugin, the Web and anything
# built on top. These tests are the contract: a session written by an older Requivo keeps loading, and
# a session written by a newer one is refused clearly instead of being half-understood.

# Verbatim shape of a session.json as 0.8.2 wrote it — including `prompt_versions`, a key that has
# since been removed. Frozen on purpose: editing it to match today's model would defeat the test.
SESSION_JSON_0_8_2 = """{
  "format_version": 1,
  "requivo_version": "0.8.2",
  "session_id": "d4f1a0c2e5b74d0e9a3c8b1f2e6d7a45",
  "slug": "leave-approval",
  "created_at": "2026-07-30T09:12:00Z",
  "updated_at": "2026-07-30T09:41:00Z",
  "provider": "anthropic",
  "model_name": "claude-sonnet-5",
  "context_cards": null,
  "request_hash": "sha256:6b2f1c",
  "schema_version": 1,
  "prompt_versions": {},
  "current_revision": 2,
  "revisions": [
    {"revision": 1, "created_at": "2026-07-30T09:12:00Z", "previous_revision": null,
     "provider": "anthropic", "model_name": "claude-sonnet-5", "surface": "cli-discover",
     "prompt_version": null, "model_hash": "sha256:aaa"},
    {"revision": 2, "created_at": "2026-07-30T09:41:00Z", "previous_revision": 1,
     "provider": "anthropic", "model_name": "claude-sonnet-5", "surface": "cli-answer",
     "prompt_version": null, "model_hash": "sha256:bbb"}
  ],
  "artifact_status": {
    "prd": {"revision": 2, "filename": "prd.md", "updated_at": "2026-07-30T09:42:00Z", "stale": false}
  }
}"""


def test_a_session_written_by_an_older_requivo_still_loads(workspace):
    d = store.canonical_dir("leave-approval")
    d.mkdir(parents=True, exist_ok=True)
    (d / "session.json").write_text(SESSION_JSON_0_8_2)

    meta = store.read_meta("leave-approval")
    assert meta.current_revision == 2 and meta.provider == "anthropic"
    assert [r.surface for r in meta.revisions] == ["cli-discover", "cli-answer"]
    assert meta.artifact_status["prd"].filename == "prd.md"
    assert meta.artifact_status["prd"].stale is False
    # A field this version dropped is ignored, not fatal — that is what lets a key be retired without
    # a format bump, and what makes the next reader's job survivable.
    assert not hasattr(meta, "prompt_versions")
    # Fields added since simply take their defaults.
    assert meta.revisions[0].prompt_version is None


def test_a_session_from_a_newer_requivo_is_refused_not_guessed(workspace):
    d = store.canonical_dir("from-the-future")
    d.mkdir(parents=True, exist_ok=True)
    (d / "session.json").write_text(SESSION_JSON_0_8_2.replace('"format_version": 1', '"format_version": 2'))
    with pytest.raises(RequivoError) as ei:
        store.read_meta("from-the-future")
    assert ei.value.code == "invalid_session"
    assert "upgrade requivo" in str(ei.value).lower()


def test_update_missing_session_raises(workspace):
    with pytest.raises(SessionNotFoundError):
        SessionService().update_model("ghost", _full_model())


# ── legacy migration on first mutation ──────────────────────────────────────────


def test_a_legacy_session_is_named_in_the_error_rather_than_migrated_behind_your_back(workspace):
    """`out/` was the store until 0.8.0, and until 0.9.8 every read silently fell back to it and every
    mutation migrated one in place. That kept old sessions working without the user knowing, which is
    also what was wrong with it: the fallback ran on every read of every session for a layout nothing
    has written in two minor versions, and "where does this session live?" had two answers throughout
    the code. Migration is explicit now — so the one thing this layer still owes is an error that says
    which command to run, instead of a bare "no session"."""
    legacy = store.legacy_dir("old")
    legacy.mkdir(parents=True)
    (legacy / "model.json").write_text(json.dumps(_full_model()))
    (legacy / "request.txt").write_text("Legacy request.")
    (legacy / "prd.md").write_text("# Legacy PRD\n")

    svc = SessionService()
    assert not svc.exists("old")
    with pytest.raises(SessionNotFoundError) as e:
        svc.load_model("old")
    assert e.value.details.get("legacy") is True
    assert "session migrate" in str(e.value)

    # And the explicit migration is intact: the model becomes revision 1, artifacts come with it,
    # and the originals are left where they were.
    store.migrate_legacy("old")
    assert store.session_exists("old")
    assert (legacy / "model.json").exists()
    result = svc.update_model("old", _full_model(**{"problem": _slot(90, "explicit", "high", "P")}))
    assert result.revision == 2
    d = store.canonical_dir("old")
    assert (d / "revisions" / "0001-model.json").exists()
    assert (d / "artifacts" / "prd.md").read_text() == "# Legacy PRD\n"
