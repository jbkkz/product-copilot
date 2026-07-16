from __future__ import annotations

import textwrap

from product_copilot.core.analysis import _label, _readiness_blockers, _state_of
from product_copilot.core.contracts import (
    Brief, Confidence, EngineOutput, EstimateDraft, Impact, Leverage, Stories,
)


STATE_ROWS = [
    ("confirmed", "✅ Confirmed"),
    ("inferred", "🟡 Inferred"),
    ("unknown", "⚪ Unknown"),
]


def _wrap(text: str, indent: str = "  ", width: int = 80) -> str:
    return textwrap.fill(text, width=width, initial_indent=indent, subsequent_indent=indent)


def _bullet(text: str, marker: str = "•", indent: str = "  ", width: int = 80) -> str:
    return textwrap.fill(
        text, width=width, initial_indent=f"{indent}{marker} ", subsequent_indent=f"{indent}  "
    )


def _labeled(label: str, text: str, lw: int = 9, width: int = 80, indent: str = "  ") -> str:
    prefix = f"{indent}{label:<{lw}} "
    return textwrap.fill(text, width=width, initial_indent=prefix, subsequent_indent=" " * len(prefix))


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
        print("\nDESIGN DECISIONS")
        for d in brief.decisions:
            print(_bullet(d.decision, marker="✓", indent="  "))
            if d.why:
                print(_labeled("Why", d.why, lw=12, indent="      "))
            if d.alternative:
                print(_labeled("Alternative", d.alternative, lw=12, indent="      "))
            if d.tradeoff:
                print(_labeled("Tradeoff", d.tradeoff, lw=12, indent="      "))
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
                    if o.modules:
                        print(f"        ↳ reaches: {', '.join(o.modules)}")

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


def render_impact(report) -> None:
    """Focused propagation view: name slots, see what rests on them go stale."""
    from product_copilot.core.dependencies import ARTIFACT_FILES
    print("\n" + "═" * 64)
    print("IMPACT — what rests on: " + ", ".join(report.changed))
    print("═" * 64)
    if report.empty:
        print("\n  Nothing downstream depends on these — safe to revisit in isolation.")
        return

    if report.decisions:
        print("\nDECISIONS TO RE-VALIDATE")
        for d in report.decisions:
            print(_bullet(d.decision))
            print(f"    ↳ rests on: {', '.join(d.rests_on)}")

    if report.artifacts:
        print("\nARTIFACTS THAT GO STALE")
        for name in report.artifacts:
            f = ARTIFACT_FILES.get(name)
            where = f" ({f})" if f else " (regenerate on demand)"
            print(f"  • {name}{where}")
        print("\n  → Regenerate these after confirming the change.")


def render_dependency_map(out: EngineOutput) -> None:
    """No-args overview: for every slot that can still move, what it would invalidate."""
    from product_copilot.core.dependencies import propagate
    print("\n" + "═" * 64)
    print("DEPENDENCY MAP — change a slot, see the blast radius")
    print("═" * 64)
    for sid in out.model:
        rep = propagate(out, [sid])
        if rep.empty:
            continue
        print(f"\n{_label(sid)}")
        if rep.decisions:
            print(f"  decisions: {'; '.join(d.decision for d in rep.decisions)}")
        if rep.artifacts:
            print(f"  artifacts: {', '.join(rep.artifacts)}")


def render_stale(pairs, changed_labels) -> None:
    """After a discovery turn moved a slot, warn that already-generated artifacts are now stale."""
    if not pairs:
        return
    print("\n" + "─" * 64)
    print(f"⚠  STALE — you just changed: {', '.join(changed_labels)}")
    print("   These already-generated artifacts no longer match the model:")
    for _name, filename in pairs:
        print(f"     • {filename}")
    print("   → Regenerate them to pick up the change.")
