from __future__ import annotations

import functools
import json
import os
import re
import sys
import textwrap
from enum import Enum
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
MAX_TURNS = 8
SOFT_COMPLETENESS = 70  # below this a slot is "soft" (tunable)

STATE_ROWS = [
    ("confirmed", "✅ Confirmed"),
    ("inferred", "🟡 Inferred"),
    ("unknown", "⚪ Unknown"),
]


# ── Output contracts ────────────────────────────────────────────────────────
# Pydantic validates every model reply at the boundary. Rename a slot or a key
# and validation fails loudly in _complete() instead of silently mis-rendering.


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
    questions: list[Question] = Field(default_factory=list)
    summary: Summary


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


class Challenge(BaseModel):
    # Not "what did we learn" but "what should we contest" — the senior-PM pushback on the premise.
    headline: str          # 3–6 words naming the thing being challenged
    premise: str           # the assumption the request takes for granted
    alternative: str       # a concrete, domain-grounded alternative worth weighing
    consequence: str       # what the current premise risks or costs
    recommendation: str    # what to do about it before build


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
    decisions: list[str] = Field(default_factory=list)      # decisions already made (the decision log)
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


# ── Schema metadata (slot → pillar / label) ─────────────────────────────────


@functools.lru_cache(maxsize=1)
def _slot_meta() -> tuple[dict, dict]:
    slots = json.loads((ROOT / "framework" / "model_schema.json").read_text())["slots"]
    return ({s["id"]: s["pillar"] for s in slots}, {s["id"]: s["label"] for s in slots})


def _label(slot_id: str) -> str:
    return _slot_meta()[1].get(slot_id, slot_id)


# ── Prompt assembly ─────────────────────────────────────────────────────────


def load_context() -> str:
    cards = []
    for path in sorted((ROOT / "context").glob("*.md")):
        if path.name.startswith("_"):
            continue
        cards.append(f"## {path.stem}\n{path.read_text()}")
    return "\n\n".join(cards)


def build_prompt(name: str) -> str:
    """Load a prompt file and inject the schema + product context."""
    schema = (ROOT / "framework" / "model_schema.json").read_text()
    text = (ROOT / "prompts" / name).read_text()
    return text.replace("{{SCHEMA}}", schema).replace("{{CONTEXT}}", load_context())


# ── Model call (shared) ─────────────────────────────────────────────────────


def _first_text(resp) -> str:
    """First text block of the response — skips thinking/tool_use blocks."""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


def _extract_json(text: str) -> dict:
    """Best-effort JSON extraction: strip a ```json fence, else slice { … }."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("no JSON object found in the reply")
        text = text[start : end + 1]
    return json.loads(text)


def _complete(client: Anthropic, system: str, messages: list[dict], out_model, retries: int = 2):
    """One call → validated `out_model`. Retries with a nudge on malformed/non-conformant JSON.
    The nudge lives in a local copy so the caller's clean history is never polluted."""
    attempt = messages
    last_err = None
    for _ in range(retries + 1):
        resp = client.messages.create(
            model=os.getenv("MODEL", "claude-sonnet-5"),
            max_tokens=4000,
            system=system,
            messages=attempt,
        )
        raw = _first_text(resp)
        try:
            return out_model.model_validate(_extract_json(raw))
        except (json.JSONDecodeError, ValueError, ValidationError) as e:
            last_err = e
            attempt = attempt + [
                {"role": "assistant", "content": raw or "(empty)"},
                {"role": "user", "content": f"Your reply did not match the required schema ({e}). Reply with ONLY the JSON object, no prose, no code fence."},
            ]
    raise RuntimeError(f"No schema-valid JSON after {retries + 1} attempts: {last_err}")


def run(client: Anthropic, messages: list[dict], retries: int = 2) -> EngineOutput:
    """Engine turn: request/answers → filled model."""
    return _complete(client, build_prompt("engine.md"), messages, EngineOutput, retries)


# ── Model persistence (the model is the product; artifacts are views of it) ──


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:5]
    return "-".join(words) or "discovery"


def save_model(out: EngineOutput, slug: str) -> Path:
    """Persist the model — the durable product. Every artifact is regenerated from this file."""
    folder = ROOT / "out" / slug
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "model.json"
    path.write_text(out.model_dump_json(indent=2))
    return path


def load_model(path: Path) -> EngineOutput:
    """Load a saved model so artifacts can be regenerated without redoing discovery."""
    return EngineOutput.model_validate_json(path.read_text())


def write_artifact(slug: str, filename: str, content: str) -> Path:
    """Write a generated artifact next to its model in out/<slug>/."""
    folder = ROOT / "out" / slug
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    path.write_text(content)
    return path


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


def soft_slots(out: EngineOutput) -> list[str]:
    """Slots that still carry real uncertainty AND move the solution — the objective drivers of
    the estimate spread. Soft = medium/high impact and (low completeness or not yet explicit)."""
    soft = []
    for slot_id, s in out.model.items():
        if s.impact in (Impact.medium, Impact.high) and (
            s.completeness < SOFT_COMPLETENESS or s.confidence is not Confidence.explicit
        ):
            soft.append(slot_id)
    return soft


def estimate_confidence(n_soft: int) -> str:
    """Estimate confidence derived from how many high-impact slots are still soft."""
    if n_soft <= 1:
        return "high"
    if n_soft <= 3:
        return "medium"
    return "low"


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


# ── Rendering ───────────────────────────────────────────────────────────────


def _wrap(text: str, indent: str = "  ", width: int = 80) -> str:
    return textwrap.fill(text, width=width, initial_indent=indent, subsequent_indent=indent)


def _bullet(text: str, marker: str = "•", indent: str = "  ", width: int = 80) -> str:
    return textwrap.fill(
        text, width=width, initial_indent=f"{indent}{marker} ", subsequent_indent=f"{indent}  "
    )


def _labeled(label: str, text: str, lw: int = 9, width: int = 80, indent: str = "  ") -> str:
    prefix = f"{indent}{label:<{lw}} "
    return textwrap.fill(text, width=width, initial_indent=prefix, subsequent_indent=" " * len(prefix))


def _is_deferred(s: Slot) -> bool:
    """Low-impact, unfilled slots are intentionally parked, not weaknesses."""
    return s.impact is Impact.low and s.completeness < SOFT_COMPLETENESS


def _readiness_blockers(out: EngineOutput) -> list[str]:
    """High-impact slots not yet explicitly confirmed — what stands between here and build."""
    return [sid for sid, s in out.model.items() if s.impact is Impact.high and s.confidence is not Confidence.explicit]


def _state_of(s: Slot) -> str:
    if s.confidence is Confidence.explicit:
        return "confirmed"
    if s.confidence is Confidence.inferred:
        return "inferred"
    return "unknown"


def render_understanding(out: EngineOutput) -> None:
    print("UNDERSTANDING")
    for state, label in STATE_ROWS:
        names = [_label(sid) for sid, s in out.model.items() if _state_of(s) == state]
        if names:
            print(textwrap.fill(" · ".join(names), width=80, initial_indent=f"  {label}   ", subsequent_indent=" " * 15))


def render_readiness(out: EngineOutput) -> None:
    print("READY FOR IMPLEMENTATION?")
    blockers = [_label(b) for b in _readiness_blockers(out)]
    status = "Ready" if not blockers else ("Nearly ready" if len(blockers) <= 2 else "Not ready")
    print(f"  {'Status':<20} {status}")
    if blockers:
        print(_labeled("Blocking decision", "Confirm " + ", ".join(b.lower() for b in blockers), lw=20))
    gaps = [
        _label(sid)
        for sid, s in out.model.items()
        if s.impact is not Impact.high and s.confidence is not Confidence.explicit
    ]
    if gaps:
        print(_labeled("Remaining gaps", ", ".join(gaps), lw=20))


def render_turn(out: EngineOutput) -> None:
    """Lightweight per-turn view: what's understood + what's being asked."""
    print()
    render_understanding(out)
    blockers = [_label(b) for b in _readiness_blockers(out)]
    verdict = "✅ Ready" if not blockers else (f"⚠ Nearly — {len(blockers)} to confirm" if len(blockers) <= 2 else f"⛔ Not yet — {len(blockers)} open")
    print(f"\n  Ready?  {verdict}" + (f"  → {', '.join(blockers)}" if blockers else ""))
    if out.questions:
        print("\nPRIORITY QUESTIONS")
        for i, q in enumerate(out.questions, 1):
            print(f"  {i}. {q.q}")
            print(f"     → {_label(q.slot)}")


def render_brief(out: EngineOutput, brief: Brief) -> None:
    """The deliverable: a two-tier solution assessment — an executive summary a PM reads in seconds,
    then the full analysis below (including what to *challenge*, not just what was learned). Written
    in a PM's language, never the engine's internals."""
    print("\n" + "═" * 64)
    print("SOLUTION ASSESSMENT")
    print("═" * 64)

    # ── Executive summary (what a PM reads first) ──
    print("\nEXECUTIVE SUMMARY")
    if brief.problem:
        print(_labeled("Problem", brief.problem))
    print(_labeled("Solution", brief.solution or out.summary.objective))
    if brief.challenges:
        more = f"   (+{len(brief.challenges) - 1} more below)" if len(brief.challenges) > 1 else ""
        print(_labeled("Challenge", brief.challenges[0].headline + more))
    if brief.risks:
        more = f"   (+{len(brief.risks) - 1} more below)" if len(brief.risks) > 1 else ""
        print(_labeled("Risks", brief.risks[0] + more))
    unknowns = [_label(b) for b in _readiness_blockers(out)] + brief.open_decisions
    if unknowns:
        print(_labeled("Unknowns", " · ".join(unknowns)))
    if brief.next_steps:
        more = f"   (+{len(brief.next_steps) - 1} more below)" if len(brief.next_steps) > 1 else ""
        print(_labeled("Next", brief.next_steps[0] + more))

    print("\n  " + "─" * 22 + " full analysis " + "─" * 22 + "\n")

    # ── Full analysis ──
    render_understanding(out)

    if brief.decisions or brief.open_decisions:
        print("\nDECISION LOG")
        if brief.decisions:
            print("  Decided")
            for d in brief.decisions:
                print(_bullet(d, marker="✓", indent="    "))
        if brief.open_decisions:
            print("  Still to decide")
            for d in brief.open_decisions:
                print(_bullet(d, marker="•", indent="    "))

    if brief.challenges:
        print("\nCHALLENGES")
        for c in brief.challenges:
            print(_bullet(c.headline, marker="⚑", indent="  "))
            print(_labeled("Premise", c.premise, lw=12, indent="      "))
            print(_labeled("Alternative", c.alternative, lw=12, indent="      "))
            print(_labeled("Consequence", c.consequence, lw=12, indent="      "))
            print(_labeled("Recommend", c.recommendation, lw=12, indent="      "))

    print(f"\nCOMPLEXITY  {brief.complexity.value.upper()}")
    for r in brief.complexity_reasons:
        print(_bullet(r, marker="·", indent="    "))
    if brief.cost_driver:
        print(_labeled("Cost driver", brief.cost_driver, lw=13))

    if brief.risks:
        print("\nMAIN RISKS")
        for r in brief.risks:
            print(_bullet(r, marker="⚠"))

    if brief.opportunities:
        print("\nOPPORTUNITIES")
        for lev, label in [(Leverage.high, "High leverage"), (Leverage.medium, "Medium leverage"), (Leverage.future, "Future idea")]:
            group = [o for o in brief.opportunities if o.leverage is lev]
            if group:
                print(f"  {label}")
                for o in group:
                    print(_bullet(o.text, marker="◆", indent="    "))

    if brief.next_steps:
        print("\nRECOMMENDED NEXT STEPS")
        for i, step in enumerate(brief.next_steps, 1):
            print(_bullet(step, marker=f"{i}.", indent="  "))

    print()
    render_readiness(out)


def render_stories(s: Stories) -> None:
    print("\n=== USER STORIES ===")
    for st in s.stories:
        print(f"\n[{st.id}] {st.title}")
        if st.as_a or st.i_want or st.so_that:
            print(f"  As a {st.as_a}, I want {st.i_want}, so that {st.so_that}.")
        for ac in st.acceptance:
            print(f"  ✓ {ac}")
        if st.slots:
            print(f"  ↳ from: {', '.join(st.slots)}")


def render_estimate(draft: EstimateDraft, soft: list[str], confidence: str) -> None:
    total_low = sum(i.days_low for i in draft.items)
    total_high = sum(i.days_high for i in draft.items)
    print(f"\n=== ESTIMATE (from the model)   Confidence: {confidence.upper()} ===")
    print(f"{'Task':<44} {'Cplx':<5} {'Estimate':<11} Drives")
    for i in draft.items:
        est = f"{i.days_low:g}–{i.days_high:g} d"
        print(f"{i.title[:43]:<44} {i.complexity.value:<5} {est:<11} {', '.join(i.drives)}")
    print(f"{'─' * 43:<44} {'':<5} {'─' * 9:<11}")
    print(f"{'TOTAL':<44} {'':<5} {total_low:g}–{total_high:g} d")
    if soft:
        print(f"\nSpread driven by unresolved slots: {', '.join(_label(s) for s in soft)}")
    if draft.risks:
        print("Risks / unknowns:")
        for r in draft.risks:
            print(f"  - {r}")


def prd_markdown(prd: PRD) -> str:
    """Render a PRD as a clean, shareable Markdown document."""
    out: list[str] = [f"# {prd.title}", "", "> Product Requirements Document — generated by Product Copilot", ""]

    def section(heading: str, lines: list[str]) -> None:
        if lines:
            out.extend([f"## {heading}", "", *lines, ""])

    def bullets(items: list[str]) -> list[str]:
        return [f"- {i}" for i in items]

    if prd.summary:
        section("Summary", [prd.summary])
    if prd.problem:
        section("Problem", [prd.problem])
    section("Goals", bullets(prd.goals))
    section("Users & roles", bullets(prd.users))

    scope: list[str] = []
    if prd.in_scope:
        scope += ["**In scope**", "", *bullets(prd.in_scope), ""]
    if prd.out_of_scope:
        scope += ["**Out of scope**", "", *bullets(prd.out_of_scope)]
    section("Scope", scope)

    if prd.requirements:
        rows = ["| ID | Requirement | Priority |", "|----|-------------|----------|"]
        rows += [f"| {r.id} | {r.requirement} | {r.priority.value.capitalize()} |" for r in prd.requirements]
        section("Functional requirements", rows)

    section("Workflow", [f"{i}. {step}" for i, step in enumerate(prd.workflow, 1)])
    section("Business rules", bullets(prd.business_rules))
    section("Permissions", bullets(prd.permissions))
    section("Integrations & notifications", bullets(prd.integrations))
    section("Edge cases", bullets(prd.edge_cases))
    section("Acceptance criteria", [f"- [ ] {a}" for a in prd.acceptance_criteria])
    section("Assumptions", bullets(prd.assumptions))
    section("Open questions", bullets(prd.open_questions))
    section("Risks", bullets(prd.risks))

    return "\n".join(out).rstrip() + "\n"


_KIND_TAG = {
    ScenarioKind.happy_path: "Happy path",
    ScenarioKind.edge_case: "Edge case",
    ScenarioKind.error: "Error",
    ScenarioKind.permission: "Permission",
}


def criteria_markdown(ac: AcceptanceCriteria) -> str:
    """Render acceptance criteria as a Given/When/Then Markdown checklist for recette."""
    out: list[str] = [f"# {ac.title}", "", "> Acceptance criteria — generated by Product Copilot", ""]

    for feature in ac.features:
        out += [f"## {feature.name}", ""]
        for s in feature.scenarios:
            out.append(f"### [ ] {s.id} — {s.title}  _{_KIND_TAG.get(s.kind, s.kind.value)}_")
            out.append("")
            for i, g in enumerate(s.given):
                out.append(f"- **{'Given' if i == 0 else 'And'}** {g}")
            if s.when:
                out.append(f"- **When** {s.when}")
            for i, t in enumerate(s.then):
                out.append(f"- **{'Then' if i == 0 else 'And'}** {t}")
            out.append("")

    if ac.open_questions:
        out += ["## Open questions", "", *[f"- {q}" for q in ac.open_questions], ""]

    return "\n".join(out).rstrip() + "\n"


def epic_markdown(epic: Epic) -> str:
    """Render a delivery epic as Markdown: goal, scope, then a checklist of trackable issues."""
    out: list[str] = [f"# Epic: {epic.title}", "", "> Delivery epic — generated by Product Copilot", ""]
    if epic.milestone:
        out += [f"**Milestone:** {epic.milestone}", ""]
    if epic.goal:
        out += [f"**Goal:** {epic.goal}", ""]
    if epic.business_value:
        out += [f"**Business value:** {epic.business_value}", ""]

    if epic.in_scope:
        out += ["## In scope", "", *[f"- {i}" for i in epic.in_scope], ""]
    if epic.out_of_scope:
        out += ["## Out of scope", "", *[f"- {i}" for i in epic.out_of_scope], ""]

    if epic.issues:
        out += ["## Issues", ""]
        for issue in epic.issues:
            out.append(f"### [ ] {issue.id} — {issue.title}")
            out.append("")
            meta: list[str] = []
            if issue.labels:
                meta.append("**Labels:** " + ", ".join(f"`{l}`" for l in issue.labels))
            if issue.depends_on:
                meta.append("**Depends on:** " + ", ".join(issue.depends_on))
            if meta:
                out += [" · ".join(meta), ""]
            if issue.description:
                out += [issue.description, ""]

    if epic.open_questions:
        out += ["## Open questions", "", *[f"- {q}" for q in epic.open_questions], ""]

    return "\n".join(out).rstrip() + "\n"


EPIC_EXPORT_FORMAT = "product-copilot-epic"
EPIC_EXPORT_VERSION = 1


def epic_export(epic: Epic) -> dict:
    """A tool-neutral, importable view of an epic — maps cleanly onto GitHub or GitLab issues.

    A stable, versioned envelope an importer (or an n8n flow) can validate and feed to either
    tracker's API. The epic becomes a tracking issue / GitLab epic; each issue keeps its labels,
    the shared milestone, and `depends_on` as issue refs so relationships can be wired after create.
    """
    description = "\n\n".join(
        part
        for part in [
            epic.goal,
            f"**Business value:** {epic.business_value}" if epic.business_value else "",
            ("**In scope:**\n" + "\n".join(f"- {i}" for i in epic.in_scope)) if epic.in_scope else "",
            ("**Out of scope:**\n" + "\n".join(f"- {i}" for i in epic.out_of_scope)) if epic.out_of_scope else "",
        ]
        if part
    )
    return {
        "format": EPIC_EXPORT_FORMAT,
        "version": EPIC_EXPORT_VERSION,
        "epic": {
            "title": epic.title,
            "description": description,
            "labels": ["epic"],
            "milestone": epic.milestone,
        },
        "issues": [
            {
                "ref": issue.id,
                "title": issue.title,
                "description": issue.description,
                "labels": issue.labels,
                "milestone": epic.milestone,
                "depends_on": issue.depends_on,
            }
            for issue in epic.issues
        ],
        "open_questions": epic.open_questions,
    }


def epic_export_json(epic: Epic) -> str:
    return json.dumps(epic_export(epic), indent=2) + "\n"


def to_github(export: dict, slug: str) -> dict:
    """Adapter: neutral epic export → a GitHub issue-creation plan (pure, no network).

    An automation (e.g. an n8n flow) creates the child issues first, then the tracking issue.
    GitHub has no native epic or issue dependency, so we degrade honestly: the epic becomes a
    tracking issue with a task list, and `depends_on` is stated in each issue body. Every issue
    carries an idempotency label (`pc-epic:<slug>`) so a re-run can find-then-skip existing issues
    instead of duplicating. `milestone` is a name — the automation resolves it to GitHub's numeric id.
    """
    label = f"pc-epic:{slug}"
    title_by_ref = {i["ref"]: i["title"] for i in export["issues"]}
    epic_title = export["epic"]["title"]

    def child_body(issue: dict) -> str:
        parts = [issue["description"]] if issue["description"] else []
        deps = [title_by_ref.get(ref, ref) for ref in issue.get("depends_on", [])]
        if deps:
            parts.append("**Depends on:** " + ", ".join(deps))
        parts.append(f"_Part of epic: {epic_title}_")
        return "\n\n".join(parts)

    task_list = "\n".join(f"- [ ] {i['title']}" for i in export["issues"])
    tracking_body = (export["epic"]["description"] + "\n\n### Issues\n\n" + task_list).strip()

    return {
        "target": "github",
        "idempotency_label": label,
        "tracking_issue": {
            "title": f"Epic: {epic_title}",
            "body": tracking_body,
            "labels": export["epic"]["labels"] + [label],
            "milestone": export["epic"]["milestone"],
        },
        "issues": [
            {
                "ref": issue["ref"],
                "title": issue["title"],
                "body": child_body(issue),
                "labels": issue["labels"] + [label],
                "milestone": issue["milestone"],
            }
            for issue in export["issues"]
        ],
    }


def to_github_json(epic: Epic, slug: str) -> str:
    return json.dumps(to_github(epic_export(epic), slug), indent=2) + "\n"


def to_gitlab(export: dict, slug: str) -> dict:
    """Adapter: neutral epic export → a GitLab issue-creation plan (pure, no network).

    GitLab maps more faithfully than GitHub: `depends_on` becomes structured issue `links`
    (`blocks`) an automation wires after create — not body text. Native Epics are Premium-only, so
    for portability the epic is a tracking issue with a task list on any tier. Each issue carries the
    `pc-epic:<slug>` idempotency label; `milestone` is a name the automation resolves to its id.
    """
    label = f"pc-epic:{slug}"
    epic_title = export["epic"]["title"]

    def child_description(issue: dict) -> str:
        parts = [issue["description"]] if issue["description"] else []
        parts.append(f"_Part of epic: {epic_title}_")
        return "\n\n".join(parts)

    task_list = "\n".join(f"- [ ] {i['title']}" for i in export["issues"])
    tracking_description = (export["epic"]["description"] + "\n\n### Issues\n\n" + task_list).strip()

    # depends_on: the dependency blocks the dependent issue. Wire as GitLab issue links after create.
    links = [
        {"source_ref": ref, "target_ref": issue["ref"], "type": "blocks"}
        for issue in export["issues"]
        for ref in issue.get("depends_on", [])
    ]

    return {
        "target": "gitlab",
        "idempotency_label": label,
        "tracking_issue": {
            "title": f"Epic: {epic_title}",
            "description": tracking_description,
            "labels": export["epic"]["labels"] + [label],
            "milestone": export["epic"]["milestone"],
        },
        "issues": [
            {
                "ref": issue["ref"],
                "title": issue["title"],
                "description": child_description(issue),
                "labels": issue["labels"] + [label],
                "milestone": issue["milestone"],
            }
            for issue in export["issues"]
        ],
        "links": links,
    }


def to_gitlab_json(epic: Epic, slug: str) -> str:
    return json.dumps(to_gitlab(epic_export(epic), slug), indent=2) + "\n"


def release_markdown(rn: ReleaseNotes) -> str:
    """Render client-facing release notes as a clean, shareable Markdown announcement."""
    heading = f"# {rn.title}" + (f" — {rn.version}" if rn.version else "")
    out: list[str] = [heading, "", "> Release notes — generated by Product Copilot", ""]

    def section(heading: str, lines: list[str]) -> None:
        if lines:
            out.extend([f"## {heading}", "", *lines, ""])

    if rn.summary:
        out += [rn.summary, ""]
    section("What's new", [f"- {h}" for h in rn.highlights])
    section("Not included yet", [f"- {l}" for l in rn.known_limitations])
    section("Before you start", [f"- {n}" for n in rn.notes])

    return "\n".join(out).rstrip() + "\n"


# ── Multi-turn loop ─────────────────────────────────────────────────────────


def converse(client: Anthropic, request: str) -> EngineOutput | None:
    """Fill the model, ask, feed answers back, until no high-value question remains.
    Returns the final model (None if the user stopped early). Finalization (brief, save) is
    handled by the caller so the interactive and --from paths share it."""
    messages = [{"role": "user", "content": request}]
    out = None
    for turn in range(1, MAX_TURNS + 1):
        print(f"\n──────────── TURN {turn} ────────────")
        out = run(client, messages)
        render_turn(out)

        if not out.questions:
            break

        print("\nYour answers (Enter = skip a question · 'q' = stop):")
        answers = []
        try:
            for i, q in enumerate(out.questions, 1):
                ans = input(f"  {i}. {q.q}\n     > ").strip()
                if ans.lower() == "q":
                    print("Stopped.")
                    return None
                if ans:
                    answers.append(f"[slot: {q.slot}] Q: {q.q} → A: {ans}")
        except (EOFError, KeyboardInterrupt):
            print("\nStopped.")
            return None

        if not answers:
            print("No answer provided — stopping.")
            return None

        # The assistant's prior model IS the state we refine — carry it in the history.
        messages.append({"role": "assistant", "content": out.model_dump_json()})
        messages.append({"role": "user", "content": "Client answers:\n" + "\n".join(answers)})
    else:
        print(f"\n⚠️  Reached the {MAX_TURNS}-turn limit.")

    return out


# ── Entry point ─────────────────────────────────────────────────────────────


def _flag_value(args: list[str], name: str) -> str | None:
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def main() -> None:
    args = sys.argv[1:]
    flags = {a for a in args if a.startswith("--")}
    from_path = _flag_value(args, "--from")
    # --release optionally takes a version token (e.g. --release v1.0); ignore a following flag.
    release_version = _flag_value(args, "--release") or ""
    if release_version.startswith("--"):
        release_version = ""
    consumed = {from_path, release_version}
    positional = [a for a in args if not a.startswith("--") and a not in consumed]
    client = Anthropic()

    if from_path:
        # Regenerate artifacts from a saved model — no discovery.
        out = load_model(Path(from_path))
        slug = Path(from_path).parent.name
        print(f"Loaded model ← {from_path}")
        quick = False
    elif positional:
        arg = positional[0]
        request = Path(arg).read_text() if Path(arg).exists() else arg
        slug = _slug(Path(arg).stem if Path(arg).exists() else request)
        # Interactive loop, or a single quick pass with --once / no TTY.
        quick = "--once" in flags or not sys.stdin.isatty()
        if quick:
            out = run(client, [{"role": "user", "content": request}])
            render_turn(out)
        else:
            out = converse(client, request)
        if out:
            print(f"\nSaved model → {save_model(out, slug)}")
    else:
        print('Usage: python src/engine.py [--once] [--stories] [--estimate] [--prd] [--criteria] [--epic] [--epic-json] [--epic-github] [--epic-gitlab] [--release [version]] "request" | file.md')
        print('       python src/engine.py --from out/<slug>/model.json [--stories] [--estimate] [--prd] [--criteria] [--epic] [--epic-json] [--epic-github] [--epic-gitlab] [--release [version]]')
        sys.exit(1)

    if not out:
        return

    # The discovery brief is the default deliverable (skipped on a quick --once pass).
    if not quick:
        print("\nGenerating the discovery brief…")
        render_brief(out, advise(client, out))

    # Delivery pipeline. --estimate implies stories (it estimates them).
    if "--stories" in flags or "--estimate" in flags:
        stories = derive_stories(client, out)
        render_stories(stories)
        if "--estimate" in flags:
            draft, soft, confidence = estimate(client, out, stories)
            render_estimate(draft, soft, confidence)

    # Artifact generators: model → file.
    if "--prd" in flags:
        print("\nGenerating the PRD…")
        markdown = prd_markdown(generate_prd(client, out))
        path = write_artifact(slug, "prd.md", markdown)
        print(markdown)
        print(f"\nWrote PRD → {path}")

    if "--criteria" in flags:
        print("\nGenerating the acceptance criteria…")
        markdown = criteria_markdown(generate_criteria(client, out))
        path = write_artifact(slug, "acceptance-criteria.md", markdown)
        print(markdown)
        print(f"\nWrote acceptance criteria → {path}")

    if flags & {"--epic", "--epic-json", "--epic-github", "--epic-gitlab"}:
        print("\nGenerating the delivery epic…")
        epic = generate_epic(client, out)  # one model call; every view renders from it
        if "--epic" in flags:
            markdown = epic_markdown(epic)
            path = write_artifact(slug, "epic.md", markdown)
            print(markdown)
            print(f"\nWrote epic → {path}")
        if "--epic-json" in flags:
            path = write_artifact(slug, "epic.json", epic_export_json(epic))
            print(f"Wrote neutral epic export (GitHub/GitLab-importable) → {path}")
        if "--epic-github" in flags:
            path = write_artifact(slug, "epic.github.json", to_github_json(epic, slug))
            print(f"Wrote GitHub issue-creation plan → {path}")
        if "--epic-gitlab" in flags:
            path = write_artifact(slug, "epic.gitlab.json", to_gitlab_json(epic, slug))
            print(f"Wrote GitLab issue-creation plan → {path}")

    if "--release" in flags:
        print("\nGenerating the release notes…")
        markdown = release_markdown(generate_release(client, out, release_version))
        path = write_artifact(slug, "release-notes.md", markdown)
        print(markdown)
        print(f"\nWrote release notes → {path}")


if __name__ == "__main__":
    main()
