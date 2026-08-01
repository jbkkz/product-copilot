"""Session routes — create a discovery, list is on home, view one session, export its model."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from requivo.core.context import available_cards, resolve_cards
from requivo.core.errors import InputTooLargeError, SessionNotFoundError
from requivo.core.persistence import validate_slug
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from requivo.web.config import MAX_REQUEST_CHARS, MAX_SLUG_CHARS, provider_status
from requivo.web.dependencies import get_discovery, get_sessions, safe_slug
from requivo.web.templating import templates
from requivo.web.viewmodels.sessions import session_detail

router = APIRouter()


@router.get("/sessions/new")
def new_session(request: Request):
    return templates.TemplateResponse(request, "sessions/new.html", {
        "provider": provider_status(),
        "cards": available_cards(),
    })


@router.post("/sessions")
def create_session(
    request: Request,
    request_text: str = Form(...),
    slug: str = Form(""),
    cards: list[str] = Form(default=[]),
    provider: str = Form("create_only"),
    sessions: SessionService = Depends(get_sessions),
    discovery: DiscoveryService = Depends(get_discovery),
):
    """Create a session from a product request. `provider=anthropic` runs one discovery turn now;
    `create_only` just captures the request (no LLM), to run discovery later."""
    # Bounds are refusals, not truncations: a request silently cut at 20k would be reasoned over as if
    # it were the whole thing, and the user would never learn which half the engine saw.
    text = request_text.strip()
    if len(text) > MAX_REQUEST_CHARS:
        raise InputTooLargeError(
            f"the product request exceeds {MAX_REQUEST_CHARS:,} characters — trim it and resubmit",
            details={"limit": MAX_REQUEST_CHARS, "length": len(text)})
    chosen_slug = slug.strip()
    if len(chosen_slug) > MAX_SLUG_CHARS:
        raise InputTooLargeError(
            f"the session name exceeds {MAX_SLUG_CHARS} characters",
            details={"limit": MAX_SLUG_CHARS, "length": len(chosen_slug)})
    if chosen_slug:
        validate_slug(chosen_slug)  # InvalidSlugError → clean 400
    else:
        chosen_slug = None
    # An unknown card is an error, not something to filter out: dropping it leaves an empty selection,
    # which every reader downstream treats as "load every card" — the opposite of narrowing.
    picked = resolve_cards(cards)

    if not text:
        return templates.TemplateResponse(request, "sessions/new.html", {
            "provider": provider_status(), "cards": available_cards(),
            "error": "A product request is required.",
        }, status_code=400)

    if provider == "anthropic":
        new_slug = discovery.start(text, cards=picked, slug=chosen_slug, finalize=False,
                                   surface="web-discover")
    else:
        new_slug = discovery.create_only(text, cards=picked, slug=chosen_slug)
    return RedirectResponse(url=f"/sessions/{new_slug}", status_code=303)


@router.get("/sessions/{slug}")
def session_page(request: Request, slug: str = Depends(safe_slug),
                 sessions: SessionService = Depends(get_sessions)):
    if not sessions.exists(slug):
        raise SessionNotFoundError(f"no session '{slug}'", details={"slug": slug})
    meta = sessions.meta(slug)
    if meta.current_revision == 0:
        # 'Create session only' with no discovery yet — offer to run it.
        return templates.TemplateResponse(request, "sessions/detail.html", {
            "pending": True, "slug": slug,
            "request_text": sessions.request_text(slug), "context_cards": meta.context_cards,
            "provider": provider_status(),
        })
    return templates.TemplateResponse(request, "sessions/detail.html", {
        "pending": False, "s": session_detail(sessions, slug),
        "provider": provider_status(),
    })


@router.get("/sessions/{slug}/export")
def export_model(slug: str = Depends(safe_slug), sessions: SessionService = Depends(get_sessions)):
    """Download the validated model — the durable product — as JSON."""
    if not sessions.exists(slug):
        raise SessionNotFoundError(f"no session '{slug}'", details={"slug": slug})
    model_json = sessions.load_model(slug).model_dump_json(indent=2)
    return PlainTextResponse(model_json, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="{slug}.model.json"'})
