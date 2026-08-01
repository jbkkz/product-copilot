"""Artifact routes — generate (brief/PRD), view, and download, always tied to a source revision.

Generation goes through `DiscoveryService.generate`, which calls the provider and saves via
`ArtifactService` with the current revision — so staleness is tracked by the Core exactly as for the
CLI. Viewing and downloading read the saved content through the service; a route never touches a file.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from requivo.services.artifacts import ARTIFACT_FILENAMES, ArtifactService, UnknownArtifactTypeError
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from requivo.web.config import provider_status
from requivo.web.dependencies import get_artifacts, get_discovery, get_sessions, safe_slug
from requivo.web.templating import templates
from requivo.web.viewmodels.sessions import ARTIFACT_LABELS, session_detail

router = APIRouter()

# The Web generates these two today (the spec's first version); the rest already exist as CLI generators
# and can be added here without new orchestration.
GENERATABLE = ("brief", "prd")


@router.post("/sessions/{slug}/artifacts/{artifact_type}")
def generate_artifact(
    request: Request,
    artifact_type: str,
    slug: str = Depends(safe_slug),
    discovery: DiscoveryService = Depends(get_discovery),
    sessions: SessionService = Depends(get_sessions),
):
    """Generate a brief or PRD and save it against the session, then return the refreshed artifacts
    region for an HTMX swap."""
    if artifact_type not in GENERATABLE:
        raise UnknownArtifactTypeError(
            f"the web interface does not generate {artifact_type!r} yet; supported: {', '.join(GENERATABLE)}",
            details={"type": artifact_type})
    discovery.generate(slug, artifact_type, surface=f"web-{artifact_type}")
    return templates.TemplateResponse(request, "artifacts/list.html", {
        "s": session_detail(sessions, slug), "provider": provider_status(),
    })


@router.get("/sessions/{slug}/artifacts/{artifact_type}")
def view_artifact(
    request: Request,
    artifact_type: str,
    download: bool = False,
    slug: str = Depends(safe_slug),
    artifacts: ArtifactService = Depends(get_artifacts),
):
    """View a saved artifact's Markdown (rendered escaped, in a code block) or download the raw file."""
    content = artifacts.show(slug, artifact_type)  # SessionNotFoundError → 404 if absent
    if download:
        filename = ARTIFACT_FILENAMES.get(artifact_type, f"{artifact_type}.md")
        return PlainTextResponse(content, media_type="text/markdown", headers={
            "Content-Disposition": f'attachment; filename="{filename}"'})
    return templates.TemplateResponse(request, "artifacts/detail.html", {
        "slug": slug, "artifact_type": artifact_type,
        "label": ARTIFACT_LABELS.get(artifact_type, artifact_type), "content": content,
        "provider": provider_status(),
    })
