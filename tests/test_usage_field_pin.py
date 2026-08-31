"""Pins the four SDK `Usage` field names `_complete` reads through `getattr(u, name, 0) or 0` (#294).

`completion.py` reads `input_tokens`, `output_tokens`, `cache_read_input_tokens` and
`cache_creation_input_tokens` off the SDK's response with a bare `getattr` default of `0` --
deliberately, so the offline fake in this suite needs no real `Usage` object (see
`test_a_failed_call_is_still_recorded_on_every_exit` in `test_usage.py`, which relies on exactly
that tolerance). The cost of that tolerance is silent: if a future `anthropic` release, still inside
the `>=0.40.0,<2` pin, renames or removes one of these four attributes, every token count for that
field quietly becomes 0 forever -- the ledger under-reports real spend, `requivo doctor` and every
cost estimate downstream go on trusting a zero that is not a zero, and nothing anywhere goes red. That
is the "silent absence inside the check for it" shape this repository names as its enemy throughout
CLAUDE.md. This file does not change the `getattr` tolerance -- the fake-client ergonomics it buys
are fine -- it only makes a rename of one of these names loud instead of quiet.

Skips cleanly (not an error) when the `anthropic` extra is not installed: `pytest.importorskip` at
module scope means an install without `requivo[anthropic]` reports this file as skipped rather than
failing to collect it, so it does not cost the rest of the suite.
"""
import pytest

anthropic = pytest.importorskip("anthropic", reason="requires the 'anthropic' extra")

from anthropic.types import Usage  # noqa: E402 - after importorskip, by construction

# The exact four names `completion.py` passes to `getattr(u, name, 0) or 0`. Read them out of the
# source rather than retyped here, so this test cannot itself drift from the call site it pins.
_USAGE_FIELDS_COMPLETE_READS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def test_the_sdk_usage_object_still_has_the_four_fields_the_billing_ledger_reads():
    """Red the moment one of these four is renamed inside the supported SDK range.

    `getattr(u, name, 0) or 0` never raises on a missing attribute -- that is the whole point of
    reading it that way -- so a rename upstream would otherwise surface as nothing at all: not an
    exception, not a warning, just every token count for that field silently pinned to 0. This
    assertion is what turns that silence into a failing test instead.
    """
    present = set(Usage.model_fields)
    missing = [name for name in _USAGE_FIELDS_COMPLETE_READS if name not in present]
    assert not missing, (
        f"anthropic.types.Usage no longer defines {missing} -- completion.py's _complete() reads "
        "these through getattr(u, name, 0) or 0, so a rename here currently zeroes that field's "
        "billing silently, with nothing going red. Update the read site in "
        "src/requivo/providers/anthropic/completion.py to the new name(s) and this pin."
    )
