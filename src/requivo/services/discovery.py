"""DiscoveryService — the provider-backed application orchestration, shared by every interface.

The Core is provider-free, and the CLI and Web must not each re-orchestrate "call the provider, then
apply through SessionService". This service *is* that orchestration, in one place: it holds a
`ReasoningProvider` plus the session/artifact services and exposes interface-neutral operations —
start a discovery, fold in answers, generate an artifact. The terminal CLI and the local Web are thin
callers over it, so there is exactly one place that turns a provider reply into a validated, versioned
model change.

It talks to the provider through the protocol only (`analyze` / `generate` / `provenance`), never to a
vendor's functions directly. That is what keeps the seam real rather than decorative: swapping in a
second provider is a constructor argument, and the provenance stamped on each revision comes from the
provider itself instead of a hard-coded `"anthropic"` string.

It never touches the filesystem or `model.json` directly — every write goes through `SessionService`
(validate → diff → propagate → revision → stale-flag) and `ArtifactService` (save with source
revision), so revision handling and staleness are identical to every other surface.
"""

from __future__ import annotations

from dataclasses import dataclass

from requivo.core.contracts import EngineOutput
from requivo.core.errors import RevisionConflictError
from requivo.core.persistence import ArtifactStatus
from requivo.render.markdown import brief_markdown, criteria_markdown, epic_markdown, prd_markdown, release_markdown
from requivo.services.artifacts import ArtifactService
from requivo.services.sessions import SessionService, SessionSnapshot, UpdateResult

# artifact type → the writer that turns its contract into the Markdown that gets saved. This is the
# vocabulary of "things a generation produces a document for"; `stories` and `estimate` are absent on
# purpose — they are terminal analyses that feed the estimate pipeline, not deliverables with a file.
_WRITERS = {
    "prd": prd_markdown,
    "criteria": criteria_markdown,
    "epic": epic_markdown,
    "release": release_markdown,
}

# Everything `generate()` can produce, in the order a user meets them. This is the source every
# interface asks — the CLI's verbs, the Web's buttons — so a new generator becomes available
# everywhere by being registered here, rather than by each surface keeping its own list and drifting.
GENERATABLE: tuple[str, ...] = ("brief", *_WRITERS)


@dataclass
class Generated:
    """What one generation produced. `status` is the saved artifact's provenance; `artifact` is the
    typed contract behind it, so a caller can render its own view (the CLI's terminal layout, the epic
    exports) without paying for a second provider call; `model` is the model it was rendered from —
    which for the assessment is the *post-absorption* model, not the one read at the start."""

    status: ArtifactStatus
    artifact: object
    model: EngineOutput


def _require_revision_zero(slug: str, revision: int) -> None:
    """A first discovery may only land on a session that has no model yet.

    Discovery *replaces* the model — it reasons from the request alone, without the current model —
    so running it on a session already at revision N does not refine that understanding, it discards
    it and writes a naive first-turn one over the top. The optimistic lock does not catch this: the
    call reads revision N and writes against revision N, so the precondition is satisfied while the
    content is a regression. The revision itself has to be the rule, and it cannot live in an
    interface (the Web only shows the button at revision 0) — a business rule enforced by a hidden
    button is not enforced."""
    if revision > 0:
        raise RevisionConflictError(
            f"session '{slug}' already carries a model (revision {revision}) — a fresh discovery "
            f'would replace it. Refine it instead (`requivo answer {slug} "…"`), or run this '
            "discovery under another slug.",
            details={"slug": slug, "expected": 0, "actual": revision})



def _require_a_model(slug: str, snap: SessionSnapshot) -> EngineOutput:
    """Generation may only run on a session that *has* a model — the mirror of the rule above (#152).

    `SessionSnapshot.model` is `None` before the first model, which it says on the field, and
    `generate`/`reason` unpacked it and handed it to the provider unchecked. Every generator builds
    its user message as `out.model_dump_json(...)`, so the failure landed as an `AttributeError`
    while assembling the prompt: no API call, nothing written, and a raw traceback where every other
    refusal in this codebase is a structured error naming the remedy.

    Written here rather than at each call site for the reason its sibling gives in the paragraph
    above: the Web already had this rule — `if meta.current_revision == 0` in `routes/sessions.py`,
    which renders an "offer to run discovery" page instead — and the CLI had nothing, so the rule was
    enforced on one surface out of three. Hiding a button is good on top of an enforced rule and is
    not one.

    Returning the model rather than returning `None` is deliberate: the caller binds the result, so
    the narrowing is in the type as well as in the control flow, and the eight Pyright errors this
    closes cannot come back as a new call site that forgets the guard."""
    if snap.model is None:
        raise RevisionConflictError(
            f"session '{slug}' has no model yet (revision 0) — there is nothing to generate from. "
            f'Run `requivo discover` on it, or `requivo answer {slug} "…"` if a discovery is in '
            "progress.",
            details={"slug": slug, "expected": 1, "actual": snap.revision})
    return snap.model


def absorb_reasoning(out: EngineOutput, brief) -> None:
    """Persist the assessment's reasoning (decisions, challenges, opportunities) into the model so every
    generator inherits it, not just the facts. Called wherever the assessment is produced, before the
    model is applied — the single definition, shared by the CLI and the Web."""
    out.decisions = brief.decisions
    out.challenges = brief.challenges
    out.opportunities = brief.opportunities


class DiscoveryService:
    """Provider-backed orchestration over the session/artifact services.

    The provider is built lazily, so constructing the service never needs an API key — only the
    operations that actually reason do (consulting an existing session needs none). Inject a
    `ReasoningProvider` to swap the reasoning backend; `client=` is the shorthand for "the default
    provider over this SDK client", which is what the tests and the CLI use.
    """

    def __init__(self, provider=None, *, client=None, sessions: SessionService | None = None,
                 artifacts: ArtifactService | None = None, repo=None):
        self._provider = provider
        self._client = client
        self.sessions = sessions or SessionService(repo)
        # The artifact service defaults to *this service's* storage, not to the process default. On a
        # file backing the two were indistinguishable — both resolve to the same workspace — which is
        # what hid the bug: constructing `DiscoveryService(sessions=SessionService(postgres_repo))`
        # sent the sessions to Postgres and the artifacts to the local filesystem, and every call
        # succeeded. One repository per service, chosen once, is the only shape that cannot split.
        self.artifacts = artifacts or ArtifactService(self.sessions.repo)

    def _need_provider(self):
        """The reasoning provider, built on first use so a key is only required for provider actions.
        The default is imported here rather than at module scope: the service depends on the protocol,
        and only the fallback construction knows which implementation is the default one."""
        if self._provider is None:
            from requivo.providers.anthropic import AnthropicProvider
            self._provider = AnthropicProvider(self._client)
        return self._provider

    def _provenance(self, op: str, *, cards: list[str] | None, surface: str) -> dict:
        """The provenance for a revision: what the provider says about itself, plus which of our
        surfaces asked for it (the one thing the provider cannot know)."""
        return {**self._need_provider().provenance(op, only=cards), "surface": surface}

    # ── discovery ────────────────────────────────────────────────────────────────
    def create_only(self, request: str, *, cards: list[str] | None = None,
                    slug: str | None = None) -> str:
        """Persist a request as a session with no model yet — no LLM call. The 'Create session only'
        path: capture the request now, run discovery later."""
        return self.sessions.create_session(request, context_cards=cards, slug=slug).slug

    def _claim_session(self, request: str, *, cards: list[str] | None, slug: str | None):
        """Create (or reuse) the session a first discovery will land on, and hold it to revision 0.

        Idempotent creation and "a discovery replaces the model" are each reasonable alone and unsafe
        together: the second `discover` of the same request lands on the first one's session. This is
        the single gate, so every entry point — `start`, `finalize_discovery`, the CLI's interactive
        loop — refuses the same case in the same words."""
        provider = self._need_provider()
        meta = self.sessions.create_session(
            request, context_cards=cards, slug=slug,
            provider=provider.name, model_name=provider.model_name())
        _require_revision_zero(meta.slug, meta.current_revision)
        return meta

    def finalize_discovery(self, request: str, out: EngineOutput, *, cards: list[str] | None = None,
                           slug: str | None = None, brief=None, surface: str = "discover") -> str:
        """Create the session and apply a discovered model through the validated path. When a `brief` is
        given (a finalized discovery), its reasoning is absorbed into the model first. Shared by the
        CLI's interactive loop (which produced `out` itself) and `start()`.

        A first discovery lands on revision 0 and nothing else. Session creation is idempotent — the
        same request reuses its session — so without that precondition a re-run would quietly replace a
        model that had been refined over several turns with a naive first-turn one, and a write that
        landed while the provider was reasoning would be overwritten the same way. Both cases are a
        `revision_conflict`, which is recoverable; a silent replacement is not."""
        meta = self._claim_session(request, cards=cards, slug=slug)
        if brief is not None:
            absorb_reasoning(out, brief)
        self.sessions.update_model(
            meta.slug, out.model_dump_json(), expected_revision=0,
            provenance=self._provenance("analyze", cards=cards, surface=surface))
        return meta.slug

    def start(self, request: str, *, cards: list[str] | None = None, slug: str | None = None,
              finalize: bool = False, surface: str = "discover") -> str:
        """Run one discovery turn on a fresh request and apply it, returning the session slug. With
        `finalize`, also produce and absorb the solution assessment's reasoning.

        The session is claimed *before* the provider is called. Creation is idempotent, so re-running
        a discovery whose session already carries a model is refused — and refusing it after the call
        means having paid for reasoning (twice, when finalizing) that can only be thrown away. The
        check is cheap and the call is not."""
        provider = self._need_provider()
        meta = self._claim_session(request, cards=cards, slug=slug)
        out = provider.analyze(request, only=cards)
        brief = provider.generate("brief", out, only=cards) if finalize else None
        return self.finalize_discovery(request, out, cards=cards, slug=meta.slug, brief=brief,
                                       surface=surface)

    # ── interactive drafting (before there is a session) ─────────────────────────
    # An interactive surface reasons several turns against a request that has not been persisted
    # yet, shows each one, collects answers and reasons again — and only then claims a session and
    # applies the result. The two operations below are that loop's provider calls, so a surface can
    # own the *loop* (prompting, rendering, when to stop) without owning a client (#77).
    #
    # Deliberately not a callback and not a generator: the service is handed the state and returns a
    # result, exactly as every other operation here does. A seam that reached back into the caller to
    # ask a question would have moved the coupling rather than removed it, and `DiscoveryService`
    # would be the layer that knows a terminal exists.
    #
    # Nothing here writes, so there is no revision, no provenance and no lock to get wrong. What is
    # drafted becomes real through `finalize_discovery`, which is where the revision-zero gate and the
    # validated apply path live.

    def draft_turn(self, request: str, *, current_model: EngineOutput | None = None,
                   answers: str | None = None, cards: list[str] | None = None) -> EngineOutput:
        """One un-persisted discovery turn: the request alone on the first call, then the model so far
        plus the answers just given.

        The model *is* the accumulated state — a turn needs the original request for context, the
        current model, and the new answers, and nothing else — which is what lets the same operation
        serve a blocking TTY loop, a web form and a Claude Code turn.

        `reuse_system=True` because this is the one operation on this service that a caller repeats:
        a drafting loop makes several calls off a byte-identical system prompt (the CLI's caps at
        eight), so the cache breakpoint is genuinely read back and earns its 1.25x write. Every other
        operation here is one call per invocation and says the opposite (#9, #58)."""
        return self._need_provider().analyze(
            request, current_model=current_model, answers=answers, only=cards, reuse_system=True)

    def draft_assessment(self, model: EngineOutput, *, cards: list[str] | None = None):
        """The solution assessment for a model that is not a session yet — the last provider call of
        an interactive discovery, before `finalize_discovery` absorbs its reasoning and writes.

        Distinct from `generate(slug, "brief")`, which reads a persisted session, applies the absorbed
        reasoning as a revision and saves a document. There is no session here to read or write, so
        this reasons and returns; the write is one call later and goes through the same validated path
        as every other surface's."""
        return self._need_provider().generate("brief", model, only=cards)

    def run_discovery(self, slug: str, *, surface: str = "discover") -> UpdateResult:
        """Run the first discovery turn on an already-created session (the 'create session only' path
        run later): read its stored request + cards, reason, and apply the model as revision 1.

        Held to revision 0 like every other first discovery, and held *before* the provider call:
        this reasons from the request alone — it never sees the current model — so on a session that
        has been refined it would write a naive first-turn model over that work, with the optimistic
        lock satisfied throughout (it reads revision N and writes against N). The `POST
        /sessions/{slug}/discover` route reaches this directly; the Web only offers the button at
        revision 0, but that is a rendering decision, not a rule."""
        self.sessions.ensure_canonical(slug)
        snap = self.sessions.snapshot(slug)
        _require_revision_zero(slug, snap.revision)
        out = self._need_provider().analyze(snap.request, only=snap.context_cards)
        return self.sessions.update_model(
            slug, out.model_dump_json(), expected_revision=snap.revision,
            provenance=self._provenance("analyze", cards=snap.context_cards, surface=surface))

    # ── refinement ───────────────────────────────────────────────────────────────
    def answer(self, slug: str, answers: str, *, expected_revision: int | None = None,
               surface: str = "answer") -> UpdateResult:
        """Fold the user's answers into a session's model as a new revision.

        A turn has the same seam as a generation: the provider reasons over the model as it was, and the
        session can move meanwhile. So the precondition defaults to the revision this turn actually read
        — a caller that knows better (the Web, which carries the revision the user saw in the form) can
        still pass its own. The turn reasons from one coherent `SessionSnapshot` — the revision it will
        be held to and the model it reasoned over are the same read, not two. A legacy `out/` session is
        migrated first, so there is always a real revision to hold it to."""
        self.sessions.ensure_canonical(slug)
        snap = self.sessions.snapshot(slug)
        out = self._need_provider().analyze(
            snap.request, current_model=snap.model, answers=answers, only=snap.context_cards)
        return self.sessions.update_model(
            slug, out.model_dump_json(),
            expected_revision=expected_revision if expected_revision is not None else snap.revision,
            provenance=self._provenance("analyze", cards=snap.context_cards, surface=surface))

    # ── generation ───────────────────────────────────────────────────────────────
    def reason(self, slug: str, artifact_type: str, **kwargs):
        """Produce an artifact's typed contract without saving anything — for the terminal-only views
        (`stories`, `estimate`) that are analyses rather than deliverables. Still goes through the
        provider seam, so no interface reaches past it to a vendor's functions. Nothing is written, so
        there is no provenance to get wrong — but the model and the cards it is read against still come
        from one snapshot, so the analysis is of a session state that actually existed.

        `**kwargs` is what an analysis needs beyond the model: `estimate` is read against the
        `stories` a previous call produced. Until #77 that one call was made by `cli.py` directly, on
        a second client of its own, which is exactly the "no interface reaches past it" claim above
        being false one line below where it was written."""
        snap = self.sessions.snapshot(slug)
        model = _require_a_model(slug, snap)
        return self._need_provider().generate(artifact_type, model, only=snap.context_cards,
                                              **kwargs)

    def generate(self, slug: str, artifact_type: str, *, surface: str = "generate", **kwargs) -> Generated:
        """Generate an artifact through the provider and save it against the session with its source
        revision. Every interface goes through here, so a given artifact is produced, saved and tracked
        identically whether it was asked for from the terminal, the browser, or Claude Code.

        `brief` (the solution assessment) is the one with an extra step: its reasoning is absorbed back
        into the model as a revision, so downstream artifacts inherit the decisions and challenges, not
        just the facts.

        **Generation is not atomic.** A provider call runs for seconds to minutes, and the session can
        move underneath it — a second browser tab folding in answers, a CLI apply, a Claude Code turn.
        So the revision the model was read at is captured *before* the call and carried through both
        writes: as the optimistic-lock precondition on any apply (a concurrent change becomes a clean
        conflict instead of silently overwriting that revision) and as the artifact's recorded source
        (so a document written from revision 1 is never filed as if it came from revision 2).

        The revision and the model come from one `SessionSnapshot`, because reading them separately
        made the provenance a lie in the other direction: a write landing between the two reads gave
        revision N with the model of N+1, and the artifact was generated from the newer model and
        filed against the older revision — a mismatch nothing downstream can detect, since the number
        is perfectly plausible."""
        self.sessions.ensure_canonical(slug)  # migrate a legacy session before its first artifact write
        snap = self.sessions.snapshot(slug)
        source_revision, cards = snap.revision, snap.context_cards
        out = _require_a_model(slug, snap)
        provider = self._need_provider()

        if artifact_type == "brief":
            brief = provider.generate("brief", out, only=cards)
            absorb_reasoning(out, brief)
            # `out` is the revision-N model plus the reasoning just derived from it. Applying it without
            # the precondition would discard any revision that landed while the provider was reasoning.
            applied = self.sessions.update_model(
                slug, out.model_dump_json(), expected_revision=source_revision,
                provenance=self._provenance("brief", cards=cards, surface=surface))
            # The assessment renders exactly the model that apply just wrote, so it belongs to that revision.
            status = self.artifacts.save(slug, "brief", brief_markdown(out, brief),
                                         source_revision=applied.revision)
            return Generated(status=status, artifact=brief, model=out)

        try:
            writer = _WRITERS[artifact_type]
        except KeyError as e:
            raise ValueError(f"{artifact_type!r} has no saveable document — use `reason()`") from e
        artifact = provider.generate(artifact_type, out, only=cards, **kwargs)
        status = self._save_generated(slug, artifact_type, writer(artifact), source_revision)
        return Generated(status=status, artifact=artifact, model=out)

    def _save_generated(self, slug: str, artifact_type: str, content: str, source_revision: int):
        """Save a generated artifact against the revision it was actually produced from.

        An artifact written from revision 1 while revision 2 was landing must not inherit revision 2's
        freshness — that is the one case where a stale document reports itself as up to date. This used
        to be handled here, by re-diffing after the write and replaying the change through the graph.
        It now belongs to `ArtifactService.save`, which does it for *every* caller rather than only the
        provider path: the same hazard reaches a Claude Code turn saving a document it wrote earlier.
        Passing the honest source revision is the whole contribution this layer needs to make."""
        return self.artifacts.save(slug, artifact_type, content, source_revision=source_revision)
