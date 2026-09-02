"""No-JS fallback for the three htmx-only forms (#428).

`app.css:16-17` and `docs/web.md:352` both say the page works fully without JavaScript. Three forms
did not: the answers form (`discovery/_questions.html`) and both generate-document forms
(`artifacts/list.html`) carried `hx-post`/`hx-target`/`hx-swap` and no `method=`/`action=` fallback.
With JavaScript off, a form with neither submits as a plain GET to the page it is on — the typed
answers silently discarded, and the process-lifetime CSRF token plus the full answer text landing in
the URL and browser history.

`raw_client` (`tests/web/conftest.py`) sends no `HX-Request` header, matching a browser with
JavaScript off — exactly what makes `hx-post` never fire in the first place. `client` sends
`HX-Request: true`, modelling htmx actually loaded. Every "no-JS still works" case here is paired
with the ordinary "JS still works" case in the same fixture, so a fix that broke the htmx path would
show up here too, not only in `test_web_discovery.py`.
"""

from __future__ import annotations

import re

from requivo.web.config import MAX_ANSWERS_CHARS
from requivo.web.security import CSRF_FIELD, csrf_token
from tests.web.conftest import BRIEF_REPLY, HIGH_EXPLICIT, HIGH_INFERRED, engine_reply


def _seed(client, slug="leave-approval"):
    client.post("/sessions", data={"request_text": "x", "slug": slug, "provider": "anthropic",
                                   CSRF_FIELD: csrf_token()})


# -- the CSS claim, made checkable ------------------------------------------------

def test_the_three_forms_all_carry_a_plain_post_fallback(client, with_provider, monkeypatch):
    """Static proof the GET-with-token-in-URL shape is unreachable: every htmx-only form also carries
    a method="post" action="..." fallback matching its own hx-post, exactly like every other form in
    the tree (home.html, sessions/detail.html's discover form)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED))
    _seed(client)
    page = client.get("/sessions/leave-approval").text

    answers_action = re.search(r'<form[^>]*action="(/sessions/leave-approval/answers)"', page)
    assert answers_action, "the answers form has no method=post action= fallback"
    assert 'hx-post="/sessions/leave-approval/answers"' in page

    generate_actions = re.findall(
        r'<form[^>]*method="post"[^>]*action="(/sessions/leave-approval/artifacts/[^"]+)"', page)
    assert generate_actions, "no generate-document form carries a method=post action= fallback"


# -- the answers form --------------------------------------------------------------

def test_a_no_js_answers_submit_applies_the_answer_and_lands_on_the_session_page(raw_client,
                                                                                 with_provider):
    with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED),
                  engine_reply(converged=True, problem=HIGH_EXPLICIT, business_rules=HIGH_EXPLICIT))
    _seed(raw_client)
    r = raw_client.post("/sessions/leave-approval/answers",
                        data={"answers": "Exceptions go to HR.", "expected_revision": "1",
                              CSRF_FIELD: csrf_token()},
                        follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/sessions/leave-approval"
    # the second scripted reply only pops if discovery.answer() actually reached the provider --
    # proof the typed answer was folded into the model rather than silently dropped
    page = raw_client.get("/sessions/leave-approval").text
    assert "No question left" in page


def test_a_js_answers_submit_is_unchanged_a_fragment_not_a_redirect(client, with_provider):
    """The must-fire pair of the test above: an htmx-tagged request still gets the fragment swap."""
    with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED),
                  engine_reply(converged=True, problem=HIGH_EXPLICIT, business_rules=HIGH_EXPLICIT))
    _seed(client)
    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": "Exceptions go to HR.", "expected_revision": "1"},
                    follow_redirects=False)
    assert r.status_code == 200
    assert "What changed" in r.text


def test_a_no_js_oversized_answers_submit_keeps_the_typed_text_on_a_full_page(raw_client,
                                                                              with_provider):
    with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED))
    _seed(raw_client)
    too_long = "y" * (MAX_ANSWERS_CHARS + 1)
    r = raw_client.post("/sessions/leave-approval/answers",
                        data={"answers": too_long, "expected_revision": "1",
                              CSRF_FIELD: csrf_token()})
    assert r.status_code == 413
    assert too_long in r.text, "the typed answer must survive the refusal, not be dropped (#30)"
    assert "characters" in r.text
    # a full page, not a bare fragment with no shell
    assert "</html>" in r.text.lower()


# -- the two generate-document forms -----------------------------------------------

def test_a_no_js_generate_submit_saves_the_document_and_lands_on_the_session_page(raw_client,
                                                                                  with_provider,
                                                                                  monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with_provider(engine_reply(converged=True, problem=HIGH_EXPLICIT), BRIEF_REPLY)
    _seed(raw_client)
    r = raw_client.post("/sessions/leave-approval/artifacts/brief",
                        data={CSRF_FIELD: csrf_token()}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/sessions/leave-approval"
    page = raw_client.get("/sessions/leave-approval").text
    assert "Decision brief" in page and "Up to date" in page


def test_a_js_generate_submit_is_unchanged_a_fragment_not_a_redirect(client, with_provider,
                                                                     monkeypatch):
    """The must-fire pair: an htmx-tagged generate request still gets the fragment swap."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with_provider(engine_reply(converged=True, problem=HIGH_EXPLICIT), BRIEF_REPLY)
    _seed(client)
    r = client.post("/sessions/leave-approval/artifacts/brief", follow_redirects=False)
    assert r.status_code == 200
    assert "Decision brief" in r.text
