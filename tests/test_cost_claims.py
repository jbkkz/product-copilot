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
  is `build_prompt`'s system prompt plus, for every operation but the first discovery turn, a real
  resolved model a call sends as its own user message -- taken from every captured discovery reply in
  `fixtures/golden/` (both the single-pass `runs` and the per-turn `turns`), not guessed, and split by
  whether the assessment has absorbed its reasoning into the model yet (`_model_dump_tokens`). Output
  is the captured replies in `fixtures/golden/`. Edit a prompt, change what a generator sends, or
  re-capture a baseline far enough and the published range stops being true.
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
from requivo.core.contracts import Brief, EngineOutput
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


def _model_dump_tokens(*, absorbed: bool) -> list:
    """The size, in tokens, of a real resolved model exactly as a generator or a refinement turn
    sends it -- `out.model_dump_json()` in `generators.py`. Built from the same captured discovery
    replies `measured_output_tokens()` reads (`runs` **and** `turns`; an interactive capture like
    `training-budget.runs.json` carries only `turns`, and skipping it would silently drop the
    deepest, largest models a real refinement turn ever sends -- exactly the state this measurement
    exists to price), validated as the real `EngineOutput` contract rather than hand-assembled.

    Two populations, because a model looks different depending on when a call sees it:

    `absorbed=False` -- the model as every discovery turn but the first sees it, and as `advise`
    (the `brief` generator) sees it too: the reasoning layer is still empty, because the assessment
    that fills it (`absorb_reasoning`, services/discovery.py) has not run yet.

    `absorbed=True` -- the model as every OTHER generator sees it: `finalize_discovery` calls
    `absorb_reasoning(out, brief)` before any of them run, copying `advise()`'s own
    decisions/challenges/opportunities onto the model every later generator serializes. Built by
    applying that same copy to each captured `run`/`brief` pair -- there is no captured brief for a
    `turns` sequence (the golden harness runs no assessment mid-interactive-capture), so only `runs`
    contributes here."""
    sizes = []
    for path in sorted((ROOT / "fixtures" / "golden").glob("*.runs.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        runs = data.get("runs") or []
        briefs = data.get("briefs") or []
        if absorbed:
            for i, run in enumerate(runs):
                if i >= len(briefs):
                    continue
                out = EngineOutput.model_validate(run)
                brief = Brief.model_validate(briefs[i])
                out = out.model_copy(update={
                    "decisions": brief.decisions, "challenges": brief.challenges,
                    "opportunities": brief.opportunities,
                })
                sizes.append(_tokens(out.model_dump_json()))
        else:
            replies = list(runs)
            for turn_seq in data.get("turns") or []:
                replies.extend(t["model"] for t in turn_seq if isinstance(t, dict) and "model" in t)
            for reply in replies:
                sizes.append(_tokens(EngineOutput.model_validate(reply).model_dump_json()))
    assert sizes, "no golden captures were read -- an empty scan cannot support a published range"
    return sizes


def measured_input_tokens() -> tuple:
    """Every operation's assembled system prompt, plus what a real call actually adds on top of it as
    its own user message. Exactly one call has nothing to add -- the very first discovery turn --
    which is `analyze`'s bare system-prompt entry below. Every other call this system makes attaches
    a resolved model (or, for `estimate`, the stories the model was just decomposed into -- accepted
    as the same order of magnitude rather than measured separately, since no `Stories` reply is
    captured in `fixtures/golden/`). `advise` (`brief`) and a refinement turn (`analyze`, turn 2
    onward) both see the model before its reasoning layer is filled; every other generator sees it
    after. See `_model_dump_tokens` for exactly which captured replies back each of those two
    states."""
    bare = _model_dump_tokens(absorbed=False)
    absorbed = _model_dump_tokens(absorbed=True)
    sizes = []
    for op, name in _OP_PROMPTS.items():
        system = _tokens(build_prompt(name, None))
        if op == "analyze":
            sizes.append(system)                  # the very first turn: nothing to attach yet
            sizes.append(system + min(bare))       # every turn after it: the model so far, unreasoned
            sizes.append(system + max(bare))
        elif op == "brief":
            sizes.append(system + min(bare))       # advise() produces the reasoning; can't have it yet
            sizes.append(system + max(bare))
        else:
            sizes.append(system + min(absorbed))   # every later generator inherits advise()'s reasoning
            sizes.append(system + max(absorbed))
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
    number and the full-session range -- both derived, neither typed.

    The README used to assert a flat "under $1" ceiling instead of the derived range. That is a
    hand-typed claim wearing a derived number's clothes -- true only as long as nobody widens the
    table's own input range, and #404 is exactly a change that did. State the real figure instead, the
    same way the per-call range already is, so a future widening moves this text with it rather than
    quietly outdating it."""
    text = README.read_text(encoding="utf-8")
    per_call = _expected(1)
    assert f"${per_call[0]:.2f}" in text and f"${per_call[1]:.2f}" in text, (
        f"the README does not state the per-call range {per_call}")
    session_row = _rows()["A complete session, end to end"]
    session = _expected(int(session_row.group("calls")))
    assert f"${session[0]:.2f}" in text and f"${session[1]:.2f}" in text, (
        f"the README does not state the full-session range {session}")
