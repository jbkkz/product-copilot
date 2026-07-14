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

PILLAR_LABELS = {
    "why": "Business understanding",
    "what": "Scope & rules",
    "how": "Configuration & access",
    "validate": "Validation & rollout",
}
PILLAR_ORDER = ["why", "what", "how", "validate"]


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


class Brief(BaseModel):
    # The advisory layer: what a senior consultant would add on top of the discovery.
    introduces: list[str] = Field(default_factory=list)
    complexity: Level
    cost_driver: str = ""
    risks: list[str] = Field(default_factory=list)
    opportunities: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


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


def _bar(pct: int, width: int = 10) -> str:
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _status_word(pct: int) -> str:
    if pct >= 85:
        return "Strong"
    if pct >= 65:
        return "Solid"
    if pct >= 40:
        return "Partial"
    return "Thin"


def _wrap(text: str, indent: str = "  ", width: int = 78) -> str:
    return textwrap.fill(text, width=width, initial_indent=indent, subsequent_indent=indent)


def _bullet(text: str, marker: str = "•", indent: str = "  ", width: int = 78) -> str:
    return textwrap.fill(
        text, width=width, initial_indent=f"{indent}{marker} ", subsequent_indent=f"{indent}  "
    )


def _is_deferred(s: Slot) -> bool:
    """Low-impact, unfilled slots are intentionally parked, not weaknesses."""
    return s.impact is Impact.low and s.completeness < SOFT_COMPLETENESS


def _readiness_blockers(out: EngineOutput) -> list[str]:
    """High-impact slots not yet explicitly confirmed — what stands between here and build."""
    return [sid for sid, s in out.model.items() if s.impact is Impact.high and s.confidence is not Confidence.explicit]


def _print_pillar_bars(out: EngineOutput) -> None:
    pillars, _ = _slot_meta()
    for pillar in PILLAR_ORDER:
        items = [
            (sid, s)
            for sid, s in out.model.items()
            if pillars.get(sid) == pillar and not _is_deferred(s)
        ]
        if not items:
            continue
        avg = round(sum(s.completeness for _, s in items) / len(items))
        gaps = [_label(sid) for sid, s in items if s.completeness < 60 or s.confidence is Confidence.empty]
        line = f"  {PILLAR_LABELS[pillar]:<24} {_bar(avg)}  {_status_word(avg)}"
        if gaps:
            line += f"  · unclear: {', '.join(gaps)}"
        print(line)


def _readiness(out: EngineOutput) -> tuple[str, list[str]]:
    """Verdict + the areas to confirm — in plain language, no internal metrics."""
    blockers = [_label(b) for b in _readiness_blockers(out)]
    if not blockers:
        return "✅ Yes — nothing high-impact is unresolved", []
    if len(blockers) <= 2:
        return f"⚠ Nearly — {len(blockers)} to confirm first", blockers
    return f"⛔ Not yet — {len(blockers)} areas still open", blockers


def render_status(out: EngineOutput) -> None:
    print("\nDISCOVERY STATUS")
    _print_pillar_bars(out)
    verdict, items = _readiness(out)
    print(f"\n  Ready for implementation?  {verdict}")
    if items:
        print(f"                             → {', '.join(items)}")


def render_turn(out: EngineOutput) -> None:
    """Lightweight per-turn view: progress + what's being asked."""
    render_status(out)
    if out.questions:
        print("\nPRIORITY QUESTIONS")
        for i, q in enumerate(out.questions, 1):
            print(f"  {i}. {q.q}")
            print(f"     → {_label(q.slot)}")


def render_brief(out: EngineOutput, brief: Brief) -> None:
    """The deliverable: a one-page discovery brief in the language of a PM, not the engine's
    internals (no slots, no completeness numbers)."""
    s = out.summary
    print("\n" + "═" * 60)
    print("DISCOVERY BRIEF")
    print("═" * 60)

    if s.objective:
        print("\nOBJECTIVE")
        print(_wrap(s.objective))

    print("\nWHAT'S UNDERSTOOD")
    _print_pillar_bars(out)
    if brief.introduces:
        print("\n  This involves:")
        for it in brief.introduces:
            print(_bullet(it, indent="    "))
    tail = f"   ·   main cost driver: {brief.cost_driver}" if brief.cost_driver else ""
    print(f"\n  Complexity: {brief.complexity.value.upper()}{tail}")

    blockers = _readiness_blockers(out)
    deferred = [sid for sid, sl in out.model.items() if _is_deferred(sl)]
    if blockers or s.assumptions or deferred:
        print("\nWHAT'S STILL UNCLEAR")
        for b in blockers:
            note = "still an inference, not stated by the client" if out.model[b].confidence is Confidence.inferred else "not yet answered"
            print(_bullet(f"{_label(b)} — {note}"))
        for a in s.assumptions:
            print(_bullet(f"Assumed: {a}"))
        if deferred:
            print(_bullet("Parked as low impact (revisit after launch): " + ", ".join(_label(d) for d in deferred)))

    if brief.risks:
        print("\nMAIN RISKS")
        for r in brief.risks:
            print(_bullet(r, marker="⚠"))

    if brief.next_steps or brief.opportunities:
        print("\nRECOMMENDED NEXT STEPS")
        for i, step in enumerate(brief.next_steps, 1):
            print(_bullet(step, marker=f"{i}."))
        for o in brief.opportunities:
            print(_bullet(f"Worth considering: {o}", marker="◆"))

    verdict, items = _readiness(out)
    explicit = sum(1 for sl in out.model.values() if sl.confidence is Confidence.explicit)
    print("\nREADY FOR IMPLEMENTATION?")
    print(f"  {verdict}")
    if items:
        print(f"  → confirm: {', '.join(items)}")
    print(f"  {explicit} of {len(out.model)} areas confirmed · {len(s.assumptions)} assumptions to validate")


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
    positional = [a for a in args if not a.startswith("--") and a != from_path]
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
        print('Usage: python src/engine.py [--once] [--stories] [--estimate] [--prd] "request" | file.md')
        print('       python src/engine.py --from out/<slug>/model.json [--stories] [--estimate] [--prd]')
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


if __name__ == "__main__":
    main()
