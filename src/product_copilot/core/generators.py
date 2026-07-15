from __future__ import annotations

from anthropic import Anthropic

from product_copilot.core.analysis import estimate_confidence, soft_slots
from product_copilot.core.contracts import (
    AcceptanceCriteria, Brief, Epic, EngineOutput, EstimateDraft, PRD, ReleaseNotes, Stories,
)
from product_copilot.core.llm import _complete, build_prompt


def derive_stories(client: Anthropic, out: EngineOutput) -> Stories:
    """Pipeline stage: a filled model → implementable user stories."""
    system = build_prompt("stories.md")
    user = "Completed requirements model to decompose into user stories:\n" + out.model_dump_json(indent=2)
    return _complete(client, system, [{"role": "user", "content": user}], Stories)


def advise(client: Anthropic, out: EngineOutput) -> Brief:
    """Finalization stage: a completed model → design considerations, risks, opportunities."""
    system = build_prompt("brief.md")
    user = "Completed requirements model to advise on:\n" + out.model_dump_json(indent=2)
    return _complete(client, system, [{"role": "user", "content": user}], Brief)


def generate_prd(client: Anthropic, out: EngineOutput) -> PRD:
    """Artifact generator: a model → a Product Requirements Document."""
    system = build_prompt("prd.md")
    user = "Completed requirements model to turn into a PRD:\n" + out.model_dump_json(indent=2)
    return _complete(client, system, [{"role": "user", "content": user}], PRD)


def generate_criteria(client: Anthropic, out: EngineOutput) -> AcceptanceCriteria:
    """Artifact generator: a model → Given/When/Then acceptance criteria (the recette checklist)."""
    system = build_prompt("criteria.md")
    user = "Completed requirements model to turn into acceptance criteria:\n" + out.model_dump_json(indent=2)
    return _complete(client, system, [{"role": "user", "content": user}], AcceptanceCriteria)


def generate_epic(client: Anthropic, out: EngineOutput) -> Epic:
    """Artifact generator: a model → a delivery epic (work breakdown into trackable issues)."""
    system = build_prompt("epic.md")
    user = "Completed requirements model to turn into a delivery epic:\n" + out.model_dump_json(indent=2)
    return _complete(client, system, [{"role": "user", "content": user}], Epic)


def generate_release(client: Anthropic, out: EngineOutput, version: str = "") -> ReleaseNotes:
    """Artifact generator: a model → client-facing release notes. The caller may stamp a version."""
    system = build_prompt("release.md")
    user = "Completed requirements model to turn into release notes:\n" + out.model_dump_json(indent=2)
    notes = _complete(client, system, [{"role": "user", "content": user}], ReleaseNotes)
    if version:
        notes.version = version
    return notes


def estimate(client: Anthropic, out: EngineOutput, stories: Stories) -> tuple[EstimateDraft, list[str], str]:
    """Pipeline stage: stories + the model's soft slots → a day-based estimate.
    Returns (draft, soft_slots, confidence) — the latter two are Python-authoritative."""
    soft = soft_slots(out)
    system = build_prompt("estimate.md")
    user = (
        "User stories to estimate:\n"
        + stories.model_dump_json(indent=2)
        + "\n\nUnresolved (soft) slots — widen the range for any story that depends on one:\n"
        + (", ".join(soft) if soft else "(none — the model is solid)")
    )
    draft = _complete(client, system, [{"role": "user", "content": user}], EstimateDraft)
    return draft, soft, estimate_confidence(len(soft))
