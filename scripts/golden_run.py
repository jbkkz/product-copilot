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
for that request, so it is opt-in. It is *described* as being for a couple of representative
requests; in practice all six single-pass baselines in ``fixtures/golden/`` carry one, and that gap
matters because a lens's grading was once designed around the sentence rather than the fixtures
(#162). Check the baselines before reasoning from this paragraph.

A request carrying an **answer sheet** (``answer.<slot>:`` lines in ``requests.md``) is captured
differently again: `capture_interactive` drives `DiscoveryService.draft_turn` for up to
``GOLDEN_TURNS`` turns per run, answering off the sheet. That is the only shape that can see what #77
changed — from turn 3 the interactive loop is grounded on the carried model alone, where the old one
re-sent the whole transcript, and turns 1 and 2 are byte-identical between the two (#137).

Cost: K API calls per request (default 3 × 6 single-pass requests = 18), doubled where ``--brief`` is
on, and K × ``GOLDEN_TURNS`` for an interactive one (15 at the defaults) — so capture an interactive
request on its own rather than as part of a full-set run. Needs ANTHROPIC_API_KEY in ``.env``.

Usage:
    python scripts/golden_run.py              # every request
    python scripts/golden_run.py <slug>...    # only the named one(s)
    python scripts/golden_run.py <slug> --brief   # also capture the assessment
    GOLDEN_K=5 python scripts/golden_run.py   # override runs-per-request
    GOLDEN_TURNS=8 python scripts/golden_run.py <slug>   # override turns-per-run
"""

from __future__ import annotations

import sys

from anthropic import Anthropic
from dotenv import load_dotenv

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from golden_lib import (  # noqa: E402
    GOLDEN,
    MEASURABLE_DEPTH,
    REPO,
    REQUESTS,
    TURNS,
    AnswerSheet,
    K,
    Turn,
    answers_for_turn,
    brief_consensus,
    configure_output,
    dump_runs,
    dump_turn_runs,
    is_interactive,
    parse_requests,
    stability,
    turn_lens,
)

sys.path.insert(0, str(REPO / "src"))
from requivo.providers.anthropic import advise, run  # noqa: E402
from requivo.services.discovery import DiscoveryService  # noqa: E402

load_dotenv()


def capture_interactive(client: Anthropic, req: dict) -> None:
    """K interactive conversations, each driven off this request's answer sheet.

    Reasoning goes through `DiscoveryService.draft_turn` rather than through `run()` directly, and
    that is the whole validity of the measurement: `draft_turn` is the production interactive path,
    and it is the shape #77 changed. A loop that assembled its own message list here would capture a
    conversation no surface has held since #77 (#137).

    The rest mirrors `converse()` — the turn counter, the answer format, and stopping when nothing
    could be answered. What it does not mirror is a human, so the answers come from the sheet. There
    is no session, no revision and no write anywhere in this: `draft_turn` reasons and returns.
    """
    disco = DiscoveryService(client=client)
    runs: list[list[Turn]] = []
    for i in range(K):
        sheet = AnswerSheet(req["answers"])
        turns: list[Turn] = []
        out, answers = None, None
        for index in range(1, TURNS + 1):
            out = disco.draft_turn(req["request"], current_model=out, answers=answers, cards=None)
            print(f"    run {i + 1}/{K}  turn {index}/{TURNS}", end="\r", flush=True)
            # `answered` records what was actually sent onward, so the last turn's is empty even
            # where the sheet still had something to say. Recording an answer the engine never saw
            # would put a slot in `covered` that the conversation never covered, and the re-ask
            # count is measured against exactly that set.
            done = index == TURNS or not out.questions
            block, answered = (None, []) if done else answers_for_turn(out.questions, sheet)
            turns.append(Turn(index=index, answered=answered, model=out))
            if block is None:
                break
            answers = block
        runs.append(turns)
    dump_turn_runs(req["slug"], req["request"], req["answers"], runs)

    lens = turn_lens(runs)
    depth = "/".join(str(d) for d in lens["depths"])
    verdict = "deep enough" if lens["deep_enough"] else f"SHALLOW — under {MEASURABLE_DEPTH} turns"
    print(f"  ✓ {req['slug']:<20} interactive · turns {depth} across {lens['n']} runs · {verdict}")
    st = stability([run[-1].model for run in runs])
    print(f"    final model         {st['unanimous']['impact']}/{st['total_slots']} slots unanimous "
          f"on impact · {st['unanimous']['state']}/{st['total_slots']} on confidence")
    for key, caption in (("reasked", "re-asked after the client answered"),
                         ("lost", "answered early, not confirmed at the end"),
                         ("regressed", "completeness fell back")):
        hits = lens[key]
        detail = ", ".join(f"{lab} ({c}/{lens['n']})" for lab, c in sorted(hits.items())) or "—"
        print(f"    {caption:<38} {detail}")


def capture(client: Anthropic, req: dict, with_brief: bool = False) -> None:
    if is_interactive(req):
        if with_brief:
            # Said rather than silently dropped: --brief doubles the calls, and on a request that
            # already costs K x TURNS that is a spend nobody asked for. The assessment lens watches a
            # different thing from the turn lens and neither needs the other.
            print(f"  ! {req['slug']:<20} --brief is not captured for an interactive request "
                  f"(it would double a {K * TURNS}-call capture); the turn lens follows",
                  file=sys.stderr)
        return capture_interactive(client, req)

    models, briefs = [], ([] if with_brief else None)
    for i in range(K):
        # `reuse_system=True` explicitly: this loop sends engine.md's system prompt K times, so the
        # breakpoint is genuinely re-read here — the same declaration the `advise` call below makes,
        # now stated rather than left to `run()`'s default (#58).
        out = run(client, [{"role": "user", "content": req["request"]}], reuse_system=True)
        models.append(out)
        if with_brief:
            # `reuse_system=True`: unlike the CLI, this loop sends brief.md's system prompt K times, so
            # the cache breakpoint is genuinely re-read here and is worth its 1.25x write (#9).
            briefs.append(advise(client, out, reuse_system=True))  # see --brief in the header
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
    # First, before anything can print: this script spends real API calls and writes each request's
    # baseline before it reports on it, so a glyph the console cannot encode must not be able to kill
    # it over work already paid for (invariant 16, #164).
    configure_output()
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
    # An interactive request costs a call per turn, so the total is per-request rather than a single
    # multiplication — and it is an upper bound, because a conversation that runs out of answers stops
    # early. Stating it as "up to" is the honest form: the number that matters before spending is the
    # ceiling, not the average.
    calls = sum(K * (TURNS if is_interactive(r) else (2 if with_brief else 1)) for r in runs)
    print(f"Capturing {len(runs)} request(s) × {K} runs → {GOLDEN.relative_to(REPO)}/  "
          f"(up to {calls} API calls{', assessment included' if with_brief else ''})")
    for req in runs:
        try:
            capture(client, req, with_brief)
        except Exception as exc:  # one bad request should not lose the others
            print(f"  ✗ {req['slug']:<20} FAILED: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
