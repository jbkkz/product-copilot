"""Session routes — create a discovery, list is on home, view one session, export its model."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from requivo.core.context import resolve_cards
from requivo.core.errors import InputTooLargeError, InvalidSlugError, SessionNotFoundError
from requivo.core.persistence import validate_slug
from requivo.providers.errors import EngineError
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from requivo.web.config import MAX_REQUEST_CHARS, MAX_SLUG_CHARS, provider_status
from requivo.web.dependencies import get_discovery, get_sessions, safe_slug
from requivo.web.routes.home import home_context
from requivo.web.spend import track_web_usage
from requivo.web.templating import templates
from requivo.web.viewmodels.sessions import session_detail

router = APIRouter()


# How long a provider's own words may be when they ride back on a URL. Not a security boundary --
# Jinja escapes the value and this server is local, single-user and says so in its own footer -- but a
# redirect is not the place for an unbounded string, and a message this long has stopped being a
# message.
_MAX_NOTICE_CHARS = 300


def analysis_failed(slug: str, exc: EngineError) -> RedirectResponse:
    """Send the reader to the session that *was* saved, carrying why the analysis was not (#207).

    Public, and shared with `routes/discovery.py`: both doors onto a first analysis can fail the same
    way and must land the reader in the same place. A second copy is a second wording.

    A redirect rather than a rendered page, so a refresh cannot re-POST a paid call. The cause travels
    as a query parameter because it is the actionable half -- "API unavailable" and "the key was
    rejected" need different things from the reader, and a generic notice makes them identical.
    """
    notice = quote(exc.message[:_MAX_NOTICE_CHARS])
    return RedirectResponse(url=f"/sessions/{slug}?analysis_failed={notice}", status_code=303)


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
    # Two names because there are two meanings, and sharing one was a bug. `typed_slug` is the string
    # the reader put in the box — always a string, empty when they left it alone. `chosen_slug` is the
    # argument the service takes, where `None` means *derive a slug from the request*. When one
    # variable carried both, an empty box became `None` before the empty-request arm was reached, and
    # the re-render stringified it: the reader got `value="None"` in a field they never touched, which
    # also fails the field's own `pattern` and so had to be cleared before they could resubmit. A
    # refusal built to stop costing the reader work had started adding some.
    typed_slug = slug.strip()

    def refused(status: int, code: str, message: str):
        """Hand the page back with the submission still in it, rather than sending the reader to an
        error page whose only affordance is *Back to sessions* (#30).

        Every refusal on this form goes through here, so a reader never has to learn which of them
        keeps their work. The status is unchanged — 413 is still 413 — and so is the error code,
        which rides the banner rather than a full page. It reads `typed_slug`, never `chosen_slug`:
        what goes back in the form is what the reader submitted, not what the service was going to be
        told.
        """
        return templates.TemplateResponse(request, "home.html", home_context(
            sessions, error=message, error_code=code,
            form={"request_text": text, "slug": typed_slug, "cards": cards, "provider": provider},
        ), status_code=status)

    if len(text) > MAX_REQUEST_CHARS:
        return refused(413, InputTooLargeError.code,
                       f"the product request exceeds {MAX_REQUEST_CHARS:,} characters — trim it and "
                       "resubmit")
    if len(typed_slug) > MAX_SLUG_CHARS:
        return refused(413, InputTooLargeError.code,
                       f"the session name exceeds {MAX_SLUG_CHARS} characters")
    if typed_slug:
        # The session-name field's *other* refusal, and it re-renders for the same reason. Leaving one
        # of one field's two refusals throwing the reader's work away is a worse state than either
        # arm alone, because which one they hit is not something they can predict.
        try:
            validate_slug(typed_slug)
        except InvalidSlugError as exc:
            return refused(400, exc.code, exc.message)
    # An empty box means *derive a slug from the request*, which the service spells `None`. Computed
    # here as its own value rather than by overwriting `typed_slug`, so nothing below can hand the
    # service's vocabulary back to the reader as if they had typed it.
    chosen_slug = typed_slug or None
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
        # Claim, then reason — two service operations rather than `start()`, because the route needs
        # the slug in its hands *before* the paid call can fail (#207). `claim_session` is public for
        # exactly this: a surface that owns its own flow takes invariant 13's gate itself. Nothing is
        # reimplemented here; `run_discovery` is the same operation the pending page's own button
        # already posts to.
        new_slug = discovery.claim_session(text, cards=picked, slug=chosen_slug).slug
        # Logged, not shown, for the reason `routes/discovery.py` states at its own copy of this call
        # (#253): both doors onto a first analysis answer with a 303, and a redirect has no body to
        # put a figure in. The operator's terminal is the channel that works on the success arm and
        # the failure arm alike, and this is the very first paid call a new user ever makes.
        with track_web_usage("web-discover"):
            try:
                discovery.run_discovery(new_slug, surface="web-discover")
            except EngineError as e:
                # The request is already safely captured at revision 0, and the page we are about to
                # send them to *already* offers the retry button. Letting this propagate mapped it to
                # a 502 and `errors/500.html` — "Something went wrong… check the server logs. Back to
                # sessions." — which says nothing about the pasted email having been saved, so a
                # first-time user on a transient API error reasonably concludes the product ate their
                # request. The good outcome was one redirect away the whole time. Pinned by
                # `test_a_failed_first_analysis_lands_on_the_session_that_was_saved`.
                return analysis_failed(new_slug, e)
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
            # Set only by `_analysis_failed`'s redirect. Anyone can put it in a URL by hand, which on a
            # local single-user server with no authentication is not a boundary worth defending -- and
            # Jinja escapes it either way.
            "analysis_failed": request.query_params.get("analysis_failed"),
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
