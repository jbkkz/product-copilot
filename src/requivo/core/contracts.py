from __future__ import annotations

import functools
import json
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from requivo.paths import FRAMEWORK

SOFT_COMPLETENESS = 70  # below this a slot is "soft" (tunable)


@functools.lru_cache(maxsize=1)
def schema_slot_ids() -> tuple[frozenset[str], frozenset[str]]:
    """(allowed, required) slot ids from framework/model_schema.json. `required` excludes any slot
    flagged `optional`. Cached — the schema is read once. This is the single source of the slot
    vocabulary the model must speak; the contract and readiness both defer to it."""
    slots = json.loads((FRAMEWORK / "model_schema.json").read_text())["slots"]
    allowed = frozenset(s["id"] for s in slots)
    required = frozenset(s["id"] for s in slots if not s.get("optional", False))
    return allowed, required


def missing_required_slots(present: set[str]) -> list[str]:
    """Required slot ids absent from `present`, in schema order — what a complete model still owes."""
    _, required = schema_slot_ids()
    return [sid for sid in _schema_order() if sid in required and sid not in present]


def unknown_slots(present: set[str]) -> list[str]:
    """Slot ids in `present` that the schema does not define — hallucinated / typo'd keys."""
    allowed, _ = schema_slot_ids()
    return sorted(present - allowed)


@functools.lru_cache(maxsize=1)
def _schema_order() -> tuple[str, ...]:
    slots = json.loads((FRAMEWORK / "model_schema.json").read_text())["slots"]
    return tuple(s["id"] for s in slots)


class Confidence(str, Enum):
    explicit = "explicit"
    inferred = "inferred"
    empty = "empty"


class Impact(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class Level(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class Slot(BaseModel):
    completeness: int = Field(ge=0, le=100)
    confidence: Confidence
    impact: Impact
    value: str = ""
    evidence: str = ""


class Question(BaseModel):
    q: str
    slot: str
    why: str


class Summary(BaseModel):
    objective: str = ""
    scope: str = ""
    assumptions: list[str] = Field(default_factory=list)
    blind_spot: str = ""


class EngineOutput(BaseModel):
    # protected_namespaces=() lets us keep the field literally named `model`.
    model_config = ConfigDict(protected_namespaces=())
    model: dict[str, Slot]
    # The engine asks at most 6 (the prompt says 3–6; the stop signal is []). The cap is an invariant,
    # not a suggestion — a turn that floods 12 questions has stopped prioritising by information value.
    questions: list[Question] = Field(default_factory=list, max_length=6)
    summary: Summary
    # The reasoning layer — persisted so generators inherit it, not just the facts.
    # Filled at discovery finalization by absorbing advise()'s Brief. These types are
    # defined below (Brief section); forward-referenced here, resolved by
    # EngineOutput.model_rebuild() at the end of this module.
    decisions: list[DesignDecision] = Field(default_factory=list)
    challenges: list[Challenge] = Field(default_factory=list)
    opportunities: list[Opportunity] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_slot_vocabulary(self):
        # Every slot id the output names — in the model AND in the questions it targets — must be one
        # the schema defines. A hallucinated or typo'd key would otherwise sit unseen by every
        # schema-driven view, or point a question at a slot that doesn't exist. Completeness (the full
        # required set) is enforced at the discovery boundary, not here, so internal partial
        # projections (diff/propagate) stay constructable; the vocabulary check is safe everywhere.
        bad_model = unknown_slots(set(self.model))
        if bad_model:
            raise ValueError(f"unknown slots (not in schema): {bad_model}")
        allowed, _ = schema_slot_ids()
        bad_questions = sorted({q.slot for q in self.questions if q.slot not in allowed})
        if bad_questions:
            raise ValueError(f"questions target unknown slots (not in schema): {bad_questions}")
        # The reasoning layer carries DAG edges into the slots: a decision rests on `derived_from`, a
        # challenge contests `contests`. An edge to a slot the schema does not define would let the
        # dependency graph (propagate / impact) look rigorous while pointing at nothing — so the same
        # vocabulary rule applies to every reference, not just the model and the questions.
        bad_refs = sorted(
            {sid for d in self.decisions for sid in d.derived_from if sid not in allowed}
            | {sid for c in self.challenges for sid in c.contests if sid not in allowed}
        )
        if bad_refs:
            raise ValueError(f"reasoning references unknown slots (not in schema): {bad_refs}")
        return self


class Story(BaseModel):
    id: str
    title: str
    as_a: str = ""
    i_want: str = ""
    so_that: str = ""
    acceptance: list[str] = Field(default_factory=list)
    slots: list[str] = Field(default_factory=list)


class Stories(BaseModel):
    stories: list[Story]


class Complexity(str, Enum):
    S = "S"
    M = "M"
    L = "L"


class EstimateItem(BaseModel):
    story_id: str
    title: str
    complexity: Complexity
    days_low: float = Field(ge=0)
    days_high: float = Field(ge=0)
    drives: list[str] = Field(default_factory=list)
    note: str = ""


class EstimateDraft(BaseModel):
    # What the LLM produces. Totals, confidence and spread_drivers are computed in Python
    # (from real slot data) so they can't be hallucinated.
    items: list[EstimateItem]
    risks: list[str] = Field(default_factory=list)


class Leverage(str, Enum):
    high = "high"
    medium = "medium"
    future = "future"


class Opportunity(BaseModel):
    text: str
    leverage: Leverage
    modules: list[str] = Field(default_factory=list)  # concrete modules the leverage reaches (grounds it)


class Challenge(BaseModel):
    # Not "what did we learn" but "what should we contest" — the senior-PM pushback on the premise.
    headline: str          # 3–6 words naming the thing being challenged
    premise: str           # the assumption the request takes for granted
    alternative: str       # a concrete, domain-grounded alternative worth weighing
    consequence: str       # what the current premise risks or costs
    recommendation: str    # what to do about it before build
    contests: list[str] = Field(default_factory=list)  # slot ids whose premise this contests


class DesignDecision(BaseModel):
    # A settled decision. why/alternative/tradeoff are filled only where there was a real fork —
    # trivial sourcing facts stay a bare `decision` line.
    decision: str          # what was decided
    why: str = ""          # the rationale
    alternative: str = ""  # what was weighed instead
    tradeoff: str = ""     # the cost accepted for this choice
    derived_from: list[str] = Field(default_factory=list)  # slot ids the decision rests on (the DAG edge)


class Brief(BaseModel):
    # The advisory layer: what a senior consultant would add on top of the discovery.
    problem: str = ""                                   # one-line problem statement (exec summary)
    solution: str = ""                                  # one-line solution statement (exec summary)
    introduces: list[str] = Field(default_factory=list)
    challenges: list[Challenge] = Field(default_factory=list)  # premises worth contesting before build
    complexity: Level
    complexity_reasons: list[str] = Field(default_factory=list)  # the "because …" behind the verdict
    cost_driver: str = ""
    risks: list[str] = Field(default_factory=list)
    opportunities: list[Opportunity] = Field(default_factory=list)  # ranked by leverage
    next_steps: list[str] = Field(default_factory=list)
    decisions: list[DesignDecision] = Field(default_factory=list)  # settled decisions, with tradeoffs
    open_decisions: list[str] = Field(default_factory=list)  # decisions still to make


class Priority(str, Enum):
    must = "must"
    should = "should"
    could = "could"


class Requirement(BaseModel):
    id: str
    requirement: str
    priority: Priority


class PRD(BaseModel):
    title: str
    summary: str = ""
    problem: str = ""
    goals: list[str] = Field(default_factory=list)
    users: list[str] = Field(default_factory=list)
    in_scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    workflow: list[str] = Field(default_factory=list)
    business_rules: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    integrations: list[str] = Field(default_factory=list)
    edge_cases: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class ScenarioKind(str, Enum):
    happy_path = "happy_path"
    edge_case = "edge_case"
    error = "error"
    permission = "permission"


class Scenario(BaseModel):
    id: str
    title: str
    kind: ScenarioKind = ScenarioKind.happy_path
    given: list[str] = Field(default_factory=list)
    when: str = ""
    then: list[str] = Field(default_factory=list)


class Feature(BaseModel):
    name: str
    scenarios: list[Scenario] = Field(default_factory=list)


class AcceptanceCriteria(BaseModel):
    title: str
    features: list[Feature] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class EpicIssue(BaseModel):
    id: str
    title: str
    description: str = ""
    labels: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class Epic(BaseModel):
    title: str
    goal: str = ""
    business_value: str = ""
    in_scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    milestone: str = ""
    issues: list[EpicIssue] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class ReleaseNotes(BaseModel):
    title: str
    version: str = ""
    summary: str = ""
    highlights: list[str] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# EngineOutput's reasoning fields forward-reference the types defined above; resolve them.
EngineOutput.model_rebuild()
