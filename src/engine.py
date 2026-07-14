from __future__ import annotations

import json
import os
import re
import sys
from enum import Enum
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
MAX_TURNS = 8


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


def derive_stories(client: Anthropic, out: EngineOutput) -> Stories:
    """Second pipeline stage: a filled model → implementable user stories."""
    system = build_prompt("stories.md")
    user = "Completed requirements model to decompose into user stories:\n" + out.model_dump_json(indent=2)
    return _complete(client, system, [{"role": "user", "content": user}], Stories)


SOFT_COMPLETENESS = 70  # below this a slot is "soft" (tunable)


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
    """Third pipeline stage: stories + the model's soft slots → a day-based estimate.
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


def avg_completeness(out: EngineOutput) -> int:
    vals = [s.completeness for s in out.model.values()]
    return round(sum(vals) / len(vals)) if vals else 0


def render(out: EngineOutput) -> None:
    s = out.summary
    print("\n=== BUSINESS SUMMARY ===")
    print(f"Objective : {s.objective}")
    print(f"Scope     : {s.scope}")
    print(f"Blind spot: {s.blind_spot}")
    if s.assumptions:
        print("Assumptions made:")
        for a in s.assumptions:
            print(f"  - {a}")

    print("\n=== PRIORITY QUESTIONS (Uncertainty × Impact) ===")
    for i, q in enumerate(out.questions, 1):
        print(f"{i}. {q.q}")
        print(f"   → [{q.slot}] {q.why}")

    print(f"\n=== MODEL STATE (avg completeness: {avg_completeness(out)}%) ===")
    for slot_id, slot in out.model.items():
        print(
            f"  {slot_id:<18} {str(slot.completeness) + '%':<5} "
            f"{slot.confidence.value:<9} impact={slot.impact.value}"
        )


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
        print(f"\nSpread driven by unresolved slots: {', '.join(soft)}")
    if draft.risks:
        print("Risks / unknowns:")
        for r in draft.risks:
            print(f"  - {r}")


# ── Multi-turn loop ─────────────────────────────────────────────────────────


def converse(client: Anthropic, request: str) -> EngineOutput | None:
    """Fill the model, ask, feed answers back, until no high-value question remains.
    Returns the final model (or None if the user stopped before it converged)."""
    messages = [{"role": "user", "content": request}]
    for turn in range(1, MAX_TURNS + 1):
        print(f"\n──────────── TURN {turn} ────────────")
        out = run(client, messages)
        render(out)

        if not out.questions:
            print("\n✅ Model complete enough — no remaining high-information-value question.")
            return out

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

    print(f"\n⚠️  Reached the {MAX_TURNS}-turn limit.")
    return out


# ── Entry point ─────────────────────────────────────────────────────────────


def main() -> None:
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not argv:
        print('Usage: python src/engine.py [--once] [--stories] [--estimate] "the client request…"  |  path/to/request.md')
        sys.exit(1)

    arg = argv[0]
    request = Path(arg).read_text() if Path(arg).exists() else arg
    client = Anthropic()

    # Interactive loop unless --once or no TTY (piped/CI).
    if "--once" in flags or not sys.stdin.isatty():
        out = run(client, [{"role": "user", "content": request}])
        render(out)
    else:
        out = converse(client, request)

    # Pipeline stages. --estimate implies stories (it estimates them).
    want_estimate = "--estimate" in flags
    if out and ("--stories" in flags or want_estimate):
        stories = derive_stories(client, out)
        render_stories(stories)
        if want_estimate:
            draft, soft, confidence = estimate(client, out, stories)
            render_estimate(draft, soft, confidence)


if __name__ == "__main__":
    main()
