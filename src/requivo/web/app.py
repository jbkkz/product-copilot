"""The FastAPI application factory for Requivo Web.

`create_app()` wires the routers, mounts the local static files (and *only* those — never the
workspace, `.requivo`, `.env` or `.git`), sets conservative security headers, and turns structured
`RequivoError`s into clean pages (never a traceback). It is a factory: importing this module binds no
port and starts no server — `requivo web` calls it.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles

from requivo.core.errors import RequivoError
from requivo.providers.anthropic import EngineError
from requivo.web.routes import artifacts, discovery, health, home, sessions
from requivo.web.security import install_cross_site_guard
from requivo.web.templating import STATIC_DIR, templates

# Map a structured error code to an HTTP status. Anything unlisted is a 400 (bad request); an
# EngineError (provider transport) is handled separately as a 502 (upstream failure).
_STATUS_BY_CODE = {
    "session_not_found": 404,
    "invalid_slug": 400,
    "invalid_model": 400,
    "unknown_slot": 400,
    "unknown_context_card": 400,
    "missing_required_slot": 400,
    "invalid_session": 400,
    "input_too_large": 413,
    "revision_conflict": 409,
    "stale_artifact": 409,
    "unknown_artifact_type": 400,
    "cross_site_request": 403,
}

# A locked-down CSP: everything is same-origin, images may be inline data URIs. No external hosts, so
# the vendored HTMX and local CSS are the only scripts/styles — nothing loads from a CDN.
_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


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
        # Provider transport failures are an upstream problem (502); everything else maps by code.
        status = 502 if isinstance(exc, EngineError) else _STATUS_BY_CODE.get(exc.code, 400)
        return _render_error(request, status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        return _render_error(request, 400, "invalid_request", "The form submission was not valid.")

    @app.exception_handler(404)
    async def _not_found(request: Request, exc):
        return _render_error(request, 404, "not_found", "That page does not exist.")

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        # No traceback to the browser; the code is a stable handle for the logs.
        return _render_error(request, 500, "internal_error", "Something went wrong on the server.")

    app.include_router(health.router)
    app.include_router(home.router)
    app.include_router(sessions.router)
    app.include_router(discovery.router)
    app.include_router(artifacts.router)
    return app
