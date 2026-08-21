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

## Two shapes of request

Most requests are **single-pass**: one discovery call, captured K times. That is the right shape for
watching a prompt or a context card, and it is the wrong shape for watching the *interactive* loop,
because a single-pass capture only ever has a turn 1.

A request that also carries `answer.<slot>:` lines is **interactive**. It drives
`DiscoveryService.draft_turn` — the loop behind `requivo discover` — for up to `GOLDEN_TURNS` turns
per run, answering the engine's questions off those lines. Each line is one *layer*: the next thing
that client has to say when the engine comes back to that slot. Layers are handed out in order and
then run out, which is what keeps the conversation moving instead of looping on one reply; a question
the sheet cannot answer is skipped, exactly as a user pressing Enter skips it, and a turn that
answers nothing ends the capture.

It exists because the interactive loop is grounded differently from turn 3 onward: it carries the
model and no longer re-sends the transcript. Turns 1 and 2 are byte-identical to the loop it
replaced, so only a capture that runs deep says anything at all about that change.

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
- **The interactive lens** watches turn 3 and beyond on an interactive request, in three measures —
  questions **re-asked** after the client had already answered them, early confirmations the model
  **lost** by the end, and completeness that **regressed** across a deep turn. Each is a way the
  carried model could turn out to be a lossy summary of the transcript it replaced. Findings are
  reported per run and as the unanimous set, on the same rule as a slot move: act on unanimous.
- **A capture that stopped short is not a clean one.** The lens prints the depth each run reached and
  warns when any of them came in under five turns, because a conversation that converged at turn 3
  never reached the question. And on a single-pass baseline it says *not measured* rather than
  printing an empty finding set, which would read exactly like a clean one.

## It measures movement, not improvement

The slot tiers are a projection; the questions and challenges are the product. `--questions` is usually
what settles whether a change was an improvement or merely a movement. When the finance card landed,
the engine stopped asking *"what exactly are these totals?"* and started asking *"a traceable
adjustment entry, or an override?"* — that's the read that matters.

## Cost

K calls per request, doubled under `--brief`. An **interactive** request costs K × `GOLDEN_TURNS`
instead — 15 at the defaults — so capture it on its own rather than as part of a full-set run.
Re-capture the targeted request first and the full set only before committing a baseline. The
harness's own logic is unit-tested in `tests/test_golden_lib.py`, and its capture loop in
`tests/test_golden_capture.py` (no API calls in either).
