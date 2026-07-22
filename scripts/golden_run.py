#!/usr/bin/env python
"""Capture the golden baseline — K runs per request (the regression reference).

Reads the fixed request set in ``fixtures/golden/requests.md`` and runs discovery **K times** per
request (K=3 by default; override with ``GOLDEN_K``), writing all K models to
``fixtures/golden/<slug>.runs.json``. Capturing K runs — not one — is what lets ``golden_diff`` tell a
real prompt/context-card effect apart from run-to-run sampling noise: the model family in use exposes
no sampling controls, so noise can't be pinned, only measured. See ``golden_lib`` for the reasoning.

Workflow:
    1. baseline committed (``fixtures/golden/*.runs.json`` in HEAD)
    2. edit a prompt (``prompts/engine.md``) or add/change a context card
    3. python scripts/golden_run.py        # re-capture the K-run baseline
    4. python scripts/golden_diff.py        # only changes above the noise floor are shown
    5. commit the new baseline if the change is intended

Cost: K API calls per request (default 3 × 6 requests = 18). Needs ANTHROPIC_API_KEY in ``.env``.

Usage:
    python scripts/golden_run.py            # every request
    python scripts/golden_run.py <slug>...  # only the named one(s)
    GOLDEN_K=5 python scripts/golden_run.py # override runs-per-request
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv
from anthropic import Anthropic

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from golden_lib import GOLDEN, K, REPO, REQUESTS, dump_runs, parse_requests, stability  # noqa: E402

sys.path.insert(0, str(REPO / "src"))
from product_copilot.core.discovery import run  # noqa: E402

load_dotenv()


def capture(client: Anthropic, req: dict) -> None:
    models = []
    for i in range(K):
        models.append(run(client, [{"role": "user", "content": req["request"]}]))
        print(f"    run {i + 1}/{K} done", end="\r", flush=True)
    dump_runs(req["slug"], req["request"], models)
    st = stability(models)
    # Show the noise floor up front: how much of the model was stable across the K runs.
    print(f"  ✓ {req['slug']:<20} {st['unanimous']['impact']}/{st['total_slots']} slots "
          f"unanimous on impact · {st['unanimous']['state']}/{st['total_slots']} on confidence "
          f"· stable themes: {', '.join(st['themes']) or '—'}")


def main(argv: list[str]) -> int:
    if not REQUESTS.exists():
        print(f"Missing request set: {REQUESTS}", file=sys.stderr)
        return 1
    runs = parse_requests(REQUESTS)
    wanted = set(argv)
    if wanted:
        runs = [r for r in runs if r["slug"] in wanted]
        for slug in sorted(wanted - {r["slug"] for r in runs}):
            print(f"  ! unknown slug (skipped): {slug}", file=sys.stderr)
    if not runs:
        print("Nothing to capture.", file=sys.stderr)
        return 1

    GOLDEN.mkdir(parents=True, exist_ok=True)
    client = Anthropic()
    print(f"Capturing {len(runs)} request(s) × {K} runs → {GOLDEN.relative_to(REPO)}/")
    for req in runs:
        try:
            capture(client, req)
        except Exception as exc:  # one bad request should not lose the others
            print(f"  ✗ {req['slug']:<20} FAILED: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
