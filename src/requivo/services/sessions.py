"""SessionService — create sessions and apply model updates through one validated pipeline.

`update_model` is the single write path for the model, whatever produced the proposal (the Anthropic
provider, a Claude Code proposal file, a future Web client): validate → diff against the current
model → propagate the blast radius → save a new revision → flag the artifacts that went stale →
compute readiness. It returns a structured `UpdateResult` so any caller can render it or emit `--json`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from requivo.core import persistence as store
from requivo.core.analysis import _label, _readiness_blockers
from requivo.core.contracts import EngineOutput
from requivo.core.dependencies import diff_models, propagate
from requivo.core.errors import SessionNotFoundError
from requivo.core.persistence import SessionMeta
from requivo.core.validation import validate_proposal


@dataclass
class Readiness:
    ready: bool
    blocking_slots: list[str]  # slot ids, schema order

    def to_dict(self) -> dict:
        return {"ready": self.ready, "blocking_slots": self.blocking_slots}


@dataclass
class UpdateResult:
    """The structured outcome of applying a proposal — the payload of `model apply [--json]`."""
    status: str                                   # "applied"
    revision: int
    changed_slots: list[str]                      # slot ids that materially moved
    invalidated_decisions: list[str] = field(default_factory=list)  # decision text needing re-validation
    stale_artifacts: list[str] = field(default_factory=list)        # artifact types now out of date
    readiness: Readiness = field(default_factory=lambda: Readiness(False, []))

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "revision": self.revision,
            "changed_slots": self.changed_slots,
            "invalidated_decisions": self.invalidated_decisions,
            "stale_artifacts": self.stale_artifacts,
            "readiness": self.readiness.to_dict(),
        }


def _readiness(model: EngineOutput) -> Readiness:
    blockers = _readiness_blockers(model)
    return Readiness(ready=not blockers, blocking_slots=blockers)


class SessionService:
    """Create, resolve, load, and mutate sessions. Stateless — every method reads/writes the store —
    so it is safe to construct per call (the CLI does) or hold as a singleton (a future Web app might)."""

    # ── resolution ────────────────────────────────────────────────────────────
    def resolve_slug(self, reference: str | Path) -> str:
        """Turn a user reference into a slug. Accepts a bare slug, a path to a session directory, or a
        path to a model.json — under either the canonical `.requivo/sessions/` or legacy `out/` root."""
        ref = str(reference)
        p = Path(ref)
        if p.name == "model.json" or p.name == "session.json":
            return p.parent.name
        if p.exists() and p.is_dir():
            return p.name
        return ref  # a bare slug

    def exists(self, slug: str) -> bool:
        """True if a usable session exists under either root (canonical or legacy)."""
        return store.session_exists(slug) or store.legacy_exists(slug)

    def _ensure_canonical(self, slug: str) -> None:
        """Before any mutation, make sure the session lives in the canonical store — migrating a
        legacy `out/<slug>/` session in place (preserving the originals) on first write."""
        if store.session_exists(slug):
            return
        if store.legacy_exists(slug):
            store.migrate_legacy(slug)
            return
        raise SessionNotFoundError(f"no session '{slug}'", details={"slug": slug})

    # ── creation ──────────────────────────────────────────────────────────────
    def create_session(self, request: str, *, context_cards: list[str] | None = None,
                        slug: str | None = None, provider: str | None = None,
                        model_name: str | None = None) -> SessionMeta:
        """Create a fresh session from a request (no model yet). If `slug` is omitted it is derived
        from the request and made collision-safe against existing sessions."""
        base = slug or store._slug(request)
        chosen = self._unique_slug(base, request)
        if store.session_exists(chosen):
            return store.read_meta(chosen)  # idempotent: re-discovering the same request reuses it
        return store.create_session(chosen, request, provider=provider, model_name=model_name,
                                    context_cards=context_cards)

    def ensure_canonical(self, slug: str) -> None:
        """Public form of the migrate-on-first-mutation guard — call before writing an artifact to a
        session that may still live only in the legacy `out/` store."""
        self._ensure_canonical(slug)

    def _unique_slug(self, base: str, request: str) -> str:
        """Reuse the folder for the same request (idempotent re-init); otherwise suffix a short hash so
        two different requests never collide on one slug."""
        if not store.session_exists(base):
            return base
        if store.session_request(base).strip() == request.strip():
            return base
        return f"{base}-{hashlib.sha1(request.encode('utf-8')).hexdigest()[:6]}"

    # ── reads ─────────────────────────────────────────────────────────────────
    def meta(self, slug: str) -> SessionMeta:
        """The session metadata, migrating a legacy session's *view* is not done here (reads stay
        non-mutating); a legacy-only session has no canonical meta, so callers that need meta for a
        read-only op should use `load_model` which tolerates the legacy layout."""
        return store.read_meta(slug)

    def load_model(self, slug: str) -> EngineOutput:
        """The current model. Reads the canonical store, falling back to a legacy `out/<slug>/` model
        for read-only operations (status/impact) so they work without forcing a migration."""
        if store.session_exists(slug):
            return store.load_session_model(slug)
        if store.legacy_exists(slug):
            return EngineOutput.model_validate_json((store.legacy_dir(slug) / "model.json").read_text())
        raise SessionNotFoundError(f"no session '{slug}'", details={"slug": slug})

    def list_sessions(self) -> list[SessionMeta]:
        return [store.read_meta(s) for s in store.list_session_slugs()]

    def cards(self, slug: str) -> list[str] | None:
        """The context-card selection recorded for a session (None == all cards) — read from canonical
        metadata, falling back to the legacy sidecar so a not-yet-migrated session keeps its cards."""
        if store.session_exists(slug):
            return store.read_meta(slug).context_cards
        if store.legacy_exists(slug):
            return store.session_cards(store.legacy_dir(slug) / "model.json")
        return None

    def request_text(self, slug: str) -> str:
        """The originating request — canonical request.md, or the legacy request.txt sidecar."""
        if store.session_exists(slug):
            return store.session_request(slug)
        if store.legacy_exists(slug):
            return store.load_request(store.legacy_dir(slug) / "model.json")
        return ""

    # ── the write path ──────────────────────────────────────────────────────────
    def diff(self, slug: str, proposal: dict | str, *, require_complete: bool = True) -> UpdateResult:
        """Dry run of `update_model`: validate the proposal and report what *would* change, without
        writing anything (`model diff`). `revision` is the revision that would be created."""
        new = validate_proposal(proposal, require_complete=require_complete)
        current = self.load_model(slug) if self.exists(slug) else None
        return self._plan(slug, current, new, apply=False)

    def update_model(self, slug: str, proposal: dict | str, *,
                     require_complete: bool = True) -> UpdateResult:
        """Validate a proposal and apply it as a new revision (`model apply`). Migrates a legacy
        session on this first mutation, saves the prior model as a revision, flags stale artifacts,
        and returns the structured outcome."""
        new = validate_proposal(proposal, require_complete=require_complete)
        self._ensure_canonical(slug)
        current = self.load_model(slug) if store.read_meta(slug).current_revision > 0 else None
        return self._plan(slug, current, new, apply=True)

    def _plan(self, slug: str, current: EngineOutput | None, new: EngineOutput, *, apply: bool) -> UpdateResult:
        # A first model (no prior) counts every present slot as changed, so the whole blast radius is
        # reported; otherwise only the slots that materially moved.
        changed = diff_models(current, new) if current is not None else list(new.model.keys())
        report = propagate(new, changed)
        invalidated = [d.decision for d in report.decisions]

        if apply:
            revision, meta = store.save_revision(slug, new)
            # Only artifacts that were actually generated can go stale — flag those in the blast radius.
            stale = [t for t in report.artifacts if t in meta.artifact_status]
            if stale:
                for t in stale:
                    meta.artifact_status[t].stale = True
                store.write_meta(slug, meta)
        else:
            meta = store.read_meta(slug) if store.session_exists(slug) else None
            revision = (meta.current_revision + 1) if meta else 1
            generated = set(meta.artifact_status) if meta else set()
            stale = [t for t in report.artifacts if t in generated]

        return UpdateResult(
            status="applied" if apply else "planned",
            revision=revision,
            changed_slots=changed,
            invalidated_decisions=invalidated,
            stale_artifacts=stale,
            readiness=_readiness(new),
        )

    # ── status ──────────────────────────────────────────────────────────────────
    def status(self, slug: str) -> dict:
        """A machine-readable status snapshot for `status --json`: readiness, blocking slots (with
        labels), current revision, and the artifacts with their freshness."""
        model = self.load_model(slug)
        rd = _readiness(model)
        meta = store.read_meta(slug) if store.session_exists(slug) else None
        artifacts = {}
        if meta:
            for t, st in meta.artifact_status.items():
                stale = st.stale or st.revision != meta.current_revision
                artifacts[t] = {"revision": st.revision, "filename": st.filename, "stale": stale}
        return {
            "slug": slug,
            "revision": meta.current_revision if meta else None,
            "readiness": {
                "ready": rd.ready,
                "blocking_slots": [{"slot": s, "label": _label(s)} for s in rd.blocking_slots],
            },
            "artifacts": artifacts,
        }
