"""Discovery routes — run the first turn on a captured request, and fold in answers.

Both go through `DiscoveryService`, which reasons via the provider and applies the result through the
same validated path (validate → diff → revision → stale-flag) as every other surface. The answers turn
carries `expected_revision` so a stale submission is rejected with a clean conflict instead of clobbering
a concurrent change.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from requivo.core.errors import InputTooLargeError
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from requivo.web.config import MAX_ANSWERS_CHARS, provider_status
from requivo.web.dependencies import get_discovery, get_sessions, safe_slug
from requivo.web.routes.sessions import _PROVIDER_FAILURE, analysis_failed
from requivo.web.spend import track_web_usage
from requivo.web.templating import templates
from requivo.web.viewmodels.sessions import session_detail
from requivo.web.viewmodels.status import impact_view
from requivo.web.viewmodels.usage import usage_view

router = APIRouter()


@router.post("/sessions/{slug}/discover")
def run_discovery(slug: str = Depends(safe_slug),
                  discovery: DiscoveryService = Depends(get_discovery)):
    """Run the first discovery turn on a 'create session only' session, then show the result.

    The failure is handled the same way the create route handles it (#207): this session already
    exists and this page already carries the retry button, so a transient provider error goes back to
    it with the cause stated, rather than to a 500 page that hides both.
    """
    # Logged always; carried to the following GET when there is a figure to carry (#253). This path
    # answers with a 303 so a refresh cannot re-POST a paid call, and a redirect has no body of its
    # own — `track_web_usage(..., carry_to=slug)` stashes the view server-side for `session_page`'s
    # GET to pop, rather than putting a forgeable number on the URL. The log line is unconditional and
    # is what survives the failure arm below, whether or not anything was stashed.
    with track_web_usage("web-discover", carry_to=slug):
        try:
            discovery.run_discovery(slug, surface="web-discover")
        except _PROVIDER_FAILURE as e:
            return analysis_failed(slug, e)
    return RedirectResponse(url=f"/sessions/{slug}", status_code=303)


@router.post("/sessions/{slug}/answers")
def submit_answers(
    request: Request,
    slug: str = Depends(safe_slug),
    answers: str = Form(...),
    expected_revision: int = Form(...),
    discovery: DiscoveryService = Depends(get_discovery),
    sessions: SessionService = Depends(get_sessions),
):
    """Fold the answers into the model as a new revision (optimistic-locked on `expected_revision`),
    then return the refreshed status region for an HTMX swap. A revision conflict surfaces as a clean
    error fragment via the app's exception handler."""
    # A plain form submit (no JS, or JS that has not loaded htmx yet) carries no `HX-Request`
    # header — that is what tells this route apart from the fragment the form's own `hx-post` asks
    # for, and is the read-side half of #428's fix: the form now also carries `method="post"
    # action="…"`, so a no-JS submit reaches this route as a real POST instead of the bare GET a
    # form with neither attribute falls back to.
    is_htmx = request.headers.get("HX-Request") == "true"
    text = answers.strip()
    if len(text) > MAX_ANSWERS_CHARS:
        # Refused rather than truncated — half an answer folded into the model is worse than none,
        # because nothing downstream can tell it was cut. That is unchanged (invariant 3).
        #
        # What changed is the recovery (#30). Raising rendered `errors/_error.html`, and this form
        # posts with `hx-swap="outerHTML"` onto `#session-body` — the region that *contains* the
        # textarea. So the refusal did not merely fail to keep what was typed: the swap deleted the
        # field it was typed into, and there was no Back to return to. The whole region is returned
        # instead, with the submission still in it and the refusal stated on the form.
        #
        # A no-JS request never receives that fragment at all — it has no htmx to swap it in, so a
        # bare `#session-body` region would render as the whole (shell-less) page (#428). It gets the
        # full `sessions/detail.html` instead, carrying the same error context.
        if not is_htmx:
            detail = session_detail(sessions, slug)
            return templates.TemplateResponse(request, "sessions/detail.html", {
                "pending": False, "s": detail, "is_example": detail["is_example"],
                "provider": provider_status(),
                "answers_error": f"the answers exceed {MAX_ANSWERS_CHARS:,} characters — split them "
                                 "across two turns",
                "answers_error_code": InputTooLargeError.code,
                "submitted_answers": text,
            }, status_code=413)
        return templates.TemplateResponse(request, "sessions/_session.html", {
            "s": session_detail(sessions, slug),
            "provider": provider_status(),
            "answers_error": f"the answers exceed {MAX_ANSWERS_CHARS:,} characters — split them "
                             "across two turns",
            "answers_error_code": InputTooLargeError.code,
            "submitted_answers": text,
        }, status_code=413)
    # This answers with a fragment the reader stays on, so the footprint rides the response as well
    # as the log (#253) — the same treatment artifact generation gets, and the opposite of the two
    # redirecting paths, which have no body to put it in. The ledger is opened around the call and
    # read after it: a view model over the ledger, never a second computation of the same numbers.
    #
    # A no-JS submit *is* one of the bodyless-redirect paths (#428) — `carry_to=slug` only when this
    # request is about to become one, so `spend.py`'s stash is what the following GET reads. Passing
    # it unconditionally would leave a figure stashed and unread behind the fragment path too, and
    # the *next*, unrelated GET of the session (a reload, a later visit) would then show a spend
    # receipt for an action that page had nothing to do with — read-once is deliberate exactly to
    # avoid that (spend.py's own docstring), and it only works when a courtesy stash is left for the
    # cases that actually need one.
    with track_web_usage("web-answer", carry_to=None if is_htmx else slug) as spend:
        result = discovery.answer(slug, text, expected_revision=expected_revision,
                                  surface="web-answer")
        usage = usage_view(spend)
    if not is_htmx:
        # No fragment to swap: the session page itself already states what changed (#428). The
        # spend footprint rides the stash above rather than the response, which is what makes it
        # visible after the 303 at all. A 303 so a refresh cannot silently re-POST the answers again.
        return RedirectResponse(url=f"/sessions/{slug}", status_code=303)
    return templates.TemplateResponse(request, "sessions/_session.html", {
        "s": session_detail(sessions, slug),
        "update": impact_view(result),
        "provider": provider_status(),
        "usage": usage,
    })
