"""Artifact routes — generate (brief/PRD), view, and download, always tied to a source revision.

Generation goes through `DiscoveryService.generate`, which calls the provider and saves via
`ArtifactService` with the current revision — so staleness is tracked by the Core exactly as for the
CLI. Viewing and downloading read the saved content through the service; a route never touches a file.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from requivo.render.html import markdown_to_html
from requivo.services.artifacts import ARTIFACT_FILENAMES, ArtifactService, UnknownArtifactTypeError
from requivo.services.discovery import GENERATABLE, DiscoveryService
from requivo.services.sessions import SessionService
from requivo.web.config import provider_status
from requivo.web.dependencies import get_artifacts, get_discovery, get_sessions, safe_slug
from requivo.web.spend import track_web_usage
from requivo.web.templating import templates
from requivo.web.viewmodels.labels import artifact_label
from requivo.web.viewmodels.sessions import session_detail
from requivo.web.viewmodels.usage import usage_view

router = APIRouter()


@router.post("/sessions/{slug}/artifacts/{artifact_type}")
def generate_artifact(
    request: Request,
    artifact_type: str,
    slug: str = Depends(safe_slug),
    discovery: DiscoveryService = Depends(get_discovery),
    sessions: SessionService = Depends(get_sessions),
):
    """Generate an artifact and save it against the session, then return the refreshed artifacts region
    for an HTMX swap. The vocabulary is the service's `GENERATABLE`, not a list kept here — the Web
    offers whatever the shared orchestration can produce, so the surfaces cannot drift apart."""
    if artifact_type not in GENERATABLE:
        raise UnknownArtifactTypeError(
            f"{artifact_type!r} is not a generated artifact; supported: {', '.join(GENERATABLE)}",
            details={"type": artifact_type})
    is_htmx = request.headers.get("HX-Request") == "true"
    # A generation is the paid step a reader is most likely to repeat — a document that reads badly
    # invites another click — so it is the one whose cost was most worth stating and was stated
    # nowhere (#253). The fragment it swaps in carries the footprint on the htmx path; a no-JS submit
    # (#428) is a bodyless redirect instead, so `carry_to=slug` stashes the same figure for the
    # session page's next GET to pop (`spend.py`) — passed only on that path, or the fragment path's
    # figure would sit stashed and unread, then surface again on some later, unrelated visit to the
    # session (spend.py's stash is deliberately read-once per action, not per page load).
    with track_web_usage(f"web-{artifact_type}", carry_to=None if is_htmx else slug) as spend:
        discovery.generate(slug, artifact_type, surface=f"web-{artifact_type}")
        usage = usage_view(spend)
    if not is_htmx:
        # A plain form submit (#428): the form now carries `method="post" action="…"` beside its
        # `hx-post`, so a no-JS reader reaches this route as a real POST rather than the bare GET a
        # form with neither attribute falls back to. No fragment to swap; the spend footprint rides
        # the stash above, and the session page this redirects to shows the freshly generated
        # document.
        return RedirectResponse(url=f"/sessions/{slug}", status_code=303)
    return templates.TemplateResponse(request, "artifacts/list.html", {
        "s": session_detail(sessions, slug), "provider": provider_status(),
        "usage": usage,
    })


@router.get("/sessions/{slug}/artifacts/{artifact_type}")
def view_artifact(
    request: Request,
    artifact_type: str,
    download: bool = False,
    slug: str = Depends(safe_slug),
    artifacts: ArtifactService = Depends(get_artifacts),
):
    """View a saved artifact as a formatted document, or download the raw Markdown file.

    **Two views of one file, and only the view moved** (#235). The download stays byte-identical to
    what `ArtifactService` saved, because the file is the artifact: it is what gets handed to a
    tracker, a colleague or the CLI, and a browser reformatting it on the way out would break every
    consumer that is not a browser. Pinned by
    `test_downloading_an_artifact_still_serves_the_bytes_that_were_saved`.

    The rendered half goes through `markdown_to_html`, which escapes every run of document text
    before it builds any tag — see that module. It has to, because the template renders the result
    with autoescape off; that is not a shortcut but the only way to apply markup at all, and it is
    why the escaping is the renderer's job rather than Jinja's here.
    """
    # `artifacts.show()` calls `ArtifactService._filename` first, which raises
    # `UnknownArtifactTypeError` (400) for any type not in `ARTIFACT_FILENAMES` -- so an
    # `artifact_type` that reaches the `if download:` branch below is already a real key.
    content = artifacts.show(slug, artifact_type)  # SessionNotFoundError → 404 if absent
    if download:
        # No invented-filename fallback (#270): `.get(artifact_type, f"{artifact_type}.md")` used to
        # guess a name for a type nothing ever produced, against the repo's own refuse-don't-guess
        # rule (invariant 3) -- and the guess could never actually be reached, since the line above
        # already refuses anything `ARTIFACT_FILENAMES` does not know. Plain indexing says so.
        filename = ARTIFACT_FILENAMES[artifact_type]
        return PlainTextResponse(content, media_type="text/markdown", headers={
            "Content-Disposition": f'attachment; filename="{filename}"'})
    return templates.TemplateResponse(request, "artifacts/detail.html", {
        "slug": slug, "artifact_type": artifact_type,
        "label": artifact_label(artifact_type), "document": markdown_to_html(content),
        "provider": provider_status(),
    })
