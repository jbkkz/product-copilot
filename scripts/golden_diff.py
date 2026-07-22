#!/usr/bin/env python
"""The regression lens — changes that clear the noise floor, not run-to-run jitter.

Compares each request's working-tree K-run baseline against its committed baseline in git ``HEAD``,
and reports a slot's impact/confidence as *moved* only when the old baseline was unanimous on that
dimension (a reliable reference) and the new consensus clearly shifted. A dimension that flickers
across the K runs is noise and stays silent — that is the whole point of capturing K runs instead of
one. See ``golden_lib`` for the consensus and floor logic.

With no committed baseline yet (a fresh capture), it instead prints the **noise floor** itself: how
much of each request's model is stable enough to diff on. A request with few unanimous slots will only
ever surface large changes; that's information, not a failure.

Workflow: golden_run.py (re-capture) → golden_diff.py (read the signal) → commit if intended.

Usage:
    python scripts/golden_diff.py            # every request
    python scripts/golden_diff.py <slug>...  # only the named one(s)
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


def diff_one(slug: str) -> bool:
    """Print the signal for one request. Returns True if anything material moved (or it's new)."""
    path = runs_path(slug)
    if not path.exists():
        print(f"\n{slug}\n  ! no working-tree capture (run golden_run.py first)")
        return False

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
        return True

    old = load_runs(old_text)
    m = movements(old, new)

    print(f"\n{slug}")
    if not m["moved"] and not m["themes_added"] and not m["themes_removed"]:
        print("  · no change above the noise floor")
        return False

    if m["moved"]:
        print(f"  slots      {len(m['moved'])} moved (clearing the floor):")
        for mv in m["moved"]:
            print(f"               {mv['slot']:<22} {mv['dim']} {mv['from']}→{mv['to']}"
                  f"   (was {mv['old_agree']}/{mv['n']} unanimous, now {mv['new_agree']}/{mv['n']})")
    if m["themes_added"]:
        print(f"  questions  + stable theme(s): {', '.join(m['themes_added'])}")
    if m["themes_removed"]:
        print(f"  questions  − stable theme(s): {', '.join(m['themes_removed'])}")
    return True


def main(argv: list[str]) -> int:
    slugs = argv or sorted(p.name[: -len(".runs.json")]
                           for p in GOLDEN.glob("*.runs.json"))
    if not slugs:
        print("No golden baselines found. Run golden_run.py first.", file=sys.stderr)
        return 1

    print("Golden diff — working tree vs HEAD (only changes above the noise floor)")
    moved = sum(diff_one(slug) for slug in slugs)
    print(f"\n{'─' * 60}\n{moved}/{len(slugs)} request(s) moved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
