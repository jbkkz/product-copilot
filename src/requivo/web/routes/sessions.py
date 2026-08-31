"""Session routes — create a discovery, list is on home, view one session, export its model."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from requivo.core.context import resolve_cards
from requivo.core.errors import InputTooLargeError, InvalidSlugError, ProviderOutputError, SessionNotFoundError
from requivo.core.persistence import validate_slug
from requivo.providers.errors import EngineError
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from requivo.web.config import MAX_REQUEST_CHARS, MAX_SLUG_CHARS, provider_status
from requivo.web.dependencies import get_discovery, get_sessions, safe_slug
from requivo.web.routes.home import home_context
from requivo.web.spend import pop_web_usage, track_web_usage
from requivo.web.templating import templates
from requivo.web.viewmodels.sessions import session_detail

router = APIRouter()

# The provider seam fails two ways on a first analysis: a transport failure (`EngineError` — API
# unavailable, output truncated) and the JSON retry loop giving up on a reply that never holds the
# contract (`ProviderOutputError`). `app.py`'s own `_status_for` already treats both as one family —
# 502 either way, "provider transport is a family, not a code" — so the recovery path here has to
# answer for both too. Catching `EngineError` alone left `ProviderOutputError` reaching the generic
# 500/502 handler instead of `analysis_failed`: the request was still saved, but the reader was sent
# to a page that does not say so.
_PROVIDER_FAILURE = (EngineError, ProviderOutputError)


# How long a provider's own words may be when they ride back on a URL. Not a security boundary --
# Jinja escapes the value and this server is local, single-user and says so in its own footer -- but a
# redirect is not the place for an unbounded string, and a message this long has stopped being a
# message.
_MAX_NOTICE_CHARS = 300


def analysis_failed(slug: str, exc: EngineError | ProviderOutputError) -> RedirectResponse:
    """Send the reader to the session that *was* saved, carrying why the analysis was not (#207).

    Public, and shared with `routes/discovery.py`: both doors onto a first analysis can fail the same
    way and must land the reader in the same place. A second copy is a second wording.

    A redirect rather than a rendered page, so a refresh cannot re-POST a paid call. The cause travels
    as a query parameter because it is the actionable half -- "API unavailable" and "the key was
    rejected" need different things from the reader, and a generic notice makes them identical.

    `exc` is one of `_PROVIDER_FAILURE` -- both members are `RequivoError`s with the same `.message`
    shape, and this function reads only that, never the code, so the two failure classes render
    identically here on purpose.

    **The saved-reply path (#283) is carried separately, and deliberately excluded from what gets
    truncated (#362).** `ProviderOutputError.message` puts that path at its own *tail* -- see
    `completion.py`'s give-up exit -- so a message this long simply loses it: on a realistic contract
    violation the full message runs past a thousand characters and the path never reaches the first
    `_MAX_NOTICE_CHARS` of it; on the shortest possible cause the notice ends *mid-filename*, at a
    path that does not resolve. Neither is a message that has merely lost some words -- the second is
    actively worse than no path, since it looks complete and is not. `exc.details["raw_reply_path"]`
    (only `ProviderOutputError` ever sets it) rides as its own query parameter, untruncated, and is
    stripped from the text `notice` is sliced from -- an `endswith` check on the path itself, not on
    the sentence around it, so a future reword of that sentence does not silently stop matching. The
    CLI is unaffected: `exc.message` itself is never touched, only what this function derives from a
    local copy of it.
    """
    message = exc.message
    saved_path = exc.details.get("raw_reply_path") if isinstance(exc, ProviderOutputError) else None
    if saved_path and message.endswith(str(saved_path)):
        message = message[: -len(str(saved_path))].rstrip()
    notice = quote(message[:_MAX_NOTICE_CHARS])
    url = f"/sessions/{slug}?analysis_failed={notice}"
    if saved_path:
        url += f"&analysis_failed_path={quote(str(saved_path))}"
    return RedirectResponse(url=url, status_code=303)


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
        # Logged always; carried to the following GET when there is a figure to carry (#253) --
        # `routes/discovery.py`'s copy of this call carries the same shape and the same reasoning.
        # This is the very first paid call a new user ever makes, and its result rides the redirect
        # to `/sessions/{new_slug}` server-side rather than on the URL.
        with track_web_usage("web-discover", carry_to=new_slug):
            try:
                discovery.run_discovery(new_slug, surface="web-discover")
            except _PROVIDER_FAILURE as e:
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
    # Popped unconditionally: `None` on every ordinary page view (nothing was ever stashed for this
    # slug), and the one figure a create/discover redirect just stashed on the landing view right
    # after it (#253). Read-once by construction -- a reload of this same page pops nothing a second
    # time, which is deliberate (see `spend.py`).
    usage = pop_web_usage(slug)
    if meta.current_revision == 0:
        # 'Create session only' with no discovery yet — offer to run it. `usage` is set here only when
        # a first analysis spent tokens and then failed: the model never advanced past revision 0, but
        # the spend is real and already logged, so the retry page states it rather than the silence a
        # bare "your request was saved" would otherwise leave.
        return templates.TemplateResponse(request, "sessions/detail.html", {
            "pending": True, "slug": slug,
            "request_text": sessions.request_text(slug), "context_cards": meta.context_cards,
            "provider": provider_status(),
            # Set only by `_analysis_failed`'s redirect. Anyone can put it in a URL by hand, which on a
            # local single-user server with no authentication is not a boundary worth defending -- and
            # Jinja escapes it either way.
            "analysis_failed": request.query_params.get("analysis_failed"),
            # The saved-reply path (#283), carried outside the truncated notice above and rendered in
            # full (#362) -- absent whenever the failure was an `EngineError` or the debug write
            # itself failed, in which case `analysis_failed`'s own sentence already covers the cause
            # without a path to attach.
            "analysis_failed_path": request.query_params.get("analysis_failed_path"),
            "usage": usage,
        })
    return templates.TemplateResponse(request, "sessions/detail.html", {
        "pending": False, "s": session_detail(sessions, slug),
        "provider": provider_status(),
        "usage": usage,
    })


@router.get("/sessions/{slug}/export")
def export_model(slug: str = Depends(safe_slug), sessions: SessionService = Depends(get_sessions)):
    """Download the validated model — the durable product — as JSON."""
    if not sessions.exists(slug):
        raise SessionNotFoundError(f"no session '{slug}'", details={"slug": slug})
    model_json = sessions.load_model(slug).model_dump_json(indent=2)
    return PlainTextResponse(model_json, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="{slug}.model.json"'})
