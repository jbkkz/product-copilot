#!/usr/bin/env python
"""The regression lens — changes that clear the noise floor, not run-to-run jitter.

Compares each request's working-tree K-run baseline against its committed baseline in git ``HEAD``,
and reports a slot's impact/confidence as *moved* only when the old baseline was unanimous on that
dimension (a reliable reference) and the new consensus clearly shifted. A dimension that flickers
across the K runs is noise and stays silent — that is the whole point of capturing K runs instead of
one. Moves are split into **strong** (the new runs are unanimous too, so no single run's jitter can
explain it) and **weak** (a bare majority — at K=3 that is one run flipping). Act on strong; watch
weak in aggregate. See ``golden_lib`` for the consensus and floor logic.

With no committed baseline yet (a fresh capture), it instead prints the **noise floor** itself: how
much of each request's model is stable enough to diff on. A request with few unanimous slots will only
ever surface large changes; that's information, not a failure.

An **interactive** request (one with an answer sheet) gets a second readout on top: what the capture's
deep turns did — questions re-asked after the client answered them, confirmations the model stopped
carrying, completeness that fell back. That lens has its own third state and says *not measured*
rather than printing an empty finding set, because a single-pass baseline is silent about turn 3 in a
way that reads exactly like a clean one (#137).

Workflow: golden_run.py (re-capture) → golden_diff.py (read the signal) → commit if intended.

Usage:
    python scripts/golden_diff.py              # every request
    python scripts/golden_diff.py <slug>...    # only the named one(s)
    python scripts/golden_diff.py <slug> --questions   # the questions themselves, old vs new
"""

from __future__ import annotations

import subprocess
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from golden_lib import (  # noqa: E402
    GOLDEN,
    MEASURABLE_DEPTH,
    REPO,
    brief_movements,
    load_briefs,
    load_runs,
    load_turns,
    movements,
    runs_path,
    stability,
    turn_lens,
    turn_movements,
)


def _head_version(rel_path: str) -> str | None:
    res = subprocess.run(["git", "show", f"HEAD:{rel_path}"],
                         cwd=REPO, capture_output=True, text=True)
    return res.stdout if res.returncode == 0 else None


def diff_one(slug: str) -> str:
    """Print the signal for one request. Returns its status: ``moved``, ``flat``, or ``stale``
    (no capture on disk, or a capture that is byte-identical to HEAD and so never landed)."""
    path = runs_path(slug)
    if not path.exists():
        print(f"\n{slug}\n  ! no working-tree capture (run golden_run.py first)")
        return "stale"

    new = load_runs(path.read_text(encoding="utf-8"))
    old_text = _head_version(f"fixtures/golden/{slug}.runs.json")

    new_text = path.read_text(encoding="utf-8")

    if old_text is None:
        # No baseline yet — report the noise floor so we know how trustworthy future diffs will be.
        st = stability(new)
        print(f"\n{slug}  ⊕ NEW (no baseline in HEAD)")
        print(f"  noise floor  {st['unanimous']['impact']}/{st['total_slots']} slots unanimous on "
              f"impact, {st['unanimous']['state']}/{st['total_slots']} on confidence, across "
              f"{st['n']} runs")
        print(f"  stable themes: {', '.join(st['themes']) or '—'}")
        # On a first interactive capture this readout *is* the finding — there is nothing to diff
        # against, and what the deep turns did is the whole reason the request exists (#137).
        _show_turns(None, load_turns(new_text))
        return "moved"

    if new_text == old_text:
        # Byte-identical to HEAD means the capture never landed — the engine is non-deterministic, so
        # a genuine re-run can't reproduce a file exactly. Reporting "no change" here would be a false
        # all-clear, which is the one failure mode a regression lens must not have.
        print(f"\n{slug}\n  ! capture identical to HEAD — not re-captured (re-run golden_run.py)")
        return "stale"

    old = load_runs(old_text)
    m = movements(old, new)

    print(f"\n{slug}")
    # Before the flat short-circuit below, and deliberately. The turn lens watches something the slot
    # consensus cannot see, so a capture whose slots held still can still have started re-asking
    # questions the client already answered — reporting "no change above the noise floor" over the top
    # of that would be the false all-clear this file exists to avoid. (The assessment lens *is* behind
    # that short-circuit and has the same exposure; that is a separate defect, reported not fixed.)
    turn_signal = _show_turns(load_turns(old_text), load_turns(new_text))

    if not m["moved"] and not m["themes_added"] and not m["themes_removed"]:
        print("  · no change above the noise floor")
        return "moved" if turn_signal else "flat"

    def _show(entries: list[dict], tier: str) -> None:
        print(f"  {tier:<10} {len(entries)} slot(s):")
        for mv in entries:
            print(f"               {mv['slot']:<22} {mv['dim']} {mv['from']}→{mv['to']}"
                  f"   (was {mv['old_agree']}/{mv['n']}, now {mv['new_agree']}/{mv['n']})")

    if m["strong"]:
        _show(m["strong"], "strong")
    if m["weak"]:
        _show(m["weak"], "weak")
    if m["themes_added"]:
        print(f"  questions  + stable theme(s): {', '.join(m['themes_added'])}")
    if m["themes_removed"]:
        print(f"  questions  − stable theme(s): {', '.join(m['themes_removed'])}")

    strong = bool(m["strong"]) or turn_signal
    old_briefs, new_briefs = load_briefs(old_text), load_briefs(new_text)
    if old_briefs and new_briefs:
        strong = _show_assessment(old_briefs, new_briefs) or strong
    return "moved" if strong else "weak"


def _show_turns(old_turns, new_turns) -> bool:
    """Print what the interactive capture says about turn 3 and beyond. Returns True on a *strong*
    signal there — a finding every run agrees on.

    Four states, and the last two are the ones this function exists for:

    - **not applicable** — neither side is interactive. A single-pass request is not silent about the
      deep turns, it has none, so this prints nothing at all rather than a reassuring dash.
    - **compared** — both sides are interactive; the unanimous sets are diffed.
    - **first capture** — the working tree has turns and HEAD does not. The lens is printed with no
      comparison, because on a first capture the readout *is* the finding.
    - **lens lost** — HEAD had turns and the working tree does not. That is a request that stopped
      being interactive, and it has to be loud: the deep-turn lens went away, which reads exactly like
      it went quiet.
    """
    if new_turns is None and old_turns is None:
        return False
    if new_turns is None:
        print("  interactive  ! the baseline has turns and this capture does not — the deep-turn "
              "lens is gone, which is not the same as clean")
        return True

    lens = turn_lens(new_turns)
    depth = "/".join(str(d) for d in lens["depths"])
    print(f"  interactive  turns {depth} across {lens['n']} run(s)"
          + ("" if lens["deep_enough"]
             else f"  ⚠ under {MEASURABLE_DEPTH} — this capture did not reach the deep turns"))
    for key, caption in (("reasked", "re-asked after the client answered"),
                         ("lost", "answered early, not confirmed at the end"),
                         ("regressed", "completeness fell back")):
        detail = ", ".join(f"{lab} ({c}/{lens['n']})" for lab, c in sorted(lens[key].items()))
        print(f"               {caption:<38} {detail or '—'}")

    move = turn_movements(old_turns, new_turns)
    if not move["measured"]:
        print(f"               no comparison: {move['reason']}")
        return False
    strong = False
    for key, caption in (("reasked", "re-asks"), ("lost", "lost confirmations"),
                         ("regressed", "completeness regressions")):
        if move[f"{key}_added"]:
            strong = True
            print(f"               + {caption} in every run: {', '.join(move[f'{key}_added'])}")
        if move[f"{key}_removed"]:
            print(f"               − {caption} no longer in every run: "
                  f"{', '.join(move[f'{key}_removed'])}")
    return strong


def _show_assessment(old_briefs: list, new_briefs: list) -> bool:
    """Print what moved in the assessment. Returns True if a *strong* signal was found there.

    A lost challenge theme counts as strong on its own: the engine used to contest that premise in a
    majority of runs and stopped. On the deliverable, losing a challenge is the regression that
    matters most — sharper questions are worth little if the pushback quietly disappears."""
    b = brief_movements(old_briefs, new_briefs)
    strong = False
    if b["complexity"]:
        c = b["complexity"]
        tier = "strong" if c["strong"] else "weak"
        strong = strong or c["strong"]
        print(f"  assessment {tier} complexity {c['from']}→{c['to']}"
              f"   (was {c['old_agree']}/{c['n']}, now {c['new_agree']}/{c['n']})")
    if b["themes_removed"]:
        strong = True
        print(f"  assessment − challenge(s) no longer raised: {'; '.join(b['themes_removed'])}")
    if b["themes_added"]:
        print(f"  assessment + challenge(s) now raised: {'; '.join(b['themes_added'])}")
    if not (b["complexity"] or b["themes_added"] or b["themes_removed"]):
        print("  assessment · verdict and challenges unchanged")
    return strong


def questions_one(slug: str) -> None:
    """Print the questions each baseline actually asked, run by run, old then new.

    The slot tiers above are a *projection* of the model; the questions are what the user meets. In
    practice a card or prompt change reads far more clearly here than in a per-slot impact shift, so
    this is the view to open when a diff says something moved and you want to know whether it moved
    in a good direction."""
    path = runs_path(slug)
    old_text = _head_version(f"fixtures/golden/{slug}.runs.json")
    if not path.exists() or old_text is None:
        print(f"\n{slug}\n  ! need both a working-tree capture and a HEAD baseline")
        return
    new_text = path.read_text(encoding="utf-8")
    for title, text in (("HEAD", old_text), ("working tree", new_text)):
        print(f"\n{slug} — {title}")
        turns = load_turns(text)
        if turns is not None:
            # An interactive capture: the question that settles this issue is *when* something was
            # asked, not whether it was, so the turn number leads and an already-answered slot is
            # marked. Reading the final model's questions alone would show only what the last turn
            # happened to still be asking.
            for i, run in enumerate(turns, 1):
                print(f"  run {i}")
                covered: set[str] = set()
                for turn in run:
                    print(f"    turn {turn.index}")
                    for q in turn.model.questions:
                        again = "  ← already answered" if q.slot in covered else ""
                        print(f"      [{q.slot}] {q.q}{again}")
                    if turn.answered:
                        print(f"      answered: {', '.join(turn.answered)}")
                    covered.update(turn.answered)
            continue
        for i, m in enumerate(load_runs(text), 1):
            print(f"  run {i}")
            for q in m.questions:
                print(f"    [{q.slot}] {q.q}")
        for i, b in enumerate(load_briefs(text), 1):
            print(f"  run {i} — challenges")
            for c in b.challenges:
                # headline+premise names the contest; alternative+recommendation are what separate a
                # real architect's pushback from a bare observation — show them so a prompt edit can be
                # judged on the half of the challenge that actually carries the domain grounding.
                print(f"    ‹{c.headline}› {c.premise}")
                print(f"        alt: {c.alternative}")
                print(f"        rec: {c.recommendation}")


def main(argv: list[str]) -> int:
    show_questions = "--questions" in argv
    argv = [a for a in argv if a != "--questions"]
    slugs = argv or sorted(p.name[: -len(".runs.json")]
                           for p in GOLDEN.glob("*.runs.json"))
    if not slugs:
        print("No golden baselines found. Run golden_run.py first.", file=sys.stderr)
        return 1

    if show_questions:
        for slug in slugs:
            questions_one(slug)
        return 0

    print("Golden diff — working tree vs HEAD (strong = every run agrees, before and after)")
    results = [diff_one(slug) for slug in slugs]
    moved, weak, stale = (results.count(k) for k in ("moved", "weak", "stale"))
    line = f"{moved}/{len(slugs)} request(s) moved on strong signal."
    if weak:
        line += f"  {weak} moved on weak signal only (watch, don't act)."
    if stale:
        line += f"  ⚠ {stale} not re-captured — that is not a clean bill of health."
    print(f"\n{'─' * 60}\n{line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
