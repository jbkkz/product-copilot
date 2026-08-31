"""Liveness endpoint — a cheap 200 for the CI smoke test and any local health check — and the one
other route small enough to sit beside it: the browser's own implicit favicon probe."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from requivo import __version__
from requivo.web.templating import STATIC_DIR

router = APIRouter()


@router.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "requivo-web", "version": __version__})


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    """A browser requests `/favicon.ico` itself, regardless of what `<link rel="icon">` names — this
    is what stopped that request from 404ing into the operator's logs on every page load (#241).

    The brand mark is an SVG (`base.html` already draws it inline; `static/favicon.svg` is the same
    shape as a standalone file), served here under `.ico` with an honest `image/svg+xml` media type
    rather than a synthesized binary ICO — modern browsers render whatever image format the response
    actually carries. No sized PNG/ICO fallback is shipped: it would need real image-generation
    tooling to produce correctly rather than a hand-authored guess at the format, for a browser
    population that, on an evergreen product like this one, already renders SVG icons natively.
    """
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")
