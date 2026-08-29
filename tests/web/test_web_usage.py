"""What a paid web action cost, said out loud (#253).

The repo's own ordering is "Web is the product experience, the CLI is infrastructure", and cost
observability existed only on the infrastructure. `track_usage()` was opened in `cli.py` and nowhere
else, and `record_call()` is an explicit no-op with no active ledger — so every provider call made
through a browser was billed by Anthropic and recorded nowhere: not shown, not logged, not
recoverable afterwards.

Three states, and the two that are not a number are the ones worth testing:

* a **priced** call reports exact tokens and a cost labelled as an estimate, with the rate date;
* an **unpriced** call says there is no price on file and never guesses one;
* a call the provider reported **no usage** for, and an offline page, say nothing at all — and those
  two are not the same as a page that lost its usage line.

Offline, isolated workspace per test; the fixtures live in `tests/web/conftest.py`.
"""

from __future__ import annotations

import logging

from tests.web.conftest import BRIEF_REPLY, HIGH_EXPLICIT, HIGH_INFERRED, Spend, engine_reply

# Enough tokens that the rendered figures are unmistakable in a page of other numbers.
PAID = Spend(input_tokens=9000, output_tokens=3000, cache_read_input_tokens=400)

# 9000 + 400 + 3000. Written out so the assertions below read as a claim about the arithmetic rather
# than as a copy of whatever the code happened to produce.
PAID_TOKENS = "12,400"


def _analysed(client, with_provider, *replies, spend=PAID):
    """A session at revision 1, created through the web with a provider that reports a spend."""
    fake = with_provider(engine_reply(problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED),
                         *replies, spend=spend)
    client.post("/sessions", data={"request_text": "A leave approval system",
                                   "slug": "leave-approval", "provider": "anthropic"})
    return fake


# ── what a paid action reports ────────────────────────────────────────────────

def test_an_answers_turn_says_what_it_spent(client, with_provider):
    """The fragment the reader lands on after a paid turn states the tokens and the estimate.

    Tokens are exact and the cost is labelled — the same split `render_usage` makes on the CLI, in
    the Web's own vocabulary rather than the engine's.
    """
    _analysed(client, with_provider,
              engine_reply(converged=True, problem=HIGH_EXPLICIT, business_rules=HIGH_EXPLICIT))

    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": "Exceptions go to HR.", "expected_revision": "1"})

    assert r.status_code == 200
    assert PAID_TOKENS in r.text, "the turn reported no token count"
    assert "estimate" in r.text, "a cost printed without the word estimate reads as a bill"
    assert "$" in r.text


def test_a_generation_says_what_it_spent(client, with_provider):
    """Every paid action, not only the conversational one. Generating a document is the step a reader
    is most likely to repeat, and the one whose cost nothing was reporting."""
    _analysed(client, with_provider, BRIEF_REPLY)

    r = client.post("/sessions/leave-approval/artifacts/brief")

    assert r.status_code == 200
    assert PAID_TOKENS in r.text
    assert "estimate" in r.text


# ── the two silences, which are not the same silence ──────────────────────────

def test_an_offline_page_reports_no_spend(client, with_provider):
    """Reading a session costs nothing, so it says nothing.

    The must-fire control for every assertion above: a template that printed a usage line
    unconditionally would satisfy all of them and would be reporting a cost for a page view.
    """
    _analysed(client, with_provider)

    for path in ("/", "/sessions/leave-approval"):
        page = client.get(path).text
        assert "estimate" not in page, f"{path} reported a spend for a page that made no call"
        assert PAID_TOKENS not in page


def test_a_call_the_provider_reported_no_usage_for_says_nothing_rather_than_zero(client,
                                                                                 with_provider):
    """Zero tokens is not a measurement (#253).

    The provider can answer without usage figures, and it does throughout this suite's offline fakes.
    Printing "0 tokens, est. $0.000" for that would be this project's own defect class: a value that
    could not be read rendered as a value that was read and found to be nothing.
    """
    _analysed(client, with_provider,
              engine_reply(converged=True, problem=HIGH_EXPLICIT, business_rules=HIGH_EXPLICIT),
              spend=None)

    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": "Exceptions go to HR.", "expected_revision": "1"})

    assert r.status_code == 200
    assert "estimate" not in r.text, "a call with no reported usage was rendered as a $0.000 turn"


def test_an_unpriced_call_says_so_rather_than_guessing(client, with_provider, monkeypatch):
    """No price on file is an answer; a guessed number is not (invariant 6, and `cost_usd`'s own rule).

    A model the rate table does not carry — a preview, a new generation, a name the operator pinned
    by hand — produces exact tokens and no cost at all. The one thing this must never do is fall back
    to a neighbouring model's rate, which is how an estimate becomes an invented bill.
    """
    monkeypatch.setenv("MODEL", "claude-something-nobody-priced")
    _analysed(client, with_provider,
              engine_reply(converged=True, problem=HIGH_EXPLICIT, business_rules=HIGH_EXPLICIT))

    r = client.post("/sessions/leave-approval/answers",
                    data={"answers": "Exceptions go to HR.", "expected_revision": "1"})

    assert r.status_code == 200
    assert PAID_TOKENS in r.text, "tokens are exact even when the price is unknown"
    assert "no price on file" in r.text
    assert "est. ~$" not in r.text, "an unpriced call must not print a dollar figure"


def test_a_failed_paid_call_still_records_what_it_spent(client, with_provider, caplog):
    """A call that failed is still billed, and the reader still gets the error page (#253).

    The same contract as `test_a_failed_call_is_still_recorded_on_every_exit` one layer up. On this
    path the fragment is replaced by the app's error rendering, so the spend cannot ride the
    response — it goes to the `requivo.web` logger, which is the terminal the operator started the
    server in. Silence there would mean a paid, failed turn left no trace anywhere.
    """
    # Three malformed replies: the JSON retry loop makes three attempts and spends on every one, then
    # gives up as a clean `EngineError`. A failure the provider itself does not handle would prove
    # nothing here, because it would never reach the recording exit this test is about.
    _analysed(client, with_provider, "not json", "not json", "not json")

    with caplog.at_level(logging.INFO, logger="requivo.web"):
        r = client.post("/sessions/leave-approval/answers",
                        data={"answers": "Exceptions go to HR.", "expected_revision": "1"})

    assert r.status_code >= 400, "the failure still has to reach the reader as an error"
    logged = [rec.getMessage() for rec in caplog.records]
    assert any("web-answer" in line for line in logged), (
        "a paid turn that failed was not recorded anywhere: " + repr(logged))
