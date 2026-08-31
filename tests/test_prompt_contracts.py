"""Every prompt's Output-format example must be accepted by the contract its operation parses with.

The failure this file exists in order to catch (#266)
-----------------------------------------------------
Eight prompt assets each end with a `# Output format` section holding a worked JSON example, and
eight Pydantic contracts each define what a reply may contain. CLAUDE.md's "The output contract
(keep in sync)" lists the eight pairs, and until this file they were kept in agreement **by hand**.

The drift is invisible offline and expensive online. Every contract an LLM fills is
`extra="forbid"` (invariant 4), so a model that obeys a stale example -- a renamed key, a field the
contract dropped, a required field the example never shows -- produces a reply that
`ModelProposal`/`Brief`/... refuses. `_complete()` then retries twice with a corrective nudge and
raises `EngineError`: up to three times the call cost for one operation, paid per invocation, while
the offline suite stays green because nothing in it ever parses a prompt's example.

This guard is that missing parse. It is offline by construction -- it reads the assets and calls
`model_validate`; no client is built and no request is made.

Where the pairing comes from
----------------------------
Nowhere in this file. `_OP_PROMPTS` gives op -> prompt, and the contract is read **out of the
generator's own source**: each operation is exactly one `_complete(client, system, messages,
Contract, ...)` call, so `contract_for()` walks that function's AST and resolves the fourth
positional argument against the generators module. A hand-written pairing table here would be a
ninth thing to keep in sync -- this issue's own defect class, one layer up -- and it would validate
each prompt against the contract the *test* believes in rather than the one the code uses.

`analyze` is the one op that is not in `_GENERATORS`: it is the discovery turn, whose entry point is
`generators.run`, and `_OP_PROMPTS`'s own comment says so. It is named here rather than inferred, and
`test_every_reachable_operation_is_covered_by_this_guard` asserts the two tables together account for
every op, so an operation added to either one without the other goes red instead of going unscanned.

Placeholders
------------
`engine.md` cannot write a literal slot id -- the model fills whichever slots the request touches --
so it writes `<slot_id>` for both the key in `model` and the target of a question. Those are
substituted for a real required slot id before validation. The substitution is load-bearing rather
than cosmetic: `ModelProposal` checks every slot id against the schema vocabulary, so the raw example
is refused, which `test_the_placeholder_substitution_is_load_bearing` pins. `_PLACEHOLDERS` is
checked for deadness too -- an entry no prompt contains is a normalisation nobody needs any more, and
it would sit here forever looking like coverage.

What this guard cannot see
--------------------------
Stated rather than left to read as clean:

  - **an optional contract field the example never mentions.** `model_validate` enforces the required
    fields and refuses unknown ones; it says nothing about a field the contract added with a default
    and the prompt was never told about, which the model will then never fill. Not guarded on
    purpose: the three derived `id` fields (`Challenge`, `Opportunity`, `DesignDecision`) are
    legitimately absent from `brief.md`, and a coverage rule would go red on them today and force a
    prompt edit -- which costs a golden-harness capture -- for every future optional field.
  - **the prose around the JSON block.** Six of the eight assets close the fence and then add a
    "Required fields." paragraph restating some of the same constraints in words; a paragraph that
    contradicts its contract still reads as instruction to the model, and only the example is parsed
    here. Out of scope per #266.
  - **an element contract inside a list the example leaves empty.** Only `epic.md`'s `depends_on: []`
    is such a list today, and it holds plain strings.
  - **semantic quality.** Whether a prompt produces a *good* artifact is the golden harness's
    question (`scripts/golden_diff.py`), and it costs API calls.

The scan set (#10, and the reason this file repeats it)
--------------------------------------------------------
Every assertion below is a negative one, and `Path.glob` over a directory that has been renamed
returns `[]` rather than raising -- so "no offenders" over an empty scan is an all-clear nobody
earned. `scan_prompts()` therefore treats an unreadable or empty prompt root as an error.
`tests/test_boundaries.py` has the same rule and its own `scan()`; the two are deliberately not
shared, because that one walks `*.py` and pairs each file with the dotted package its relative
imports resolve against, none of which means anything for a Markdown asset. What is shared is the
rule, and it is restated here rather than imported so that this file fails on its own terms.
"""
from __future__ import annotations

import ast
import inspect
import json
import re
import textwrap
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError, create_model

from requivo.core.contracts import StrictModel, schema_slot_ids
from requivo.paths import PROMPTS
from requivo.providers.anthropic import generators
from requivo.providers.anthropic.generators import _GENERATORS, _OP_PROMPTS

# The discovery turn is not a generator: it produces the model rather than a view of one, so it has no
# entry in `_GENERATORS` and reaches the provider through `run()`. Named here, and checked against
# `_OP_PROMPTS` below, so this is a stated exception rather than a silently unscanned operation.
ANALYZE_OP = "analyze"
ANALYZE_ENTRY = generators.run

# A prompt whose absence means the scan is not looking at Requivo's prompt assets at all -- a moved
# `assets/` layout, a renamed package, a relocated test file. Two stable anchors rather than the full
# listing, so adding a prompt does not fail this file while a rename still does.
PROMPT_ANCHORS = ("engine.md", "brief.md")

# The token a prompt writes where it cannot write a real value, and what has to replace it before the
# contract will look at the example. Checked for deadness by
# `test_every_declared_placeholder_still_appears_in_a_prompt`: an entry no prompt uses is a
# normalisation with nothing to normalise, and it would sit here looking like coverage.
_PLACEHOLDERS = ("<slot_id>",)

_HEADING = re.compile(r"^# Output format[ \t]*$", re.M)
_FENCE = re.compile(r"^```json[^\n]*\n(.*?)^```", re.S | re.M)


def scan_prompts() -> list[Path]:
    """Every prompt asset, sorted. An empty result is an error rather than an answer (#10)."""
    if not PROMPTS.is_dir():
        raise AssertionError(
            f"the prompt-contract guard could not scan {PROMPTS}: no such directory. This is 'could "
            f"not look', not 'looked and found nothing' -- fix the path, never the assertion."
        )
    found = sorted(PROMPTS.glob("*.md"))
    if not found:
        raise AssertionError(
            f"the prompt-contract guard scanned {PROMPTS} and found no prompt assets. An empty scan "
            f"set cannot support a 'no drift' verdict."
        )
    return found


def generator_for(op: str):
    """The function that issues `op`'s one provider call."""
    return ANALYZE_ENTRY if op == ANALYZE_OP else _GENERATORS[op]


def contract_for(op: str) -> type[BaseModel]:
    """The contract `op` actually parses its reply with, read out of the generator's own source.

    Derived rather than declared: a table in this file would be a ninth hand-synced pairing, and it
    would let a generator switched to a different contract stay green here while every real call
    retried and failed.
    """
    fn = generator_for(op)
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_complete"
    ]
    if len(calls) != 1:
        raise AssertionError(
            f"{op}: expected exactly one `_complete(...)` call in {getattr(fn, '__name__', fn)}(), "
            f"found {len(calls)}. The contract cannot be derived, which is 'could not look' -- fix "
            f"the derivation or the generator, never this assertion."
        )
    args = calls[0].args
    if len(args) < 4 or not isinstance(args[3], ast.Name):
        raise AssertionError(
            f"{op}: {getattr(fn, '__name__', fn)}() does not pass its contract as the fourth "
            f"positional argument to `_complete(...)`, so this guard cannot tell which contract the "
            f"reply is parsed with."
        )
    contract = getattr(generators, args[3].id, None)
    if not (isinstance(contract, type) and issubclass(contract, BaseModel)):
        raise AssertionError(
            f"{op}: {getattr(fn, '__name__', fn)}() passes {args[3].id!r} to `_complete(...)`, which "
            f"is not a Pydantic contract reachable from the generators module."
        )
    return contract


def output_format_example(op: str, text: str) -> str:
    """The raw JSON of `text`'s Output-format example.

    Three distinct refusals rather than one, because they call for three different repairs and a
    single "could not find an example" would hide which. This is the must-fire control #266 asks
    for: a prompt restructured so that its example moves or disappears must turn this file red
    rather than quietly stop being checked.
    """
    heading = _HEADING.search(text)
    if heading is None:
        raise AssertionError(
            f"{_OP_PROMPTS[op]} has no `# Output format` section, so {op}'s reply shape is documented "
            f"nowhere this guard can read. A prompt that stops carrying an example stops being "
            f"checked against its contract, which is the drift #266 is about."
        )
    fence = _FENCE.search(text[heading.end():])
    if fence is None:
        raise AssertionError(
            f"{_OP_PROMPTS[op]} has an `# Output format` section with no ```json fence under it. The "
            f"example is what this guard validates; prose alone cannot be parsed."
        )
    return fence.group(1)


def substitute_placeholders(value: Any, slot_id: str) -> Any:
    """Replace every placeholder token in keys and string values, recursively."""
    if isinstance(value, dict):
        return {
            substitute_placeholders(k, slot_id): substitute_placeholders(v, slot_id)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [substitute_placeholders(v, slot_id) for v in value]
    if isinstance(value, str):
        for token in _PLACEHOLDERS:
            value = value.replace(token, slot_id)
        return value
    return value


def sample_slot_id() -> str:
    """A real, required slot id -- deterministic so a failure message is reproducible."""
    _, required = schema_slot_ids()
    return sorted(required)[0]


def example_for(op: str, *, normalised: bool = True) -> Any:
    """`op`'s Output-format example, parsed, with placeholders resolved unless asked otherwise."""
    text = (PROMPTS / _OP_PROMPTS[op]).read_text(encoding="utf-8")
    raw = output_format_example(op, text)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"{_OP_PROMPTS[op]}'s Output-format example is not valid JSON ({exc}). The prompt tells "
            f"the model to reply with 'only a valid JSON object'; the example has to be one."
        ) from exc
    return substitute_placeholders(parsed, sample_slot_id()) if normalised else parsed


# --------------------------------------------------------------------------------------------------
# The scan set itself: "could not look" must never render as "looked and found nothing".
# --------------------------------------------------------------------------------------------------

def test_the_guard_scans_the_real_prompt_assets():
    """Name what was scanned. Everything below this line rests on the assets actually being here."""
    names = sorted(p.name for p in scan_prompts())
    missing = [anchor for anchor in PROMPT_ANCHORS if anchor not in names]
    assert not missing, (
        f"the prompt-contract guard scanned {PROMPTS} and did not find {missing}; it is not looking "
        f"at Requivo's prompt assets. Scanned: {names}"
    )


def test_the_guard_refuses_a_scan_it_could_not_make(monkeypatch, tmp_path):
    """The positive control for #10: a renamed assets tree must be an error, not an all-clear. The
    directory is absent, so `glob` returns [] and every negative assertion in this file would hold
    while checking nothing."""
    renamed_away = tmp_path / "prompts"
    assert list(renamed_away.glob("*.md")) == [], "the shape being guarded against: glob returns [], not an error"
    monkeypatch.setattr(f"{__name__}.PROMPTS", renamed_away)
    with pytest.raises(AssertionError, match="no such directory"):
        scan_prompts()


def test_the_guard_refuses_an_empty_prompt_directory(monkeypatch, tmp_path):
    """The other shape of the same hole: the directory resolves, and holds nothing."""
    empty = tmp_path / "prompts"
    empty.mkdir()
    monkeypatch.setattr(f"{__name__}.PROMPTS", empty)
    with pytest.raises(AssertionError, match="no prompt assets"):
        scan_prompts()


def test_every_reachable_operation_is_covered_by_this_guard():
    """`_OP_PROMPTS` is the registry of every operation that sends a prompt; `_GENERATORS` plus the one
    named discovery entry point is the registry of what issues each call. They have to describe the
    same set: an op in one and not the other is either a prompt nothing sends or a paid call this
    guard never validates, and both are silent today."""
    assert set(_OP_PROMPTS) == {ANALYZE_OP} | set(_GENERATORS), (
        f"_OP_PROMPTS covers {sorted(_OP_PROMPTS)} while the generators (plus {ANALYZE_OP!r}) cover "
        f"{sorted({ANALYZE_OP} | set(_GENERATORS))}. An operation in only one of them is unscanned."
    )


def test_every_prompt_asset_belongs_to_an_operation():
    """The scan set and the registry must account for each other in both directions. A prompt file no
    operation names is dead weight that still reads as maintained; an op naming a file that is not
    there fails `build_prompt` at call time, on a paid path."""
    on_disk = {p.name for p in scan_prompts()}
    registered = set(_OP_PROMPTS.values())
    assert on_disk == registered, (
        f"prompt assets on disk {sorted(on_disk)} and prompts named by _OP_PROMPTS "
        f"{sorted(registered)} disagree; unclaimed: {sorted(on_disk - registered)}, missing: "
        f"{sorted(registered - on_disk)}"
    )


def test_every_declared_placeholder_still_appears_in_a_prompt():
    """A placeholder no prompt writes is a normalisation with nothing to normalise. Left in place it
    reads like coverage, and the next person adding a token cannot tell which entries are live."""
    text = "".join(p.read_text(encoding="utf-8") for p in scan_prompts())
    dead = [token for token in _PLACEHOLDERS if token not in text]
    assert not dead, (
        f"these placeholders are declared here and written by no prompt: {dead}. Either a prompt "
        f"stopped using one (drop it) or it was renamed (update it) -- do not leave it."
    )


# --------------------------------------------------------------------------------------------------
# The guard itself: every example, against the contract its own generator parses with.
# --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("op", sorted(_OP_PROMPTS))
def test_the_output_format_example_validates_against_its_contract(op):
    """The whole point. A drift here costs three paid calls and an `EngineError` per invocation, and
    nothing else in this suite can see it."""
    contract = contract_for(op)
    try:
        contract.model_validate(example_for(op))
    except ValidationError as exc:
        raise AssertionError(
            f"{_OP_PROMPTS[op]}'s Output-format example is refused by {contract.__name__}, the "
            f"contract {op!r} replies are parsed with. A model obeying this example produces a reply "
            f"`_complete()` retries twice and then fails on -- three paid calls per invocation. Fix "
            f"whichever of the two moved; a prompt edit also owes a golden-harness capture.\n{exc}"
        ) from exc


def test_every_operation_resolves_to_a_contract_an_llm_may_fill():
    """The derivation is the load-bearing half of this file, so assert something about what it found
    rather than counting it.

    Counting was the first version of this test and it could not fail: `contract_for` either raises
    or returns a contract, so a comprehension over the unique keys of `_OP_PROMPTS` holds exactly
    that many entries whenever the assertion is reached at all. What is genuinely worth asserting is
    narrower than `contract_for`'s own `BaseModel` check -- invariant 4 says everything an LLM fills
    inherits `StrictModel`, and it is that base's `extra="forbid"` which turns a drifted example into
    a loud refusal instead of a silently trimmed reply. A contract that lost it would leave every row
    of the table above passing while checking much less than this file claims to.
    """
    resolved = {op: contract_for(op) for op in sorted(_OP_PROMPTS)}
    permissive = sorted(op for op, contract in resolved.items() if not issubclass(contract, StrictModel))
    assert not permissive, (
        f"these operations parse replies with a contract that is not a StrictModel: {permissive}. "
        f"Without extra=forbid a key the prompt no longer asks for is dropped rather than refused, "
        f"so validating an example against it proves much less than it appears to. Resolved: "
        f"{ {op: contract.__name__ for op, contract in resolved.items()} }"
    )


# --------------------------------------------------------------------------------------------------
# Positive controls. "No drift" also passes when the check could not fire.
# --------------------------------------------------------------------------------------------------

def test_a_renamed_key_in_an_example_is_refused():
    """#266's own validation step: rename `headline` in the brief example and the guard must go red.
    Without this, an extractor that quietly returned an empty object would report every prompt
    clean."""
    example = example_for("brief")
    challenge = example["challenges"][0]
    challenge["header"] = challenge.pop("headline")
    with pytest.raises(ValidationError):
        contract_for("brief").model_validate(example)


def test_a_required_contract_field_the_example_never_fills_is_refused():
    """The other direction of the same drift: a contract that gains a required field the prompt was
    never told about. Modelled as a subclass rather than by editing `contracts.py`, so the control
    exercises the check without moving the thing being checked."""
    extended = create_model(
        "ReleaseNotesWithNewRequiredField",
        __base__=contract_for("release"),
        audience=(str, ...),
    )
    with pytest.raises(ValidationError):
        extended.model_validate(example_for("release"))


def test_the_placeholder_substitution_is_load_bearing():
    """`<slot_id>` is not a slot the schema defines, so the raw engine example must be refused and the
    substituted one accepted. A substitution that had silently become a no-op would leave the analyze
    row of the table above passing for the wrong reason -- or failing for one."""
    contract = contract_for(ANALYZE_OP)
    with pytest.raises(ValidationError):
        contract.model_validate(example_for(ANALYZE_OP, normalised=False))
    contract.model_validate(example_for(ANALYZE_OP))


def test_the_extractor_refuses_a_prompt_with_no_output_format_section():
    """A prompt restructured so its example moves must go red rather than stop being checked."""
    with pytest.raises(AssertionError, match="no `# Output format` section"):
        output_format_example("brief", "# Role\n\nAdvise on the model.\n")


def test_the_extractor_refuses_an_output_format_section_with_no_json_fence():
    """Prose under the heading is not an example. Separate from the case above because the repair is
    different, and one message covering both would name neither."""
    with pytest.raises(AssertionError, match="no ```json fence"):
        output_format_example("brief", "# Output format\n\nReply with a JSON object.\n")


def test_the_extractor_reads_the_fence_under_the_heading_not_the_first_in_the_file():
    """Prompts carry illustrative fenced blocks above their Output-format section. Anchoring on the
    heading is what makes this guard read the reply shape; a whole-file search would validate a
    snippet, and would then report a clean pass for a prompt whose real example had drifted."""
    text = (
        '# Role\n\n```json\n{"illustration": true}\n```\n\n'
        '# Output format\n\n```json\n{"title": "x"}\n```\n'
    )
    assert json.loads(output_format_example("release", text)) == {"title": "x"}


def test_the_strictness_check_fires_on_a_permissive_contract(monkeypatch):
    """The positive control for the test above, which asserts a negative. `contract_for` only checks
    `BaseModel`, so a contract that dropped `StrictModel` resolves fine and reaches the assertion --
    which must then fail rather than shrug."""
    permissive = create_model("PermissiveBrief", __base__=BaseModel, problem=(str, ""))
    monkeypatch.setattr(generators, "Brief", permissive)
    with pytest.raises(AssertionError, match="not a StrictModel"):
        test_every_operation_resolves_to_a_contract_an_llm_may_fill()


def test_the_derivation_refuses_a_generator_it_cannot_read(monkeypatch):
    """`contract_for` must fail loudly when the shape it reads changes, not fall back to a guess. A
    generator that stopped passing its contract positionally would otherwise silently unscan its
    prompt -- the same all-clear-nobody-earned this file exists to remove."""
    def not_a_generator(client, out):
        return None

    monkeypatch.setitem(_GENERATORS, "brief", not_a_generator)
    with pytest.raises(AssertionError, match="expected exactly one"):
        contract_for("brief")
