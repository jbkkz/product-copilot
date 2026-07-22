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

``--brief`` additionally captures the **assessment** for each run — the deliverable, not just the
discovery state. It watches the complexity verdict and the challenge headlines (what the engine chose
to contest), which is what a change to ``prompts/brief.md`` actually moves. It doubles the API calls
for that request, so it is opt-in and meant for a couple of representative requests, not all six.

Cost: K API calls per request (default 3 × 6 requests = 18), doubled where ``--brief`` is on. Needs
ANTHROPIC_API_KEY in ``.env``.

Usage:
    python scripts/golden_run.py              # every request
    python scripts/golden_run.py <slug>...    # only the named one(s)
    python scripts/golden_run.py <slug> --brief   # also capture the assessment
    GOLDEN_K=5 python scripts/golden_run.py   # override runs-per-request
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv
from anthropic import Anthropic

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from golden_lib import (GOLDEN, K, REPO, REQUESTS, brief_consensus, dump_runs,  # noqa: E402
                        parse_requests, stability)

sys.path.insert(0, str(REPO / "src"))
from product_copilot.core.discovery import run  # noqa: E402
from product_copilot.core.generators import advise  # noqa: E402

load_dotenv()


def capture(client: Anthropic, req: dict, with_brief: bool = False) -> None:
    models, briefs = [], ([] if with_brief else None)
    for i in range(K):
        out = run(client, [{"role": "user", "content": req["request"]}])
        models.append(out)
        if with_brief:
            briefs.append(advise(client, out))    # a second call per run — see --brief in the header
        print(f"    run {i + 1}/{K} done", end="\r", flush=True)
    dump_runs(req["slug"], req["request"], models, briefs)
    st = stability(models)
    # Show the noise floor up front: how much of the model was stable across the K runs.
    print(f"  ✓ {req['slug']:<20} {st['unanimous']['impact']}/{st['total_slots']} slots "
          f"unanimous on impact · {st['unanimous']['state']}/{st['total_slots']} on confidence "
          f"· stable themes: {', '.join(st['themes']) or '—'}")
    if with_brief:
        bc = brief_consensus(briefs)
        stable = sorted(bc["themes"])
        print(f"    assessment          complexity {bc['complexity'][0]} "
              f"({bc['complexity'][1]}/{bc['n']} runs) · stable challenges: "
              f"{'; '.join(stable) or '—'}")


def main(argv: list[str]) -> int:
    if not REQUESTS.exists():
        print(f"Missing request set: {REQUESTS}", file=sys.stderr)
        return 1
    with_brief = "--brief" in argv
    argv = [a for a in argv if a != "--brief"]
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
    calls = len(runs) * K * (2 if with_brief else 1)
    print(f"Capturing {len(runs)} request(s) × {K} runs → {GOLDEN.relative_to(REPO)}/  "
          f"({calls} API calls{', assessment included' if with_brief else ''})")
    for req in runs:
        try:
            capture(client, req, with_brief)
        except Exception as exc:  # one bad request should not lose the others
            print(f"  ✗ {req['slug']:<20} FAILED: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
