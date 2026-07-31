from __future__ import annotations

from anthropic import Anthropic

from requivo.core.analysis import estimate_confidence, soft_slots
from requivo.core.contracts import (
    PRD,
    AcceptanceCriteria,
    Brief,
    EngineOutput,
    Epic,
    EstimateDraft,
    ReleaseNotes,
    Stories,
)
from requivo.core.llm import _complete, build_prompt

# Every generator threads `only` — the context-card selection its discovery ran against, read from
# session.json by the CLI — so an artifact is grounded in the same cards discovery used, not silently
# the full set. None means all cards (the default and the pre-0.6.1 behaviour).


def derive_stories(client: Anthropic, out: EngineOutput, only: list[str] | None = None) -> Stories:
    """Pipeline stage: a filled model → implementable user stories."""
    system = build_prompt("stories.md", only)
    user = "Completed requirements model to decompose into user stories:\n" + out.model_dump_json(indent=2)
    return _complete(client, system, [{"role": "user", "content": user}], Stories)


def advise(client: Anthropic, out: EngineOutput, only: list[str] | None = None) -> Brief:
    """Finalization stage: a completed model → design considerations, risks, opportunities."""
    system = build_prompt("brief.md", only)
    user = "Completed requirements model to advise on:\n" + out.model_dump_json(indent=2)
    return _complete(client, system, [{"role": "user", "content": user}], Brief)


def generate_prd(client: Anthropic, out: EngineOutput, only: list[str] | None = None) -> PRD:
    """Artifact generator: a model → a Product Requirements Document."""
    system = build_prompt("prd.md", only)
    user = "Completed requirements model to turn into a PRD:\n" + out.model_dump_json(indent=2)
    return _complete(client, system, [{"role": "user", "content": user}], PRD)


def generate_criteria(client: Anthropic, out: EngineOutput, only: list[str] | None = None) -> AcceptanceCriteria:
    """Artifact generator: a model → Given/When/Then acceptance criteria (the recette checklist)."""
    system = build_prompt("criteria.md", only)
    user = "Completed requirements model to turn into acceptance criteria:\n" + out.model_dump_json(indent=2)
    return _complete(client, system, [{"role": "user", "content": user}], AcceptanceCriteria)


def generate_epic(client: Anthropic, out: EngineOutput, only: list[str] | None = None) -> Epic:
    """Artifact generator: a model → a delivery epic (work breakdown into trackable issues)."""
    system = build_prompt("epic.md", only)
    user = "Completed requirements model to turn into a delivery epic:\n" + out.model_dump_json(indent=2)
    return _complete(client, system, [{"role": "user", "content": user}], Epic)


def generate_release(client: Anthropic, out: EngineOutput, version: str = "",
                     only: list[str] | None = None) -> ReleaseNotes:
    """Artifact generator: a model → client-facing release notes. The caller may stamp a version."""
    system = build_prompt("release.md", only)
    user = "Completed requirements model to turn into release notes:\n" + out.model_dump_json(indent=2)
    notes = _complete(client, system, [{"role": "user", "content": user}], ReleaseNotes)
    if version:
        notes.version = version
    return notes


def estimate(client: Anthropic, out: EngineOutput, stories: Stories,
             only: list[str] | None = None) -> tuple[EstimateDraft, list[str], str]:
    """Pipeline stage: stories + the model's soft slots → a day-based estimate.
    Returns (draft, soft_slots, confidence) — the latter two are Python-authoritative."""
    soft = soft_slots(out)
    system = build_prompt("estimate.md", only)
    user = (
        "User stories to estimate:\n"
        + stories.model_dump_json(indent=2)
        + "\n\nUnresolved (soft) slots — widen the range for any story that depends on one:\n"
        + (", ".join(soft) if soft else "(none — the model is solid)")
    )
    draft = _complete(client, system, [{"role": "user", "content": user}], EstimateDraft)
    return draft, soft, estimate_confidence(len(soft))
