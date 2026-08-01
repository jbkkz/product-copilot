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
from requivo.core.dependencies import diff_models
from requivo.core.persistence import ArtifactStatus
from requivo.render.markdown import brief_markdown, criteria_markdown, epic_markdown, prd_markdown, release_markdown
from requivo.services.artifacts import ArtifactService
from requivo.services.sessions import SessionService, UpdateResult

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
                 artifacts: ArtifactService | None = None):
        self._provider = provider
        self._client = client
        self.sessions = sessions or SessionService()
        self.artifacts = artifacts or ArtifactService()

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

    def finalize_discovery(self, request: str, out: EngineOutput, *, cards: list[str] | None = None,
                           slug: str | None = None, brief=None, surface: str = "discover") -> str:
        """Create the session and apply a discovered model through the validated path. When a `brief` is
        given (a finalized discovery), its reasoning is absorbed into the model first. Shared by the
        CLI's interactive loop (which produced `out` itself) and `start()`."""
        provider = self._need_provider()
        meta = self.sessions.create_session(
            request, context_cards=cards, slug=slug,
            provider=provider.name, model_name=provider.model_name())
        if brief is not None:
            absorb_reasoning(out, brief)
        self.sessions.update_model(
            meta.slug, out.model_dump_json(),
            provenance=self._provenance("analyze", cards=cards, surface=surface))
        return meta.slug

    def start(self, request: str, *, cards: list[str] | None = None, slug: str | None = None,
              finalize: bool = False, surface: str = "discover") -> str:
        """Run one discovery turn on a fresh request and apply it, returning the session slug. With
        `finalize`, also produce and absorb the solution assessment's reasoning."""
        provider = self._need_provider()
        out = provider.analyze(request, only=cards)
        brief = provider.generate("brief", out, only=cards) if finalize else None
        return self.finalize_discovery(request, out, cards=cards, slug=slug, brief=brief, surface=surface)

    def run_discovery(self, slug: str, *, surface: str = "discover") -> UpdateResult:
        """Run the first discovery turn on an already-created session (the 'create session only' path
        run later): read its stored request + cards, reason, and apply the model as revision 1."""
        request = self.sessions.request_text(slug)
        cards = self.sessions.cards(slug)
        out = self._need_provider().analyze(request, only=cards)
        return self.sessions.update_model(
            slug, out.model_dump_json(),
            provenance=self._provenance("analyze", cards=cards, surface=surface))

    # ── refinement ───────────────────────────────────────────────────────────────
    def answer(self, slug: str, answers: str, *, expected_revision: int | None = None,
               surface: str = "answer") -> UpdateResult:
        """Fold the user's answers into a session's model as a new revision.

        A turn has the same seam as a generation: the provider reasons over the model as it was, and the
        session can move meanwhile. So the precondition defaults to the revision this turn actually read
        — a caller that knows better (the Web, which carries the revision the user saw in the form) can
        still pass its own. Only a session with no metadata yet (a legacy `out/` layout, migrated on
        this write) goes without one, because there is no revision to hold it to."""
        read_revision = self.sessions.meta(slug).current_revision if self.sessions.exists_meta(slug) else None
        before = self.sessions.load_model(slug)
        cards = self.sessions.cards(slug)
        out = self._need_provider().analyze(
            self.sessions.request_text(slug), current_model=before, answers=answers, only=cards)
        return self.sessions.update_model(
            slug, out.model_dump_json(),
            expected_revision=expected_revision if expected_revision is not None else read_revision,
            provenance=self._provenance("analyze", cards=cards, surface=surface))

    # ── generation ───────────────────────────────────────────────────────────────
    def reason(self, slug: str, artifact_type: str):
        """Produce an artifact's typed contract without saving anything — for the terminal-only views
        (`stories`, `estimate`) that are analyses rather than deliverables. Still goes through the
        provider seam, so no interface reaches past it to a vendor's functions."""
        return self._need_provider().generate(
            artifact_type, self.sessions.load_model(slug), only=self.sessions.cards(slug))

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
        (so a document written from revision 1 is never filed as if it came from revision 2)."""
        self.sessions.ensure_canonical(slug)  # migrate a legacy session before its first artifact write
        source_revision = self.sessions.meta(slug).current_revision
        out = self.sessions.load_model(slug)
        cards = self.sessions.cards(slug)
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
        """Save a generated artifact against the revision it was actually produced from, then replay any
        change that landed during generation through the dependency graph.

        Without the replay, an artifact written from revision 1 while revision 2 was being applied would
        be recorded at the current revision and inherit that revision's freshness — the one case where
        a stale document reports itself as up to date."""
        status = self.artifacts.save(slug, artifact_type, content, source_revision=source_revision)
        current = self.sessions.meta(slug).current_revision
        if current != source_revision:
            changed = diff_models(self.sessions.load_revision(slug, source_revision),
                                  self.sessions.load_model(slug))
            self.artifacts.mark_stale(slug, changed)
            status = self.sessions.meta(slug).artifact_status[artifact_type]
        return status
