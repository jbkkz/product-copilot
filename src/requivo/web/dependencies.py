"""FastAPI dependencies — construct the shared application services per request.

The services are stateless over an injected `FileSessionRepository` (they read/write the workspace on
each call), so a fresh instance per request is correct and cheap. Routes depend on these instead of
importing the store, so the filesystem is only ever reached through a service — never from a route.
"""

from __future__ import annotations

from requivo.core.persistence import _refuse_new_reserved_slug, _slug_shape
from requivo.paths import session_root
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
    """Validate a `{slug}` path parameter in Core (strict kebab-case, no traversal) before any route
    uses it. Raises `InvalidSlugError` — the app's exception handler turns that into a clean 400.

    **This is the read-time half of #372's creation/read split, and the asymmetry is the point**
    (#396). Every route taking this dependency addresses a session that must *already exist* — the
    write routes included, because refining a session or generating an artifact against it is still
    a read of its name, not a request to make one. So the reserved Windows device-name refusal
    (#221) is conditional here, exactly as `canonical_dir` and `lock_path` make it: a name a session
    already occupies on disk reads through, and a name nothing occupies is refused as strictly as
    before. Unconditional, this one guard put a whole surface out of reach — every `{slug}` route
    the Web has, read routes included, 400 for a session `requivo session show` opens fine.

    Creation is the other half and stays unconditional. `POST /sessions` — the one route that can
    bring a slug into existence — takes its name from a form field rather than the path, so it never
    reaches this dependency at all; it calls `validate_slug` directly in `routes/sessions.py`, which
    is also what lets it re-render the form with the reader's work still in it. Widening *that* call
    would widen creation, which is exactly what must not happen. Both halves are pinned together in
    one fixture by
    `test_a_reserved_slug_already_on_disk_is_reachable_through_the_web_read_routes`.
    """
    slug = _slug_shape(slug)
    # Probed against the session root, which is where "does something already claim this name?" is
    # decided — the same argument `lock_path` makes for probing there rather than at the path it is
    # about to build.
    _refuse_new_reserved_slug(slug, session_root() / slug)
    return slug
