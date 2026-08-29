"""Requivo Web security: the slug guard, the cross-site layers, and what may not reach the browser.

Split out of `test_web.py` by #142, and the one file in that split whose separation is not an argument
about size. Among sixty tests about routing and templates, a security assertion that stops being
collected looks exactly like one that passes. Here the list is short enough that a missing test is a
shorter list, and the file is one somebody opens on purpose.

Offline (a fake provider), isolated workspace per test; the fixtures and the seeded-session helper
live in `tests/web/conftest.py`.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from requivo.web.security import (
    CSRF_FIELD,
    CSRF_HEADER,
    MissingRequestTokenError,
    _enforce,
    _same_trust_domain,
    csrf_token,
)
from tests.web.conftest import HIGH_EXPLICIT, _make_session, full_model

# ── the headers every response carries ────────────────────────────────────────


def test_security_headers_present(client):
    h = client.get("/").headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in h and "default-src 'self'" in h["Content-Security-Policy"]
    # Presence only. Which value is correct is not a spelling to pin here — it is a decision, and it is
    # argued and asserted by its consequence in
    # `test_the_policy_this_app_sends_and_the_origin_guard_it_runs_agree` (#47). What belongs here is
    # that the app states the policy rather than inheriting whatever the browser defaults to.
    assert "Referrer-Policy" in h


# ── the slug guard ────────────────────────────────────────────────────────────

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


def test_a_token_this_server_cannot_compare_is_refused_rather_than_crashing(raw_client):
    """A token the comparison cannot read is a *wrong token*, not an unhandled exception (#212).

    `secrets.compare_digest` on two `str` arguments raises `TypeError` unless both are ASCII-only, and
    `_submitted_token` returns whatever the client sent: a form field decoded as UTF-8, or a header
    Starlette decoded as latin-1. So one non-ASCII character turned the guard's clean 403 into a
    `TypeError` that escaped `_guard`'s two `except` arms, propagated *past* the `security_headers`
    middleware and landed on the outermost 500 handler — meaning the security module's only crash path
    was also the one response class that shipped with no CSP, no nosniff and no Referrer-Policy.

    The headers assertion is the point rather than a bonus. A fix that returned 403 by catching
    `TypeError` at the wrong layer would satisfy the status code and still answer without them, which
    is the half of the defect nobody would notice.
    """
    hostile = raw_client.post(
        "/sessions",
        data={"request_text": "x", "provider": "create_only", CSRF_FIELD: "é"})
    assert hostile.status_code == 403, "a token that cannot be compared has to be refused, not crash"
    assert "missing_request_token" in hostile.text
    for header in ("Content-Security-Policy", "X-Content-Type-Options", "Referrer-Policy"):
        assert header in hostile.headers, (
            f"the refusal answered without {header} — it escaped the header middleware")

    # Must fire. An ASCII wrong token already took this path before the fix, so without this control
    # the assertions above would also pass against a guard that refused *every* token, headers and
    # all, and told us nothing about the non-ASCII one.
    control = raw_client.post(
        "/sessions",
        data={"request_text": "x", "provider": "create_only", CSRF_FIELD: "wrong"})
    assert control.status_code == 403
    assert "Content-Security-Policy" in control.headers

    # …and the other must-fire half: a *valid* token still passes, so the fix did not close the door
    # on the browser path it exists to serve.
    ok = raw_client.post(
        "/sessions",
        data={"request_text": "x", "slug": "still-works", "provider": "create_only",
              CSRF_FIELD: csrf_token()},
        follow_redirects=False)
    assert ok.status_code == 303


def test_a_latin1_token_header_reaches_the_refusal_rather_than_the_comparison(app):
    """The header half of the same defect, driven at the ASGI seam where it actually arrives.

    Starlette decodes header bytes as latin-1, so a raw 0xe9 byte in `x-csrf-token` becomes a
    one-character non-ASCII `str` before `_enforce` ever sees it — a value no HTTP client library will
    let a test send through the ordinary API, because httpx encodes request headers as ASCII. Driving
    the scope directly is what makes this leg assertable at all; it is the same input a proxy or a
    mangling client can put on the wire.
    """
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/sessions",
        "raw_path": b"/sessions",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [(b"host", b"127.0.0.1:8765"), (b"x-csrf-token", b"\xe9")],
        "client": ("127.0.0.1", 1234),
        "app": app,
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    with pytest.raises(MissingRequestTokenError):
        asyncio.run(_enforce(Request(scope, receive)))


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


def test_a_host_the_server_cannot_determine_is_refused_rather_than_skipped(app):
    """#45: the allowlist used to read `if host and host not in allowed_hosts()`, so `""` — what
    `_hostname` returns when it could not determine a host at all — skipped the check entirely rather
    than failing it. That is this project's own house defect class landing in a security module: a
    check that cannot read its input treats that as *no check needed* instead of as a refusal, and
    reports nothing while it is off. Because the host allowlist is the one check that also runs on
    reads, the skip took a plain `GET` past the only DNS-rebinding guard there is.

    Reported observed at the socket against the 0.10.1 candidate: `GET / HTTP/1.1` with an empty
    `Host:` answered 200. `TestClient` reproduces that row exactly — httpx forwards `Host: ""` and it
    arrives in the ASGI scope as `(b"host", b"")`, which is the same value uvicorn hands the
    middleware.

    The accept beside it is the control, and it is not decoration: a refusal-only test passes against
    a guard that refuses every request, which is a worse outcome than the bug it was written for. So a
    determined loopback host on a plain `GET` — the everyday read path — must still answer 200 in this
    same fixture.
    """
    c = TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)

    empty = c.get("/", headers={"Host": ""})
    assert empty.status_code == 403
    # Names its own arm rather than borrowing the generic another-origin wording, as #43 did for the
    # opaque origin: a guard that could not look must not print what a guard that looked and refused
    # prints. Since #52 that arm has its own **code**, which is the stable identifier
    # `docs/compatibility.md` says to assert on — this used to match the message text because
    # `cross_site_request` was raised for all six arms and the wording was the only handle there was.
    assert "undetermined_host" in empty.text

    # whitespace-only is the same undetermined state by a different spelling — `_hostname` strips first
    assert c.get("/", headers={"Host": "   "}).status_code == 403

    # must still pass, same fixture: a determined loopback host on a read
    assert c.get("/").status_code == 200
    assert c.get("/", headers={"Host": "localhost:8765"}).status_code == 200


def test_a_request_that_states_no_host_at_all_is_refused(app):
    """The other observed row: `GET / HTTP/1.0` with **no** `Host` header, which h11 admits (it only
    requires `Host` on HTTP/1.1) and which answered 200.

    Driven against `_enforce` over a hand-built ASGI scope rather than over HTTP, because no client
    this suite can build will omit the header — httpx raises `TypeError: Header value must be str or
    bytes` on a `None` value, and `TestClient` always derives one from `base_url`. The scope below is
    what uvicorn hands the middleware for that request: headers verbatim, no `host` among them. Run
    through `asyncio.run` so this needs no async plugin, which the dev extra does not carry.

    The determined-host scope is asserted first and is the must-fire control — without it a hand-built
    scope that was simply malformed, or an `_enforce` that raised on everything, would pass this test
    while checking nothing.
    """
    import asyncio

    from starlette.requests import Request

    from requivo.web.security import CrossSiteRequestError, _enforce

    def verdict(headers: list[tuple[bytes, bytes]]) -> str:
        """`"accepted"`, or the refusal's error **code** — never a bare boolean, so the arm is visible
        here. The code rather than the message since #52: each arm now carries its own, and a code is
        the identifier `docs/compatibility.md` says to assert on."""
        async def run() -> str:
            scope = {"type": "http", "method": "GET", "path": "/", "query_string": b"",
                     "headers": headers}
            try:
                await _enforce(Request(scope))
            except CrossSiteRequestError as exc:
                return exc.code
            return "accepted"
        return asyncio.run(run())

    assert verdict([(b"host", b"127.0.0.1:8765")]) == "accepted"          # must fire
    assert verdict([]) == "undetermined_host"                            # no Host header at all
    assert verdict([(b"host", b"evil.example")]) == "host_not_allowed"   # and the mismatch arm survives


# ── the origin check: which hostnames are one trust domain (#43) ──────────────

def _guard_post(app, *, host: str, slug: str, headers: dict | None = None):
    """One write, addressed to `host`, carrying a valid request token — so the only thing under test is
    the origin check. Redirects are not followed, so an accepted write reads as 303 and a refused one as
    403 rather than both landing on a rendered page."""
    c = TestClient(app, base_url=f"http://{host}", raise_server_exceptions=False)
    c.headers[CSRF_HEADER] = csrf_token()
    return c.post("/sessions",
                  data={"request_text": "A leave approval system.", "slug": slug,
                        "provider": "create_only"},
                  headers=headers or {}, follow_redirects=False)


def test_the_loopback_spellings_are_one_origin_and_evil_example_still_is_not(app):
    """#43: `localhost`, `127.0.0.1` and `::1` are three spellings of one machine — this module's own
    host allowlist already treats them as interchangeable — and the origin check compared them as
    strings, so a page served on one spelling could not post to the other. Reported from a real browser
    on the 0.10.0 wheel: the form could not be submitted at all, and the natural recovery (change the
    address, submit again) resubmits with the stale `Origin` and reproduces the same 403.

    The refusal half is not optional and lives in this fixture on purpose: an acceptance-only test
    passes just as well against a guard that has been deleted.
    """
    assert _guard_post(app, host="127.0.0.1:8765", slug="lb-localhost-origin",
                       headers={"Origin": "http://localhost:8765"}).status_code == 303
    assert _guard_post(app, host="localhost:8765", slug="lb-ipv4-origin",
                       headers={"Origin": "http://127.0.0.1:8765"}).status_code == 303
    assert _guard_post(app, host="127.0.0.1:8765", slug="lb-ipv6-origin",
                       headers={"Origin": "http://[::1]:8765"}).status_code == 303
    # must still fire — same fixture, same valid token, only the origin differs
    assert _guard_post(app, host="127.0.0.1:8765", slug="hostile-vs-ipv4",
                       headers={"Origin": "http://evil.example"}).status_code == 403
    assert _guard_post(app, host="localhost:8765", slug="hostile-vs-localhost",
                       headers={"Origin": "http://evil.example"}).status_code == 403


def test_a_referer_gets_the_same_equivalence_and_the_same_refusal(app):
    """`Referer` is the fallback the same line reads, so it has to move with `Origin` — the reporter's
    probe measured both, and a fix that widened only one would leave half the dead end in place."""
    assert _guard_post(app, host="127.0.0.1:8765", slug="ref-loopback",
                       headers={"Referer": "http://localhost:8765/sessions"}).status_code == 303
    # must still fire
    assert _guard_post(app, host="127.0.0.1:8765", slug="ref-hostile",
                       headers={"Referer": "http://evil.example/x"}).status_code == 403


def test_the_opaque_origin_is_refused_deliberately_and_says_which_arm_fired(app):
    """`Origin: null` is a browser declining to attribute the context it is posting from. Before #43 it
    was refused only by accident — `_hostname("null")` returns the literal string `"null"`, which then
    failed an equality test — and an accident is not a decision. It is refused on purpose now, and the
    asymmetry with an *absent* origin is the reason rather than an oversight: browsers attach `Origin`
    to every POST, so silence means no browser is speaking, while `null` is the one origin a
    browser-borne attacker can choose to emit (a sandboxed cross-site frame).

    The accept beside it is the control. Without it a harness that refused everything — a broken app
    fixture, a token that stopped matching — would pass this test while checking nothing.
    """
    r = _guard_post(app, host="127.0.0.1:8765", slug="opaque-origin", headers={"Origin": "null"})
    assert r.status_code == 403
    assert "opaque origin" in r.text          # the opaque arm, not the generic another-origin refusal
    # must be accepted, same fixture
    assert _guard_post(app, host="127.0.0.1:8765", slug="attributed-origin",
                       headers={"Origin": "http://localhost:8765"}).status_code == 303


# Fetch's *append a request `Origin` header* consults the referrer policy for any request that is not
# CORS-mode — an ordinary HTML form submit is a navigation, not CORS — and whose method is not
# GET/HEAD. This table is that algorithm restricted to the only case this app's plain forms produce: a
# **same-origin** post over plain HTTP. Only `no-referrer` replaces the origin with the opaque value.
# The downgrade-sensitive policies null it solely on an HTTPS→HTTP downgrade, which a same-origin
# request cannot be, and `same-origin` nulls it solely when the request is genuinely cross-origin.
_ORIGIN_A_BROWSER_SENDS_ON_A_SAME_ORIGIN_FORM_POST = {
    "no-referrer": "null",
    "no-referrer-when-downgrade": "SELF",
    "origin": "SELF",
    "origin-when-cross-origin": "SELF",
    "same-origin": "SELF",
    "strict-origin": "SELF",
    "strict-origin-when-cross-origin": "SELF",
    "unsafe-url": "SELF",
}


def test_the_policy_this_app_sends_and_the_origin_guard_it_runs_agree(app, client):
    """The header this app emits must not produce an `Origin` this app's own guard refuses (#47).

    Neither half was wrong alone, which is exactly why nothing caught it. `Referrer-Policy:
    no-referrer` is a defensible header. Refusing the opaque origin is a deliberate decision with its
    own test three functions up (#43). The defect lived only in the composition: a same-origin form
    post, carrying a valid token, arriving as `Origin: null` because of a header this server had just
    sent, and then refused by the server that sent it. Both files were individually green, so no
    per-file test could see it and none did — the product's entry path was unusable in Chrome for a
    release.

    **What this does not do is reproduce the browser.** `TestClient` implements no referrer policy, and
    neither does `curl` — which is why the maintainer's own header-matrix probes reached two wrong
    diagnoses before the cause was found by reading what the *server sends*. So the browser's half is
    supplied from the table above rather than executed. What is genuinely under test is the pair of
    facts either side of it: the header this app really emits, read off a real response, and the real
    guard's real verdict on the origin that header implies. A browser-engine test remains the missing
    coverage and is out of this change's scope.
    """
    policy = client.get("/").headers["Referrer-Policy"]
    assert policy in _ORIGIN_A_BROWSER_SENDS_ON_A_SAME_ORIGIN_FORM_POST, (
        f"Referrer-Policy {policy!r} is not in the table this test reasons over. Add what Fetch says "
        "it does to a same-origin form post; do not drop the assertion.")
    origin = _ORIGIN_A_BROWSER_SENDS_ON_A_SAME_ORIGIN_FORM_POST[policy]
    if origin == "SELF":
        origin = "http://127.0.0.1:8765"

    # must fire: the guard really is refusing opaque origins in this fixture. Without it the acceptance
    # below would read exactly the same against a guard that had been deleted — and `no-referrer`
    # shipping a second time would then land green.
    assert _guard_post(app, host="127.0.0.1:8765", slug="composed-opaque",
                       headers={"Origin": "null"}).status_code == 403

    assert _guard_post(app, host="127.0.0.1:8765", slug="composed-real",
                       headers={"Origin": origin}).status_code == 303, (
        f"Referrer-Policy: {policy} makes a browser attach Origin: {origin} to a same-origin form "
        "post, and this app's own cross-site guard refuses it — the form cannot be submitted (#47)")


def test_no_origin_headers_at_all_keeps_its_current_behaviour(app):
    """A scripted client sends neither header and the request token is what gates it — `curl` with a
    valid token is a supported caller, and this suite's own posts are that caller. #43 asked whether the
    absent case should be tightened to match `null`; it is deliberately left alone, so it is pinned here
    rather than left implicit, with the token-less post beside it as the control that the write path is
    still guarded at all."""
    assert _guard_post(app, host="127.0.0.1:8765", slug="no-origin-stated").status_code == 303
    bare = TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
    assert bare.post("/sessions", data={"request_text": "x", "slug": "no-token-at-all",
                                        "provider": "create_only"}).status_code == 403


def test_two_hostnames_nobody_could_determine_are_not_a_match():
    """`_hostname` returns `""` when it could not find a hostname at all — an absent or unparseable
    `Host`, or an origin like `http:///` that is a valid URL naming nobody. Two of those facing each
    other compared equal and read as *same trust domain*, so the one input where **neither** side was
    determined produced the same verdict as a verified match. A check that could not look has to say
    so rather than answer; found by the audit on this diff, and fixed here because the extracted helper
    is what makes the claim by name.

    Asserted directly rather than over HTTP: no client this suite can build will omit a `Host` header,
    so the integration route cannot reach the state. The two rows below it are the must-fire control —
    without them a helper hard-wired to `False` would pass this test while checking nothing.
    """
    assert _same_trust_domain("", "") is False              # neither side determined — not a match
    assert _same_trust_domain("", "127.0.0.1") is False
    assert _same_trust_domain("127.0.0.1", "") is False
    # must fire
    assert _same_trust_domain("localhost", "127.0.0.1") is True
    assert _same_trust_domain("evil.example", "evil.example") is True
    assert _same_trust_domain("evil.example", "127.0.0.1") is False


def test_a_cross_port_loopback_origin_is_accepted_and_that_is_the_decision(app):
    """#46: `_hostname` discards the port on both sides, so the set this check accepts is *any page on
    any loopback port* — not, as the docstring used to claim, a page that can only have come from this
    process. The claim was false; the behaviour it described is deliberate and is pinned here.

    Why it is deliberate rather than tolerated: the request token is what gates the write, and a page
    on another loopback port cannot obtain it. The browser's own same-origin policy is (scheme, host,
    port), so `http://localhost:3000` reading a page served on `:8765` is a cross-origin read, this app
    sets no CORS headers, and the response body is unreadable to it. `Sec-Fetch-Site` refuses the
    cross-port post as `same-site` before this line is even reached, on every browser that sends it.
    Comparing the port here would add nothing those two do not already do, and it is the exact shape of
    false positive that #43 was: a default port elided in an `Origin` but present in a `Host` refuses a
    form with no way forward.

    Pinned so that making the comparison port-exact is a change somebody has to argue with — and
    against the docstring — rather than a quiet tightening. This is a characterization test for a
    deliberate non-change: it passed before #46 was addressed and passes after, which is the point.
    The hostile row beside it is the must-fire control.
    """
    assert _guard_post(app, host="127.0.0.1:8765", slug="cross-port-loopback",
                       headers={"Origin": "http://localhost:3000"}).status_code == 303
    # must still fire — a port is not what makes a foreign host acceptable either
    assert _guard_post(app, host="127.0.0.1:8765", slug="cross-port-hostile",
                       headers={"Origin": "http://evil.example:8765"}).status_code == 403


def test_operator_listed_hosts_are_not_interchangeable_with_one_another(app, monkeypatch):
    """The equivalence is the fixed loopback set this module defines, never whatever an operator put in
    `REQUIVO_WEB_ALLOWED_HOSTS`. Those are real hostnames and two of them may well be meant as two
    distinct origins; a blanket `allowed_hosts()` membership test would have made that call on the
    operator's behalf. Each is still same-origin with itself, which is the accept half."""
    monkeypatch.setenv("REQUIVO_WEB_ALLOWED_HOSTS", "app.internal,admin.internal")
    assert _guard_post(app, host="app.internal", slug="named-self",
                       headers={"Origin": "http://app.internal"}).status_code == 303
    # must still fire, in both directions — loopback does not launder into the opt-in set either
    assert _guard_post(app, host="app.internal", slug="named-sibling",
                       headers={"Origin": "http://admin.internal"}).status_code == 403
    assert _guard_post(app, host="app.internal", slug="loopback-into-named",
                       headers={"Origin": "http://localhost:8765"}).status_code == 403
    assert _guard_post(app, host="127.0.0.1:8765", slug="named-into-loopback",
                       headers={"Origin": "http://app.internal"}).status_code == 403


# ── what must not reach the browser ───────────────────────────────────────────

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
