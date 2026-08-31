"""What a run spent — a provider-neutral ledger of API calls, tokens, latency and cost.

A sibling of `paths.py` and `streams.py`: a small cross-cutting facility that belongs to no layer.
Providers record into it, `render/terminal.py` prints it, `cli.py` scopes it around a command, and
`services/discovery.py` reads it back (`current_ledger()`, #292) to stamp what a provider-backed
apply spent onto that revision's own provenance. It lives here rather than in `providers/` because
nothing about it is a vendor's — calls, tokens, cache tiers and latency are concepts any provider
has, and its own docstring already said it was presentation-free. It sat in `providers/anthropic.py`,
which meant the purest view layer in the tree had to name a vendor module to print a cost line
(#167). It is not in `core/` either: core validates and versions the model, and what an API call cost
is not a fact about the model.

**Cost is arithmetic here and nowhere else.** A `CallRecord` carries the rate it was billed at,
stamped by the provider that made the call, so this module holds no price table and consults none.
That is the difference between a neutral ledger and one that merely looks neutral: a ledger that
looked up a rate would have to know whose rate to look up. It also makes the estimate honest across
a price change — the rate recorded is the rate that was in force when the tokens were spent, not
whichever one is live when the line is printed.

Three states, deliberately, because two of them are easy to confuse: a priced call, an unpriced one
(`rate_per_mtok is None` — no price on file for that model, and `cost_usd()` refuses to guess), and
a call whose rate is known but whose *provenance* is not (`priced_as_of is None`). The renderer says
something different for each; an estimate printed with no rate date reads exactly like one printed
with a current date.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class CallRecord:
    """One provider call's usage — summed across its retry attempts (retries spend tokens too).

    `rate_per_mtok` and `priced_as_of` are provenance, stamped by the provider at the moment it
    records the call: the USD rate per million tokens `(input, output)` it was billed at, and the
    date of the table that rate came from. Both absent means nobody could price this call, and
    `cost_usd()` says so rather than guessing (invariant 6: provenance is real or absent).
    """
    model: str
    input_tokens: int = 0        # uncached, full-price input
    output_tokens: int = 0
    cache_read_tokens: int = 0   # served from cache (~0.1x input price)
    cache_write_tokens: int = 0  # written to cache (~1.25x input price)
    latency_ms: int = 0
    attempts: int = 1
    rate_per_mtok: tuple[float, float] | None = None
    priced_as_of: str | None = None


@dataclass
class UsageLedger:
    """Accumulates the API usage of a session (one `requivo` command). Presentation-free — the
    renderer turns it into a line; the cost estimate lives here because it is pure arithmetic over
    the records, and stays pure because each record brought its own rate."""
    calls: list[CallRecord] = field(default_factory=list)

    def record(self, rec: CallRecord) -> None:
        self.calls.append(rec)

    @property
    def input_tokens(self) -> int:
        return sum(c.input_tokens for c in self.calls)

    @property
    def output_tokens(self) -> int:
        return sum(c.output_tokens for c in self.calls)

    @property
    def cache_read_tokens(self) -> int:
        return sum(c.cache_read_tokens for c in self.calls)

    @property
    def cache_write_tokens(self) -> int:
        return sum(c.cache_write_tokens for c in self.calls)

    @property
    def latency_ms(self) -> int:
        return sum(c.latency_ms for c in self.calls)

    @property
    def models(self) -> list[str]:
        seen: list[str] = []
        for c in self.calls:
            if c.model not in seen:
                seen.append(c.model)
        return seen

    @property
    def priced_as_of(self) -> list[str]:
        """The distinct rate-table dates behind this ledger's cost, in the order first seen.

        A list rather than a string because a ledger can legitimately span two providers, and empty
        when nothing was priced — which is the third state the renderer needs: an estimate with no
        rate date must not print as one with a current date.
        """
        seen: list[str] = []
        for c in self.calls:
            if c.priced_as_of is not None and c.priced_as_of not in seen:
                seen.append(c.priced_as_of)
        return seen

    def cost_usd(self) -> float | None:
        """Estimated USD across all calls, or None if any call went unpriced — never guess a price.

        Cache reads bill ~0.1x the input rate, cache writes ~1.25x. The rates come off the records,
        so this is arithmetic and holds no opinion about whose prices they were or when they lapse.
        """
        total = 0.0
        for c in self.calls:
            if c.rate_per_mtok is None:
                return None
            in_rate, out_rate = c.rate_per_mtok
            total += (c.input_tokens * in_rate
                      + c.cache_read_tokens * in_rate * 0.1
                      + c.cache_write_tokens * in_rate * 1.25
                      + c.output_tokens * out_rate) / 1_000_000
        return total


# Session-scoped ledger. A ContextVar (not a module global) so it is isolated per call stack and
# trivially reset — cli.py opens `track_usage()` around a command; a provider just records if one is
# active.
_LEDGER: contextvars.ContextVar[UsageLedger | None] = contextvars.ContextVar("usage_ledger", default=None)


@contextmanager
def track_usage():
    """Scope a UsageLedger over a block. A provider's completion loop records into it; nothing else
    changes. No ledger active means `record_call` is a no-op, which is what keeps the offline verbs
    and the test fakes free of it."""
    ledger = UsageLedger()
    token = _LEDGER.set(ledger)
    try:
        yield ledger
    finally:
        _LEDGER.reset(token)


def record_call(rec: CallRecord) -> None:
    """File one call against the active ledger, if there is one.

    **This is the cross-module half of a constraint that used to be two adjacent lines**, and #74
    named it as the thing most likely to break quietly in the split: a provider must record the
    spend *before* it surfaces a clean failure, because a failed call is still billed for whatever
    it consumed. The ordering lives at the provider's failure exits, not here — this function cannot
    enforce it — but it is stated at both ends so that neither end reads as arbitrary.
    `test_a_failed_call_is_still_recorded_on_every_exit` is what goes red when an exit stops
    recording.
    """
    ledger = _LEDGER.get()
    if ledger is not None:
        ledger.record(rec)


def current_ledger() -> UsageLedger | None:
    """The ledger active on this call stack, or `None` when no `track_usage()` scope is open (#292).

    Read-only counterpart to `record_call`: a caller that wants to know *what a specific operation
    just spent* — a revision's provenance is the first one — needs to read the ledger back, not only
    write to it. `None` is a real, common answer (most of the offline test suite never opens a
    ledger at all) and callers must treat it as "nothing to report", never as "spent nothing": the
    two look identical from in here and only the caller can tell them apart from context, which is
    exactly invariant 6's rule about provenance applied to this ledger."""
    return _LEDGER.get()
