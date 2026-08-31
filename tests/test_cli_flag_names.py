"""The CLI flag names, and the error channel one of them was silently switching.

Two issues ship here as one unit -- both are about what a flag is *called* and what that name
implies to the code reading it.

#83 -- `epic --json` meant "also write a second export file", not "emit JSON on stdout" as it does
on every other verb. The name was half the problem. The other half is that `cli.app()` reads
`want_json = getattr(args, "json", False)` *generically* and uses that same attribute to switch
failures from prose-on-stderr to a structured envelope-on-stdout. So `epic --json` also changed how
failures were reported, while its two actual siblings -- `--github` and `--gitlab`, same shape,
same effect -- did not. Renaming the flag to `--export-json` removes the `json` attribute from
`epic`'s namespace, that `getattr` falls through to `False`, and all three export flags report a
failure the same way. The rename is the visible half; this file asserts the half that mattered.

#85 -- the context-card selector was spelled `--context` on two verbs and `--cards` on a third.
Both spellings now work everywhere; the dest is unchanged on each verb, so no handler moved.

#72 added the parser-shape tests at the foot of the file: which verbs the parser binds, what it does
with a verb it does not know, and that every command the docs promise is really there. Same subject
one level up -- what the CLI *is called*, read off the built parser rather than off the source.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
from pathlib import Path

import anthropic
import httpx
import pytest

from requivo.cli import _build_parser, app
from requivo.core import persistence as store
from requivo.core.contracts import EngineOutput, _schema_order, schema_slot_ids


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path


class _RaisingClient:
    """A client whose create() raises a transport error, which the provider wraps as an
    EngineError -- a RequivoError, and therefore the exact exception `app()` routes through
    `want_json`. The failure has to be the *same* one on all three flags for the comparison to
    mean anything, so it is raised unconditionally."""

    def __init__(self):
        self.messages = self

    def create(self, **kwargs):
        raise anthropic.APIConnectionError(
            message="boom", request=httpx.Request("POST", "https://api.anthropic.com"))


class _CannedClient:
    """A client that answers with one canned JSON reply -- enough for `epic` to reach its writers.
    Local on purpose: `tests/_fakes.py` carries the shared `FakeClient`, and this one is shaped for a
    single verb's failure path rather than for reuse. Until #72 this note read "the equivalent helper
    in test_engine.py is that module's fixture, and a test that reaches across modules for it breaks
    when the other module reorganises" -- that module was then split, which is the reorganisation the
    note was worried about, so the reason is restated against something that will not move."""

    class _Block:
        type = "text"

        def __init__(self, text):
            self.text = text

    class _Response:
        stop_reason = "end_turn"

        def __init__(self, text):
            self.content = [_CannedClient._Block(text)]

    def __init__(self, *replies):
        self._replies = list(replies)
        self.messages = self

    def create(self, **kwargs):
        return _CannedClient._Response(self._replies.pop(0))


def _session_with_a_model(slug):
    store.create_session(slug, f"request for {slug}")
    _, required = schema_slot_ids()
    model = {sid: {"completeness": 0, "confidence": "empty", "impact": "low"}
             for sid in _schema_order() if sid in required}
    model["problem"] = {"completeness": 80, "confidence": "explicit", "impact": "high"}
    store.save_revision(slug, EngineOutput.model_validate(
        {"model": model, "questions": [], "summary": {"objective": "A leave approval system"}}))
    return slug


def _run_capturing(argv, client):
    """Run `app()` and return (exit_code, stdout, stderr). `app()` raises SystemExit on every
    clean failure, so the code is part of the observation, not an accident of the harness."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            app(argv, client=client)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


def _walk_actions(parser):
    """Yield (verb path, action) for every argparse action reachable from the root parser."""
    stack = [(parser, ())]
    while stack:
        p, path = stack.pop()
        for action in p._actions:
            if isinstance(action, argparse._SubParsersAction):
                stack.extend((sub, (*path, name)) for name, sub in action.choices.items())
            else:
                yield " ".join(path), action


EXPORT_FLAGS = ("--export-json", "--github", "--gitlab")


def test_epic_export_flags_report_the_same_failure_identically():
    """The regression this issue is actually about. Before the rename, `--json` printed a JSON
    envelope on stdout and exited 1; `--github` and `--gitlab` printed prose on stderr and exited
    1. Same failure, two channels, chosen by a flag whose documented job was to write a file."""
    slug = _session_with_a_model("flagtest-epic-errors")

    results = {flag: _run_capturing(["epic", slug, flag], client=_RaisingClient())
               for flag in EXPORT_FLAGS}

    # Positive half first: the failure really happened and really said something. Without this the
    # identity assertion below would pass just as happily on three empty strings -- which is what a
    # harness that never reached the provider would produce.
    for flag, (code, out, err) in results.items():
        assert code == 1, f"{flag}: expected the clean-failure exit, got {code}"
        assert "Anthropic API unavailable" in err, f"{flag}: prose failure missing from stderr"
        assert out.strip() == "", f"{flag}: nothing should reach stdout, got {out[:120]!r}"

    # And now the identity itself.
    assert len(set(results.values())) == 1, (
        "the three export flags render the same provider failure differently: "
        + json.dumps({f: {"code": c, "stdout": o[:80], "stderr": e[:80]}
                      for f, (c, o, e) in results.items()}, indent=2))


def test_the_structured_envelope_is_still_reachable_on_a_verb_that_keeps_json():
    """The control for the assertion above. `epic` must NOT emit the envelope -- but the harness
    has to be able to see one when it is emitted, or `out.strip() == ""` is measuring nothing. A
    verb that keeps a real `--json` still routes a RequivoError through `e.to_dict()` on stdout."""
    code, out, err = _run_capturing(["session", "show", "no-such-session", "--json"], client=None)

    assert code == 1
    envelope = json.loads(out)                    # it really is JSON, on stdout
    assert envelope["code"] and envelope["message"]
    assert err.strip() == ""                      # and the prose channel stayed quiet


def test_epic_no_longer_accepts_the_old_json_spelling():
    """The break, asserted rather than assumed. `--json` is not a prefix of `--export-json`, so
    argparse rejects it outright (exit 2) instead of quietly meaning something new."""
    slug = _session_with_a_model("flagtest-epic-old-flag")
    code, _out, err = _run_capturing(["epic", slug, "--json"], client=_RaisingClient())

    assert code == 2                              # argparse's usage error, not a run that happened
    assert "--json" in err


def test_epic_export_json_still_writes_the_neutral_export():
    """The rename moved the name, not the behaviour: `--export-json` writes the same file that
    `--json` used to."""
    slug = _session_with_a_model("flagtest-epic-writes")
    epic = {"title": "X", "issues": [{"id": "I-1", "title": "Build the request form"}]}
    with contextlib.redirect_stdout(io.StringIO()):
        app(["epic", slug, "--export-json"], client=_CannedClient(json.dumps(epic)))

    written = store.canonical_dir(slug).joinpath("artifacts", "epic.json")
    assert json.loads(written.read_text(encoding="utf-8"))


def test_every_other_verb_that_declares_json_still_binds_it_to_the_json_dest():
    """The generic `getattr(args, "json", False)` in `app()` is the thing the rename must not
    disturb. Walk the parser: every verb that offers a `--json` option string must still land it on
    the `json` dest, and `epic` must offer none. A rename that moved a dest by accident -- or a
    later verb that spells its flag `--export-json` and expects an envelope -- fails here."""
    offenders, epic_json, verbs_with_json = [], [], []
    for verb, action in _walk_actions(_build_parser()):
        if "--json" in action.option_strings:
            verbs_with_json.append(verb)
            if action.dest != "json":
                offenders.append((verb, action.dest))
        if verb == "epic" and action.dest == "json":
            epic_json.append(action.option_strings)

    assert offenders == [], f"--json bound to a dest other than `json`: {offenders}"
    assert epic_json == [], f"epic still carries a `json` dest: {epic_json}"
    # The count is the "thirteen other verbs" from the issue, asserted as a floor rather than an
    # exact number so adding a verb does not fail this test for the wrong reason.
    assert len(verbs_with_json) >= 13, verbs_with_json


CARD_SELECTOR_VERBS = (
    (("discover", "a request"), "context"),
    (("session", "init", "a request"), "context"),
    (("context",), "cards"),
)


@pytest.mark.parametrize(("argv_head", "dest"), CARD_SELECTOR_VERBS)
@pytest.mark.parametrize("spelling", ["--context", "--cards"])
def test_both_spellings_of_the_card_selector_reach_the_same_dest(argv_head, dest, spelling):
    """`--context` is the documented primary; `--cards` is a permanent alias. Both are option
    strings on one argparse action, so they cannot drift apart -- and the dest is unchanged on
    each verb, so no handler had to move."""
    args = _build_parser().parse_args([*argv_head, spelling, "b2b-platform"])
    assert getattr(args, dest) == "b2b-platform"


def test_the_card_selector_is_one_action_not_two():
    """The alias must be a second option string on the *same* action, never a second argument.
    Two arguments would give the last one on the command line the win and silently drop the other,
    which is the failure mode this test exists to make impossible."""
    seen = {}
    for verb, action in _walk_actions(_build_parser()):
        if {"--context", "--cards"}.intersection(action.option_strings):
            seen.setdefault(verb, []).append(sorted(action.option_strings))

    for verb, actions in seen.items():
        assert actions == [["--cards", "--context"]], (
            f"{verb}: --context/--cards must be two option strings on one action, got {actions}")
    assert set(seen) == {"discover", "session init", "context", "session rescope"}, seen


def test_the_context_verb_prints_the_same_cards_under_either_spelling():
    """End-to-end on the one card-selecting verb that needs no provider, so the alias is proved
    against the real handler and not only against the parser."""
    def run(spelling):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            app(["context", spelling, "b2b-platform"], client=None)
        return buf.getvalue()

    printed = run("--cards")
    assert printed.strip()                      # it really printed a card, not an empty selection
    assert printed == run("--context")


# ── the `--json` perimeter (#102) ────────────────────────────────────────────
#
# `docs/compatibility.md` bounded this surface by naming six of fourteen outputs and justifying the
# six as "what the Claude Code plugin drives". That was wrong in both directions by three entries
# each, nothing tested it, and eight outputs sat in neither column — one of which #84 had already
# made a breaking change to. The perimeter is now the whole set, which is what makes this guard
# trivial: a subset would need the test to encode the boundary, and the boundary is what drifts.

_PROMISE_SECTION = "## The `--json` outputs are public"


def _json_verbs(parser: argparse.ArgumentParser, prefix: str = "") -> list[str]:
    """Every verb path that accepts `--json`, read off the built parser.

    From the parser and not from a grep of the source: a grep validates the reader's regex, and the
    thing being promised is what the command actually accepts.
    """
    found = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                path = f"{prefix} {name}".strip()
                if any("--json" in (a.option_strings or []) for a in sub._actions):
                    found.append(path)
                found += _json_verbs(sub, path)
    return found


def test_every_json_verb_is_inside_the_promise():
    """The page promises every `--json` output. This is what stops that being a sentence.

    Both directions are checked. A new verb with `--json` and no row is the case #84 walked into —
    a public output nobody promised, broken before anyone noticed there was no promise to break. A
    row for a verb that no longer takes `--json` is the mirror: a promise about something that
    cannot be observed, which reads as coverage this does not have.
    """
    verbs = sorted(_json_verbs(_build_parser()))
    # must fire: the walk really found the surface. An empty list would make every assertion below
    # vacuously true — `assert not []` is an all-clear nobody earned.
    assert len(verbs) >= 10, f"the parser walk looks blind: {verbs}"
    assert "doctor" in verbs and "session list" in verbs

    page = Path(__file__).resolve().parents[1] / "docs" / "compatibility.md"
    text = page.read_text(encoding="utf-8")
    start = text.index(_PROMISE_SECTION)
    section = text[start:text.index("\n## ", start + 1)]

    named = set(re.findall(r"`requivo ([a-z]+(?: [a-z]+)?)`", section))

    missing = sorted(v for v in verbs if v not in named)
    assert not missing, (
        "these verbs accept `--json` and are not named in the promise table, so their output is "
        f"public by accident rather than by decision: {missing}")

    # The other direction, and it is not symmetric hand-waving: a name in the table that the parser
    # does not produce is a promise about an output that does not exist.
    stale = sorted(n for n in named if n not in verbs)
    assert not stale, f"the promise table names verbs that do not take `--json`: {stale}"


# ── the parser's shape: which verbs exist at all ─────────────────────────────


def test_pc_parser_binds_every_subcommand():
    cases = {
        ("discover", "req"): "_cmd_discover",
        ("status", "m.json"): "_cmd_status",
        ("impact", "m.json"): "_cmd_impact",
        ("brief", "m.json"): "_cmd_brief",
        ("prd", "m.json"): "_cmd_prd",
        ("stories", "m.json"): "_cmd_stories",
        ("estimate", "m.json"): "_cmd_estimate",
        ("criteria", "m.json"): "_cmd_criteria",
        ("epic", "m.json"): "_cmd_epic",
        ("release", "m.json"): "_cmd_release",
    }
    for argv, fname in cases.items():
        assert _build_parser().parse_args(list(argv)).func.__name__ == fname
    assert _build_parser().parse_args(["epic", "m", "--github", "--gitlab"]).github
    assert _build_parser().parse_args(["release", "m", "v1.0"]).version == "v1.0"


def test_pc_unknown_command_errors():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["bogus"])


def test_documented_cli_commands_exist():
    # Guard doc/CLI drift: every top-level command the README and docs/cli.md promise must be a real
    # subcommand, so a rename or removal can't leave the docs pointing at a command that doesn't exist.
    import argparse

    parser = _build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    documented = {"discover", "answer", "status", "impact", "brief", "prd", "stories", "estimate",
                  "criteria", "epic", "release", "web", "demo", "doctor", "schema", "context",
                  "session", "model", "artifact"}
    missing = documented - set(sub.choices)
    assert not missing, f"documented CLI commands missing from the parser: {sorted(missing)}"


# ── every real flag is mentioned in docs/cli.md (#284, the inverse of #72's direction) ───────
#
# #72 (above) guards that a command docs/cli.md promises really exists in the parser. This is the
# other direction: a flag the parser actually binds and docs/cli.md never mentions is a flag a user
# hunting for it will not find -- the exact gap #284 was filed over (`session init --slug` and
# `--provider` existed and were absent from the reference).


def _real_flags(parser: argparse.ArgumentParser, prefix: str = ""):
    """Yield (verb path, action) for every argparse action that carries a real option string --
    `-h`/`--help` excluded, since every verb has it and documenting it would be noise."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from _real_flags(sub, f"{prefix} {name}".strip())
        elif set(action.option_strings) - {"-h", "--help"}:
            yield (prefix, action)


def test_every_real_flag_is_documented_in_the_cli_reference():
    """Read off the built parser, not off a hand-maintained list -- a hand-maintained list is what
    let `--provider` ship silently undocumented in the first place. Checked against the *long*
    option string only (`--output`, never `-o`): a short flag is one or two characters and is
    already, coincidentally, a substring of ordinary prose almost everywhere, so checking it would
    make the assertion pass whether or not anyone had actually written the flag down."""
    page = Path(__file__).resolve().parents[1] / "docs" / "cli.md"
    text = page.read_text(encoding="utf-8")

    checked, missing = 0, []
    for verb, action in _real_flags(_build_parser()):
        long_forms = [opt for opt in action.option_strings if opt.startswith("--")]
        candidates = long_forms or list(action.option_strings)
        checked += 1
        if not any(opt in text for opt in candidates):
            missing.append((verb or "(top level)", action.option_strings))

    # must fire: an empty walk would make the assertion below vacuously true.
    assert checked >= 30, f"the parser walk looks blind: only {checked} flag(s) found"
    assert not missing, (
        "these flags are real (the parser binds them) and appear nowhere in docs/cli.md, so a "
        f"user hunting for them will not find them: {missing}"
    )
