"""The FastAPI application factory for Requivo Web.

`create_app()` wires the routers, mounts the local static files (and *only* those — never the
workspace, `.requivo`, `.env` or `.git`), sets conservative security headers, and turns structured
`RequivoError`s into clean pages (never a traceback). It is a factory: importing this module binds no
port and starts no server — `requivo web` calls it.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles

from requivo.core.errors import RequivoError
from requivo.providers.anthropic import EngineError
from requivo.web.routes import artifacts, discovery, health, home, sessions
from requivo.web.security import install_cross_site_guard
from requivo.web.templating import STATIC_DIR, templates

# Map a structured error code to an HTTP status.
#
# **Every code gets a row.** This used to default to 400 for anything unlisted, which meant a code
# added in one lane arrived at the browser wearing a plausible, wrong status rather than as an
# obvious gap: `context_unreadable` — the server unable to read its own card directory — was
# reported to the reader as "your request was bad" (#34). Six codes were sitting on that default;
# for two of them 400 happened to be right, four were wrong, and one of the four had been noticed.
# `tests/web/test_web.py::test_every_error_code_has_an_explicit_http_status`
# walks the RequivoError subclasses and fails on the next omission, so the table cannot silently
# fall behind the vocabulary again.
#
# The 4xx/5xx split is the question the default got wrong: is this about what the caller sent, or
# about the state of the server they sent it to?
_STATUS_BY_CODE = {
    # 4xx — the caller's request
    "session_not_found": 404,
    "invalid_slug": 400,
    "invalid_model": 400,
    "unknown_slot": 400,
    "unknown_context_card": 400,
    "missing_required_slot": 400,
    # The malformed-session family, one row per arm since #82. The arms disagree about the 4xx/5xx
    # question — that disagreement is *why* they are eight codes rather than one, and it is the third
    # of the three things that made this split worth doing before a 1.0 (the other two: `details`
    # shapes with no common key, and a documented promise the code could not carry).
    #
    # The family base answers **500**, not the 400 it inherited. Nothing raises it directly, so the
    # number is nominal — but a nominal number is still a number a reader sees, and "a session on disk
    # is malformed" is a fact about the store. The old 400 was the same misattribution #34 fixed for
    # `context_unreadable`: telling the reader their request was bad when the server could not read
    # its own state.
    "invalid_session": 500,           # the family base; nothing raises it directly
    # 409, not 426. Upgrade Required is defined for *connection protocol* negotiation and the spec
    # requires an `Upgrade` header naming what to move to; we have none to send, and the thing needing
    # an upgrade is the reader's Requivo rather than the transport. A status that is exactly right in
    # English and wrong in its RFC is worse than a general one, because the next client is written
    # against the RFC. Conflict is what this is: the resource's state against what this build can do.
    "unsupported_format_version": 409,
    "unsupported_schema_version": 409,
    "session_unreadable": 500,        # the store, not the request
    "artifact_revision_out_of_range": 500,
    "unreadable_source_revision": 500,  # a real revision was stated; the history is what is incomplete
    "inconsistent_archive": 400,      # the caller handed us this archive
    "unreadable_archive": 400,        # …and this one
    # …and the seven shape refusals between them, which kept `invalid_model` — a code about a
    # proposal — until #101. 400 for the same reason as its two siblings, and not 5xx: nothing is
    # written to the store until the archive has passed, so the store is not what is wrong. Not 409
    # either: nothing in the store conflicts with it, and re-sending the same archive unchanged can
    # never succeed. The status does not move — 400 before under `invalid_model`, 400 now — so what
    # a consumer sees change is the code, which is exactly what #101 set out to move.
    "invalid_archive": 400,           # …and the seven shapes in between (#101)
    "import_move_failed": 500,        # the archive was fine and the store refused it
    # 400 and not 409: an `artifact save` with no source revision is not a conflict with the store's
    # state, it is a request that never said the one thing only the caller knows. Nothing about the
    # session is wrong, and the remedy is entirely in the caller's hands — state the revision (#57).
    "unstated_source_revision": 400,
    "invalid_filename": 400,          # a path target the caller supplied
    "empty_selector_token": 400,      # a stray comma in what the caller typed
    "empty_selection": 400,           # a selection the caller supplied that selects nothing
    "unsafe_selector_token": 400,     # a control character in a name the caller supplied
    "unknown_artifact_type": 400,
    # The cross-site family, one row per arm since #52. They are all 403 and the guard renders that
    # status directly rather than reading this table — these rows classify the *codes*, which is what
    # `test_every_error_code_has_an_explicit_http_status` walks and what a consumer sees. The family
    # base keeps its row for the same reason: it is still in the vocabulary, and a code in the
    # vocabulary with no row is exactly the gap that test exists to make loud.
    "cross_site_request": 403,        # the family base; nothing raises it directly
    "undetermined_host": 403,
    "host_not_allowed": 403,
    "cross_site_fetch": 403,
    "opaque_origin": 403,
    "origin_mismatch": 403,
    "missing_request_token": 403,
    "input_too_large": 413,
    # 409 — a conflict with the store's current state, not a malformed request
    "revision_conflict": 409,
    "stale_artifact": 409,
    "session_exists": 409,
    # …and its neighbour, for the destination that holds no session at all. Not 500 beside
    # `import_move_failed`: the store is in a state that conflicts with the request, which is
    # exactly what 409 is for, and nothing failed that a retry could fix (#114).
    "import_destination_occupied": 409,
    # 5xx — the server, or what it depends on
    "context_unreadable": 500,        # we cannot read our own card directory: permissions, usually
    "no_context_cards": 500,          # this install shipped no cards; nothing the caller sent caused it
    "provider_output_invalid": 502,   # upstream would not hold the contract, after every retry
    "session_locked": 503,            # the write never started; retrying it unchanged is correct
}

# What an *unknown* code gets. Deliberately a 5xx: with every known code mapped above, this only
# fires for a code this version has never heard of, and "we could not classify this" is not evidence
# that the caller erred. Blaming the reader for a gap in our own table is the generalised form of the
# bug #34 reports.
_UNCLASSIFIED_STATUS = 500

# A locked-down CSP: everything is same-origin, images may be inline data URIs. No external hosts, so
# the vendored HTMX and local CSS are the only scripts/styles — nothing loads from a CDN.
_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")

# `no-referrer` here made the app unusable in a browser, and the mechanism is worth stating in full
# because both halves were individually correct (#47).
#
# A `Referrer-Policy` governs the requests *our own pages* make. It says nothing about a request some
# other page sends to this server — that is governed by that page's policy, not ours — so this header
# has never been part of the cross-site guard's defence. It only ever constrains us.
#
# What it constrained was the `Origin` header on our own forms. Fetch's *append a request `Origin`
# header* consults the referrer policy for any request that is **not** CORS-mode and whose method is
# not `GET`/`HEAD`, and under `no-referrer` it replaces the serialized origin with the opaque value
# `null`. An ordinary HTML form submit is a navigation, not CORS — so `home.html` (create a session)
# and `sessions/detail.html` (run discovery), the two plain-form posts in this app, arrived carrying
# `Origin: null`. `security._enforce` refuses the opaque origin deliberately (#43). The result was a
# 403 on the product's entry path, from a same-origin request carrying a valid token.
#
# The HTMX posts (answers, generation) go out as XHR, which is CORS-mode, so that clause never applied
# to them and they were unaffected. That asymmetry is why the failure looked like "the form is broken"
# rather than "the app is broken".
#
# `same-origin` is the strictest value that leaves the guard something to read. It sends the full
# referrer within this app and **nothing at all** to any other origin, so the privacy intent of
# `no-referrer` is kept for the only case where it did anything: navigating away. The alternatives and
# what they cost:
#
#   * `strict-origin-when-cross-origin` (the browser default) also fixes it, and hands a third party
#     `http://localhost:8765` on any outbound navigation — a gratuitous "this machine runs Requivo Web
#     on this port" that buys nothing here.
#   * Removing the header defers to the browser's default, which is that same value on current
#     browsers and unstated on older ones. This app states its headers rather than inheriting them.
#   * `origin` / `unsafe-url` leak strictly more, for nothing.
#
# The cost of `same-origin` is that a same-origin `Referer` now carries the full URL, session slug
# included — the reader's own request name, travelling to the server already holding it.
#
# For a *same-origin* request, `no-referrer` is the only policy value that produces `Origin: null`:
# the downgrade-sensitive values (`strict-origin`, `no-referrer-when-downgrade`,
# `strict-origin-when-cross-origin`) null it only on an HTTPS→HTTP downgrade, which same-origin cannot
# be, and `same-origin` itself nulls it only when the request *is* cross-origin.
# `tests/web/test_web.py::test_the_policy_this_app_sends_and_the_origin_guard_it_runs_agree` asserts
# that composition — the defect lived between two files, so no per-file test could see it.
_REFERRER_POLICY = "same-origin"

# Under `requivo web` this rides uvicorn's handler, so a traceback lands in the terminal the user
# started the server in — the only place a local, single-user app has to put one.
logger = logging.getLogger("requivo.web")


def _status_for(exc: RequivoError) -> int:
    """The HTTP status a structured error is reported with.

    A function rather than an inline lookup so the classification can be asserted directly, for every
    code, without driving a request that reaches it — several codes (`session_locked`,
    `provider_output_invalid`) need a race or an upstream failure to reach honestly.

    `EngineError` is answered ahead of the table on purpose: provider transport is a family, not a
    code, so `provider_unavailable` deliberately has no row and the allowlist in the tests records why.
    """
    if isinstance(exc, EngineError):
        return 502
    return _STATUS_BY_CODE.get(exc.code, _UNCLASSIFIED_STATUS)


def _render_error(request: Request, status: int, code: str, message: str):
    """Render an error the way the request expects: a small inline fragment for an HTMX swap, a full
    page otherwise. Never leaks a traceback."""
    ctx = {"status": status, "code": code, "message": message}
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request, "errors/_error.html", ctx, status_code=status)
    template = "errors/404.html" if status == 404 else (
        "errors/500.html" if status >= 500 else "errors/error.html")
    return templates.TemplateResponse(request, template, ctx, status_code=status)


def create_app() -> FastAPI:
    app = FastAPI(title="Requivo Web", docs_url=None, redoc_url=None, openapi_url=None)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Installed before the header middleware so it ends up *inside* it: a request the guard turns away
    # still leaves with the same CSP and nosniff headers as any other response.
    install_cross_site_guard(app, _render_error)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", _REFERRER_POLICY)
        response.headers.setdefault("Content-Security-Policy", _CSP)
        return response

    @app.exception_handler(RequivoError)
    async def _requivo_error(request: Request, exc: RequivoError):
        status = _status_for(exc)
        if status >= 500:
            # A 5xx here is our fault, not the reader's, and the page they get says almost nothing.
            # Without this the operator has no record of a condition the reader cannot act on — the
            # same argument the unexpected-exception handler below already makes.
            logger.error("%s serving %s %s: %s", exc.code, request.method, request.url.path,
                         exc.message)
        return _render_error(request, status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        return _render_error(request, 400, "invalid_request", "The form submission was not valid.")

    @app.exception_handler(404)
    async def _not_found(request: Request, exc):
        return _render_error(request, 404, "not_found", "That page does not exist.")

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        # No traceback to the browser — but it has to go *somewhere*, or an unexpected failure is
        # invisible: the user sees a generic page and the operator has nothing to debug from. The
        # method and path are enough to locate it; the request body is deliberately not logged, since
        # it is the user's own product request.
        logger.exception("unhandled error serving %s %s", request.method, request.url.path)
        return _render_error(request, 500, "internal_error", "Something went wrong on the server.")

    app.include_router(health.router)
    app.include_router(home.router)
    app.include_router(sessions.router)
    app.include_router(discovery.router)
    app.include_router(artifacts.router)
    return app
