"""Requivo Web tests — offline (a fake provider), isolated workspace per test.

They exercise the real app: routing, templates, the shared services, security (slug/traversal, no key
in HTML, escaping), and the discovery/artifact flows with a mocked provider.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from requivo.cli import app as cli_app
from requivo.services.sessions import SessionService
from requivo.web.config import MAX_ANSWERS_CHARS, MAX_REQUEST_CHARS, MAX_SLUG_CHARS
from requivo.web.security import CSRF_FIELD, csrf_token
from requivo.web.templating import TEMPLATES_DIR
from tests.web.conftest import BRIEF_REPLY, CRITERIA_REPLY, PRD_REPLY, engine_reply, full_model

HIGH_EXPLICIT = {"completeness": 90, "confidence": "explicit", "impact": "high"}
HIGH_INFERRED = {"completeness": 30, "confidence": "inferred", "impact": "high"}


def _make_session(slug="leave-approval", **model_over):
    """Seed a discovered session directly through the service (no provider), for view/security tests."""
    svc = SessionService()
    svc.create_session("A leave approval request", slug=slug)
    model = {"model": full_model(**model_over), "questions": [], "summary": {"objective": "Leave system"}}
    svc.update_model(slug, json.dumps(model))
    return slug


# ── packaging / smoke ─────────────────────────────────────────────────────────

def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_web_help_exits_cleanly():
    with pytest.raises(SystemExit) as ei:
        cli_app(["web", "--help"])
    assert ei.value.code == 0


def test_static_assets_are_served_locally(client):
    # HTMX is vendored — served from the package, never a CDN.
    assert client.get("/static/vendor/htmx.min.js").status_code == 200
    assert client.get("/static/css/app.css").status_code == 200
    assert client.get("/static/js/app.js").status_code == 200


def test_security_headers_present(client):
    h = client.get("/").headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in h and "default-src 'self'" in h["Content-Security-Policy"]


# ── app / pages ───────────────────────────────────────────────────────────────

def test_home_without_sessions(client):
    r = client.get("/")
    assert r.status_code == 200 and "Nothing here yet" in r.text


def test_home_lists_an_existing_session(client):
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    r = client.get("/")
    assert r.status_code == 200 and "leave-approval" in r.text


def test_session_page_renders_understanding(client):
    _make_session("leave-approval", problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED)
    r = client.get("/sessions/leave-approval")
    assert r.status_code == 200
    assert "What Requivo understood" in r.text and "Are we ready?" in r.text


def test_missing_session_is_404(client):
    r = client.get("/sessions/does-not-exist")
    assert r.status_code == 404 and "Not found" in r.text


def test_export_returns_model_json(client):
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    r = client.get("/sessions/leave-approval/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert "leave-approval.model.json" in r.headers["content-disposition"]
    assert "problem" in r.json()["model"]


# ── discovery (mocked provider) ───────────────────────────────────────────────

def test_create_session_runs_discovery(client, with_provider):
    with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED))
    r = client.post("/sessions", data={"request_text": "A leave approval system",
                                       "slug": "leave-approval", "provider": "anthropic"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/sessions/leave-approval"
    page = client.get("/sessions/leave-approval")
    assert "What could change the solution" in page.text
    assert "How are exceptions handled" in page.text


def test_create_only_then_run_discovery(client, with_provider):
    # No LLM at creation…
    r = client.post("/sessions", data={"request_text": "A leave system", "slug": "leave-only",
                                       "provider": "create_only"}, follow_redirects=False)
    assert r.status_code == 303
    pending = client.get("/sessions/leave-only")
    assert "Awaiting analysis" in pending.text
    # …then discovery on demand.
    with_provider(engine_reply(converged=True, problem=HIGH_EXPLICIT))
    r = client.post("/sessions/leave-only/discover", follow_redirects=False)
    assert r.status_code == 303
    assert "No question left that would change the solution" in client.get("/sessions/leave-only").text


def test_answers_apply_and_return_status_partial(client, with_provider):
    with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED),
                  engine_reply(converged=True, problem=HIGH_EXPLICIT, business_rules=HIGH_EXPLICIT))
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})
    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": "Exceptions go to HR.", "expected_revision": "1"})
    assert r.status_code == 200
    assert "What changed" in r.text  # the swapped body leads with the impact of the answers


def test_revision_conflict_is_clean(client, with_provider):
    # Two replies: one for discovery, one for the answers turn (the turn runs before the apply where the
    # optimistic-lock check fires).
    with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED),
                  engine_reply(converged=True, problem=HIGH_EXPLICIT))
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})
    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": "…", "expected_revision": "999"})  # stale expectation
    assert r.status_code == 409  # RevisionConflictError → clean 409, not a traceback


# ── artifacts ─────────────────────────────────────────────────────────────────

def test_generate_brief_and_prd_and_view(client, with_provider):
    with_provider(engine_reply(converged=True, problem=HIGH_EXPLICIT), BRIEF_REPLY, PRD_REPLY)
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})
    assert client.post("/sessions/leave-approval/artifacts/brief").status_code == 200
    assert client.post("/sessions/leave-approval/artifacts/prd").status_code == 200
    # both are listed, fresh
    page = client.get("/sessions/leave-approval")
    assert "Decision brief" in page.text and "PRD" in page.text and "Up to date" in page.text
    # view + download the PRD
    view = client.get("/sessions/leave-approval/artifacts/prd")
    assert view.status_code == 200 and "Leave approval — PRD" in view.text
    dl = client.get("/sessions/leave-approval/artifacts/prd?download=1")
    assert dl.headers["content-disposition"].endswith('filename="prd.md"')


def test_related_change_marks_artifact_stale(client, with_provider):
    # generate a PRD (consumes business_rules), then a material change to business_rules → PRD stale.
    with_provider(
        engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED),   # discover
        PRD_REPLY,                                                           # generate prd @ rev1
        engine_reply(converged=True, problem=HIGH_EXPLICIT, business_rules=HIGH_EXPLICIT),  # answer
    )
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})
    client.post("/sessions/leave-approval/artifacts/prd")
    client.post("/sessions/leave-approval/answers", data={"answers": "explicit now", "expected_revision": "1"})
    assert "Needs updating" in client.get("/sessions/leave-approval").text


def test_the_web_offers_every_artifact_the_service_can_generate(client, with_provider, monkeypatch):
    # The Web used to keep its own two-entry list while the service could produce five. The buttons
    # still come from the service's vocabulary — what changed is their weight: the decision brief is
    # the primary action, the rest live under "More documents". Available, not equal.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")   # the toolbar only shows with a provider
    with_provider(CRITERIA_REPLY)
    _make_session("leave-approval", problem=HIGH_EXPLICIT)

    page = client.get("/sessions/leave-approval").text
    assert "Generate decision brief" in page          # the one primary call to action
    assert "More documents" in page
    for label in ("PRD", "Acceptance criteria", "Delivery epic", "Release notes"):
        assert label in page, f"no generate button for {label}"

    assert client.post("/sessions/leave-approval/artifacts/criteria").status_code == 200
    saved = client.get("/sessions/leave-approval/artifacts/criteria")
    assert saved.status_code == 200 and "acceptance criteria" in saved.text


def test_terminal_only_analyses_are_not_generatable_artifacts(client, with_provider):
    # `stories` reasons but produces no document (it feeds the estimate), so there is nothing to save
    # or track. Rejected before any provider call — the fake would raise if reached.
    with_provider()
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    assert client.post("/sessions/leave-approval/artifacts/stories").status_code == 400


# ── security ──────────────────────────────────────────────────────────────────

def test_slug_traversal_is_rejected(client):
    # A dot-segment slug never resolves to a path; the guard raises invalid_slug (400) or the route
    # simply does not match (404) — either way, nothing outside the store is reached.
    assert client.get("/sessions/..%2f..%2fsecret").status_code in (400, 404)
    assert client.get("/sessions/a..b").status_code == 400   # matches {slug}, fails validation
    assert client.post("/sessions", data={"request_text": "x", "slug": "../escape",
                                          "provider": "create_only"}).status_code == 400


# ── cross-site protection ─────────────────────────────────────────────────────
# Listening on 127.0.0.1 keeps nobody out: any page open in the same browser can post to a known local
# port without a preflight, and for this app writing *is* the damage (sessions created, provider calls
# billed). These pin each layer of web/security.py independently.

def test_a_write_without_the_request_token_is_refused(raw_client):
    r = raw_client.post("/sessions", data={"request_text": "x", "slug": "evil", "provider": "create_only"})
    assert r.status_code == 403
    assert raw_client.get("/").status_code == 200          # reads are untouched


def test_the_token_works_as_a_form_field(raw_client):
    # The browser path: a hidden input, not a header — no page in this app can set a request header.
    r = raw_client.post("/sessions", data={"request_text": "x", "slug": "ok", "provider": "create_only",
                                           CSRF_FIELD: csrf_token()}, follow_redirects=False)
    assert r.status_code == 303


def test_forms_render_the_request_token(client):
    assert csrf_token() in client.get("/").text


def test_a_write_from_another_origin_is_refused(client):
    r = client.post("/sessions", data={"request_text": "x", "provider": "create_only"},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_a_browser_declared_cross_site_write_is_refused(client):
    r = client.post("/sessions", data={"request_text": "x", "provider": "create_only"},
                    headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_a_request_addressed_to_another_host_is_refused(app):
    # DNS rebinding: `evil.example` resolving to 127.0.0.1 is same-origin to the browser, so it would
    # pass every other check *and* be able to read the token off the page. The host allowlist is the
    # only guard that catches it, which is why it also runs on reads.
    rebound = TestClient(app, base_url="http://evil.example", raise_server_exceptions=False)
    assert rebound.get("/").status_code == 403


# ── input bounds ──────────────────────────────────────────────────────────────

def test_an_oversized_request_is_refused_not_truncated(client):
    r = client.post("/sessions", data={"request_text": "x" * (MAX_REQUEST_CHARS + 1),
                                       "provider": "create_only"})
    assert r.status_code == 413
    assert not SessionService().list_sessions()      # nothing was created from the truncated half


def test_a_request_of_exactly_the_ceiling_is_accepted(client):
    """`MAX_REQUEST_CHARS` is the maximum *permitted* length — "past the ceiling the field is refused",
    and the message says "exceeds" — so the comparison is `>`, not `>=`. Issue #8 read the equality case
    as an off-by-one, but the reason a clipped paste landed exactly on the admitted value was the
    `maxlength` attribute being set to the same number, not the comparison being wrong. Tightening to
    `>=` would refuse a legal 20,000-character request and make the real limit 19,999."""
    r = client.post("/sessions", data={"request_text": "x" * MAX_REQUEST_CHARS,
                                       "provider": "create_only"}, follow_redirects=False)
    assert r.status_code == 303
    assert len(SessionService().list_sessions()) == 1


def test_oversized_answers_are_refused_before_the_paid_turn(client, with_provider):
    """The answers field has the same ceiling and the same refusal, and had no test at all. The
    at-ceiling half is the positive control: without it, a harness that refused everything (or a session
    that never reached revision 1) would satisfy the 413 on its own."""
    fake = with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED),
                         engine_reply(converged=True, problem=HIGH_EXPLICIT))
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval",
                                   "provider": "anthropic"})
    assert len(fake.calls) == 1                       # discovery ran

    too_long = client.post("/sessions/leave-approval/answers",
                           data={"answers": "y" * (MAX_ANSWERS_CHARS + 1), "expected_revision": "1"})
    assert too_long.status_code == 413
    assert len(fake.calls) == 1                       # refused before the provider was billed for it

    at_ceiling = client.post("/sessions/leave-approval/answers",
                             data={"answers": "y" * MAX_ANSWERS_CHARS, "expected_revision": "1"})
    assert at_ceiling.status_code == 200
    assert len(fake.calls) == 2                       # …and the legal one did run


def test_an_oversized_session_name_is_refused(client):
    """The session-name field lost its `maxlength` too — same class, same file, same `>` refusal. The
    template scan below proves the attribute is gone; this proves the refusal it was hiding is actually
    reachable, which is the half that matters. The at-ceiling case is the positive control: without it,
    a 413 raised for any other reason would read as this check firing."""
    too_long = client.post("/sessions", data={"request_text": "x", "slug": "a" * (MAX_SLUG_CHARS + 1),
                                              "provider": "create_only"})
    assert too_long.status_code == 413
    assert not SessionService().list_sessions()

    at_ceiling = client.post("/sessions", data={"request_text": "x", "slug": "a" * MAX_SLUG_CHARS,
                                                "provider": "create_only"}, follow_redirects=False)
    assert at_ceiling.status_code == 303
    assert len(SessionService().list_sessions()) == 1


def test_no_rendered_field_clips_what_the_user_pasted(client, with_provider):
    """Invariant 3 (refuse, don't truncate) at the one place a real user meets it. `maxlength` makes the
    server's refusal unreachable from the UI: a browser clips a paste to the allowance with no event, no
    message and no visual difference, so an over-long request arrives at exactly the ceiling and passes
    every check the server has. #8."""
    with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED))
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval",
                                   "provider": "anthropic"})
    home = client.get("/").text
    session = client.get("/sessions/leave-approval").text
    # Positive control first: an empty or errored render would satisfy "no maxlength" by rendering no
    # fields at all, and the silence would read as a pass.
    assert 'name="request_text"' in home and 'name="slug"' in home
    assert 'name="answers"' in session
    for page_name, page in (("home", home), ("session", session)):
        assert "maxlength" not in page, f"the {page_name} page renders a field that silently clips input"


def test_no_template_carries_a_clipping_attribute():
    """The rendered check above only sees what those two routes produce. This covers every template,
    including ones added later: nothing in this app may clip input client-side. A client-side affordance
    is welcome, but it must count and warn — never trim what the user actually typed."""
    templates = sorted(TEMPLATES_DIR.rglob("*.html"))
    # Positive control: the scan found the form templates it is supposed to be reading. `rglob` over a
    # mislocated directory returns an empty list, which every "not in" assertion below would pass.
    bodies = {p: p.read_text(encoding="utf-8") for p in templates}   # explicit: cp1252 can't read these
    assert len(bodies) >= 10
    assert any("<textarea" in body for body in bodies.values())
    clipping = sorted(p.name for p, body in bodies.items() if "maxlength" in body)
    assert not clipping, f"templates clip input client-side: {clipping}"


def test_an_unknown_context_card_is_refused(client):
    # Filtering it out would leave an empty selection, which every reader treats as "all cards" — a
    # typo would silently widen the context instead of narrowing it.
    r = client.post("/sessions", data={"request_text": "x", "provider": "create_only",
                                       "cards": ["no-such-card"]})
    assert r.status_code == 400
    assert not SessionService().list_sessions()


def test_api_key_never_reaches_the_browser(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-sentinel-123")
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    for path in ("/", "/sessions/new", "/sessions/leave-approval"):
        assert "sk-secret-sentinel-123" not in client.get(path).text


def test_user_content_is_escaped(client, with_provider):
    with_provider(json.dumps({
        "model": full_model(problem=HIGH_EXPLICIT), "questions": [],
        "summary": {"objective": "<script>alert(1)</script>"}}))
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})
    page = client.get("/sessions/leave-approval").text
    assert "<script>alert(1)</script>" not in page       # escaped
    assert "&lt;script&gt;" in page


# ── the MVP flow ──────────────────────────────────────────────────────────────
# One workflow leads the product: paste a request → read what was understood → answer the few
# questions that could change the solution → see what moved → generate one decision brief. These
# tests pin that flow's shape, not just that its routes respond.

def test_home_leads_with_the_request_form(client):
    page = client.get("/").text
    assert 'name="request_text"' in page
    assert "Save request" in page or "Analyse request" in page
    # The provider is a setting, not a question the reader has to answer: it exists, but only inside
    # the advanced disclosure. Position is the honest assertion here — server-rendered <details>
    # keeps its contents in the markup, so "not present" would be a lie.
    assert page.index("Advanced settings") < page.index('id="provider"')


def test_home_survives_a_session_with_no_model(client):
    """A captured-but-unanalysed session is a normal row. It used to take the whole list down: the row
    builder asked for a status, `status()` needs a model, and one 404 became the home page's."""
    from requivo.services.discovery import DiscoveryService
    DiscoveryService().create_only("A leave approval system", slug="not-analysed-yet")
    r = client.get("/")
    assert r.status_code == 200
    assert "Awaiting analysis" in r.text and "not-analysed-yet" not in r.text.split("Recent")[0]


def test_sessions_new_redirects_to_the_request_form(client):
    r = client.get("/sessions/new", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/"


def test_only_the_top_questions_lead_the_page(client, with_provider):
    six = [{"q": f"Question {i}?", "slot": "business_rules", "why": "it moves the build"}
           for i in range(6)]
    with_provider(engine_reply(questions=six, problem=HIGH_EXPLICIT))
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})
    page = client.get("/sessions/leave-approval").text
    lead = page.split("Traceability details")[0]
    assert lead.count("Why it matters") == 5           # five lead the page…
    assert "1 further question" in lead                # …and the sixth is accounted for, not dropped
    assert "Question 5?" in page                       # still there, under traceability


def test_no_raw_slot_ids_reach_the_page(client, with_provider):
    with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED))
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})
    page = client.get("/sessions/leave-approval").text
    # Every slot id with an underscore — the ones no human label contains, so a hit is the engine's
    # vocabulary leaking into the reader's.
    for slot_id in ("business_rules", "config_vs_custom", "success_metrics", "current_process",
                    "edge_cases", "business_objects"):
        assert slot_id not in page, f"slot id {slot_id!r} rendered to the reader"


def test_readiness_is_one_action_state_with_reasons(client, with_provider):
    with_provider(engine_reply(converged=True, problem=HIGH_INFERRED))   # a high-impact topic unresolved
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})
    page = client.get("/sessions/leave-approval").text
    assert "Not ready to produce a reliable scope" in page
    assert "Real problem" in page                       # the reason, by its human label
    assert "Ready for a first decision brief" not in page


def test_answers_report_what_changed_and_what_needs_review(client, with_provider):
    with_provider(
        engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED),
        PRD_REPLY,
        engine_reply(converged=True, problem=HIGH_EXPLICIT, business_rules=HIGH_EXPLICIT),
    )
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})
    client.post("/sessions/leave-approval/artifacts/prd")
    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": "Exceptions go to HR.", "expected_revision": "1"})
    assert r.status_code == 200
    assert "What changed" in r.text
    assert "Business rules" in r.text                   # the area that moved, by its human label
    assert "Needs review" in r.text and "PRD" in r.text  # the document the change reaches


def test_an_unrelated_change_leaves_a_document_alone(client, with_provider):
    """The differentiator cuts both ways: a change that misses a document's dependencies must not
    flag it. Reporting is not one of the PRD's inputs, so moving it changes nothing the PRD rests on."""
    with_provider(
        engine_reply(problem=HIGH_EXPLICIT, reporting={"completeness": 10, "confidence": "empty",
                                                       "impact": "low"}),
        PRD_REPLY,
        engine_reply(converged=True, problem=HIGH_EXPLICIT,
                     reporting={"completeness": 80, "confidence": "explicit", "impact": "low"}),
    )
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})
    client.post("/sessions/leave-approval/artifacts/prd")
    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": "Reporting is a weekly export.", "expected_revision": "1"})
    assert "Needs review" not in r.text
    assert "Needs updating" not in client.get("/sessions/leave-approval").text


def test_a_changed_answer_moves_the_scope_and_the_brief(client, with_provider):
    """The canonical scope-change story, end to end: a two-way sync during migration is decided, a
    brief is written from it, then the answer changes to a one-time cutover — and the integration
    topic moves, the brief is marked as needing an update, and the page says so."""
    with_provider(
        engine_reply(problem=HIGH_EXPLICIT,
                     integrations={"completeness": 40, "confidence": "inferred", "impact": "high",
                                   "value": "Both systems stay in sync during the pilot."}),
        BRIEF_REPLY,
        engine_reply(converged=True, problem=HIGH_EXPLICIT,
                     integrations={"completeness": 90, "confidence": "explicit", "impact": "high",
                                   "value": "One-time migration; the legacy system becomes read-only."}),
    )
    client.post("/sessions", data={"request_text": "Migrate leave to the new HR system",
                                   "slug": "leave-migration", "provider": "anthropic"})
    assert client.post("/sessions/leave-migration/artifacts/brief").status_code == 200
    assert "Up to date" in client.get("/sessions/leave-migration").text

    r = client.post("/sessions/leave-migration/answers",
                    data={"answers": "The migration is one-time — the legacy system becomes read-only.",
                          "expected_revision": "2"})
    assert "What changed" in r.text
    assert "Integrations &amp; notifications" in r.text  # the topic that moved, in the reader's words
    assert "Decision brief" in r.text                   # …and the document it reaches
    assert "Needs updating" in client.get("/sessions/leave-migration").text


# ── the error-code → HTTP status contract (#34) ───────────────────────────────
#
# `_STATUS_BY_CODE` used to fall back to 400 for anything unlisted, so a code added in one lane got a
# plausible-but-wrong status rather than an obvious gap: `context_unreadable` — a permissions fault on
# the install's own card directory, entirely the operator's environment — reached the browser as
# "HTTP 400: your request was bad". The reader was told they did something wrong; the server could not
# read its own assets. The missing row is the symptom, the silent default is the defect, so the guard
# below is a completeness test rather than one more row.


def _all_error_codes() -> dict[str, str]:
    """Every `RequivoError` code the package can raise, mapped to the class carrying it.

    Discovered by walking subclasses after importing every module, not by reading one file: three of
    them live outside `core/errors.py` (`web.security`, `services.artifacts`, `providers.anthropic`),
    and two of those are already in the status table — so "every code in core/errors.py" would be the
    wrong scan set in both directions.
    """
    import importlib
    import pkgutil

    import requivo
    from requivo.core.errors import RequivoError

    unimportable = {}
    for m in pkgutil.walk_packages(requivo.__path__, "requivo."):
        try:
            importlib.import_module(m.name)
        except Exception as e:  # noqa: BLE001
            unimportable[m.name] = f"{type(e).__name__}: {e}"
    # A module that would not import is a hole in the scan set, and a hole here is invisible: the
    # test would pass by not looking. `tests/test_boundaries.py` makes the same point about an empty
    # glob — `assert not []` is an all-clear nobody earned.
    assert not unimportable, f"scan set incomplete, cannot speak for these codes: {unimportable}"

    found = {RequivoError.code: "requivo.core.errors.RequivoError"}

    def walk(cls):
        for sub in cls.__subclasses__():
            found[sub.code] = f"{sub.__module__}.{sub.__qualname__}"
            walk(sub)

    walk(RequivoError)
    return found


# Codes deliberately absent from `_STATUS_BY_CODE`. An entry here is a claim that the code cannot
# reach the table, and the reason is the part a reviewer checks — an allowlist without one is just a
# quieter version of the default this test exists to remove.
_NOT_STATUS_MAPPED = {
    "provider_unavailable":
        "EngineError is classified by the isinstance branch in the handler, ahead of the table, so a "
        "row here would never be read. Pinned by test_a_provider_transport_failure_is_still_502.",
    "requivo_error":
        "the abstract base. Nothing raises it directly; a bare one reaching the handler is a defect, "
        "and the unclassified default is what surfaces it rather than dressing it as the caller's.",
}


def test_every_error_code_has_an_explicit_http_status():
    """The compounding half of #34: a new code is a red leg here, not a wrong answer to a user.

    The table's default was 400, so the only thing standing between a newly added code and "your
    request was bad" was somebody remembering a file in another lane. Four codes were sitting on that
    default when this was written — `context_unreadable`, `session_exists`, `session_locked` and
    `provider_output_invalid` — and only the first had been filed.
    """
    from requivo.web.app import _STATUS_BY_CODE

    codes = _all_error_codes()
    # must fire: the walk really found the vocabulary, so the assertions below mean something
    assert len(codes) >= 15, f"scan set looks blind: {sorted(codes)}"
    assert {"session_not_found", "invalid_slug", "cross_site_request"} <= set(codes)

    unclassified = sorted(set(codes) - set(_STATUS_BY_CODE) - set(_NOT_STATUS_MAPPED))
    assert not unclassified, (
        "these error codes have no explicit HTTP status and would fall through to the unclassified "
        f"default: { {c: codes[c] for c in unclassified} }")

    both = sorted(set(_STATUS_BY_CODE) & set(_NOT_STATUS_MAPPED))
    assert not both, f"claimed unreachable by the table and also listed in it: {both}"

    dead = sorted(set(_STATUS_BY_CODE) - set(codes))
    assert not dead, f"status rows for codes nothing raises any more: {dead}"

    stale = sorted(set(_NOT_STATUS_MAPPED) - set(codes))
    assert not stale, f"allowlisted codes that no longer exist: {stale}"


@pytest.mark.parametrize("code, status, why", [
    ("context_unreadable", 500,
     "the server cannot read its own card directory — the operator's environment, not the request"),
    ("no_context_cards", 500,
     "the install shipped no context cards; nothing the caller sent could have avoided it"),
    ("provider_output_invalid", 502,
     "the upstream model would not hold the contract after every retry — same family as a transport "
     "failure, which is already 502"),
    ("session_exists", 409,
     "a conflict with the current state of the store, like revision_conflict and stale_artifact"),
    ("session_locked", 503,
     "nothing raced to a conclusion; the write never started and retrying it unchanged is correct"),
    ("empty_selector_token", 400, "a stray comma in what the caller typed"),
    ("empty_selection", 400, "a selection the caller supplied that selects nothing"),
    ("invalid_filename", 400, "a path target the caller supplied"),
])
def test_a_server_side_fault_is_not_reported_as_the_users_bad_request(code, status, why):
    """The five decisions this change makes, each pinned with its reason, plus the three the issue
    confirms were already right at 400. Asserted as a table so a future edit has to argue with the
    sentence rather than only with the number."""
    from requivo.web.app import _STATUS_BY_CODE
    assert _STATUS_BY_CODE[code] == status, why


def test_an_unclassified_code_is_a_server_fault_not_a_bad_request():
    """The default itself. With every known code explicitly mapped, the fallback only fires for a
    code this version has never heard of — and "we do not know what this is" is not evidence the
    caller erred. It used to be 400, which is the misreport in #34 generalised."""
    from requivo.core.errors import RequivoError, SessionNotFoundError
    from requivo.web.app import _UNCLASSIFIED_STATUS, _status_for

    class _FromTheFuture(RequivoError):
        code = "a_code_this_version_has_never_heard_of"

    assert _UNCLASSIFIED_STATUS >= 500
    assert _status_for(_FromTheFuture("...")) == _UNCLASSIFIED_STATUS
    # must fire: a code it *has* heard of is still classified from the table, not defaulted
    assert _status_for(SessionNotFoundError("...")) == 404


def test_a_provider_transport_failure_is_still_502():
    """The one allowlist entry that is about ordering: EngineError is classified by isinstance ahead
    of the table, so `provider_unavailable` deliberately has no row. If that branch were ever
    removed, this fails rather than the code quietly picking up the unclassified default."""
    from requivo.providers.anthropic import EngineError
    from requivo.web.app import _STATUS_BY_CODE, _status_for
    assert _status_for(EngineError("upstream is down")) == 502
    assert "provider_unavailable" not in _STATUS_BY_CODE


def test_context_unreadable_reaches_the_browser_as_a_server_error(client, monkeypatch):
    """End to end, through the real handler and the real templates — the mapping test above pins the
    number, this pins that the number is what a reader actually gets."""
    from requivo.core.errors import ContextUnreadableError

    # must fire: the page is fine before the fault is injected
    assert client.get("/").status_code == 200

    def _unreadable():
        raise ContextUnreadableError(
            "the context-card directory /x exists but cannot be read: denied",
            details={"directory": "/x"})

    monkeypatch.setattr("requivo.web.routes.home.available_cards", _unreadable)
    r = client.get("/")
    assert r.status_code == 500, "a permissions fault on the install's own assets is not a 400"
    assert "context_unreadable" in r.text


def test_a_taken_session_name_is_suffixed_rather_than_refused(client):
    """Why `session_exists` gets a status row but no end-to-end test — and a finding recorded where
    the next reader will look for it.

    Posting a name that is already taken by a *different* request does not raise `session_exists`:
    `create_session` falls through to a `<base>-<identity hash>` candidate, which is free, so the
    reader is redirected to a session with a name they did not choose and nothing says so. The raise
    at `services/sessions.py:163` needs *both* candidates taken with different identities, and the
    suffix is a hash of the request, so from this route that is close to unreachable.

    This pins the behaviour as it is rather than asserting it is right — the silent rename is
    reported as an adjacent finding, not fixed here, because choosing between refusing, suffixing
    loudly, and re-rendering the form is a design decision this change was not briefed to make.
    """
    first = client.post("/sessions", data={"request_text": "A leave approval request",
                                           "slug": "leave-approval", "provider": "create_only"},
                        follow_redirects=False)
    assert first.status_code == 303
    assert first.headers["location"] == "/sessions/leave-approval"

    second = client.post("/sessions", data={"request_text": "A different request entirely",
                                            "slug": "leave-approval", "provider": "create_only"},
                         follow_redirects=False)
    assert second.status_code == 303
    landed = second.headers["location"]
    assert landed.startswith("/sessions/leave-approval-"), landed
    assert landed != "/sessions/leave-approval", (
        "must fire: the second request really did get its own session, so the rename is real")
