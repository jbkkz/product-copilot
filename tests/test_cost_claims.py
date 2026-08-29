"""Every dollar figure in the docs is recomputed from the rate table (#252).

An OSS user pastes their own key and pays with their own money, and nothing they could read
beforehand said what to expect: the README never mentioned cost, and `docs/providers.md` explained
the caching mechanics without naming a figure. The CLI prints the real number, but only *after* the
spend, which is the wrong side of the decision.

Publishing a number is cheap; publishing one that silently dates itself is not, and this repository
has already paid for that once -- the rate table carried Sonnet 4.6's price for a release behind an
expiry nobody could falsify (#254). So the rule here is that a dollar figure in prose is a
**derived** value or it does not ship, and this file is the derivation.

Four things are checked, and each fails independently so a red names its own cause.

* **The rate and its date** come from `providers/anthropic/pricing.py`. Edit that table and the docs
  go red, which is the whole point: the alternative is a doc that agrees with the code on the day it
  is written and disagrees quietly on the day the price moves.
* **The token ranges bracket what this repository actually assembles and actually received.** Input
  is `build_prompt` for every operation; output is the captured replies in `fixtures/golden/`. Edit
  a prompt or re-capture a baseline far enough and the published range stops being true.
* **Every dollar figure is arithmetic over the two above**, so a hand-typed number cannot survive.
* **The call counts are stated in the table itself** and multiplied here, so a row claiming a total
  that does not follow from its own call count goes red.

What this cannot check, said here rather than left to be assumed: the **token counts are estimated
at four characters per token**, because no real ledger output is committed to this repository and
this suite makes no API calls. The docs label them as estimates for that reason. A future change
that commits a real `UsageLedger` capture should replace the estimator here and tighten the ranges.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from requivo.core.context import build_prompt
from requivo.providers.anthropic.generators import _OP_PROMPTS
from requivo.providers.anthropic.pricing import PRICING_AS_OF, price_per_mtok

ROOT = Path(__file__).resolve().parent.parent
PROVIDERS_DOC = ROOT / "docs" / "providers.md"
README = ROOT / "README.md"

MODEL = "claude-sonnet-5"
CHARS_PER_TOKEN = 4

# `| label | calls | input | output | $lo-$hi |`, with the en dash the docs actually use.
_ROW = re.compile(r"^\|\s*(?P<label>[^|]+?)\s*\|\s*(?P<calls>\d+)\s*\|"
                  r"\s*(?P<input>[\d,–—-]+|—)\s*\|\s*(?P<output>[\d,–—-]+|—)\s*\|"
                  r"\s*\*?\*?\$(?P<lo>[\d.]+)–\$(?P<hi>[\d.]+)\*?\*?\s*\|", re.M)


def _tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def measured_input_tokens() -> tuple:
    """Every operation's assembled system prompt, as tokens. This is what a call actually sends."""
    sizes = [_tokens(build_prompt(name, None)) for name in _OP_PROMPTS.values()]
    return min(sizes), max(sizes)


def measured_output_tokens() -> tuple:
    """Every reply this repository has actually captured -- the golden baselines are real API
    output, which is the closest thing to a ledger the offline suite can reach."""
    sizes = []
    for path in sorted((ROOT / "fixtures" / "golden").glob("*.runs.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        replies = list(data.get("runs") or []) + [b for b in (data.get("briefs") or []) if b]
        for turn in data.get("turns") or []:
            replies.extend(turn if isinstance(turn, list) else [turn])
        sizes.extend(_tokens(json.dumps(r, ensure_ascii=False)) for r in replies)
    assert sizes, "no golden captures were read -- an empty scan cannot support a published range"
    return min(sizes), max(sizes)


def _rows() -> dict:
    return {m.group("label"): m for m in _ROW.finditer(PROVIDERS_DOC.read_text(encoding="utf-8"))}


def _range(text: str) -> tuple:
    lo, hi = re.split(r"[–—-]", text.replace(",", ""))
    return int(lo), int(hi)


def test_the_documented_rate_and_its_date_are_the_rate_table():
    """The half that dates itself. Anthropic's price is not this project's to remember twice."""
    doc = PROVIDERS_DOC.read_text(encoding="utf-8")
    rate = price_per_mtok(MODEL)
    assert rate is not None, f"{MODEL} has no price on file -- the docs cannot quote one either"
    assert f"${rate[0]:.2f} / ${rate[1]:.2f} per million tokens" in doc, (
        f"the documented rate is not {rate} from pricing.py")
    assert PRICING_AS_OF in doc, f"the documented rate date is not PRICING_AS_OF ({PRICING_AS_OF})"


def test_the_documented_token_ranges_bracket_what_this_repository_measures():
    """A published range must be true of the prompts and replies actually in the tree. Bracketing
    rather than equality on purpose: the docs round to a readable figure, and rounding *outwards* is
    the only direction that keeps the claim honest."""
    row = _rows()["One provider call"]
    doc_in, doc_out = _range(row.group("input")), _range(row.group("output"))
    real_in, real_out = measured_input_tokens(), measured_output_tokens()
    assert doc_in[0] <= real_in[0] and doc_in[1] >= real_in[1], (
        f"documented input {doc_in} does not bracket the assembled prompts {real_in}")
    assert doc_out[0] <= real_out[0] and doc_out[1] >= real_out[1], (
        f"documented output {doc_out} does not bracket the captured replies {real_out}")


def _expected(calls: int) -> tuple:
    row = _rows()["One provider call"]
    (in_lo, in_hi), (out_lo, out_hi) = _range(row.group("input")), _range(row.group("output"))
    in_rate, out_rate = price_per_mtok(MODEL)
    lo = calls * (in_lo * in_rate + out_lo * out_rate) / 1_000_000
    hi = calls * (in_hi * in_rate + out_hi * out_rate) / 1_000_000
    return round(lo, 2), round(hi, 2)


def test_every_documented_dollar_figure_is_arithmetic_over_the_rate_table():
    """The rule this file exists for: a dollar figure is derived or it does not ship. Each row is
    checked against *its own* stated call count, so a row whose total does not follow from its own
    arithmetic is red -- which is what a hand-typed number looks like."""
    rows = _rows()
    assert len(rows) >= 4, f"the cost table lost rows -- found only {sorted(rows)}"
    for label, match in rows.items():
        calls = int(match.group("calls"))
        found = (float(match.group("lo")), float(match.group("hi")))
        assert found == _expected(calls), (
            f"row {label!r} claims {found} for {calls} call(s); the rate table gives "
            f"{_expected(calls)}")


def test_the_readme_states_a_cost_before_the_first_paid_command():
    """The figure has to be where the decision is made. `docs/providers.md` is the reference; the
    README is the page somebody reads before they set a key at all, so it carries the per-call
    number and the ceiling -- both derived, neither typed."""
    text = README.read_text(encoding="utf-8")
    per_call = _expected(1)
    assert f"${per_call[0]:.2f}" in text and f"${per_call[1]:.2f}" in text, (
        f"the README does not state the per-call range {per_call}")
    session = _rows()["A complete session, end to end"]
    ceiling = float(session.group("hi"))
    assert ceiling < 1.00, (
        f"the README claims a session stays under $1 and the table now says ${ceiling:.2f} -- fix "
        "the claim, not this assertion")
    assert "under $1" in text
