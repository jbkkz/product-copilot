"""ArtifactService — save, list, and read generated artifacts against a session.

An artifact is a *view* of the model at a specific revision. This service records that provenance
(source revision) when an artifact is saved, reports each artifact's freshness relative to the current
model, and can flag the blast radius stale after a model change. It never generates content — the
provider (or Claude Code) produces the text; this service persists and tracks it.
"""

from __future__ import annotations

from requivo.core.dependencies import REASONING_CONSUMERS, diff_models, diff_reasoning, propagate
from requivo.core.errors import RequivoError, SessionNotFoundError
from requivo.core.persistence import ArtifactStatus
from requivo.services.repository import SessionRepository, default_repository

# The saveable artifact vocabulary: type → filename under <session>/artifacts/. This is the union of
# the buildable deliverables. It differs from `dependencies.ARTIFACT_FILES` only in `stories`, which is
# saveable here but has no file there because it is a terminal analysis. The assessment *does*
# participate in staleness — it rests on every slot, so any material change unseats it — despite an
# older comment here claiming otherwise; that claim outlived the day the assessment became a saved
# artifact rather than a live view.
ARTIFACT_FILENAMES: dict[str, str] = {
    "brief": "solution-assessment.md",
    "prd": "prd.md",
    "stories": "stories.md",
    "criteria": "acceptance-criteria.md",
    "epic": "epic.md",
    "release": "release-notes.md",
}


class UnknownArtifactTypeError(RequivoError):
    code = "unknown_artifact_type"


class ArtifactService:
    def __init__(self, repo: SessionRepository | None = None):
        self.repo: SessionRepository = repo or default_repository()

    def _filename(self, artifact_type: str) -> str:
        try:
            return ARTIFACT_FILENAMES[artifact_type]
        except KeyError as e:
            raise UnknownArtifactTypeError(
                f"unknown artifact type {artifact_type!r}; known: {', '.join(sorted(ARTIFACT_FILENAMES))}",
                details={"type": artifact_type},
            ) from e

    def save(self, slug: str, artifact_type: str, content: str,
             source_revision: int | None = None) -> ArtifactStatus:
        """Persist an artifact and tie it to the model revision it was generated from. `source_revision`
        defaults to the session's current revision (the common case: generate-then-save).

        An artifact reasoned from an *older* revision is saved with its freshness already computed
        against the current model, not assumed fresh. A long generation can finish after the session
        has moved (that is why generators capture their revision up front), and Claude Code can save a
        file it produced several turns ago — in both cases the honest answer is knowable: diff the
        source revision against the current model and see whether this artifact's dependencies were
        touched. Recording it fresh because the caller said so was how a stale PRD stayed unflagged."""
        filename = self._filename(artifact_type)
        if not self.repo.has_meta(slug):
            raise SessionNotFoundError(
                f"session '{slug}' is not in the canonical store; apply a model first", details={"slug": slug})
        with self.repo.lock(slug):
            meta = self.repo.read_meta(slug)
            rev = source_revision if source_revision is not None else meta.current_revision
            stale = self._stale_since(slug, artifact_type, rev, meta.current_revision)
            return self.repo.save_artifact(slug, artifact_type, filename, content,
                                           source_revision=rev, stale=stale)

    def _stale_since(self, slug: str, artifact_type: str, source_revision: int,
                     current_revision: int) -> bool:
        """Whether an artifact generated from `source_revision` is already out of date at
        `current_revision` — the same dependency-graph question `update_model` answers, asked after
        the fact. False when the source is current (nothing moved) or the history is unreadable: an
        unanswerable freshness question must not manufacture a stale flag."""
        if source_revision >= current_revision:
            return False
        try:
            was = self.repo.load_revision(slug, source_revision)
            now = self.repo.load_model(slug)
        except RequivoError:
            return False
        if diff_reasoning(was, now).changed and artifact_type in REASONING_CONSUMERS:
            return True
        return artifact_type in set(propagate(now, diff_models(was, now)).artifacts)

    def list(self, slug: str) -> dict[str, dict]:
        """Every recorded artifact with its freshness relative to the current model revision."""
        meta = self.repo.read_meta(slug)
        out: dict[str, dict] = {}
        for t, st in meta.artifact_status.items():
            # Freshness is the explicit stale flag, set by `update_model`/`mark_stale` for exactly the
            # artifacts in a change's blast radius. The source revision is provenance only — an artifact
            # is NOT stale merely because the model moved on; a change that misses its dependencies
            # leaves it fresh (the whole point of the dependency graph).
            out[t] = {"revision": st.revision, "filename": st.filename,
                      "updated_at": st.updated_at, "stale": st.stale}
        return out

    def show(self, slug: str, artifact_type: str) -> str:
        """The saved content of an artifact."""
        filename = self._filename(artifact_type)
        content = self.repo.load_artifact(slug, filename)
        if content is None:
            raise SessionNotFoundError(
                f"session '{slug}' has no saved {artifact_type!r} artifact",
                details={"slug": slug, "type": artifact_type})
        return content

    def mark_stale(self, slug: str, changed_slots: list[str]) -> list[str]:
        """Flag every generated artifact in the blast radius of `changed_slots` stale, and return the
        types flagged. Used after a model change made outside `update_model`.

        Read-modify-write on the metadata, so it runs under the session lock like every other compound
        mutation: a concurrent writer landing between the read and the write would have its own flags
        reverted by ours."""
        with self.repo.lock(slug):
            model = self.repo.load_model(slug)
            meta = self.repo.read_meta(slug)
            hit = set(propagate(model, changed_slots).artifacts) & set(meta.artifact_status)
            for t in hit:
                meta.artifact_status[t].stale = True
            if hit:
                self.repo.write_meta(slug, meta)
            return sorted(hit)
