"""Session routes — create a discovery, list is on home, view one session, export its model."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from requivo.core.context import resolve_cards
from requivo.core.errors import InputTooLargeError, InvalidSlugError, SessionNotFoundError
from requivo.core.persistence import validate_slug
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from requivo.web.config import MAX_REQUEST_CHARS, MAX_SLUG_CHARS, provider_status
from requivo.web.dependencies import get_discovery, get_sessions, safe_slug
from requivo.web.routes.home import home_context
from requivo.web.templating import templates
from requivo.web.viewmodels.sessions import session_detail

router = APIRouter()


@router.post("/sessions")
def create_session(
    request: Request,
    request_text: str = Form(...),
    slug: str = Form(""),
    cards: list[str] = Form(default=[]),
    provider: str = Form("auto"),
    sessions: SessionService = Depends(get_sessions),
    discovery: DiscoveryService = Depends(get_discovery),
):
    """Create a session from a request and, by default, analyse it straight away.

    `provider` is `auto` unless the reader opened Advanced settings and chose otherwise: the server
    already knows whether a provider action can run, so asking the reader to declare it was asking
    them to answer a question the application could answer itself. `create_only` remains as the
    explicit 'capture it now, analyse later' choice, and is what `auto` resolves to when no provider
    is configured — a request is still worth keeping when there is nothing to reason with yet."""
    # Bounds are refusals, not truncations: a request silently cut at 20k would be reasoned over as if
    # it were the whole thing, and the user would never learn which half the engine saw.
    text = request_text.strip()
    chosen_slug = slug.strip()

    def refused(status: int, code: str, message: str):
        """Hand the page back with the submission still in it, rather than sending the reader to an
        error page whose only affordance is *Back to sessions* (#30).

        Every refusal on this form goes through here, so a reader never has to learn which of them
        keeps their work. The status is unchanged — 413 is still 413 — and so is the error code,
        which rides the banner rather than a full page.
        """
        return templates.TemplateResponse(request, "home.html", home_context(
            sessions, error=message, error_code=code,
            form={"request_text": text, "slug": chosen_slug, "cards": cards, "provider": provider},
        ), status_code=status)

    if len(text) > MAX_REQUEST_CHARS:
        return refused(413, InputTooLargeError.code,
                       f"the product request exceeds {MAX_REQUEST_CHARS:,} characters — trim it and "
                       "resubmit")
    if len(chosen_slug) > MAX_SLUG_CHARS:
        return refused(413, InputTooLargeError.code,
                       f"the session name exceeds {MAX_SLUG_CHARS} characters")
    if chosen_slug:
        # The session-name field's *other* refusal, and it re-renders for the same reason. Leaving one
        # of one field's two refusals throwing the reader's work away is a worse state than either
        # arm alone, because which one they hit is not something they can predict.
        try:
            validate_slug(chosen_slug)
        except InvalidSlugError as exc:
            return refused(400, exc.code, exc.message)
    else:
        chosen_slug = None
    # An unknown card is an error, not something to filter out: dropping it leaves an empty selection,
    # which every reader downstream treats as "load every card" — the opposite of narrowing.
    #
    # This one is deliberately left to raise. The card boxes are checkboxes over a set this page
    # rendered, so an unknown value did not come from a reader mistyping something they could correct
    # on a re-render — it came from a submission that did not originate in this form. Re-rendering
    # would dress a tampered request as a typo.
    picked = resolve_cards(cards)

    if not text:
        return refused(400, "empty_request",
                       "A request is required — paste the client or stakeholder email, or describe "
                       "what was asked in your own words.")

    if provider == "auto":
        provider = "anthropic" if provider_status().available else "create_only"
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
