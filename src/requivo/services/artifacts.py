"""ArtifactService — save, list, and read generated artifacts against a session.

An artifact is a *view* of the model at a specific revision. This service records that provenance
(source revision) when an artifact is saved, reports each artifact's freshness relative to the current
model, and can flag the blast radius stale after a model change. It never generates content — the
provider (or Claude Code) produces the text; this service persists and tracks it.
"""

from __future__ import annotations

from requivo.core import persistence as store
from requivo.core.dependencies import propagate
from requivo.core.errors import RequivoError, SessionNotFoundError
from requivo.core.persistence import ArtifactStatus

# The saveable artifact vocabulary: type → filename under <session>/artifacts/. This is the union of
# the buildable deliverables; it is a superset of `dependencies.ARTIFACT_FILES` (which tracks only the
# ones that participate in staleness — the assessment is the live analysis layer, not a stale-able
# deliverable, but it is still a saveable artifact).
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
        defaults to the session's current revision (the common case: generate-then-save)."""
        filename = self._filename(artifact_type)
        if not store.session_exists(slug):
            raise SessionNotFoundError(
                f"session '{slug}' is not in the canonical store; apply a model first", details={"slug": slug})
        meta = store.read_meta(slug)
        rev = source_revision if source_revision is not None else meta.current_revision
        return store.save_session_artifact(slug, artifact_type, filename, content, source_revision=rev)

    def list(self, slug: str) -> dict[str, dict]:
        """Every recorded artifact with its freshness relative to the current model revision."""
        meta = store.read_meta(slug)
        out: dict[str, dict] = {}
        for t, st in meta.artifact_status.items():
            stale = st.stale or st.revision != meta.current_revision
            out[t] = {"revision": st.revision, "filename": st.filename,
                      "updated_at": st.updated_at, "stale": stale}
        return out

    def show(self, slug: str, artifact_type: str) -> str:
        """The saved content of an artifact."""
        filename = self._filename(artifact_type)
        p = store.canonical_dir(slug) / "artifacts" / filename
        if not p.exists():
            raise SessionNotFoundError(
                f"session '{slug}' has no saved {artifact_type!r} artifact",
                details={"slug": slug, "type": artifact_type})
        return p.read_text()

    def mark_stale(self, slug: str, changed_slots: list[str]) -> list[str]:
        """Flag every generated artifact in the blast radius of `changed_slots` stale, and return the
        types flagged. Used after a model change made outside `update_model`."""
        model = store.load_session_model(slug)
        meta = store.read_meta(slug)
        hit = set(propagate(model, changed_slots).artifacts) & set(meta.artifact_status)
        for t in hit:
            meta.artifact_status[t].stale = True
        if hit:
            store.write_meta(slug, meta)
        return sorted(hit)
