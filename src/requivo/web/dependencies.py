"""FastAPI dependencies — construct the shared application services per request.

The services are stateless over an injected `FileSessionRepository` (they read/write the workspace on
each call), so a fresh instance per request is correct and cheap. Routes depend on these instead of
importing the store, so the filesystem is only ever reached through a service — never from a route.
"""

from __future__ import annotations

from requivo.core.persistence import validate_slug
from requivo.services.artifacts import ArtifactService
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService


def get_sessions() -> SessionService:
    return SessionService()


def get_artifacts() -> ArtifactService:
    return ArtifactService()


def get_discovery() -> DiscoveryService:
    return DiscoveryService()


def safe_slug(slug: str) -> str:
    """Validate a slug path parameter in Core (strict kebab-case, no traversal) before any route uses
    it. Raises `InvalidSlugError` — the app's exception handler turns that into a clean 400."""
    return validate_slug(slug)
