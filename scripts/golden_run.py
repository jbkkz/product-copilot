#!/usr/bin/env python
"""Capture the golden models — the regression baseline for prompts and context cards.

Reads the fixed request set in ``fixtures/golden/requests.md``, runs one single-pass discovery per
request against the live API, and writes each resulting model to ``fixtures/golden/<slug>.json``.
This is the *capture* half of the harness; ``golden_diff.py`` is the *compare* half.

The workflow: commit the current golden models as a baseline, change a prompt or add a context card,
re-run this script, then run ``golden_diff.py`` to see — in structural terms, not raw text — what the
change did to the engine's reasoning across every problem form at once.

Usage:
    python scripts/golden_run.py            # capture every request in the set
    python scripts/golden_run.py <slug>...  # capture only the named request(s)

Needs ANTHROPIC_API_KEY (and optionally MODEL) in ``.env`` — same as the CLI. Each request is one API
call.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Run from a repo checkout without installing: put src/ on the path (harmless if already installed).
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from dotenv import load_dotenv  # noqa: E402
from anthropic import Anthropic  # noqa: E402

from product_copilot.core.contracts import EngineOutput  # noqa: E402
from product_copilot.core.discovery import run  # noqa: E402

load_dotenv()

GOLDEN = REPO / "fixtures" / "golden"
REQUESTS = GOLDEN / "requests.md"


def parse_requests(path: Path) -> list[dict]:
    """Parse requests.md into ``[{slug, form, card, request}, …]``.

    A run is a ``### <slug>`` heading followed by ``key: value`` lines. Only ``request:`` is required;
    ``form``/``card`` are metadata carried through for readability. Anything outside a run block (the
    file's prose header) is ignored.
    """
    runs: list[dict] = []
    current: dict | None = None
    for line in path.read_text().splitlines():
        if line.startswith("### "):
            current = {"slug": line[4:].strip(), "form": "", "card": "", "request": ""}
            runs.append(current)
        elif current is not None and ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            key = key.strip()
            if key in ("form", "card", "request"):
                current[key] = value.strip()
    return [r for r in runs if r["request"]]


def _signal(out: EngineOutput) -> str:
    """One-line confirmation that a capture looks sane — not a diff, just a smoke signal."""
    slots = out.model
    high = sum(1 for s in slots.values() if s.impact == "high")
    filled = sum(1 for s in slots.values() if s.completeness > 0)
    return f"{len(slots)} slots · {filled} filled · {high} high-impact · {len(out.questions)} questions"


def capture(client: Anthropic, req: dict) -> Path:
    out = run(client, [{"role": "user", "content": req["request"]}])
    path = GOLDEN / f"{req['slug']}.json"
    path.write_text(out.model_dump_json(indent=2))
    print(f"  ✓ {req['slug']:<20} {_signal(out)}")
    return path


def main(argv: list[str]) -> int:
    if not REQUESTS.exists():
        print(f"Missing request set: {REQUESTS}", file=sys.stderr)
        return 1
    runs = parse_requests(REQUESTS)
    wanted = set(argv)
    if wanted:
        runs = [r for r in runs if r["slug"] in wanted]
        missing = wanted - {r["slug"] for r in runs}
        for slug in sorted(missing):
            print(f"  ! unknown slug (skipped): {slug}", file=sys.stderr)
    if not runs:
        print("Nothing to capture.", file=sys.stderr)
        return 1

    GOLDEN.mkdir(parents=True, exist_ok=True)
    client = Anthropic()
    print(f"Capturing {len(runs)} golden model(s) → {GOLDEN.relative_to(REPO)}/")
    for req in runs:
        try:
            capture(client, req)
        except Exception as exc:  # one bad run should not lose the others
            print(f"  ✗ {req['slug']:<20} FAILED: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
