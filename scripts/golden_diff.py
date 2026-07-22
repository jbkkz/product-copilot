#!/usr/bin/env python
"""The regression lens — what a prompt or context-card change did to the engine's reasoning.

Compares each golden model in the working tree (the fresh capture) against its committed baseline in
git ``HEAD``, and prints **only the structural signal**: which slots moved, how their impact and
confidence shifted, and how the priority questions re-aimed. Raw value text is intentionally *not*
diffed — a wording change on a slot is noise; a slot going from medium to high impact, or a question
theme appearing, is the signal.

Workflow:
    1. baseline committed (``fixtures/golden/*.json`` in HEAD)
    2. edit a prompt (``prompts/engine.md``) or add/change a context card
    3. python scripts/golden_run.py        # overwrites the working-tree captures
    4. python scripts/golden_diff.py        # read the signal below; decide if it was intended
    5. commit the new fixtures if the change is wanted — the baseline moves forward

This is a *lens*, not a pass/fail gate: the API is non-deterministic, so small wording drift between
runs is expected. Judge the shape of the change, not byte equality. Git is the baseline store, so
there is no separate baseline directory to keep in sync.

Usage:
    python scripts/golden_diff.py            # diff every golden model
    python scripts/golden_diff.py <slug>...  # diff only the named one(s)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from product_copilot.core.analysis import _label, _state_of  # noqa: E402
from product_copilot.core.contracts import EngineOutput  # noqa: E402
from product_copilot.core.dependencies import diff_models  # noqa: E402

GOLDEN = REPO / "fixtures" / "golden"
STATES = ("confirmed", "inferred", "unknown")


def _head_version(rel_path: str) -> str | None:
    """The committed contents of a file at HEAD, or None if it isn't tracked there yet."""
    res = subprocess.run(
        ["git", "show", f"HEAD:{rel_path}"],
        cwd=REPO, capture_output=True, text=True,
    )
    return res.stdout if res.returncode == 0 else None


def _counts(out: EngineOutput) -> tuple[int, dict[str, int]]:
    """(# high-impact slots, {state: count})."""
    high = sum(1 for s in out.model.values() if s.impact == "high")
    states = {st: sum(1 for s in out.model.values() if _state_of(s) == st) for st in STATES}
    return high, states


def _question_themes(out: EngineOutput) -> list[str]:
    """The slot labels the priority questions aim at — the engine's current focus."""
    seen, themes = set(), []
    for q in out.questions:
        lab = _label(q.slot)
        if lab not in seen:
            seen.add(lab)
            themes.append(lab)
    return themes


def _delta(old: int, new: int) -> str:
    return f"{old}→{new}" + ("" if old == new else f" ({new - old:+d})")


def _slot_transition(old_slot, new_slot) -> str:
    """A one-line description of how a single slot moved — impact and/or state and/or content."""
    parts = []
    if old_slot is None:
        return "new slot"
    if old_slot.impact != new_slot.impact:
        parts.append(f"impact {old_slot.impact}→{new_slot.impact}")
    old_state, new_state = _state_of(old_slot), _state_of(new_slot)
    if old_state != new_state:
        parts.append(f"{old_state}→{new_state}")
    if not parts:  # value moved but impact/state held — the low-signal case, named but not detailed
        parts.append("content")
    return ", ".join(parts)


def diff_one(slug: str) -> bool:
    """Print the signal for one golden model. Returns True if anything material moved."""
    new_path = GOLDEN / f"{slug}.json"
    if not new_path.exists():
        print(f"\n{slug}\n  ! no working-tree capture (run golden_run.py first)")
        return False

    new = EngineOutput.model_validate_json(new_path.read_text())
    old_text = _head_version(f"fixtures/golden/{slug}.json")

    if old_text is None:
        high, states = _counts(new)
        print(f"\n{slug}\n  ⊕ NEW — no baseline in HEAD "
              f"({len(new.model)} slots · {high} high-impact · {len(new.questions)} questions)")
        return True

    old = EngineOutput.model_validate_json(old_text)
    changed = diff_models(old, new)

    print(f"\n{slug}")
    if not changed and _question_themes(old) == _question_themes(new):
        print("  · unchanged")
        return False

    if changed:
        print(f"  slots      {len(changed)} moved:")
        for sid in changed:
            print(f"               {_label(sid):<22} {_slot_transition(old.model.get(sid), new.model[sid])}")

    old_high, old_states = _counts(old)
    new_high, new_states = _counts(new)
    if old_high != new_high:
        print(f"  impact     high-impact slots {_delta(old_high, new_high)}")
    if old_states != new_states:
        print("  confidence " + "  ".join(f"{st} {_delta(old_states[st], new_states[st])}" for st in STATES))

    old_themes, new_themes = _question_themes(old), _question_themes(new)
    if old_themes != new_themes:
        print(f"  questions  now: {', '.join(new_themes) or '—'}")
        print(f"             was: {', '.join(old_themes) or '—'}")
    return True


def main(argv: list[str]) -> int:
    slugs = argv or sorted(p.stem for p in GOLDEN.glob("*.json"))
    if not slugs:
        print("No golden models found. Run golden_run.py first.", file=sys.stderr)
        return 1

    print("Golden diff — working tree vs HEAD (structural signal only)")
    moved = sum(diff_one(slug) for slug in slugs)
    print(f"\n{'─' * 60}\n{moved}/{len(slugs)} model(s) moved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
