from __future__ import annotations

import textwrap

from requivo.core.analysis import readiness_blockers, slot_label, state_of
from requivo.core.contracts import Brief, Confidence, EngineOutput, EstimateDraft, Impact, Leverage, Stories
from requivo.core.selectors import display_text
from requivo.usage import UsageLedger

STATE_ROWS = [
    ("confirmed", "✅ Confirmed"),
    ("inferred", "🟡 Inferred"),
    ("unknown", "⚪ Unknown"),
]

# The static note under the draft banner, pulled out as a constant rather than an inline literal so
# `test_the_browsable_examples_deterministic_half_matches_the_renderer` (tests/test_cli.py) can
# import and compare it instead of duplicating the string by hand -- a hand-copied literal drifts
# silently the same way the banner prefix and the readiness block already had (#172).
DRAFT_NOTE = "(blocking decisions remain — see Unknowns below)"


# Everything below renders **LLM-authored prose**, and a client request is untrusted business data
# by SECURITY.md's own framing — so a steered reply can carry an embedded newline into a question or
# a challenge and write the line after it, at column 0, in Requivo's own voice. `display_text` is the
# neutralizer, the prose sibling of the `display_token` every diagnostic verb already calls (#40).
#
# It is applied in two places and both are deliberate. **These three helpers escape their `text`
# argument**, because they are what most untrusted prose passes through and a call site cannot
# forget them. **The bare f-strings below call it explicitly**, because there is no chokepoint that
# covers them: `streams.py` cannot help (ESC encodes fine in UTF-8, so `backslashreplace` never
# fires), and a module-wide `print` shim would escape this module's *own* newlines — the layout — as
# readily as an injected one.
#
# So the call sites are a discipline, and a discipline needs a guard rather than a promise:
# `test_every_llm_authored_string_the_terminal_renders_is_neutralized` forges every field at once
# and runs every renderer, so a field added later and printed raw goes red under its renderer's name.
#
# The label, marker and indent arguments are this module's own literals and are left alone.


def _wrap(text: str, indent: str = "  ", width: int = 80) -> str:
    return textwrap.fill(display_text(text), width=width, initial_indent=indent,
                         subsequent_indent=indent)


def _bullet(text: str, marker: str = "•", indent: str = "  ", width: int = 80) -> str:
    return textwrap.fill(
        display_text(text), width=width, initial_indent=f"{indent}{marker} ",
        subsequent_indent=f"{indent}  "
    )


def _labeled(label: str, text: str, lw: int = 9, width: int = 80, indent: str = "  ") -> str:
    prefix = f"{indent}{label:<{lw}} "
    return textwrap.fill(display_text(text), width=width, initial_indent=prefix,
                         subsequent_indent=" " * len(prefix))


def render_understanding(out: EngineOutput) -> None:
    print("UNDERSTANDING")
    for state, label in STATE_ROWS:
        names = [slot_label(sid) for sid, s in out.model.items() if state_of(s) == state]
        if names:
            print(textwrap.fill(" · ".join(names), width=80, initial_indent=f"  {label}   ", subsequent_indent=" " * 15))


def render_readiness(out: EngineOutput) -> None:
    # One question, in the vocabulary every other surface reads ("are we ready?"), answered with the
    # one boolean the Core publishes. The length of the blocker list is not a readiness signal — the
    # list itself is the answer, printed below — so branching on it invents a state the model
    # contract, the Web and the plugin all forbid (#165). Pinned across surfaces by
    # `test_readiness_renders_as_one_boolean_on_every_surface`.
    print("ARE WE READY?")
    blockers = [slot_label(b) for b in readiness_blockers(out)]
    status = "Not ready" if blockers else "Ready"
    print(f"  {'Status':<20} {status}")
    if blockers:
        print(_labeled("Blocking decision", "Confirm " + ", ".join(b.lower() for b in blockers), lw=20))
    gaps = [
        slot_label(sid)
        for sid, s in out.model.items()
        if s.impact is not Impact.high and s.confidence is not Confidence.explicit
    ]
    if gaps:
        print(_labeled("Remaining gaps", ", ".join(gaps), lw=20))


def render_turn(out: EngineOutput) -> None:
    """Lightweight per-turn view: what's understood + what's being asked."""
    print()
    render_understanding(out)
    blockers = [slot_label(b) for b in readiness_blockers(out)]
    # Same rule as `render_readiness`, and the count is gone from the verdict for the same reason: it
    # is what the deleted "nearly" arm branched on, and the blockers are named on the line already.
    verdict = "⛔ Not ready" if blockers else "✅ Ready"
    print(f"\n  Ready?  {verdict}" + (f"  → {', '.join(blockers)}" if blockers else ""))
    if out.questions:
        print("\nPRIORITY QUESTIONS")
        for i, q in enumerate(out.questions, 1):
            print(f"  {i}. {display_text(q.q)}")
            print(f"     → {slot_label(q.slot)}")   # a schema-validated slot id, not free text


def next_command(payload: dict) -> str | None:
    """The single next step for a status view, or None when there is not one (#246).

    `status` is the verb a user runs on coming back to a session, and it stopped at the question
    list. Every other surface here points at the next step once and says so — `discover` closes with
    `requivo answer`, `answer` closes with either `requivo brief` or "keep going", and the plugin's
    status skill states the rule outright. This is the CLI's implementation of it.

    **The order is a judgment, not the order the states were listed in.** Open questions outrank a
    stale artifact, because regenerating a brief against a model that is about to move is a paid call
    thrown away; a stale artifact outranks the missing-brief case, because something on disk is
    already wrong. Pinned by `test_open_questions_point_at_answer`.

    Returns `None` rather than always a string, and that third state is the whole discipline. A
    converged session with a fresh brief has no single next step — `prd`? `epic`? `criteria`? — and
    printing all three is the menu `status` already was. It is also what a bare `model.json` gets,
    since it has no session to name and a pointer at a slug that does not exist is worse than none.

    A projection over the payload, not a second computation of it: readiness, questions and artifact
    staleness are all already decided by the time this runs. Pure, and returns the command without
    its arrow, so the caller owns the presentation.
    """
    slug = payload.get("slug")
    artifacts = payload.get("artifacts")
    if not slug or artifacts is None:
        return None                      # a bare model.json — no session behind it to point at
    if payload.get("questions"):
        return f'requivo answer {slug} "<your answers>"'
    # `stale` is the explicit flag and never a revision comparison — invariant 1. Schema order is
    # whatever the metadata carries; the first stale artifact is named and `impact` covers the rest,
    # which is what keeps this one line instead of a list.
    for artifact_type, status in artifacts.items():
        if status.get("stale"):
            return (f"requivo {artifact_type} {slug}   (regenerates {status['filename']}; "
                    f"requivo impact {slug} shows what else moved)")
    if "brief" not in artifacts:
        return f"requivo brief {slug}"
    return None


def render_next_command(payload: dict) -> None:
    """Print `next_command`'s answer, once, or nothing. The arrow matches `_cmd_discover`'s and
    `_cmd_answer`'s closing lines, so the three read as one convention rather than three."""
    line = next_command(payload)
    if line:
        print(f"\n→ {line}")


def render_usage(ledger: UsageLedger) -> None:
    """One-glance API footprint of the run: calls, tokens (cached vs full-price), latency, and a
    labelled cost *estimate*. Prints nothing when no API call was made (offline verbs)."""
    processed = ledger.input_tokens + ledger.cache_read_tokens + ledger.cache_write_tokens
    if not ledger.calls or processed + ledger.output_tokens == 0:
        return  # no call, or usage absent (e.g. an offline test fake) — nothing worth printing
    print("\nAPI USAGE  (this run)")
    print(f"  {'Calls':<11} {len(ledger.calls)}")
    cached = f"  ({ledger.cache_read_tokens:,} served from cache)" if ledger.cache_read_tokens else ""
    print(f"  {'Input':<11} {processed:,} tokens{cached}")
    print(f"  {'Output':<11} {ledger.output_tokens:,} tokens")
    print(f"  {'Latency':<11} {ledger.latency_ms / 1000:.1f} s")
    cost = ledger.cost_usd()
    model = " · ".join(ledger.models)
    if cost is None:
        print(f"  {'Est. cost':<11} n/a — no price on file for {model} (tokens above are exact)")
        return
    # The rate date comes off the ledger, not off a vendor constant this module imports (#167): the
    # renderer is told what the calls were priced at, it does not look the prices up. Third state on
    # purpose — a priced call whose rate table has no date prints without the "rates as of" clause
    # rather than borrowing a date from somewhere, because an undated estimate that reads as a dated
    # one is the more expensive of the two mistakes.
    as_of = " · ".join(ledger.priced_as_of)
    stamp = f", rates as of {as_of}" if as_of else ""
    print(f"  {'Est. cost':<11} ~${cost:.3f}   ({model} — estimate{stamp})")


def render_session_cost(revisions: list) -> None:
    """The cumulative cost of every provider-backed apply a session has made so far, from the
    token/rate provenance `RevisionRecord` carries per revision (#292) -- `render_usage`'s three-state
    shape (exact tokens, a labelled estimate, or "no price on file"), applied across a session's whole
    history rather than one run.

    `revisions` is `SessionMeta.revisions` — passed as a plain list rather than importing the type,
    so this stays a projection over data the caller already holds, the same shape every other
    renderer in this module takes.

    Silent when no revision carries usage: an old session, one applied entirely through Claude Code
    (which spends no API tokens), or a workspace that never opened a `track_usage()` scope around the
    calls that produced it. Never `$0.00` -- invariant 6's rule about provenance, applied across a
    session instead of one call.

    **Partial by construction, and the printed line says so (#292, found in review).** Only a
    provider-backed *model apply* creates a `RevisionRecord` at all -- `prd`/`criteria`/`epic`/
    `release` are real, billed `provider.generate()` calls that produce no revision (they save an
    artifact, not a model change) and so have no `RevisionRecord` to carry usage on. A session that
    ran those after discovering would otherwise see a "SESSION COST" figure quietly undercounting its
    real spend with no visible sign anything was left out; the parenthetical on the header line is
    what keeps the number honest about what it does and does not cover. Stamping those calls' spend
    too is a real, reachable gap -- it needs `ArtifactStatus` to grow the same fields `RevisionRecord`
    just did, which is its own change."""
    priced_revisions = [r for r in revisions if r.usage_input_tokens is not None]
    if not priced_revisions:
        return
    input_tokens = sum(r.usage_input_tokens or 0 for r in priced_revisions)
    output_tokens = sum(r.usage_output_tokens or 0 for r in priced_revisions)
    cache_read = sum(r.usage_cache_read_tokens or 0 for r in priced_revisions)
    cache_write = sum(r.usage_cache_write_tokens or 0 for r in priced_revisions)
    total = 0.0
    fully_priced = True
    as_of: list[str] = []
    for r in priced_revisions:
        if r.usage_rate_per_mtok is None:
            fully_priced = False
            continue
        in_rate, out_rate = r.usage_rate_per_mtok
        total += ((r.usage_input_tokens or 0) * in_rate
                  + (r.usage_cache_read_tokens or 0) * in_rate * 0.1
                  + (r.usage_cache_write_tokens or 0) * in_rate * 1.25
                  + (r.usage_output_tokens or 0) * out_rate) / 1_000_000
        if r.usage_priced_as_of and r.usage_priced_as_of not in as_of:
            as_of.append(r.usage_priced_as_of)
    processed = input_tokens + cache_read + cache_write
    plural = "s" if len(priced_revisions) != 1 else ""
    print(f"\nSESSION COST  (cumulative, {len(priced_revisions)} revision{plural} -- excludes prd/"
         "criteria/epic/release generation, which is not a revision)")
    cached = f"  ({cache_read:,} served from cache)" if cache_read else ""
    print(f"  {'Input':<11} {processed:,} tokens{cached}")
    print(f"  {'Output':<11} {output_tokens:,} tokens")
    if not fully_priced:
        print(f"  {'Est. cost':<11} n/a — some revisions have no price on file (tokens above are exact)")
        return
    stamp = f", rates as of {' · '.join(as_of)}" if as_of else ""
    print(f"  {'Est. cost':<11} ~${total:.3f}   (estimate{stamp})")


def render_brief(out: EngineOutput, brief: Brief) -> None:
    """The deliverable: a two-tier decision brief — an executive summary a PM reads in seconds, then
    the full analysis below (including what to *challenge*, not just what was learned). Written in a
    PM's language, never the engine's internals.

    "Decision brief" is a caption, not an identity: the artifact type is still `brief`, the verb is
    still `requivo brief`, and the file on disk is still `solution-assessment.md` (#166)."""
    # While a blocking decision is unresolved the brief rests on unconfirmed ground — label it a
    # draft so the reader knows it is not yet ready to build from, honestly rather than in the prose.
    draft = bool(readiness_blockers(out))
    print("\n" + "═" * 64)
    print("DRAFT DECISION BRIEF" if draft else "DECISION BRIEF")
    if draft:
        print(DRAFT_NOTE)
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
    unknowns = [slot_label(b) for b in readiness_blockers(out)] + brief.open_decisions
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
                        # A free `list[str]` the model fills — no schema behind it, unlike a slot id.
                        print(f"        ↳ reaches: {', '.join(display_text(m) for m in o.modules)}")

    if brief.next_steps:
        print("\nRECOMMENDED NEXT STEPS")
        for i, step in enumerate(brief.next_steps, 1):
            print(_bullet(step, marker=f"{i}.", indent="  "))

    print()
    render_readiness(out)


def render_stories(s: Stories) -> None:
    # Every field here is the model's own text except `slots`, which `Story` validates against the
    # schema — so it is the one that needs nothing (#213).
    print("\n=== USER STORIES ===")
    for st in s.stories:
        print(f"\n[{display_text(st.id)}] {display_text(st.title)}")
        if st.as_a or st.i_want or st.so_that:
            print(f"  As a {display_text(st.as_a)}, I want {display_text(st.i_want)}, "
                  f"so that {display_text(st.so_that)}.")
        for ac in st.acceptance:
            print(f"  ✓ {display_text(ac)}")
        if st.slots:
            print(f"  ↳ from: {', '.join(st.slots)}")


def render_estimate(draft: EstimateDraft, soft: list[str], confidence: str) -> None:
    total_low = sum(i.days_low for i in draft.items)
    total_high = sum(i.days_high for i in draft.items)
    print(f"\n=== ESTIMATE (from the model)   Confidence: {confidence.upper()} ===")
    print(f"{'Task':<44} {'Cplx':<5} {'Estimate':<11} Drives")
    for i in draft.items:
        est = f"{i.days_low:g}–{i.days_high:g} d"
        # Escaped *before* the 43-character cut, so the column stays 43 wide — escaping after would
        # let one control character push the row out of the table. Either order is equally safe;
        # only this one keeps the alignment. `drives` is a free list the model fills too (#213).
        title = display_text(i.title)[:43]
        print(f"{title:<44} {i.complexity.value:<5} {est:<11} "
              f"{', '.join(display_text(d) for d in i.drives)}")
    print(f"{'─' * 43:<44} {'':<5} {'─' * 9:<11}")
    print(f"{'TOTAL':<44} {'':<5} {total_low:g}–{total_high:g} d")
    if soft:
        print(f"\nSpread driven by unresolved slots: {', '.join(slot_label(s) for s in soft)}")
    if draft.risks:
        print("Risks / unknowns:")
        for r in draft.risks:
            print(f"  - {display_text(r)}")


def render_impact(report) -> None:
    """Focused propagation view: name slots, see what rests on them go stale."""
    from requivo.core.dependencies import ARTIFACT_FILES
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

    if report.challenges:
        print("\nPREMISES TO RE-EXAMINE")
        for c in report.challenges:
            print(_bullet(c.headline))
            print(f"    ↳ contests: {', '.join(c.rests_on)}")

    if report.artifacts:
        print("\nARTIFACTS THAT GO STALE")
        for name in report.artifacts:
            f = ARTIFACT_FILES.get(name)
            where = f" ({f})" if f else " (regenerate on demand)"
            print(f"  • {name}{where}")
        print("\n  → Regenerate these after confirming the change.")


def render_dependency_map(out: EngineOutput) -> None:
    """No-args overview: for every slot that can still move, what it would invalidate.

    The decision and challenge text is the model's own, so it goes through `display_text` (#213);
    the slot labels and artifact names either side of it are this repo's tables."""
    from requivo.core.dependencies import propagate
    print("\n" + "═" * 64)
    print("DEPENDENCY MAP — change a slot, see the blast radius")
    print("═" * 64)
    for sid in out.model:
        rep = propagate(out, [sid])
        if rep.empty:
            continue
        print(f"\n{slot_label(sid)}")
        if rep.decisions:
            print(f"  decisions: {'; '.join(display_text(d.decision) for d in rep.decisions)}")
        if rep.challenges:
            print(f"  challenges: {'; '.join(display_text(c.headline) for c in rep.challenges)}")
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
