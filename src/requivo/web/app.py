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
    "invalid_session": 400,
    "invalid_filename": 400,          # a path target the caller supplied
    "empty_selector_token": 400,      # a stray comma in what the caller typed
    "empty_selection": 400,           # a selection the caller supplied that selects nothing
    "unsafe_selector_token": 400,     # a control character in a name the caller supplied
    "unknown_artifact_type": 400,
    "cross_site_request": 403,
    "input_too_large": 413,
    # 409 — a conflict with the store's current state, not a malformed request
    "revision_conflict": 409,
    "stale_artifact": 409,
    "session_exists": 409,
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
        response.headers.setdefault("Referrer-Policy", "no-referrer")
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
