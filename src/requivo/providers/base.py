"""The reasoning-provider seam.

A provider turns natural language into a validated model, and a model into an artifact contract. It
is the *only* component allowed to call an LLM. Everything downstream — validation, versioning,
readiness, impact, rendering — is deterministic core and does not care which provider produced the
model.

The protocol is kept deliberately close to the real call shapes already in the code (`analyze` for a
discovery turn, `generate` for an artifact), not a speculative universal LLM abstraction. The
Anthropic implementation, and any future one, conforms to it; the Claude Code surface bypasses it
entirely (Claude reasons, the deterministic CLI applies).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from requivo.core.contracts import EngineOutput


@runtime_checkable
class ReasoningProvider(Protocol):
    """The qualitative-reasoning contract. Deterministic core never imports an implementation of this;
    it receives the `EngineOutput` a provider produced and takes over from there."""

    name: str
    """Short identity of the implementation (`"anthropic"`), stamped on the session and on every
    revision it produces. Declared here because `DiscoveryService` reads it on the first discovery,
    before it reasons — an implementation without one is not usable, so leaving it out of the contract
    described a seam narrower than the one the code depends on.

    A bare annotation rather than a method, deliberately: `@runtime_checkable` *does* check non-method
    members, so `isinstance` rejects a provider missing this one, and it matches how the attribute is
    already exposed and read (`provider.name`, not `provider.name()`). The cost is that `issubclass`
    against this protocol now raises `TypeError` — Python refuses it for any protocol with a
    non-method member. Use `isinstance`. Note it checks *presence*, not type: an implementation is
    still trusted to make this a `str`."""

    def analyze(
        self,
        request: str,
        *,
        current_model: EngineOutput | None = None,
        answers: str | None = None,
        only: list[str] | None = None,
    ) -> EngineOutput:
        """A discovery turn: request (+ optional prior model and new answers) → a filled model.
        `only` restricts the context cards, held constant across a session's turns."""
        ...

    def generate(self, artifact_type: str, model: EngineOutput, *, only: list[str] | None = None,
                 **kwargs) -> object:
        """A model → a typed artifact contract (PRD, Stories, Epic, …). The concrete return type is
        the Pydantic contract for `artifact_type`; the caller renders/persists it. `**kwargs` carries
        the few per-artifact options a generator takes (e.g. a `version` to stamp on release notes)."""
        ...

    def model_name(self) -> str:
        """The reasoning model this provider will call — recorded on the session it produces."""
        ...

    def provenance(self, op: str, *, only: list[str] | None = None) -> dict:
        """Who reasoned, with what, and against which prompt, for one operation (`analyze` or an
        artifact type). The service records this on the revision rather than assembling it itself —
        provider identity and prompt identity belong to the layer that owns them, so a second provider
        cannot end up stamping its revisions with another's name."""
        ...
