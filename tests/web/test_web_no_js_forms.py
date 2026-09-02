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
from tests.web.conftest import BRIEF_REPLY, HIGH_EXPLICIT, HIGH_INFERRED, Spend, engine_reply

# Enough tokens that the rendered figure is unmistakable in a page of other numbers -- same
# constant-naming convention as tests/web/test_web_usage.py's PAID/PAID_TOKENS.
_PAID = Spend(input_tokens=9000, output_tokens=3000, cache_read_input_tokens=400)
_PAID_TOKENS = "12,400"


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

    # `method="post"` has to be required in the SAME tag as `action=` -- a form with `action=` but
    # no `method=` still defaults to GET (the HTML spec, and the exact bug #428 fixes), so an
    # assertion that only checks `action=` exists would stay green if a partial revert dropped just
    # the `method="post"` half of the fallback. Caught in review (#428): the first version of this
    # assertion did exactly that.
    answers_action = re.search(r'<form[^>]*method="post"[^>]*action="(/sessions/leave-approval/answers)"',
                               page)
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


# -- the spend footprint survives the redirect too (review finding, #428) ---------------

def test_a_no_js_answers_submit_still_shows_what_it_spent(raw_client, with_provider):
    """A no-JS submit that reaches the provider is real money, exactly like the htmx path -- and the
    reader who just spent it is looking at the page the 303 lands on, not a fragment. Without
    `carry_to`, `spend.py`'s stash is never written and the figure is gone the moment the redirect's
    response body (which has none) would have carried it."""
    with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED),
                  engine_reply(converged=True, problem=HIGH_EXPLICIT, business_rules=HIGH_EXPLICIT),
                  spend=_PAID)
    _seed(raw_client)
    r = raw_client.post("/sessions/leave-approval/answers",
                        data={"answers": "Exceptions go to HR.", "expected_revision": "1",
                              CSRF_FIELD: csrf_token()},
                        follow_redirects=False)
    assert r.status_code == 303
    landing = raw_client.get(r.headers["location"]).text
    assert _PAID_TOKENS in landing, "the spend the no-JS turn just made is nowhere on the page it lands on"


def test_a_no_js_generate_submit_still_shows_what_it_spent(raw_client, with_provider, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with_provider(engine_reply(converged=True, problem=HIGH_EXPLICIT), BRIEF_REPLY, spend=_PAID)
    _seed(raw_client)
    r = raw_client.post("/sessions/leave-approval/artifacts/brief",
                        data={CSRF_FIELD: csrf_token()}, follow_redirects=False)
    assert r.status_code == 303
    landing = raw_client.get(r.headers["location"]).text
    assert _PAID_TOKENS in landing, "the spend the no-JS generation just made is nowhere on the page it lands on"


def test_a_no_js_redirect_does_not_leave_a_stash_the_next_unrelated_view_would_repeat(raw_client,
                                                                                      with_provider):
    """The htmx path must not gain a `carry_to` it never had: a fragment already shows its own
    figure inline, and stashing it too would surface it a second time on some later, unrelated GET
    of the same session -- the read-once contract `spend.py` documents, broken from the other side."""
    with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED),
                  engine_reply(converged=True, problem=HIGH_EXPLICIT, business_rules=HIGH_EXPLICIT),
                  spend=_PAID)
    _seed(raw_client)
    fragment = raw_client.post("/sessions/leave-approval/answers",
                               data={"answers": "Exceptions go to HR.", "expected_revision": "1",
                                     CSRF_FIELD: csrf_token()},
                               headers={"HX-Request": "true"})
    assert _PAID_TOKENS in fragment.text          # shown once, inline, on the fragment itself
    later = raw_client.get("/sessions/leave-approval").text
    assert _PAID_TOKENS not in later, "a later, unrelated view must not repeat a stashed figure"
