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
from golden_lib import (GOLDEN, REPO, load_runs, movements, runs_path,  # noqa: E402
                        stability)


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

    new = load_runs(path.read_text())
    old_text = _head_version(f"fixtures/golden/{slug}.runs.json")

    if old_text is None:
        # No baseline yet — report the noise floor so we know how trustworthy future diffs will be.
        st = stability(new)
        print(f"\n{slug}  ⊕ NEW (no baseline in HEAD)")
        print(f"  noise floor  {st['unanimous']['impact']}/{st['total_slots']} slots unanimous on "
              f"impact, {st['unanimous']['state']}/{st['total_slots']} on confidence, across "
              f"{st['n']} runs")
        print(f"  stable themes: {', '.join(st['themes']) or '—'}")
        return "moved"

    if path.read_text() == old_text:
        # Byte-identical to HEAD means the capture never landed — the engine is non-deterministic, so
        # a genuine re-run can't reproduce a file exactly. Reporting "no change" here would be a false
        # all-clear, which is the one failure mode a regression lens must not have.
        print(f"\n{slug}\n  ! capture identical to HEAD — not re-captured (re-run golden_run.py)")
        return "stale"

    old = load_runs(old_text)
    m = movements(old, new)

    print(f"\n{slug}")
    if not m["moved"] and not m["themes_added"] and not m["themes_removed"]:
        print("  · no change above the noise floor")
        return "flat"

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
    return "moved" if m["strong"] else "weak"


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
    for title, models in (("HEAD", load_runs(old_text)), ("working tree", load_runs(path.read_text()))):
        print(f"\n{slug} — {title}")
        for i, m in enumerate(models, 1):
            print(f"  run {i}")
            for q in m.questions:
                print(f"    [{q.slot}] {q.q}")


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
