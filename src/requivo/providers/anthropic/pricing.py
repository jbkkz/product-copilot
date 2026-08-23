"""Anthropic's published rates, and the one function that stamps them onto a call.

The vendor half of what #74 called `usage.py`. The neutral half — the ledger, the records, the
arithmetic — is `requivo.usage`; what stayed here is the only part that is genuinely a fact about
Anthropic: a dated table, its expiry-aware launch prices, and the lookup over them.

Kept as a module of its own rather than folded into `client.py` for the reason #74 gives for cutting
it out of the provider in the first place: it is the part edited on a *calendar*, on a schedule that
has nothing to do with the engine. A file whose whole content is two tables and a date is one a
maintainer can open, correct and close without reading a call loop.
"""

from __future__ import annotations

from datetime import date

from requivo.usage import CallRecord

# USD per 1M tokens (input, output), from the Anthropic pricing reference as of 2026-08-01. This
# yields an *estimate*, never a bill: prices drift and intro rates lapse, so the renderer stamps this
# date and labels the number an estimate. Tokens are ground truth from the API; cost is the only
# thing here that can go stale — keep this table updateable and honest, not authoritative.
PRICING_AS_OF = "2026-08-01"
_PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Launch pricing that lapses on a known date: model → (input, output, last day inclusive). A dated
# table with no notion of expiry is wrong twice — it over-reports while an intro rate is live, then
# under-reports the day someone edits the rate in and forgets the lapse. Encoding the end date lets
# the estimate be right on both sides of it without another edit. `claude-sonnet-5` (the default
# model) is on launch pricing through 2026-08-31, reverting to the standard 3.00/15.00 above.
_LAUNCH_PRICE_PER_MTOK: dict[str, tuple[float, float, str]] = {
    "claude-sonnet-5": (2.00, 10.00, "2026-08-31"),
}


def price_per_mtok(model: str, on: date | None = None) -> tuple[float, float] | None:
    """The (input, output) USD rate per million tokens for `model` on a given day, or None when the
    model's price is unknown — never guess a price. `on` defaults to today, so a running estimate
    follows a launch rate over its expiry without a code change."""
    launch = _LAUNCH_PRICE_PER_MTOK.get(model)
    if launch is not None:
        in_rate, out_rate, until = launch
        if (on or date.today()) <= date.fromisoformat(until):
            return in_rate, out_rate
    return _PRICE_PER_MTOK.get(model)


def price_call(rec: CallRecord, on: date | None = None) -> CallRecord:
    """Stamp the rate this call was billed at onto the record, and return it.

    The rate is resolved *when the call is filed*, not when the total is rendered, and that is the
    whole reason this function exists rather than the ledger reaching back into the table above.
    Two things follow. The ledger stops being Anthropic's — it holds arithmetic over rates it was
    given, so a second provider needs no registry (#167). And an estimate spanning a price change is
    right on both sides of it: the rate recorded is the one that was in force when the tokens were
    spent, where a lookup at render time would re-price yesterday's calls at today's rate.

    Both fields are set together or neither is, which is invariant 6 applied to a price: a record
    carrying a rate with no table date would print an estimate that reads exactly like a dated one.
    An unknown model leaves both None and `cost_usd()` returns None rather than guessing.

    **It re-stamps unconditionally, and that is deliberate rather than an oversight.** Called twice
    on one record, the second call wins. A first-write-wins guard was considered and rejected: the
    only caller is `_record` in `completion.py`, which runs exactly once per `CallRecord` because
    `_complete` builds one record and reaches one exit with it, so the guard could never fire and a
    guard that provably cannot fire is worse than none -- it reads as protection against a case
    nobody has. What this function means is *price this call at Anthropic's rates*, and that is a
    question with one answer, not an answer that depends on whether somebody asked before.
    """
    rate = price_per_mtok(rec.model, on)
    if rate is not None:
        rec.rate_per_mtok = rate
        rec.priced_as_of = PRICING_AS_OF
    return rec
