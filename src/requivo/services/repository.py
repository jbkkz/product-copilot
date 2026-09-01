"""SessionRepository — the storage seam under the service layer.

`SessionService` and `ArtifactService` own the *orchestration* (validate → diff → propagate → revision
→ stale-flag → readiness); where a session physically lives is a separate concern. This module draws
that line: a `SessionRepository` protocol names exactly the storage operations the services need, and
`FileSessionRepository` implements them against the `.requivo/sessions/` layout (delegating to
`core.persistence`, so on-disk behaviour is unchanged).

The point is a non-filesystem backing: a deployment can supply a `PostgresSessionRepository` with the
same protocol and reuse the service orchestration verbatim, instead of bypassing the service or faking
a filesystem. The protocol is deliberately backing-neutral.

The pre-0.9.8 file backing carried a second store: every read fell back to a legacy `out/<slug>/`
session, and a mutation migrated one in place. That kept old sessions working without the user
knowing — which is also what was wrong with it. The fallback ran on every read of every session for
the benefit of a layout nothing has written since 0.8.0, and it made "where does this session live?"
a question with two answers everywhere in the code. Migration is now explicit (`requivo session
migrate`), and all that remains here is *detection*: a legacy directory turns "no session" into an
error that names the command to run.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Optional, Protocol, runtime_checkable

from requivo.core import persistence as store
from requivo.core.contracts import EngineOutput
from requivo.core.errors import SessionNotFoundError
from requivo.core.persistence import ArtifactStatus, SessionMeta, UnexaminableEntry


@runtime_checkable
class SessionRepository(Protocol):
    """The storage operations the service layer depends on — nothing more. Implementations map these
    onto a concrete backing (files today, Postgres elsewhere). Every method keys on a validated slug."""

    def lock(self, slug: str) -> AbstractContextManager[None]:
        """Hold exclusive write access to a session for the duration of the block.

        A model update is several storage calls — read the metadata, save a revision, rewrite the
        artifact flags — and it is only correct if no other writer can interleave with them. The
        service takes this lock around the whole sequence rather than trusting each call to be
        individually safe. Implementations must be re-entrant within a thread, since the calls inside
        the block take it again. A file backing maps this to an OS file lock; a Postgres backing maps
        it to the row lock of the enclosing transaction."""
        ...

    def exists(self, slug: str) -> bool:
        """True if a usable session exists."""
        ...

    def has_meta(self, slug: str) -> bool:
        """True if the session is in the mutation-backed store — i.e. `read_meta` would succeed. Kept
        distinct from `exists` because a backing may hold a session it cannot yet describe."""
        ...

    def ensure_writable(self, slug: str) -> None:
        """Prepare a session for its first mutation, raising `SessionNotFoundError` if there is none."""
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
        """Every session slug in the mutation-backed store, sorted. **Names known to be sessions** —
        see `list_unexaminable` for the ones the backing could not decide about."""
        ...

    def list_unexaminable(self) -> list[UnexaminableEntry]:
        """Every name the backing found and could **not** decide about, with the reason (#80).

        The third answer to *what is in the store?*, and it exists because the other two are both
        claims. Omitting the entry says nothing is wrong and loses it; listing it as a slug says it
        is a session, which is what the backing has just failed to establish. So it comes back as
        itself, and the service turns it into a degraded row.

        On the file backing this is a directory the process cannot stat into. A backing on which the
        question cannot arise — one whose enumeration either yields a row or raises for the whole
        query — returns `[]`, and that is an honest *we looked and there was nothing*. What no
        backing may do is drop a row it enumerated and could not decode: the point of the method is
        that the caller can tell those two apart, and a silent drop is the absence this whole
        listing path exists to end.

        Failing to enumerate **at all** is not this — it is the aggregate having no members to speak
        for, and it raises, exactly as `list_slugs` does."""
        ...

    def load_model(self, slug: str) -> EngineOutput:
        """The current model, raising `SessionNotFoundError` if there is none."""
        ...

    def load_revision(self, slug: str, revision: int) -> EngineOutput:
        """A historical model revision, raising `SessionNotFoundError` if it does not exist. Needed to
        answer "what changed since the model this artifact was generated from?" after the fact."""
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
                      source_revision: int, stale: bool = False) -> ArtifactStatus:
        """Persist a generated artifact and record its provenance (source revision) and freshness.
        `stale` is decided by the service from the dependency graph, never by the storage layer."""
        ...

    def load_artifact(self, slug: str, filename: str) -> Optional[str]:
        """The saved content of an artifact file, or None if it is not present.

        **None means absent, and only that.** A backing that derives a *location* from `filename` —
        a path on the file backing, a key with any namespacing of its own — must validate it and
        **raise** on a name it will not accept, never return None: a caller that cannot tell a
        refusal from an absence has been handed the wrong answer in the more dangerous direction,
        and a rejected traversal then reads as an artifact nobody has generated yet.

        A backing for which `filename` is an *opaque* key has nothing to refuse, and a miss there is
        a real absence — `InMemorySessionRepository` in the tests is that case, and returns None
        correctly. The obligation follows the path-building, not the protocol."""
        ...


class FileSessionRepository:
    """The default backing: the `.requivo/sessions/<slug>/` layout. A thin adapter over
    `core.persistence` — it holds no state, so it is safe to construct per call.

    That sentence is true and, on its own, misleading: every method below delegates to a
    module-level `core.persistence` function, and those resolve the workspace root ambiently
    (`paths.workspace_root()`, reading `REQUIVO_WORKSPACE`/cwd) on each call rather than reading
    it off `self`. So two instances of this class are indistinguishable — neither carries a
    root of its own — and the protocol's "backing-neutral, addressable by construction" promise
    does not hold for this backing today: `DiscoveryService`'s "one repository per service,
    chosen once" comment (`services/discovery.py`) is unenforceable here, because there is no
    per-instance identity for it to enforce. This is deliberately deferred to #272, which threads
    an explicit root through the store; do not construct two of these expecting them to address
    different workspaces."""

    @staticmethod
    def _missing(slug: str) -> SessionNotFoundError:
        """The one place "there is no such session" is phrased — including the case where there *is*
        one, in the retired `out/` layout, which is a different problem with a specific answer."""
        if store.legacy_exists(slug):
            return SessionNotFoundError(
                f"'{slug}' exists only in the retired out/ layout. Bring it into the session store "
                "with `requivo session migrate`, which converts every out/ session in one pass and "
                "leaves the originals in place.",
                details={"slug": slug, "legacy": True})
        return SessionNotFoundError(f"no session '{slug}'", details={"slug": slug})

    @contextmanager
    def lock(self, slug: str) -> Iterator[None]:
        with store.session_lock(slug):
            yield

    def exists(self, slug: str) -> bool:
        return store.session_exists(slug)

    def has_meta(self, slug: str) -> bool:
        return store.session_exists(slug)

    def ensure_writable(self, slug: str) -> None:
        if not store.session_exists(slug):
            raise self._missing(slug)

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

    def list_unexaminable(self) -> list[UnexaminableEntry]:
        # A second scan rather than one shared with `list_slugs`, deliberately. The two are two
        # instants and a session appearing between them can land in neither answer — the same race
        # `scan_session_root` exists to close for `doctor`. It is the lesser cost here: sharing one
        # scan means holding the partition's three parts as state on a repository that is
        # constructed per call and documented as holding none, and the answer would then be as old
        # as the handle. `session list` reads both within microseconds of each other.
        return store.list_unexaminable_entries()

    def load_model(self, slug: str) -> EngineOutput:
        if not store.session_exists(slug):
            raise self._missing(slug)
        return store.load_session_model(slug)

    def load_revision(self, slug: str, revision: int) -> EngineOutput:
        return store.load_revision_model(slug, revision)

    def save_revision(self, slug: str, model: EngineOutput, *, expected_revision: Optional[int] = None,
                      provenance: Optional[dict] = None) -> tuple[int, SessionMeta]:
        return store.save_revision(slug, model, expected_revision=expected_revision, provenance=provenance)

    def request_text(self, slug: str) -> str:
        return store.session_request(slug) if store.session_exists(slug) else ""

    def context_cards(self, slug: str) -> Optional[list[str]]:
        return store.read_meta(slug).context_cards if store.session_exists(slug) else None

    def save_artifact(self, slug: str, artifact_type: str, filename: str, content: str, *,
                      source_revision: int, stale: bool = False) -> ArtifactStatus:
        return store.save_session_artifact(slug, artifact_type, filename, content,
                                           source_revision=source_revision, stale=stale)

    def load_artifact(self, slug: str, filename: str) -> Optional[str]:
        # Delegated rather than re-joined here: this method built the artifacts/ path inline, which
        # is how the read side kept a traversal the write side had already closed at `artifact_path`.
        return store.read_artifact_file(slug, filename)


def default_repository() -> SessionRepository:
    """The repository the CLI uses — a file backing under the caller's workspace."""
    return FileSessionRepository()
