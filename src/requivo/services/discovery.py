"""DiscoveryService — the provider-backed application orchestration, shared by every interface.

The Core is provider-free, and the CLI and Web must not each re-orchestrate "call the provider, then
apply through SessionService". This service *is* that orchestration, in one place: it holds a provider
client plus the session/artifact services and exposes interface-neutral operations — start a discovery,
fold in answers, generate an artifact. The terminal CLI and the local Web are thin callers over it, so
there is exactly one place that turns a provider reply into a validated, versioned model change.

It never touches the filesystem or `model.json` directly — every write goes through `SessionService`
(validate → diff → propagate → revision → stale-flag) and `ArtifactService` (save with source
revision), so revision handling and staleness are identical to every other surface.
"""

from __future__ import annotations

from requivo.core.contracts import EngineOutput
from requivo.providers.anthropic import advise, answer_turn, current_model_name, generate_prd, new_client, run
from requivo.render.markdown import brief_markdown, prd_markdown
from requivo.services.artifacts import ArtifactService
from requivo.services.sessions import SessionService, UpdateResult


def absorb_reasoning(out: EngineOutput, brief) -> None:
    """Persist the assessment's reasoning (decisions, challenges, opportunities) into the model so every
    generator inherits it, not just the facts. Called wherever `advise()` runs, before the model is
    applied — the single definition, shared by the CLI and the Web (it used to live in `cli.py`)."""
    out.decisions = brief.decisions
    out.challenges = brief.challenges
    out.opportunities = brief.opportunities


class DiscoveryService:
    """Provider-backed orchestration over the session/artifact services. The client is built lazily, so
    constructing the service never needs an API key — only the operations that actually call the
    provider do (consulting an existing session needs none). Inject a client (or fake) for tests."""

    def __init__(self, client=None, *, sessions: SessionService | None = None,
                 artifacts: ArtifactService | None = None):
        self._client = client
        self.sessions = sessions or SessionService()
        self.artifacts = artifacts or ArtifactService()

    def _need_client(self):
        """The provider client, built on first use so a key is only required for provider actions."""
        if self._client is None:
            self._client = new_client()
        return self._client

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
        meta = self.sessions.create_session(
            request, context_cards=cards, slug=slug,
            provider="anthropic", model_name=current_model_name())
        if brief is not None:
            absorb_reasoning(out, brief)
        self.sessions.update_model(
            meta.slug, out.model_dump_json(),
            provenance={"provider": "anthropic", "surface": surface, "model_name": current_model_name()})
        return meta.slug

    def start(self, request: str, *, cards: list[str] | None = None, slug: str | None = None,
              finalize: bool = False, surface: str = "discover") -> str:
        """Run one discovery turn on a fresh request and apply it, returning the session slug. With
        `finalize`, also produce and absorb the solution assessment's reasoning."""
        client = self._need_client()
        out = run(client, [{"role": "user", "content": request}], only=cards)
        brief = advise(client, out, only=cards) if finalize else None
        return self.finalize_discovery(request, out, cards=cards, slug=slug, brief=brief, surface=surface)

    def run_discovery(self, slug: str, *, surface: str = "discover") -> UpdateResult:
        """Run the first discovery turn on an already-created session (the 'create session only' path
        run later): read its stored request + cards, reason, and apply the model as revision 1."""
        request = self.sessions.request_text(slug)
        cards = self.sessions.cards(slug)
        out = run(self._need_client(), [{"role": "user", "content": request}], only=cards)
        return self.sessions.update_model(
            slug, out.model_dump_json(),
            provenance={"provider": "anthropic", "surface": surface, "model_name": current_model_name()})

    # ── refinement ───────────────────────────────────────────────────────────────
    def answer(self, slug: str, answers: str, *, expected_revision: int | None = None,
               surface: str = "answer") -> UpdateResult:
        """Fold the user's answers into a session's model as a new revision. `expected_revision`
        (optimistic lock) lets a concurrent surface reject a stale write with a clean conflict."""
        before = self.sessions.load_model(slug)
        cards = self.sessions.cards(slug)
        out = answer_turn(self._need_client(), before, self.sessions.request_text(slug), answers, only=cards)
        return self.sessions.update_model(
            slug, out.model_dump_json(), expected_revision=expected_revision,
            provenance={"provider": "anthropic", "surface": surface, "model_name": current_model_name()})

    # ── generation ───────────────────────────────────────────────────────────────
    def generate(self, slug: str, artifact_type: str, *, surface: str = "generate"):
        """Generate an artifact through the provider and save it against the session with its source
        revision. `brief` (the solution assessment) also absorbs its reasoning back into the model as a
        revision, so downstream artifacts inherit it. Returns the saved `ArtifactStatus`.

        The first Web version supports `brief` and `prd`; the rest (stories/criteria/estimate/epic)
        already exist as CLI generators and can be added here without new orchestration."""
        self.sessions.ensure_canonical(slug)  # migrate a legacy session before its first artifact write
        out = self.sessions.load_model(slug)
        cards = self.sessions.cards(slug)
        client = self._need_client()
        if artifact_type == "brief":
            brief = advise(client, out, only=cards)
            absorb_reasoning(out, brief)
            self.sessions.update_model(
                slug, out.model_dump_json(),
                provenance={"provider": "anthropic", "surface": surface, "model_name": current_model_name()})
            return self.artifacts.save(slug, "brief", brief_markdown(out, brief))
        if artifact_type == "prd":
            return self.artifacts.save(slug, "prd", prd_markdown(generate_prd(client, out, only=cards)))
        raise ValueError(f"generation of {artifact_type!r} is not supported yet")
