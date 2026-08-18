"""Selector guards — an empty or unmatched token must never widen to everything or empty to nothing.

Issue #13. Three selectors — `resolve_cards`, `load_context` and `resolve_slots` — each turned an
absence into a confident answer, in opposite directions: an empty token matched every label, and a
selection that resolved to nothing became either *all of them* or *none of them*. Both render
exactly like a precise answer to a specific query, which is what makes them expensive.

**Every "must not widen" assertion here is paired with a "must fire" one in the same test.** A
negative assertion passes when nothing at all happens: if no context card could be read and the slot
schema were unreachable, every refusal below would pass for the wrong reason and report a coverage it
does not have. `test_harness_can_see_both_vocabularies` is the loud half — it fails rather than
skips when the fixture is blind.
"""

from __future__ import annotations

import pytest

from requivo.core.context import available_cards, build_prompt, check_selection, load_context, resolve_cards
from requivo.core.dependencies import _all_slot_ids, resolve_slots
from requivo.core.errors import EmptySelectorTokenError, RequivoError, UnknownContextCardError
from requivo.core.selectors import normalize_tokens

A_CARD = "b2b-platform"          # a bundled card, committed to the repo
ANOTHER_CARD = "financial-reporting"


# ── the positive control ─────────────────────────────────────────────────────────


def test_harness_can_see_both_vocabularies():
    """The silence half of every test below is only meaningful if the fixture can see something.

    If the bundled cards or the slot schema were unreachable, "an unknown card raises" would pass
    because *every* card is unknown, and "an empty token does not return all 15 slots" would pass
    because there are no slots to return. This fails loudly in that case rather than letting the
    other tests report a coverage they do not have."""
    cards = available_cards()
    assert A_CARD in cards and ANOTHER_CARD in cards, f"fixture is blind: cards={cards}"
    assert len(_all_slot_ids()) > 1, "fixture is blind: the slot schema resolved to fewer than 2 slots"
    assert load_context().strip(), "fixture is blind: load_context() with no selection read nothing"


# ── 1. an empty slot token resolved to every slot ────────────────────────────────


def test_empty_slot_token_is_refused_rather_than_matching_every_label():
    """`"" in label` is true for every label, so a stray empty token reported the entire model as
    changed with **zero** unmatched tokens — a total widening that reads as a precise answer."""
    # must fire: a real selection still works, and an unmatched token is still reported as unmatched
    assert resolve_slots(["workflow"]) == (["workflow"], [])
    assert resolve_slots(["permission"]) == (["permissions"], [])   # label substring still resolves
    assert resolve_slots(["workflow", "zzz"]) == (["workflow"], ["zzz"])

    # must not fire: an empty or whitespace token is a refusal, not a match against everything
    for token_list in ([""], ["  "], ["\t"], ["workflow", ""]):
        with pytest.raises(EmptySelectorTokenError):
            resolve_slots(token_list)


def test_an_empty_slot_token_does_not_report_the_whole_model_as_changed():
    """The two reachable shapes. `requivo impact <slug> ""` is what an unset shell variable expands
    to and reaches `resolve_slots` as `[""]`; a caller splitting a comma-separated value reaches it as
    a trailing `""`. Before the guard, both resolved to every slot in the schema with an empty
    unmatched list — a blast radius covering the whole model, reported as if it were a precise
    answer."""
    every = _all_slot_ids()
    assert len(every) > 1                       # must fire: there is an "everything" to widen to
    assert resolve_slots(["workflow"])[0] != sorted(every)   # must fire: a real query is narrow

    with pytest.raises(EmptySelectorTokenError) as ei:
        resolve_slots([""])                     # argv: requivo impact <slug> ""
    assert ei.value.details["position"] == 0
    with pytest.raises(EmptySelectorTokenError) as ei:
        resolve_slots("workflow,".split(","))   # a caller splitting a comma-separated value
    assert ei.value.details["position"] == 1    # names *which* token, not just that one was bad


# ── 2. a card selection that no longer resolves yielded zero product context ─────


def test_load_context_refuses_a_selection_that_matched_nothing():
    """`load_context` filtered by stem and joined; a name that matched nothing silently produced the
    empty string, and `build_prompt` spliced that into `{{CONTEXT}}` on every later turn."""
    # must fire: a real selection loads that card and only that card
    picked = load_context([A_CARD])
    assert f"## {A_CARD}" in picked and picked.strip()
    assert f"## {ANOTHER_CARD}" not in picked
    assert len(load_context()) > len(picked)    # the unrestricted load is still wider

    # must not fire: a name that resolves to nothing is a refusal, not an empty context
    with pytest.raises(UnknownContextCardError) as ei:
        load_context(["nonexistent-card"])
    assert ei.value.details["unknown"] == ["nonexistent-card"]

    # a partial miss is still a miss — the cards that *did* resolve must not mask the one that did not
    with pytest.raises(UnknownContextCardError) as ei:
        load_context([A_CARD, "nonexistent-card"])
    assert ei.value.details["unknown"] == ["nonexistent-card"]


def test_load_context_refuses_an_empty_selection_and_an_empty_token():
    # must fire: no selection at all still means every card, which is the documented contract
    assert load_context(None).strip()
    # must not fire: a selection object that selects nothing is not the same as no selection
    with pytest.raises(EmptySelectorTokenError) as ei:
        load_context([])
    assert ei.value.details == {"selector": "context card", "tokens": 0}
    assert isinstance(ei.value, RequivoError)   # rides the same envelope as every clean failure
    with pytest.raises(EmptySelectorTokenError):
        load_context([""])


def test_both_card_selectors_echo_an_unknown_name_as_the_caller_typed_it():
    """`resolve_cards` and `load_context` are one design and their errors are read side by side, so
    the name in `details["unknown"]` is the one the caller wrote — not the lower-cased key it was
    matched by. `load_context` built that list from the normalized tokens and reported `some-card`
    for a typed `Some-Card`."""
    # must fire: a real name still resolves through both, so this is about the echo and not the match
    assert resolve_cards([f" {A_CARD.upper()} "]) == [A_CARD]      # matching stays case-insensitive
    assert f"## {A_CARD}" in load_context([A_CARD.upper()])

    for selector in (resolve_cards, load_context):
        with pytest.raises(UnknownContextCardError) as ei:
            selector(["  Some-Card  "])
        assert ei.value.details["unknown"] == ["Some-Card"], f"{selector.__name__} lower-cased the echo"


def test_a_persisted_card_selection_is_visible_when_the_card_is_gone(tmp_path, monkeypatch):
    """The reported scenario. A session created on machine A with a card from the user context
    directory, opened on machine B where that card does not exist — or after it is renamed. The
    selection is read back out of `session.json` on every turn and `resolve_cards` (the guard) ran
    once, at creation, so nothing else was ever going to notice."""
    user_dir = tmp_path / "user-cards"
    user_dir.mkdir()
    (user_dir / "acme-crm.md").write_text("ACME CRM — the product context this session was built on.")
    monkeypatch.setenv("REQUIVO_CONTEXT_DIR", str(user_dir))

    # machine A: the card is there, the selection loads it
    assert "ACME CRM" in load_context(["acme-crm"])

    # machine B: the same session.json, no such card. That must be visible, not an empty string.
    monkeypatch.setenv("REQUIVO_CONTEXT_DIR", str(tmp_path / "no-user-cards"))
    with pytest.raises(UnknownContextCardError):
        load_context(["acme-crm"])


def test_build_prompt_never_silently_substitutes_an_empty_context():
    """The consequence that costs money: every provider call assembles its system prompt through
    `build_prompt`, so a stale selection reasoned with no product context at all — losing the
    `information_value = uncertainty x impact` driver — and was billed for it."""
    # must fire: a live selection reaches {{CONTEXT}}
    prompt = build_prompt("engine.md", [A_CARD])
    assert f"## {A_CARD}" in prompt and "{{CONTEXT}}" not in prompt
    # must not fire: a dead selection is a refusal, never a prompt with an empty context
    with pytest.raises(UnknownContextCardError):
        build_prompt("engine.md", ["nonexistent-card"])


# ── 3. resolve_cards had the same door ───────────────────────────────────────────


def test_resolve_cards_refuses_an_empty_token_instead_of_returning_all_cards():
    """`if not key: continue` dropped empty tokens, and a selection of *only* empty tokens then hit
    `return picked or None` — None, which every reader spells "every card". The widening the
    function's own docstring says it closes, reached through the empty-token entrance."""
    # must fire: the documented contract is unchanged
    assert resolve_cards([A_CARD, f" {ANOTHER_CARD}"]) == [A_CARD, ANOTHER_CARD]
    assert resolve_cards([]) is None                    # no selection at all == every card
    with pytest.raises(UnknownContextCardError) as ei:
        resolve_cards([A_CARD, "nope"])
    assert ei.value.details["unknown"] == ["nope"]

    # must not fire: an empty token no longer buys every card
    for token_list in ([""], [" "], [A_CARD, ""]):
        with pytest.raises(EmptySelectorTokenError):
            resolve_cards(token_list)


# ── the shared rule ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("selector, good", [
    pytest.param(resolve_cards, [A_CARD], id="resolve_cards"),
    pytest.param(load_context, [A_CARD], id="load_context"),
    pytest.param(resolve_slots, ["workflow"], id="resolve_slots"),
])
def test_every_selector_refuses_an_empty_token(selector, good):
    """One rule, one helper, three sites — so a fourth selector inherits it rather than re-deriving
    it. Stated behaviourally: whatever the selector is, an empty token is a refusal."""
    assert good[0] in repr(selector(good)), "must fire: the selector resolves a real token"
    with pytest.raises(EmptySelectorTokenError):
        selector([""])
    with pytest.raises(EmptySelectorTokenError):
        selector(["   "])


def test_the_refusal_is_a_structured_error_every_surface_can_render():
    """It rides the same envelope as every other clean failure: a stable code for `--json` and
    Claude Code, a 400 in the Web, one line in the CLI — never a traceback."""
    with pytest.raises(EmptySelectorTokenError) as ei:
        normalize_tokens(["ok", " "], what="context card")
    envelope = ei.value.to_dict()
    assert envelope["code"] == "empty_selector_token"
    assert envelope["details"] == {"selector": "context card", "position": 1}
    assert "context card" in envelope["message"]
    assert isinstance(ei.value, RequivoError)


def test_a_selector_handed_a_generator_does_not_silently_resolve_to_nothing():
    """The same absence, one layer down. `normalize_tokens` iterates the tokens it is given; a
    selector that then zips them against its own second pass would pair nothing if the caller handed
    in a generator, and return "no slots, no complaint" — this bug's own shape, introduced by fixing
    it. Both selectors that take a positional token list materialise first."""
    # must fire: a list still resolves, so the assertion below is about the generator and not the token
    assert resolve_slots(["workflow"]) == (["workflow"], [])
    assert resolve_slots(t for t in ["workflow"]) == (["workflow"], [])
    assert resolve_cards(t for t in [A_CARD]) == [A_CARD]
    # must not fire: the refusal still reaches a generator
    with pytest.raises(EmptySelectorTokenError):
        resolve_slots(t for t in ["workflow", ""])


def test_normalize_tokens_passes_real_tokens_through_stripped_and_lowercased():
    """The must-fire half of the helper: it is a normalizer, not only a refusal."""
    assert normalize_tokens([" Workflow ", "PERMISSIONS"], what="slot") == ["workflow", "permissions"]
    assert normalize_tokens([], what="slot") == []      # an empty list is not an empty token


# ── asking the same question without paying for it (#12) ─────────────────────────


def test_check_selection_answers_exactly_what_load_context_would_do():
    """`load_context` refuses a selection that no longer resolves — correct, and discovered at the
    next provider call, which costs money and happens minutes into a session. `check_selection` is
    that same guard asked as a question: it reports rather than raises, so `doctor` and
    `session verify` can answer it for free and in advance.

    The two must never drift, which is why `check_selection` runs the guard rather than
    reimplementing the rule. Both halves are asserted here: the selections that pass, and the ones
    that do not."""
    # must not fire — these are the selections that still load
    assert check_selection(None) is None            # None is the "every card" sentinel, not a selection
    assert check_selection([A_CARD]) is None
    assert check_selection([A_CARD, ANOTHER_CARD]) is None
    assert check_selection([A_CARD.upper()]) is None        # matched case-insensitively, like the loader

    # must fire — and it reports, it does not raise
    unknown = check_selection([A_CARD, "no-such-card"])
    assert isinstance(unknown, UnknownContextCardError)
    assert unknown.details["unknown"] == ["no-such-card"]
    assert unknown.to_dict()["code"] == "unknown_context_card"

    empty = check_selection([])
    assert isinstance(empty, EmptySelectorTokenError), (
        "a persisted selection of nothing is refused by load_context; the checker must say so too")
    assert isinstance(check_selection([" "]), EmptySelectorTokenError)


@pytest.mark.parametrize("selection", [None, [A_CARD], [A_CARD, ANOTHER_CARD], ["no-such-card"], []])
def test_check_selection_agrees_with_load_context_on_every_selection(selection):
    """The drift guard itself: for each selection, `check_selection` returning None must mean
    `load_context` succeeds, and returning a problem must mean it raises that same code. A checker
    that answers a slightly different question than the thing it checks is the defect this issue is
    about, one level up."""
    problem = check_selection(selection)
    try:
        load_context(selection)
    except RequivoError as e:
        assert problem is not None, f"load_context refused {selection!r} and check_selection did not"
        assert problem.code == e.code
    else:
        assert problem is None, f"check_selection refused {selection!r} and load_context did not"
