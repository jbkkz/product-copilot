"""API usage tracking: what a call cost, and what the terminal says about it.

Split out of `test_engine.py` (#72).
"""
import io
from contextlib import redirect_stdout
from datetime import date as _date

import pytest
from _fakes import _ENGINE_REPLY, _FakeBlock, _model_in_out, _run_app

from requivo.providers.anthropic import CallRecord, UsageLedger, run, track_usage
from requivo.render.terminal import render_usage


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    """Every test in this module writes sessions/artifacts into an isolated temp workspace, never the
    real repo. Points both the canonical root (.requivo/sessions) and the legacy root (out/) at tmp."""
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))


# ── Tier 3: API usage tracking (tokens / cost / latency) ──────────────────────
# Tokens are ground truth from the response; cost is a labelled estimate. The ledger accumulates
# per-call usage; the renderer turns it into a line; the CLI prints it after an API-backed command.


class _FakeUsage:
    def __init__(self, i, o, cache_read=0, cache_write=0):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_read_input_tokens = cache_read
        self.cache_creation_input_tokens = cache_write


class _UsageResponse:
    def __init__(self, text, usage):
        self.content = [_FakeBlock(text)]
        self.usage = usage


class UsageFakeClient:
    """Like FakeClient but every reply carries a usage object, so `_complete` records real numbers."""

    def __init__(self, text, usage):
        self._text, self._usage = text, usage
        self.messages = self
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _UsageResponse(self._text, self._usage)


def test_usage_ledger_totals_and_cost():
    ledger = UsageLedger()
    ledger.record(CallRecord(model="claude-sonnet-5", input_tokens=1_000_000))
    ledger.record(CallRecord(model="claude-sonnet-5", output_tokens=1_000_000))
    assert ledger.input_tokens == 1_000_000 and ledger.output_tokens == 1_000_000
    # Standard rates: 1M input @ $3 + 1M output @ $15 = $18.00. Pinned to a day after the launch
    # window so the assertion states one rate rather than whichever is live when the suite runs.
    assert abs(ledger.cost_usd(on=_date(2026, 9, 1)) - 18.0) < 1e-6


def test_usage_ledger_applies_launch_pricing_until_it_lapses():
    # Sonnet 5 runs on launch pricing ($2/$10) through 2026-08-31, then reverts to $3/$15. A dated
    # table with no expiry gets exactly one of those two days right.
    ledger = UsageLedger()
    ledger.record(CallRecord(model="claude-sonnet-5", input_tokens=1_000_000, output_tokens=1_000_000))
    assert abs(ledger.cost_usd(on=_date(2026, 8, 31)) - 12.0) < 1e-6   # launch: 2 + 10
    assert abs(ledger.cost_usd(on=_date(2026, 9, 1)) - 18.0) < 1e-6    # standard: 3 + 15


def test_usage_ledger_cost_counts_cache_tiers():
    ledger = UsageLedger()
    # cache read ≈ 0.1× input rate, cache write ≈ 1.25× input rate (Sonnet standard input $3/Mtok)
    ledger.record(CallRecord(model="claude-sonnet-5", cache_read_tokens=1_000_000,
                             cache_write_tokens=1_000_000))
    assert abs(ledger.cost_usd(on=_date(2026, 9, 1)) - (0.3 + 3.75)) < 1e-6


def test_usage_ledger_cost_is_none_for_unpriced_model():
    ledger = UsageLedger()
    ledger.record(CallRecord(model="some-future-model", input_tokens=10))
    assert ledger.cost_usd() is None


def test_render_usage_shows_tokens_cache_latency_and_estimate():
    ledger = UsageLedger()
    ledger.record(CallRecord(model="claude-sonnet-5", input_tokens=1000, output_tokens=200,
                             cache_read_tokens=500, latency_ms=1500))
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_usage(ledger)
    text = buf.getvalue()
    assert "API USAGE" in text
    assert "1,500 tokens" in text          # processed = input + cache_read = 1000 + 500
    assert "500 served from cache" in text
    assert "1.5 s" in text
    assert "Est. cost" in text and "estimate" in text


def test_render_usage_silent_without_tokens():
    # No call, or usage absent (offline fake) → nothing printed.
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_usage(UsageLedger())                                   # empty
        render_usage(UsageLedger(calls=[CallRecord(model="claude-sonnet-5")]))  # a call, zero tokens
    assert buf.getvalue() == ""


def test_render_usage_flags_unpriced_model_but_keeps_tokens():
    ledger = UsageLedger()
    ledger.record(CallRecord(model="some-future-model", input_tokens=1000, output_tokens=200))
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_usage(ledger)
    text = buf.getvalue()
    assert "1,200 tokens" in text or "1,000 tokens" in text  # tokens still reported
    assert "no price on file" in text                         # cost honestly withheld


def test_complete_records_usage_into_the_active_ledger():
    # run() → _complete records one CallRecord, summing the response's usage fields.
    client = UsageFakeClient(_ENGINE_REPLY, _FakeUsage(1000, 200, cache_read=500))
    with track_usage() as ledger:
        run(client, [{"role": "user", "content": "leave approval"}])
    assert len(ledger.calls) == 1
    assert ledger.input_tokens == 1000 and ledger.output_tokens == 200
    assert ledger.cache_read_tokens == 500
    assert ledger.cost_usd() is not None


def test_pc_status_reports_no_usage_offline():
    # An offline verb makes no call → no ledger records → no usage line.
    with _model_in_out("clitest-usage") as p:
        text = _run_app(["status", str(p)])
    assert "API USAGE" not in text
