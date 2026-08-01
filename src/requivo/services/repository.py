"""SessionRepository — the storage seam under the service layer.

`SessionService` and `ArtifactService` own the *orchestration* (validate → diff → propagate → revision
→ stale-flag → readiness); where a session physically lives is a separate concern. This module draws
that line: a `SessionRepository` protocol names exactly the storage operations the services need, and
`FileSessionRepository` implements them against the `.requivo/sessions/` layout (delegating to
`core.persistence`, so on-disk behaviour is unchanged).

The point is requivo-cloud: it can supply a `PostgresSessionRepository` with the same protocol and
reuse the service orchestration verbatim, instead of bypassing the service or faking a filesystem. The
protocol is deliberately backing-neutral — the canonical-vs-legacy `out/` split is a *file* detail, so
it lives entirely inside `FileSessionRepository` (`exists`/`load_model`/`request_text`/`context_cards`
fall back to a legacy session; `ensure_writable` migrates it on first mutation). A Postgres backing has
no legacy notion: there, `has_meta` == `exists` and `ensure_writable` is a no-op.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from requivo.core import persistence as store
from requivo.core.contracts import EngineOutput
from requivo.core.errors import SessionNotFoundError
from requivo.core.persistence import ArtifactStatus, SessionMeta


@runtime_checkable
class SessionRepository(Protocol):
    """The storage operations the service layer depends on — nothing more. Implementations map these
    onto a concrete backing (files today, Postgres in Cloud). Every method keys on a validated slug."""

    def exists(self, slug: str) -> bool:
        """True if a usable session exists at all (for a file backing, canonical OR legacy)."""
        ...

    def has_meta(self, slug: str) -> bool:
        """True if the session is in the mutation-backed store — i.e. `read_meta` would succeed. For a
        file backing this is the canonical store only (a legacy-only session has no revisions/meta)."""
        ...

    def ensure_writable(self, slug: str) -> None:
        """Prepare a session for its first mutation, raising `SessionNotFoundError` if there is none.
        For a file backing this migrates a legacy `out/<slug>/` session into the canonical store."""
        ...

    def create(self, slug: str, request: str, *, provider: Optional[str] = None,
               model_name: Optional[str] = None, context_cards: Optional[list[str]] = None) -> SessionMeta:
        """Create a fresh session (no model yet, revision 0) and return its metadata."""
        ...

    def read_meta(self, slug: str) -> SessionMeta:
        """The session metadata, raising `SessionNotFoundError` if the session has none."""
        ...

    def write_meta(self, slug: str, meta: SessionMeta) -> None:
        """Persist replacement metadata for a session."""
        ...

    def list_slugs(self) -> list[str]:
        """Every session slug in the mutation-backed store, sorted."""
        ...

    def load_model(self, slug: str) -> EngineOutput:
        """The current model, raising `SessionNotFoundError` if there is none."""
        ...

    def save_revision(self, slug: str, model: EngineOutput, *, expected_revision: Optional[int] = None,
                      provenance: Optional[dict] = None) -> tuple[int, SessionMeta]:
        """Persist a new model revision (with optimistic-lock precondition and provenance) and return
        `(new_revision, updated_meta)`."""
        ...

    def request_text(self, slug: str) -> str:
        """The originating request text (empty string if none)."""
        ...

    def context_cards(self, slug: str) -> Optional[list[str]]:
        """The context-card selection recorded for the session (None == all cards)."""
        ...

    def save_artifact(self, slug: str, artifact_type: str, filename: str, content: str, *,
                      source_revision: int) -> ArtifactStatus:
        """Persist a generated artifact and record its provenance (source revision)."""
        ...

    def load_artifact(self, slug: str, filename: str) -> Optional[str]:
        """The saved content of an artifact file, or None if it is not present."""
        ...


class FileSessionRepository:
    """The default backing: the `.requivo/sessions/<slug>/` layout, with read-only legacy `out/<slug>/`
    fallback and migrate-on-first-mutation. A thin adapter over `core.persistence` — it holds no state,
    so it is safe to construct per call. The canonical-vs-legacy logic that used to live in
    `SessionService` lives here, where it belongs (a storage detail, not orchestration)."""

    def exists(self, slug: str) -> bool:
        return store.session_exists(slug) or store.legacy_exists(slug)

    def has_meta(self, slug: str) -> bool:
        return store.session_exists(slug)

    def ensure_writable(self, slug: str) -> None:
        if store.session_exists(slug):
            return
        if store.legacy_exists(slug):
            store.migrate_legacy(slug)
            return
        raise SessionNotFoundError(f"no session '{slug}'", details={"slug": slug})

    def create(self, slug: str, request: str, *, provider: Optional[str] = None,
               model_name: Optional[str] = None, context_cards: Optional[list[str]] = None) -> SessionMeta:
        return store.create_session(slug, request, provider=provider, model_name=model_name,
                                    context_cards=context_cards)

    def read_meta(self, slug: str) -> SessionMeta:
        return store.read_meta(slug)

    def write_meta(self, slug: str, meta: SessionMeta) -> None:
        store.write_meta(slug, meta)

    def list_slugs(self) -> list[str]:
        return store.list_session_slugs()

    def load_model(self, slug: str) -> EngineOutput:
        if store.session_exists(slug):
            return store.load_session_model(slug)
        if store.legacy_exists(slug):
            return EngineOutput.model_validate_json((store.legacy_dir(slug) / "model.json").read_text())
        raise SessionNotFoundError(f"no session '{slug}'", details={"slug": slug})

    def save_revision(self, slug: str, model: EngineOutput, *, expected_revision: Optional[int] = None,
                      provenance: Optional[dict] = None) -> tuple[int, SessionMeta]:
        return store.save_revision(slug, model, expected_revision=expected_revision, provenance=provenance)

    def request_text(self, slug: str) -> str:
        if store.session_exists(slug):
            return store.session_request(slug)
        if store.legacy_exists(slug):
            return store.load_request(store.legacy_dir(slug) / "model.json")
        return ""

    def context_cards(self, slug: str) -> Optional[list[str]]:
        if store.session_exists(slug):
            return store.read_meta(slug).context_cards
        if store.legacy_exists(slug):
            return store.session_cards(store.legacy_dir(slug) / "model.json")
        return None

    def save_artifact(self, slug: str, artifact_type: str, filename: str, content: str, *,
                      source_revision: int) -> ArtifactStatus:
        return store.save_session_artifact(slug, artifact_type, filename, content,
                                           source_revision=source_revision)

    def load_artifact(self, slug: str, filename: str) -> Optional[str]:
        p = store.canonical_dir(slug) / "artifacts" / filename
        return p.read_text() if p.exists() else None


def default_repository() -> SessionRepository:
    """The repository the CLI uses — a file backing under the caller's workspace."""
    return FileSessionRepository()
