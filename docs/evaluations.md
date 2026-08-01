# Evaluations — the golden harness

> Behaviour is tuned by editing Markdown/JSON assets, and the engine is non-deterministic — so "did
> that change help?" can't be answered by one run. This harness answers it.

## Workflow

```bash
python scripts/golden_run.py <slug>          # capture a fixed request K times (K=3)
python scripts/golden_diff.py                # what moved, above the measured noise floor
python scripts/golden_diff.py <slug> --questions   # the questions and challenges themselves
```

Edit an asset → `golden_run` → `golden_diff` → commit the new baseline if the change is intended.
`fixtures/golden/requests.md` is the fixed request set — one per problem *form*.

## What it reports, and why

- **Consensus over K runs, not a single capture.** A slot dimension is a usable reference only if all
  K runs agree on it. The per-request noise floor (how many slots are unanimous) is printed so you know
  how much signal a request can carry.
- **Strong vs weak moves.** Strong = unanimous before *and* after; weak = a bare majority (at K=3, one
  run flipping). Act on strong, watch weak only in aggregate.
- **A capture identical to HEAD is reported as "not re-captured", never "no change"** — a false
  all-clear is the one failure mode a regression lens must not have.
- **The assessment lens** (`--brief`) watches the deliverable instead of the discovery state: the
  complexity verdict and which premises the engine chose to contest.

## It measures movement, not improvement

The slot tiers are a projection; the questions and challenges are the product. `--questions` is usually
what settles whether a change was an improvement or merely a movement. When the finance card landed,
the engine stopped asking *"what exactly are these totals?"* and started asking *"a traceable
adjustment entry, or an override?"* — that's the read that matters.

## Cost

K calls per request, doubled under `--brief`. Re-capture the targeted request first and the full set
only before committing a baseline. The harness's own logic is unit-tested in
`tests/test_golden_lib.py` (no API calls).
