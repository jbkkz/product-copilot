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

    def generate(self, artifact_type: str, model: EngineOutput, *, only: list[str] | None = None) -> object:
        """A model → a typed artifact contract (PRD, Stories, Epic, …). The concrete return type is
        the Pydantic contract for `artifact_type`; the caller renders/persists it."""
        ...
