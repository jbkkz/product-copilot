"""SessionService — create sessions and apply model updates through one validated pipeline.

`update_model` is the single write path for the model, whatever produced the proposal (the Anthropic
provider, a Claude Code proposal file, Requivo Web): validate → diff against the current
model → propagate the blast radius → save a new revision → flag the artifacts that went stale →
compute readiness. It returns a structured `UpdateResult` so any caller can render it or emit `--json`.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from requivo.core import persistence as store
from requivo.core.analysis import model_status, readiness_blockers
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
from requivo.core.persistence import SessionMeta, Store
from requivo.core.selectors import display_token
from requivo.core.validation import require_input_within_bounds, validate_proposal
from requivo.paths import workspace_root
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


@dataclass(frozen=True)
class SessionEntry:
    """One slug in the store, with either its metadata or the reason it could not be read (#7).

    The third state, made representable. `list_sessions()` can only answer *here is the metadata* or
    *this whole listing raised*, so an aggregate built on it has no way to say **we could not read
    this one** — and the only shapes left are to raise for the set (one bad session hides every good
    one) or to drop the member (the reader is told nothing is wrong and the session is gone). Both
    were live: the first is #7, the second is what a naive fix for it produces.

    `error` is the exception's own text rather than a code. A user whose session was written by a
    newer Requivo needs to read *upgrade requivo*, not `unreadable` — the remedy is the part worth
    keeping, and a flattened code discards exactly it."""

    slug: str
    meta: SessionMeta | None = None
    error: str | None = None

    @property
    def readable(self) -> bool:
        """True when the metadata loaded. Named rather than left as `meta is not None` so a caller
        reads the question it is asking instead of the representation of the answer."""
        return self.meta is not None


@dataclass(frozen=True)
class RescopeResult:
    """The structured outcome of `session rescope` — the payload of `session rescope [--json]`."""
    slug: str
    previous_context_cards: list[str] | None
    context_cards: list[str] | None
    revision: int
    changed: bool                                  # False when the selection did not move

    def to_dict(self) -> dict:
        return {"slug": self.slug, "previous_context_cards": self.previous_context_cards,
                "context_cards": self.context_cards, "revision": self.revision, "changed": self.changed}


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
    blockers = readiness_blockers(model)
    return Readiness(ready=not blockers, blocking_slots=blockers)


class SessionService:
    """Create, resolve, load, and mutate sessions through one validated pipeline. Storage is injected
    as a `SessionRepository` (files by default, Postgres elsewhere), so this orchestration is reused
    verbatim across backings. Stateless beyond that handle — safe to construct per call (the CLI does)
    or hold as a singleton (Requivo Web does)."""

    def __init__(self, repo: SessionRepository | None = None):
        self.repo: SessionRepository = repo or default_repository()

    # ── resolution ────────────────────────────────────────────────────────────
    def resolve_slug(self, reference: str | Path, *, accept_path: bool = True) -> str:
        """Turn a user reference into a slug. Accepts a bare slug, a path to a session directory, or a
        path to a model.json — under either the canonical `.requivo/sessions/` or legacy `out/` root.

        `accept_path=False` refuses anything path-shaped outright, naming the reference exactly as
        given (#402). The eight generator verbs (`answer`, `brief`, `prd`, `stories`, `estimate`,
        `criteria`, `epic`, `release`) pass it: they resolve a *slug* and then read and write the
        store's own copy of the session -- `ArtifactService.save` refuses anything that is not
        `has_meta(slug)` -- so they never truly "open" a file they are handed the way `status` and
        `impact` do, and a path was never a meaningful input for them. Before this, a stray or
        fabricated path was mined for its parent directory's name regardless, and reported on --
        or, worse, silently resolved against -- whatever session happens to carry that name, which
        is the wrong-cause class this refuses instead of producing.

        "Path-shaped" is decided from the string alone -- a separator, or a `model.json`/`session.json`
        basename -- never from whether something happens to exist on disk at that name (invariant 17):
        a bare slug that coincidentally collides with an unrelated directory in the caller's cwd must
        still resolve as the slug it was typed as, not be refused as a path because of filesystem noise
        nothing about the command line suggested.

        When paths are still accepted (every `deterministic/` verb, and the directory branch below),
        the same wrong-cause failure is closed at its root: a `model.json`/`session.json` reference
        is only mined for its parent directory's name when the file is actually there. A reference to
        a file that was never written falls through unchanged, so the caller's `exists()` check fails
        naming the path itself, never a slug carved out of a segment of it.

        **The directory branch itself carried the identical defect and had no such guard** (#414):
        `p.exists() and p.is_dir()` mined ANY directory's own name, whether or not a session lived
        behind it -- so a directory that merely shared its final path segment with an unrelated real
        session silently resolved to that session, one branch over from the bug #402 fixed above. It
        now mines a directory's name only when the directory carries its own session marker
        (`session.json` or `model.json`), the directory-shaped analogue of "the file is actually
        there"; a directory with neither is refused naming the path exactly as given, not a slug
        carved from it."""
        ref = str(reference)
        p = Path(ref)
        if not accept_path:
            looks_like_a_path = (
                p.name in ("model.json", "session.json")
                or os.sep in ref or (os.altsep and os.altsep in ref)
            )
            if looks_like_a_path:
                # `ref` is untrusted user input reaching a message that gets printed verbatim
                # (`cli.py`'s `app()` writes a `RequivoError` straight to stderr) -- every mention
                # goes through `display_token` (invariant 14, #40; the same call `no_session_message`
                # makes on this identical field), not only the first as it read before. A raw second
                # occurrence would leave a control character or an ANSI escape in `ref` free to forge
                # a line the refusal never wrote. `display_token` returns the value unchanged when it
                # is already one safe line, so an ordinary path still reads exactly as typed; only an
                # unsafe one is escaped, the same tradeoff `no_session_message` already makes.
                safe_ref = display_token(ref)
                raise SessionNotFoundError(
                    f"{safe_ref} looks like a path, but this command takes a session slug -- it "
                    "resolves and writes back into a session, not the file itself, so a path is "
                    "not enough to tell it which one. Pass the session's slug (see `requivo "
                    f"session list`), or inspect the file directly with `requivo status {safe_ref}`.",
                    details={"ref": ref},
                )
            return ref
        if p.name in ("model.json", "session.json"):
            # `Path.is_file()` swallows ENOENT/ENOTDIR into `False`, which is what "mine only a
            # real file" needs -- but it re-raises everything else, including `PermissionError` on
            # a directory this process cannot traverse into. `core/persistence.py`'s `_probe` exists
            # for exactly this shape (`Path.exists()` has two returns and three outcomes) and this
            # is the same probe one field over, so it gets the same third state rather than an
            # uncaught traceback escaping a verb that promises every clean failure surfaces without
            # one (#402, found in review).
            try:
                is_real_file = p.is_file()
            except OSError as e:
                raise SessionNotFoundError(
                    f"could not tell whether {display_token(ref)} is a saved model.json: {e}",
                    details={"ref": ref},
                ) from e
            return p.parent.name if is_real_file else ref
        # **The same wrong-cause class #402 closed for the model.json/session.json branch, one
        # branch over** (#414). This used to mine ANY directory's own name, unconditional on
        # whether a session actually lived behind it -- so a path merely sharing its final segment
        # with an unrelated real session silently resolved to that session, and a failure on a
        # path that resolved to nothing named a slug the user never wrote. On the same terms as
        # the file branch above ("only mined when the file is actually there"), a directory is
        # only mined for its own name when it carries a session's own marker -- canonical
        # `session.json` or legacy `model.json`.
        #
        # **Two probes here, not one, and both re-raise -- found in review of this same change.**
        # `p.exists()`/`p.is_dir()` independently stat `p` itself, which fails with `PermissionError`
        # when an ANCESTOR of the reference denies traversal, a distinct case from the marker probe
        # below failing on the referenced directory's *own* contents. Wrapping only the marker probe
        # left the entry gate itself able to raise a bare, uncaught traceback for a directory that
        # is otherwise perfectly healthy, purely because something above it on the path could not be
        # examined -- the identical third state `is_file()` above is already guarded against, missed
        # one probe over.
        try:
            is_dir = p.exists() and p.is_dir()
        except OSError as e:
            raise SessionNotFoundError(
                f"could not tell whether {display_token(ref)} is a session directory: {e}",
                details={"ref": ref},
            ) from e
        if is_dir:
            try:
                looks_like_a_session = (p / "session.json").exists() or (p / "model.json").exists()
            except OSError as e:
                raise SessionNotFoundError(
                    f"could not tell whether {display_token(ref)} is a session directory: {e}",
                    details={"ref": ref},
                ) from e
            if looks_like_a_session:
                return p.name
            raise SessionNotFoundError(
                f"{display_token(ref)} does not look like a session directory -- it has no "
                "session.json or model.json of its own, so it is not something this command "
                "can resolve a slug from. Pass the session's slug instead (see `requivo "
                "session list`).",
                details={"ref": ref},
            )
        return ref  # a bare slug

    @staticmethod
    def slug_hint(text: str) -> str:
        """Turn arbitrary text into a slug-shaped name — the surface's route to slug derivation.

        Not a repository method: deriving a name from a request is a naming policy, and it is the
        same policy whatever backs the store. It is here rather than left to each caller because
        `cli.py` was reaching into `core.persistence` for the slug derivation itself (#76), which
        is a surface holding a core implementation detail — the one direct storage call in that file
        that had no defensible reason. What keeps a surface out of the store is this seam, not the
        underscore `derive_slug` used to carry.

        The two callers want it for different inputs and the same reason: `create_session` derives
        a slug from the request text, and `requivo discover <file>` derives a *hint* from a
        filename stem, because "Leave Approval v2.md" has a space and a capital and a slug names a
        directory. Passing the raw stem through turned an ordinary input file into an
        `invalid_slug` error.
        """
        return store.derive_slug(text)

    def exists(self, slug: str) -> bool:
        """True if a usable session exists (the repository decides what backs it)."""
        return self.repo.exists(slug)

    def no_session(self, ref: str, *, what: str = "session",
                   details: dict | None = None) -> SessionNotFoundError:
        """The refusal for "there is no such session" — the surface's route to it (#243).

        The sentence itself names the sessions root, so it is the store's fact to state — read off
        *this service's own repository*, not the process ambient default (#272's cosmetic fourth).
        Before this an explicitly-rooted `SessionService` still reported the ambient workspace's root
        in this one message, which is exactly the kind of silent disagreement #272 exists to close:
        every other read on this service went to the right store, and only the error text about
        *why a session could not be found there* named a different one. Instance method now, not
        `@staticmethod`, for that reason alone — every caller already writes `svc.no_session(...)`
        on an instance, so nothing at any call site changes.

        What this method exists for beyond that is the *seam*: `cli.py` and
        `deterministic/sessions.py` raise this at six sites, and reaching into `core.persistence`
        for it would put a copy concern in the allowlist of justified **filesystem** concerns —
        which is the one thing that list must not start meaning (#76). A message is not a path, even
        when it contains one, so it comes through the service like everything else a surface needs.

        `what` widens the noun for `_resolve_ref`, the one caller whose absence is genuinely wider:
        it accepts a path to a `model.json` as well as a slug. `details` is explicit for the same
        caller and for a harder reason: its published key is `ref`, not `slug`, because what it was
        handed may be a path — and `details` is a **contract** (`docs/compatibility.md`), so a
        rewording of the message must not be able to move it. Defaulting it here rather than letting
        each site build one is what keeps the other five identical.
        """
        message = self._store_for_error_text().no_session_message(ref, what=what)
        return SessionNotFoundError(message,
                                    details=details if details is not None else {"slug": ref})

    def _store_for_error_text(self) -> Store:
        """The `core.persistence.Store` this service's own repository addresses, for the one place
        outside any repository method that reads the workspace root: `no_session`'s error text
        (#272's cosmetic fourth). Duck-typed against `self.repo.store()` rather than added to the
        `SessionRepository` protocol, for the identical reason `DiscoveryService._store_for_repo`
        gives — a Postgres backing has no filesystem root to hand back, and the fallback below is
        exactly what this call had *unconditionally* before #272, since it read the ambient default
        regardless of what `self.repo` addressed."""
        get_store = getattr(self.repo, "store", None)
        if callable(get_store):
            return cast(Store, get_store())
        return Store(workspace_root())

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
        them — but "the interfaces are careful" is not an integrity boundary, and an external consumer calls
        exactly this layer. An unknown card recorded on a session is not inert: every later turn reads
        the selection back, and an empty resolved selection means *every* card, so a bad name silently
        widens the context instead of narrowing it.

        The same argument holds for the request's *size* (#255): the Web checks `MAX_REQUEST_CHARS`
        in `routes/sessions.py` for its own friendly re-render, but that is the interface being
        careful, not a guarantee — this is the one place every request is captured before it can
        reach a provider or land on disk, so this is where the cap actually has to live."""
        require_input_within_bounds(request, field="request")
        context_cards = resolve_cards(context_cards) if context_cards else None
        base = slug or self.slug_hint(request)
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
        """Every session's metadata, raising on the first one that will not load.

        The strict read, kept as such. A caller acting on one session it named is right to want the
        failure; what must not use this is an **aggregate**, because one unreadable member then
        raises before any row exists to degrade. Those call `list_entries` instead.
        """
        return [self.repo.read_meta(s) for s in self.repo.list_slugs()]

    def list_entries(self) -> list[SessionEntry]:
        """Every session, degrading per member instead of raising for the whole set (#7).

        This is the *source* of the rows, and it is where invariant 15 has to be enforced: guarding
        the calls made on each row leaves the comprehension that produced the rows unguarded, which
        is the line that breaks first. `read_meta` refuses an unreadable `session.json` and a
        `format_version` newer than this build — so a user who ran a newer Requivo once, or imported
        a colleague's archive, loses the listing of every *other* session too.

        The catch is bare `Exception`, deliberately. An aggregate's contract is that one member
        cannot take the view down, and the set of ways a member can be broken is open — a truncated
        JSON file, a permissions fault, a pydantic `ValidationError`, a code this build has not been
        written yet. Naming a family here is how the guard ends up nominally on and effectively off
        for the next failure mode, which is the shape of #7 itself. `doctor`'s `_session_health`
        already made this call for the same question; this is that decision, reused rather than
        re-litigated. `BaseException` is *not* caught: a `KeyboardInterrupt` is not a broken session.

        A member that cannot be read is reported, never dropped. A listing that silently omits it
        tells the reader nothing is wrong and loses the session — the same absence, one step
        quieter.

        Failing to list the slugs at all is **not** caught here and propagates. That is not one
        member failing, it is the aggregate having no members to speak for: there is no row to name
        the problem in, and answering `[]` would tell a reader their sessions were deleted. It is
        the same distinction `_session_health` draws with `total: None` versus `total: 0`.

        **Between those two sits a third source of rows** (#80). `list_unexaminable` returns the
        names the store found and could not decide about — a directory the process cannot stat into
        is the file backing's case. That is neither a member failing to load nor the aggregate
        having no members: it is a name that may or may not be a session, and until #80 it was not a
        row at all, because the failure happened inside the scan and took the whole listing with it.
        It is a degraded row here for the reason every other degraded row is one: the alternatives
        are to drop it, which loses it silently, or to call it a session, which is what nobody
        established.

        Sorted by slug at the end so the two sources interleave into one listing. `list_slugs` is
        already sorted, so a workspace with nothing unexaminable in it comes back in exactly the
        order it always did.
        """
        entries = []
        for slug in self.repo.list_slugs():
            try:
                entries.append(SessionEntry(slug=slug, meta=self.repo.read_meta(slug)))
            except Exception as e:  # noqa: BLE001 - see the docstring: an open set, by contract
                entries.append(SessionEntry(slug=slug, meta=None, error=str(e)))
        for entry in self.repo.list_unexaminable():
            entries.append(SessionEntry(slug=entry.name, meta=None, error=entry.error))
        return sorted(entries, key=lambda e: e.slug)

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
            raise SessionNotFoundError(store.no_session_message(slug), details={"slug": slug})
        with self.repo.lock(slug):
            meta = self.repo.read_meta(slug)
            return SessionSnapshot(
                slug=slug,
                revision=meta.current_revision,
                model=self.load_model(slug) if meta.current_revision > 0 else None,
                request=self.repo.request_text(slug),
                context_cards=meta.context_cards,
            )

    def rescope(self, slug: str, context_cards: list[str] | None) -> RescopeResult:
        """Re-scope an existing session's context-card selection (`session rescope`).

        Argued out in #168, against the issue's own four questions, so the reasoning lives here
        rather than only in the issue:

        1. **New revision, or mutate in place?** Both, depending on what is on disk. Once a model
           exists, every revision already there was reasoned under the *old* selection — silently
           overwriting `context_cards` would leave the history claiming a switch never happened. So
           this is recorded as its own revision: the model carries forward **unchanged** (same
           content, same hash) and the provenance names the surface as a re-scope rather than a
           reasoning turn (`surface="session-rescope"`), which is exactly what
           `RevisionRecord.surface` exists to distinguish. Before any model exists (revision 0)
           there is no provenance yet for the old selection to describe — nothing has been reasoned
           against it — so this is a plain metadata write, no revision minted for content that was
           never there.
        2. **Does it mark existing artifacts stale?** No. Staleness is the dependency graph
           (invariant 1), and `ARTIFACT_SLOTS`/`REASONING_CONSUMERS` — the only two edge sets that
           feed it — know slots and reasoning, not context. Nothing an artifact already on disk
           reads has moved: the model is unchanged, so every artifact still faithfully describes
           what it was generated from. Inventing a context edge would be a real feature (a fifth
           kind of dependency `core/dependencies.py` does not have), not a re-scope.
        3. **Does it re-run anything?** No. `DiscoveryService` reads `context_cards` off a fresh
           `SessionSnapshot` on every call (`snapshot().context_cards`, read straight from
           `meta.context_cards`), so writing the new selection here is already the whole effect —
           the *next* turn reasons against it, nothing already produced is touched or redone.
        4. **Untrusted input, same as creation.** `resolve_cards` runs here exactly as it does in
           `create_session` (invariant 14's second door): a persisted `context_cards` is untrusted
           the moment it is read back, and a re-scope is a second entrance onto the same value, so
           an unknown name is refused here rather than recorded and discovered on the next turn.

        Re-scoping to the selection a session already has (order aside — this is a set) is a no-op:
        `changed=False`, nothing written, no revision spent on a switch that is not one.
        """
        self._ensure_canonical(slug)
        resolved = resolve_cards(context_cards) if context_cards else None
        with self.repo.lock(slug):
            meta = self.repo.read_meta(slug)
            previous = meta.context_cards
            same = ((sorted(previous) if previous else previous)
                    == (sorted(resolved) if resolved else resolved))
            if same:
                return RescopeResult(slug=slug, previous_context_cards=previous,
                                     context_cards=previous, revision=meta.current_revision,
                                     changed=False)
            if meta.current_revision > 0:
                model = self.load_model(slug)
                _, meta = self.repo.save_revision(slug, model,
                                                  provenance={"surface": "session-rescope"})
            else:
                # No model yet — nothing was reasoned under `previous`, so there is no revision to
                # mint. `save_revision` bumps `updated_at` for the branch above; this branch is the
                # only writer here, so it has to stamp it itself. `store._now()` rather than a second
                # implementation of "UTC, second precision, Z-suffixed" — one format, one place.
                meta.updated_at = store._now()
            meta.context_cards = resolved
            self.repo.write_meta(slug, meta)
        return RescopeResult(slug=slug, previous_context_cards=previous, context_cards=resolved,
                             revision=meta.current_revision, changed=True)

    # ── the write path ──────────────────────────────────────────────────────────
    def diff(self, slug: str, proposal: dict | str, *, require_complete: bool = True) -> UpdateResult:
        """Dry run of `update_model`: validate the proposal and report what *would* change, without
        writing anything (`model diff`). `revision` is the revision that would be created."""
        current = self.load_model(slug) if self.exists(slug) else None
        new = validate_proposal(proposal, require_complete=require_complete, current=current)
        return self._plan(slug, current, new, apply=False)

    def update_model(self, slug: str, proposal: dict | str, *, require_complete: bool = True,
                     expected_revision: int | None = None, provenance: dict | None = None) -> UpdateResult:
        """Validate a proposal and apply it as a new revision (`model apply`): saves the prior model
        as a revision, flags stale artifacts, and returns the structured outcome. A session that lives
        only in the retired `out/` layout is *named in the error*, not migrated behind your back —
        `ensure_writable` raises pointing at `requivo session migrate`.

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
