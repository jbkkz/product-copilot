"""Artifact routes — generate (brief/PRD), view, and download, always tied to a source revision.

Generation goes through `DiscoveryService.generate`, which calls the provider and saves via
`ArtifactService` with the current revision — so staleness is tracked by the Core exactly as for the
CLI. Viewing and downloading read the saved content through the service; a route never touches a file.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

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
    # A generation is the paid step a reader is most likely to repeat — a document that reads badly
    # invites another click — so it is the one whose cost was most worth stating and was stated
    # nowhere (#253). The fragment it swaps in carries the footprint; `track_web_usage` also logs it,
    # which is what covers the arm where the provider fails and this fragment is never rendered.
    with track_web_usage(f"web-{artifact_type}") as spend:
        discovery.generate(slug, artifact_type, surface=f"web-{artifact_type}")
        usage = usage_view(spend)
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
    content = artifacts.show(slug, artifact_type)  # SessionNotFoundError → 404 if absent
    if download:
        filename = ARTIFACT_FILENAMES.get(artifact_type, f"{artifact_type}.md")
        return PlainTextResponse(content, media_type="text/markdown", headers={
            "Content-Disposition": f'attachment; filename="{filename}"'})
    return templates.TemplateResponse(request, "artifacts/detail.html", {
        "slug": slug, "artifact_type": artifact_type,
        "label": artifact_label(artifact_type), "document": markdown_to_html(content),
        "provider": provider_status(),
    })
