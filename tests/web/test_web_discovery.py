"""Requivo Web discovery and artifact flows: the workflow the product leads with, end to end.

Split out of `test_web.py` by #142. Every test here drives the provider seam with a fake — create,
answer, generate, regenerate — including the MVP flow's own rendering assertions, which stay with the
flow they are a step of rather than moving to a file about markup.

The busy-rule tests (#50) are here for the same reason: what they protect is a second paid
generation, not a template.

Offline, isolated workspace per test; the fixtures and the seeded-session helper live in
`tests/web/conftest.py`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from requivo.providers.errors import EngineError
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from requivo.web.config import MAX_ANSWERS_CHARS
from requivo.web.templating import TEMPLATES_DIR
from tests.web.conftest import (
    BRIEF_REPLY,
    CRITERIA_REPLY,
    HIGH_EXPLICIT,
    HIGH_INFERRED,
    PRD_REPLY,
    _make_session,
    engine_reply,
)

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


def test_a_resolved_session_can_still_be_refined(client, with_provider):
    """Questions run out; the conversation does not.

    Once the engine returns no question, the page says so — and used to remove the answer box with the
    question list it lived in, so a session that reached "ready for a first decision brief" could no
    longer be told anything: not a correction, not a constraint that arrived late, not scope the client
    added after the fact. `answer()` never needed a question to fold text into the model."""
    with_provider(engine_reply(converged=True, problem=HIGH_EXPLICIT),
                  engine_reply(converged=True, problem=HIGH_EXPLICIT, business_rules=HIGH_EXPLICIT))
    client.post("/sessions", data={"request_text": "x", "slug": "leave-approval", "provider": "anthropic"})

    page = client.get("/sessions/leave-approval").text
    assert "No question left that would change the solution" in page
    assert 'name="answers"' in page, "a resolved session still has to be answerable"

    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": "One more thing — contractors are out of scope.",
                          "expected_revision": "1"})
    assert r.status_code == 200
    assert "What changed" in r.text


# ── one generation at a time (#50) ────────────────────────────────────────────

_BUSY_HARNESS = Path(__file__).parent / "busy_harness.js"
_ERROR_SWAP_HARNESS = Path(__file__).parent / "error_swap_harness.js"


def _busy_timeline() -> dict[str, dict]:
    """Execute the real `static/js/app.js` against a minimal DOM and return what it did, step by step.

    A `TestClient` runs no JavaScript, so without this the only assertable thing about #50 is that some
    literal string appears in the asset — which pins the implementation's spelling instead of its
    effect, and passes just as well against code that disables nothing.

    Node is the one thing here that is not guaranteed. When it is missing this **skips loudly** and
    names what went unasserted, rather than leaving a green run implying coverage it does not have.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH, so the page-wide busy rule in static/js/app.js was NOT "
                    "asserted in this run — it is browser behaviour and nothing else in this suite "
                    "can see it (#50)")
    app_js = TEMPLATES_DIR.parent / "static" / "js" / "app.js"
    proc = subprocess.run([node, str(_BUSY_HARNESS), str(app_js)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 0, "the harness itself failed, so nothing was observed:\n" + proc.stderr
    return {row["at"]: row for row in json.loads(proc.stdout)}


def test_one_generation_at_a_time_is_the_pages_rule_not_the_forms():
    """Mutual exclusion is a property of the page, not of a form (#50).

    Every generator under *More documents* is its own form posting to the same `#artifacts-region`, so
    a second click while the first call is in flight buys a second paid provider call whose result the
    first swap then discards. `markLoading` disabled only the submitting form's own button, which left
    every sibling live — the state the reader reported, and reproduced by this harness against the
    shipped asset as `disabled=[True, False, False]`.

    Every assertion below drives the real file. The two that matter most are the ones a weaker shape
    misses: that the *first* response does not unmute the page while a second call is still running (a
    boolean flag passes the simple case and fails this one), and that the state is re-asserted over
    markup a swap just brought in, which carries no `disabled` attribute of its own.
    """
    t = _busy_timeline()

    assert t["initial"]["disabled"] == [False, False, False]

    # must fire. A harness that dispatched nothing, or an app.js that muted nothing, would leave these
    # False — and every later assertion would then be about silence rather than about the rule.
    assert all(t["one in flight"]["disabled"]), (
        "one request in flight has to mute every submit button on the page, not only the one that was "
        "clicked — the sibling generator buttons are exactly what buys the duplicate call")
    assert t["one in flight"]["busy"] is True, "the page has to say it is working, not only look it"
    assert all(t["two in flight"]["disabled"])

    assert all(t["first finished, second still running"]["disabled"]), (
        "the first response must not hand the reader live buttons while a second call is still in "
        "flight — that needs a count, not a flag")
    assert not any(t["both finished"]["disabled"]), "the page has to come back when the work is done"
    assert t["both finished"]["busy"] is False

    # A swap replaces the region mid-flight; the incoming markup carries no disabled attribute.
    assert not any(t["swapped in, before afterSwap"]["disabled"]), (
        "precondition: swapped-in buttons really do arrive enabled, so the next assertion is repairing "
        "something rather than observing a no-op")
    assert all(t["swapped in, after afterSwap"]["disabled"]), (
        "htmx:afterSwap has to re-assert the busy state over markup the swap just brought in")
    assert not any(t["after the swap, request finished"]["disabled"])

    # bfcache: the shipped asset left a button disabled forever here, because the reset it does have
    # only ever touched the progress bar.
    assert all(t["in flight before pageshow"]["disabled"])
    assert not any(t["after pageshow"]["disabled"]), (
        "returning to a cached page must not restore it with its buttons still muted")


def test_the_generator_forms_all_target_one_region_which_is_why_the_rule_is_page_wide(client,
                                                                                     monkeypatch):
    """The server-side half of #50, and the reason the rule cannot live in a form.

    The JS test above needs Node; this one needs nothing, and pins the precondition that makes the
    defect possible — several sibling forms whose responses all land on the same target. If a later
    change gave each generator its own region, that would be a different (and also valid) fix, and this
    test is where the argument would surface instead of the page-wide rule quietly becoming pointless.

    **It is green on both sides of this change, deliberately.** It characterises the shape the fix
    reasons about; it does not detect the fix's absence, and it is not evidence that the fix works.
    That evidence is the Node test above, which fails on the shipped asset as `[True, False, False]`.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")   # the toolbar only shows with a provider
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    page = client.get("/sessions/leave-approval").text
    targets = re.findall(r'hx-post="/sessions/leave-approval/artifacts/[^"]+"[^>]*?'
                         r'hx-target="([^"]+)"', page, re.S)
    assert len(targets) > 1, "expected several generator forms on the page, found: " + repr(targets)
    assert set(targets) == {"#artifacts-region"}, (
        "every generator form posts to one region, so their responses collide — mutual exclusion has "
        "to be page-wide (#50)")


def test_every_rendered_button_is_reachable_by_the_page_wide_busy_rule(client, monkeypatch):
    """The busy rule selects `button[type="submit"]`, so a button without that attribute escapes it.

    HTML makes `type="submit"` the default for a bare `<button>` inside a form, which is exactly what
    makes this worth pinning: such a button submits, buys a provider call, and is invisible to the one
    selector that is supposed to mute it. It would reproduce #50 for that button alone while every test
    here stayed green — the same shape as the original defect, one attribute lower.

    A CSS-side rule cannot be asserted from Python, and the selector lives in `app.js`; what is
    assertable here is the other half of the contract, which is that the markup holds up its end.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    pages = {path: client.get(path).text for path in ("/", "/sessions/leave-approval")}

    for path, html in pages.items():
        opening_tags = re.findall(r"<button\b[^>]*>", html)
        # must fire: these pages really do render buttons, so the loop below is checking something
        # rather than iterating over nothing.
        assert opening_tags, f"expected buttons on {path}, found none — this assertion saw nothing"
        for tag in opening_tags:
            assert 'type="submit"' in tag, (
                f'{path} renders a button with no explicit type="submit", so the page-wide busy rule '
                f"in static/js/app.js cannot reach it and it can still buy a provider call: {tag}")


# ── error responses reach the eye (#203) ─────────────────────────────────────


def _swap_decisions() -> dict[int, dict]:
    """Execute the real `static/js/app.js` against htmx's own swap gate and report, per status,
    whether the page would render the response.

    Skips loudly without node, for the reason `_busy_timeline` gives: a green run must not imply
    coverage of browser behaviour nothing in this suite can otherwise see.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH, so the error-swap opt-in in static/js/app.js was NOT "
                    "asserted in this run — it is browser behaviour and nothing else in this suite "
                    "can see it (#203)")
    app_js = TEMPLATES_DIR.parent / "static" / "js" / "app.js"
    proc = subprocess.run([node, str(_ERROR_SWAP_HARNESS), str(app_js)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 0, "the harness itself failed, so nothing was observed:\n" + proc.stderr
    return {row["status"]: row for row in json.loads(proc.stdout)}


def test_error_responses_are_swapped_into_the_page_rather_than_dropped():
    """Every 4xx/5xx fragment this app builds was invisible in a real browser (#203).

    The vendored htmx swaps only 200-399, and nothing opted in. So a revision conflict from a second
    tab, the 413 that #30 built to keep the reader's typed answers, and a 502 after a minutes-long
    *paid* generation all produced the same thing: the progress bar completed, the buttons came back,
    the page did not change, and nothing was said. On the paid one the natural next move is to click
    again and pay again. The entire server-side error architecture — the status table, the fragments,
    #30's keep-your-work region — was dead code past the network boundary, and `TestClient` runs no
    JavaScript, so the Python suite was asserting bodies the browser never rendered.

    **The control is the `htmxWouldSwap` column**, and it is what makes this test mean anything: it
    records htmx's own decision before our listener runs. Without it, a harness that swapped
    everything by construction would pass while proving nothing about the fix.
    """
    d = _swap_decisions()

    # must fire: the defect, still present in the vendored library, is that htmx drops all of these.
    # If this ever goes green on its own the harness has stopped modelling htmx and every row below
    # is measuring itself.
    assert not any(d[s]["htmxWouldSwap"] for s in (400, 403, 409, 413, 500, 502)), (
        "the harness no longer reproduces htmx's swap gate, so the rows below assert nothing"
    )

    for status in (409, 413, 502):
        assert d[status]["swapped"] is True, (
            f"a {status} response is still dropped, so the reader sees a completed progress bar and "
            f"an unchanged page — for 502 that is an invitation to buy the same call twice"
        )
    for status in (400, 403, 500):
        assert d[status]["swapped"] is True, f"a {status} response is still invisible"

    assert all(d[s]["isError"] is False for s in (409, 413, 502)), (
        "a handled, rendered response should stop logging as an uncaught one"
    )

    # The other direction, which a blanket `shouldSwap = true` would quietly break: a success must
    # still swap, and htmx's own 204 "no content, change nothing" must still be honoured.
    assert d[200]["swapped"] is True, "the opt-in broke ordinary successful swaps"
    assert d[204]["swapped"] is False, (
        "204 means 'nothing to render' and htmx is right to skip it; the opt-in must not reach below "
        "400 and turn it into a swap of an empty body"
    )


# ── a failed first analysis is not a dead end (#207) ─────────────────────────


@pytest.fixture
def failing_analysis(monkeypatch):
    """A provider that claims the session fine and then fails the paid call — the transient-529 shape.

    `claim_session` stays real on purpose, because the whole point of #207 is that the request *is*
    already safely on disk when the call fails. A fake that failed earlier would test a different bug.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    def boom(self, slug, *, surface="discover"):
        raise EngineError("Anthropic API unavailable (529).")

    monkeypatch.setattr(DiscoveryService, "run_discovery", boom)


def test_a_failed_first_analysis_lands_on_the_session_that_was_saved(client, with_provider,
                                                                     failing_analysis):
    """`start()` claims the session *before* the provider call, deliberately, so a refusal costs
    nothing — which means that when the call fails the pasted email is already safe at revision 0 and
    the session's own page already renders the 'Analyse request' retry button.

    The route let `EngineError` propagate anyway, so it was mapped to 502 and `errors/500.html`:
    "Something went wrong… check the server logs. Back to sessions." Nothing said the request had been
    saved or where, so a first-time user on a transient API error reasonably concludes the product ate
    what they pasted. The good outcome was one redirect away the whole time.
    """
    with_provider()
    r = client.post("/sessions", data={"request_text": "A leave approval system.",
                                       "provider": "anthropic"}, follow_redirects=True)

    assert r.status_code == 200, "a failed first analysis still dead-ends on an error page"
    assert "Your request was saved" in r.text
    assert "Anthropic API unavailable" in r.text, "the cause was dropped, so the reader cannot act"
    assert "A leave approval system." in r.text, "the page does not show the request it saved"
    assert "Analyse request" in r.text, "the retry button the whole fix rests on is not on the page"

    metas = SessionService().list_sessions()
    assert [m.current_revision for m in metas] == [0]


def test_a_failed_retry_from_the_pending_page_re_renders_it_rather_than_a_500(client, with_provider,
                                                                             failing_analysis):
    """The second door onto the same first analysis. Both must fail into the same place, or the
    reader's experience depends on which button they happened to press."""
    with_provider()
    slug = SessionService().create_session("A leave approval system.", slug="leave").slug

    r = client.post(f"/sessions/{slug}/discover", follow_redirects=True)

    assert r.status_code == 200
    assert "Your request was saved" in r.text and "Anthropic API unavailable" in r.text
    assert "Analyse request" in r.text


def test_the_revision_zero_gate_still_holds_after_a_failed_analysis(client, with_provider,
                                                                   monkeypatch):
    """The must-fire half: recovering from the failure must not have cost the gate. A session left at
    revision 0 by a failed call is still a session a *successful* analysis may land on — and exactly
    once."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    real = DiscoveryService.run_discovery
    monkeypatch.setattr(DiscoveryService, "run_discovery",
                        lambda self, slug, *, surface="discover": (_ for _ in ()).throw(
                            EngineError("Anthropic API unavailable (529).")))

    with_provider(engine_reply())
    client.post("/sessions", data={"request_text": "A leave approval system.",
                                   "provider": "anthropic"}, follow_redirects=True)
    slug = SessionService().list_sessions()[0].slug

    monkeypatch.setattr(DiscoveryService, "run_discovery", real)
    r = client.post(f"/sessions/{slug}/discover", follow_redirects=True)

    assert r.status_code == 200
    assert SessionService().meta(slug).current_revision == 1, (
        "the retry after a failed analysis did not land, so the recovery path is a cul-de-sac"
    )


def test_an_error_fragment_retargets_but_a_full_region_keeps_its_own_target(client, with_provider):
    """The server half of #203, and the distinction is the whole design.

    Once `app.js` swaps 4xx/5xx, *where* they land decides whether the fix helps or repeats #30. The
    one-line `errors/_error.html` fragment carries `HX-Retarget: #flash`, because the form's own
    target is `#session-body` — the region holding the textarea the reader just typed into, which a
    one-line notice would replace wholesale. The 413 answers refusal returns the **full region** with
    the submission still in it, so it must keep the form's target and carry no retarget at all.

    Asserted together, in one test, because the bug is the pair being wrong relative to each other: a
    retarget on both loses #30's preserved answers, and a retarget on neither destroys the form on
    every conflict.
    """
    # One reply, because the conflict below is only reached *after* the provider call — the answers
    # turn reasons first and checks `expected_revision` at the apply. That ordering is #205's subject,
    # not this test's; noted so the reply count reads as a fact about the route rather than padding.
    with_provider(engine_reply())
    slug = _make_session()

    oversized = client.post(f"/sessions/{slug}/answers",
                            data={"answers": "x" * (MAX_ANSWERS_CHARS + 1), "expected_revision": "1"},
                            headers={"HX-Request": "true"})
    assert oversized.status_code == 413
    assert "HX-Retarget" not in oversized.headers, (
        "the full-region refusal was retargeted, so #30's preserved answers land in the flash strip "
        "and the form they were typed into is left holding the stale text"
    )
    assert "<textarea" in oversized.text and "x" * 300 in oversized.text

    conflict = client.post(f"/sessions/{slug}/answers",
                           data={"answers": "The HR lead approves.", "expected_revision": "0"},
                           headers={"HX-Request": "true"})
    assert conflict.status_code == 409
    assert conflict.headers["HX-Retarget"] == "#flash", (
        "the conflict notice would swap over #session-body and delete the answers form — #30 again, "
        "reintroduced by the very change that made errors visible"
    )
    assert conflict.headers["HX-Reswap"] == "innerHTML"
    assert "notice danger" in conflict.text


def test_every_page_carries_the_flash_region_the_retarget_aims_at(client):
    """`HX-Retarget: #flash` is a promise about the document, not about the response. If the element
    is missing htmx has nowhere to put the notice and the error is silently invisible again — the
    exact failure #203 exists to end, restored by a template edit that looked cosmetic."""
    slug = _make_session()
    for path in ("/", f"/sessions/{slug}"):
        assert 'id="flash"' in client.get(path).text, f"{path} has no flash region to retarget into"
