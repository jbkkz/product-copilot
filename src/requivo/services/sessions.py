"""SessionService — create sessions and apply model updates through one validated pipeline.

`update_model` is the single write path for the model, whatever produced the proposal (the Anthropic
provider, a Claude Code proposal file, Requivo Web): validate → diff against the current
model → propagate the blast radius → save a new revision → flag the artifacts that went stale →
compute readiness. It returns a structured `UpdateResult` so any caller can render it or emit `--json`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from requivo.core import persistence as store
from requivo.core.analysis import _readiness_blockers, model_status
from requivo.core.context import resolve_cards
from requivo.core.contracts import EngineOutput
from requivo.core.dependencies import (
    ARTIFACT_FILES,
    REASONING_CONSUMERS,
    ReasoningDiff,
    diff_models,
    diff_reasoning,
    propagate,
)
from requivo.core.errors import SessionExistsError, SessionNotFoundError
from requivo.core.persistence import SessionMeta
from requivo.core.validation import validate_proposal
from requivo.services.repository import SessionRepository, default_repository


@dataclass
class Readiness:
    ready: bool
    blocking_slots: list[str]  # slot ids, schema order

    def to_dict(self) -> dict:
        return {"ready": self.ready, "blocking_slots": self.blocking_slots}


@dataclass(frozen=True)
class SessionSnapshot:
    """One consistent read of a session: its revision, the model *at* that revision, and the inputs a
    provider call needs. Taken under the session lock, so the parts cannot disagree.

    Reading the revision and the model as two separate calls looks harmless and is not: a write
    landing between them yields revision N with the model of N+1. The generation then reasons from the
    newer model and files the artifact as coming from the older revision — content and provenance
    describing different sources, which is precisely the claim the product cannot afford to get wrong.
    Worse, it is undetectable afterwards: the number is plausible.

    The lock is released before the provider call. It is not there to make the whole operation atomic —
    it cannot be, the call takes minutes — but to make the *basis* coherent. `expected_revision` on the
    write is what handles the session moving afterwards."""

    slug: str
    revision: int
    model: EngineOutput | None          # None before the first model (revision 0)
    request: str
    context_cards: list[str] | None     # None == every card


@dataclass
class UpdateResult:
    """The structured outcome of applying a proposal — the payload of `model apply [--json]`."""
    status: str                                   # "applied"
    revision: int
    changed_slots: list[str]                      # slot ids that materially moved
    invalidated_decisions: list[str] = field(default_factory=list)  # decision text needing re-validation
    invalidated_challenges: list[str] = field(default_factory=list)  # challenge headlines now in question
    stale_artifacts: list[str] = field(default_factory=list)        # artifact types now out of date
    readiness: Readiness = field(default_factory=lambda: Readiness(False, []))
    # What moved in the reasoning layer — ids, per collection. Reported separately from
    # `changed_slots` because they answer different questions: the slots say the *facts* moved, these
    # say the *judgment over them* moved. Either can invalidate an artifact on its own.
    changed_decisions: list[str] = field(default_factory=list)
    changed_challenges: list[str] = field(default_factory=list)
    changed_opportunities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "revision": self.revision,
            "changed_slots": self.changed_slots,
            "changed_decisions": self.changed_decisions,
            "changed_challenges": self.changed_challenges,
            "changed_opportunities": self.changed_opportunities,
            "invalidated_decisions": self.invalidated_decisions,
            "invalidated_challenges": self.invalidated_challenges,
            "stale_artifacts": self.stale_artifacts,
            "readiness": self.readiness.to_dict(),
        }


def _readiness(model: EngineOutput) -> Readiness:
    blockers = _readiness_blockers(model)
    return Readiness(ready=not blockers, blocking_slots=blockers)


class SessionService:
    """Create, resolve, load, and mutate sessions through one validated pipeline. Storage is injected
    as a `SessionRepository` (files by default, Postgres in Cloud), so this orchestration is reused
    verbatim across backings. Stateless beyond that handle — safe to construct per call (the CLI does)
    or hold as a singleton (Requivo Web does)."""

    def __init__(self, repo: SessionRepository | None = None):
        self.repo: SessionRepository = repo or default_repository()

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
        """True if a usable session exists (the repository decides what backs it)."""
        return self.repo.exists(slug)

    def _ensure_canonical(self, slug: str) -> None:
        """Before any mutation, make sure the session is in the mutation-backed store — for a file
        backing this migrates a legacy `out/<slug>/` session in place on first write."""
        self.repo.ensure_writable(slug)

    # ── creation ──────────────────────────────────────────────────────────────
    def create_session(self, request: str, *, context_cards: list[str] | None = None,
                        slug: str | None = None, provider: str | None = None,
                        model_name: str | None = None) -> SessionMeta:
        """Create a fresh session from a request (no model yet). If `slug` is omitted it is derived
        from the request and made collision-safe against existing sessions.

        Creation is idempotent on *identity*, and identity is the request **and its context cards** —
        not the request alone. The cards are part of the provenance of everything a session will
        reason: the same request read against `b2b-platform` and against `event-ops` gets different
        impact estimates, so different questions. Keying on the request alone meant the second call
        silently returned the first session, with cards the caller had not asked for and had no way to
        notice. A different selection now gets its own session instead.

        The claim on a slug is `repo.create` itself, which is atomic — a check-then-create here would
        let two concurrent callers both decide the session was theirs to make.

        The card selection is resolved here rather than trusted. The CLI and the Web both call
        `resolve_cards` before they get this far, which made it look like the service could rely on
        them — but "the interfaces are careful" is not an integrity boundary, and requivo-cloud calls
        exactly this layer. An unknown card recorded on a session is not inert: every later turn reads
        the selection back, and an empty resolved selection means *every* card, so a bad name silently
        widens the context instead of narrowing it."""
        context_cards = resolve_cards(context_cards) if context_cards else None
        base = slug or store._slug(request)
        for candidate in (base, f"{base}-{self._identity_hash(request, context_cards)}"):
            try:
                return self.repo.create(candidate, request, provider=provider, model_name=model_name,
                                        context_cards=context_cards)
            except SessionExistsError:
                if self._same_identity(candidate, request, context_cards):
                    return self.repo.read_meta(candidate)  # idempotent re-init of the same discovery
        raise SessionExistsError(
            f"sessions '{base}' and '{base}-{self._identity_hash(request, context_cards)}' both exist "
            "with a different request or context selection — pass an explicit slug",
            details={"slug": base})

    def ensure_canonical(self, slug: str) -> None:
        """Public form of the migrate-on-first-mutation guard — call before writing an artifact to a
        session that may still live only in the legacy `out/` store."""
        self._ensure_canonical(slug)

    @staticmethod
    def _identity_hash(request: str, context_cards: list[str] | None) -> str:
        """The fallback slug suffix: a short hash over what makes a discovery distinct. The cards join
        the hash only when there are some, so the ordinary no-cards case keeps the slugs it had."""
        parts = [request.strip()]
        if context_cards:
            parts.append(",".join(sorted(context_cards)))
        return hashlib.sha1("␟".join(parts).encode("utf-8")).hexdigest()[:6]

    def _same_identity(self, slug: str, request: str, context_cards: list[str] | None) -> bool:
        """Whether an existing session is the same discovery: same request, same context selection.
        `None` (every card) and an explicit list are different selections, not the same one."""
        if not self.repo.has_meta(slug):
            return False  # a legacy-only session has no recorded cards to compare
        existing = self.repo.context_cards(slug)
        return (self.repo.request_text(slug).strip() == request.strip()
                and (sorted(existing) if existing else existing)
                == (sorted(context_cards) if context_cards else context_cards))

    # ── reads ─────────────────────────────────────────────────────────────────
    def meta(self, slug: str) -> SessionMeta:
        """The session metadata. A legacy-only session has no metadata, so callers that need it for a
        read-only op should use `load_model`, which tolerates the legacy layout."""
        return self.repo.read_meta(slug)

    def load_model(self, slug: str) -> EngineOutput:
        """The current model. Reads the mutation-backed store, falling back to a legacy `out/<slug>/`
        model for read-only operations (status/impact) so they work without forcing a migration."""
        return self.repo.load_model(slug)

    def exists_meta(self, slug: str) -> bool:
        """True if the session is in the mutation-backed store — i.e. `meta()` will succeed. A legacy
        `out/` session is readable but has no metadata until its first write migrates it."""
        return self.repo.has_meta(slug)

    def load_revision(self, slug: str, revision: int) -> EngineOutput:
        """A historical model revision — the basis for "what moved since this artifact was made?"."""
        return self.repo.load_revision(slug, revision)

    def list_sessions(self) -> list[SessionMeta]:
        return [self.repo.read_meta(s) for s in self.repo.list_slugs()]

    def cards(self, slug: str) -> list[str] | None:
        """The context-card selection recorded for a session (None == all cards)."""
        return self.repo.context_cards(slug)

    def request_text(self, slug: str) -> str:
        """The originating request text (empty string if none)."""
        return self.repo.request_text(slug)

    def snapshot(self, slug: str) -> SessionSnapshot:
        """One coherent read of everything a provider call needs — see `SessionSnapshot`. The session
        must be in the mutation-backed store; call `ensure_canonical` first for one that may still be
        legacy, which is what every provider-backed operation does anyway before it writes."""
        if not self.repo.has_meta(slug):
            raise SessionNotFoundError(f"no session '{slug}'", details={"slug": slug})
        with self.repo.lock(slug):
            meta = self.repo.read_meta(slug)
            return SessionSnapshot(
                slug=slug,
                revision=meta.current_revision,
                model=self.load_model(slug) if meta.current_revision > 0 else None,
                request=self.repo.request_text(slug),
                context_cards=meta.context_cards,
            )

    # ── the write path ──────────────────────────────────────────────────────────
    def diff(self, slug: str, proposal: dict | str, *, require_complete: bool = True) -> UpdateResult:
        """Dry run of `update_model`: validate the proposal and report what *would* change, without
        writing anything (`model diff`). `revision` is the revision that would be created."""
        current = self.load_model(slug) if self.exists(slug) else None
        new = validate_proposal(proposal, require_complete=require_complete, current=current)
        return self._plan(slug, current, new, apply=False)

    def update_model(self, slug: str, proposal: dict | str, *, require_complete: bool = True,
                     expected_revision: int | None = None, provenance: dict | None = None) -> UpdateResult:
        """Validate a proposal and apply it as a new revision (`model apply`). Migrates a legacy
        session on this first mutation, saves the prior model as a revision, flags stale artifacts,
        and returns the structured outcome.

        `expected_revision` is the optimistic-locking precondition (see `persistence.save_revision`):
        omit it for the single-user CLI, pass the client's last-known revision from a concurrent
        service. `provenance` records who produced the revision (provider / surface / model)."""
        self._ensure_canonical(slug)
        # One lock for the whole update. Reading the current model, saving the revision and rewriting
        # the artifact flags are three storage calls that must see one consistent session: without
        # this, a writer that lands between the read and the flag rewrite has its staleness silently
        # reverted by ours.
        #
        # Validation is *inside* the lock rather than before it, because a proposal is resolved against
        # the model it refines (`ModelProposal.resolve`): the reasoning it carries forward has to come
        # from the same model the diff is computed against, or a concurrent write could slip between
        # the two and the carried reasoning would describe a model that is no longer there.
        with self.repo.lock(slug):
            current = self.load_model(slug) if self.repo.read_meta(slug).current_revision > 0 else None
            new = validate_proposal(proposal, require_complete=require_complete, current=current)
            return self._plan(slug, current, new, apply=True,
                              expected_revision=expected_revision, provenance=provenance)

    def _plan(self, slug: str, current: EngineOutput | None, new: EngineOutput, *, apply: bool,
              expected_revision: int | None = None, provenance: dict | None = None) -> UpdateResult:
        # A first model (no prior) counts every present slot as changed, so the whole blast radius is
        # reported; otherwise only the slots that materially moved.
        changed = diff_models(current, new) if current is not None else list(new.model.keys())
        # The reasoning layer moves independently of the slots, and every generator is prompted with
        # it, so it invalidates artifacts on its own. On a first apply there is nothing to compare
        # against — the reasoning arrived with the model it describes.
        reasoning = diff_reasoning(current, new) if current is not None else ReasoningDiff()
        # Artifacts rest on slots via the static ARTIFACT_SLOTS map, so the blast radius is basis-neutral
        # — any model with these `changed` slots yields the same artifact set.
        report = propagate(new, changed)

        # Reasoning invalidation is about the *prior established* reasoning a change unseats — it exists
        # only when the current model on disk carries decisions/challenges (a refinement turn often drops
        # them from its reply, so `new` may have none). On a first apply — `current is None` — the
        # reasoning in `new` was proposed *for* this very state; it is not stale, so nothing is
        # invalidated. Computing this against `new` (the old `basis` fallback) was the bug: it reported a
        # model's own freshly-proposed decisions and challenges as invalidated on their first apply.
        if current is not None and (current.decisions or current.challenges):
            prior = propagate(current, changed)
            invalidated_decisions = [d.decision for d in prior.decisions]
            invalidated_challenges = [c.headline for c in prior.challenges]
        else:
            invalidated_decisions, invalidated_challenges = [], []

        def _resolve_stale(generated: set[str]) -> list[str]:
            # The blast radius, intersected with what actually exists on disk. Two edge sets feed it:
            # the slots an artifact consumes (ARTIFACT_SLOTS), and — when the reasoning layer moved —
            # REASONING_CONSUMERS, which is every generator, since each is prompted with the full
            # model. The saved assessment needs no special case in either: it rests on every slot.
            hit = set(report.artifacts) | (REASONING_CONSUMERS if reasoning.changed else set())
            return [t for t in ARTIFACT_FILES if t in hit and t in generated]

        if apply:
            revision, meta = self.repo.save_revision(
                slug, new, expected_revision=expected_revision, provenance=provenance)
            stale = _resolve_stale(set(meta.artifact_status))
            if stale:
                for t in stale:
                    meta.artifact_status[t].stale = True
                self.repo.write_meta(slug, meta)
        else:
            meta = self.repo.read_meta(slug) if self.repo.has_meta(slug) else None
            revision = (meta.current_revision + 1) if meta else 1
            stale = _resolve_stale(set(meta.artifact_status) if meta else set())

        return UpdateResult(
            status="applied" if apply else "planned",
            revision=revision,
            changed_slots=changed,
            invalidated_decisions=invalidated_decisions,
            invalidated_challenges=invalidated_challenges,
            stale_artifacts=stale,
            readiness=_readiness(new),
            changed_decisions=reasoning.decisions,
            changed_challenges=reasoning.challenges,
            changed_opportunities=reasoning.opportunities,
        )

    # ── status ──────────────────────────────────────────────────────────────────
    def status(self, slug: str) -> dict:
        """A machine-readable status snapshot for `status --json` — rich enough that Claude Code and a
        the Web render the full picture (understanding checklist, priority questions, gaps,
        context) without rebuilding the presentation logic in another language. Everything here is a
        pure projection of the model plus the session metadata."""
        model = self.load_model(slug)
        meta = self.repo.read_meta(slug) if self.repo.has_meta(slug) else None
        artifacts = {}
        if meta:
            for t, st in meta.artifact_status.items():
                # Explicit stale flag only — revision is provenance, not an invalidation rule. See
                # ArtifactService.list for the rationale (dependency-graph freshness, not revision drift).
                artifacts[t] = {"revision": st.revision, "filename": st.filename, "stale": st.stale}
        return {
            "slug": slug,
            "revision": meta.current_revision if meta else None,
            **model_status(model),
            "context_cards": meta.context_cards if meta else None,
            "artifacts": artifacts,
        }
