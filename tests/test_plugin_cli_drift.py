"""The plugin's CLI invocations, resolved against a *released* Requivo rather than this checkout.

`tests/test_plugin.py` compares the plugin to `src/requivo/` in the same working tree. That is a
checkout-against-checkout comparison and it cannot see the gap a user actually meets: a community
marketplace pins the plugin to a commit SHA that Anthropic's CI advances as commits land on `main`,
while `uv tool install requivo` gets the last **PyPI release**. The two artifacts drift by
construction between releases and nothing measured the gap (#96).

`scripts/plugin_cli_drift.py` is the measurement. Its logic is tested here rather than left in the
workflow shell, because a check that only exists in YAML is a check the next skill added drops
silently -- which is exactly what #93 was, for six skills.

The three states are the point, and the third one most of all. A released CLI that could not be
introspected -- PyPI unreachable, no release published, the install failed -- must not render as
`resolved` and must not render as `drift` either. The two have identical user-facing consequence and
opposite maintainer meanings, and an absence this leg produced is not an absence in the world.
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from plugin_cli_drift import (  # noqa: E402
    COULD_NOT_LOOK,
    DRIFT,
    PLUGIN_ROOT,
    RESOLVED,
    Surface,
    cli_surface,
    compare,
    invocation_sources,
    main,
    referenced_invocations,
    tree_typos,
)


def plugin_invocations():
    """Everything the real plugin executes: the six skills plus the shared preflight."""
    return referenced_invocations(invocation_sources(PLUGIN_ROOT))

# A stand-in for a released CLI. `status` deliberately takes no subcommands and `model` does, because
# the difference between those two is what decides whether a bare second word is a subcommand claim
# or an ordinary positional argument.
RELEASED = Surface(version="1.0.1", verbs={
    "status": None,
    "doctor": None,
    "model": {"apply", "show", "validate", "diff"},
    "session": {"init", "show", "list", "export", "import", "verify", "migrate"},
    "artifact": {"save", "show", "list"},
})

SKILL_FIXTURE = """
Run `requivo doctor --json` first.
Then `requivo status <slug> --json` to read it back.
Apply with `requivo model apply <slug> - --expected-revision N`.
"""


def _tree(**overrides):
    verbs = dict(RELEASED.verbs)
    verbs.update(overrides)
    return Surface(version="1.1.0.dev0", verbs=verbs)


# -- extraction -------------------------------------------------------------------


def test_referenced_invocations_reads_the_real_skills_and_finds_the_two_level_calls():
    """A positive control on the extractor. The plugin's whole contract is that Claude reasons and the
    deterministic CLI applies, so `requivo model apply` and `requivo artifact save` are the two calls
    that mutate anything -- an extractor that only saw top-level verbs would report full coverage
    while never looking at either of them."""
    found = plugin_invocations()
    assert found, "no invocations extracted -- this test would otherwise pass by having nothing to check"
    for expected in [("model", "apply"), ("model", "validate"), ("session", "init"),
                     ("artifact", "save"), ("status", None), ("doctor", None)]:
        assert expected in found, f"extractor missed {expected!r}; it found {sorted(found)}"
    # Every invocation names at least one file it came from, or a finding cannot be acted on.
    for inv, sources in found.items():
        assert sources, f"{inv!r} names no source file"


def test_the_shared_preflight_is_walked_and_not_only_the_skills():
    """`REASONING.md` holds the preflight every skill runs before its first `requivo` call, so a
    command named there executes on all six paths. It introduces no verb the skills do not already
    name, which is exactly the shape that rots quietly: leave it out and the day it stops being
    redundant is the day nothing notices."""
    sources = invocation_sources(PLUGIN_ROOT)
    names = [p.name for p in sources]
    assert "REASONING.md" in names, f"the preflight is not walked; walked {names}"
    assert names.count("SKILL.md") == 6, f"expected six skills, walked {names}"
    # And it must actually contribute -- a file that is walked but unreadable would look identical.
    from_preflight = referenced_invocations([PLUGIN_ROOT / "REASONING.md"])
    assert ("doctor", None) in from_preflight, (
        f"the preflight must name the probe it runs; extracted {sorted(from_preflight)}")


def test_a_flag_or_a_placeholder_is_not_read_as_a_subcommand(tmp_path):
    skills = tmp_path / "skills"
    (skills / "demo").mkdir(parents=True)
    (skills / "demo" / "SKILL.md").write_text(SKILL_FIXTURE, encoding="utf-8")
    found = referenced_invocations(invocation_sources(tmp_path))
    assert ("doctor", None) in found
    assert ("status", None) in found
    assert ("model", "apply") in found
    assert not any(sub in {"--json", "<slug>"} for _, sub in found)
    assert found[("model", "apply")] == ["demo"]


# -- comparison: the three states -------------------------------------------------


def test_a_plugin_that_resolves_is_resolved():
    referenced = {("status", None): ["status"], ("model", "apply"): ["answer"]}
    report = compare(referenced, tree=_tree(), released=RELEASED)
    assert report.state == RESOLVED, report
    assert report.findings == []
    assert report.checked == 2


def test_a_verb_the_release_does_not_have_is_drift():
    referenced = {("epic", None): ["brief"]}
    report = compare(referenced, tree=_tree(epic=None), released=RELEASED)
    assert report.state == DRIFT, report
    assert [f.invocation for f in report.findings] == ["requivo epic"]
    assert report.findings[0].sources == ["brief"]


def test_a_subcommand_the_release_does_not_have_is_drift():
    referenced = {("model", "rebase"): ["answer"]}
    report = compare(referenced, tree=_tree(model={"apply", "show", "validate", "diff", "rebase"}),
                     released=RELEASED)
    assert report.state == DRIFT, report
    assert report.findings[0].invocation == "requivo model rebase"


def test_a_release_that_dropped_a_verbs_subcommands_entirely_is_drift():
    """The case a released-side classifier cannot see. If the release kept `model` but removed its
    subcommand group, the top-level verb still resolves -- so `requivo model apply` would grade clean
    while being broken for every user. The tree is what says `apply` is a subcommand rather than a
    positional argument, and the tree is the same commit as the plugin, so it is the authority on what
    the plugin *meant*."""
    referenced = {("model", "apply"): ["answer"]}
    released = Surface(version="1.0.1", verbs=dict(RELEASED.verbs, model=None))
    report = compare(referenced, tree=_tree(), released=released)
    assert report.state == DRIFT, report
    assert "takes no subcommands" in report.findings[0].reason


def test_a_bare_word_the_tree_does_not_call_a_subcommand_is_an_argument_not_drift():
    """The false-positive guard. `status` takes no subcommands in the tree, so the word after it in
    `requivo status ready` is prose or a positional -- not a claim about the CLI surface. Flagging it
    would make this leg red for a sentence somebody wrote, which is the failure mode
    `plugin-validate.yml`'s header spends four paragraphs arguing against."""
    referenced = {("status", "ready"): ["status"]}
    report = compare(referenced, tree=_tree(), released=RELEASED)
    assert report.state == RESOLVED, report


def test_an_unreachable_release_is_could_not_look_and_is_neither_of_the_other_two():
    referenced = {("status", None): ["status"]}
    report = compare(referenced, tree=_tree(), released=None)
    assert report.state == COULD_NOT_LOOK, report
    assert report.state != RESOLVED and report.state != DRIFT
    assert report.detail, "could-not-look must say what it could not do"
    assert report.checked == 0


def test_an_empty_released_surface_is_could_not_look_not_wholesale_drift():
    """A CLI with zero verbs is not a measurement. Read as a surface it would report every single
    invocation as drift, which is a confident answer to a question nobody answered."""
    referenced = {("status", None): ["status"], ("model", "apply"): ["answer"]}
    report = compare(referenced, tree=_tree(), released=Surface(version="?", verbs={}))
    assert report.state == COULD_NOT_LOOK, report
    assert report.findings == []


def test_an_unreadable_tree_is_could_not_look_rather_than_a_verdict_about_the_release():
    referenced = {("status", None): ["status"]}
    report = compare(referenced, tree=None, released=RELEASED)
    assert report.state == COULD_NOT_LOOK, report
    assert "tree" in report.detail.lower()


def test_no_invocations_at_all_is_could_not_look_rather_than_a_clean_bill():
    """An empty extraction is the shape a broken skills path produces, and `all()` over an empty set is
    True -- so the honest answer is that nothing was checked, not that everything passed."""
    report = compare({}, tree=_tree(), released=RELEASED)
    assert report.state == COULD_NOT_LOOK, report


# -- the in-tree half: a misspelled subcommand -------------------------------------
#
# Two cases in one fixture on purpose. The clean one asserts that nothing fires on the real skills,
# and on its own it would pass just as happily against a harness that cannot see anything at all --
# so the case below it must fire, loudly, on the same code path.


def test_the_real_skills_name_no_subcommand_this_checkout_does_not_have():
    tree = cli_surface(sys.executable)
    assert tree is not None
    referenced = plugin_invocations()
    assert referenced, "nothing extracted -- the silence below would be the harness, not the skills"
    assert tree_typos(referenced, tree) == []


def test_a_misspelled_subcommand_is_caught_rather_than_dropped(tmp_path):
    """The gap `compare()` deliberately leaves. A skill writing `requivo model rebase` names a
    subcommand that does not exist anywhere -- neither the release nor this checkout -- and the drift
    comparison drops it, because from the released side it is indistinguishable from a positional
    argument. Here the tree can answer, so here it is asserted."""
    skills = tmp_path / "skills"
    (skills / "typo").mkdir(parents=True)
    (skills / "typo" / "SKILL.md").write_text(
        "Fix it up with `requivo model rebase <slug>` afterwards.", encoding="utf-8")
    tree = cli_surface(sys.executable)
    assert tree is not None
    findings = tree_typos(referenced_invocations(invocation_sources(tmp_path)), tree)
    assert [f.invocation for f in findings] == ["requivo model rebase"], findings
    assert findings[0].sources == ["typo"]
    assert "apply" in findings[0].reason, "the finding must name what the verb does offer"


def test_a_word_after_a_verb_with_no_subcommand_group_is_left_alone(tmp_path):
    """The other side of the same rule. `status` has no subcommand group, so the word after it is a
    positional or prose, and flagging it would redden the leg for a sentence somebody wrote."""
    skills = tmp_path / "skills"
    (skills / "prose").mkdir(parents=True)
    (skills / "prose" / "SKILL.md").write_text(
        "Then requivo status reports whether it is ready.", encoding="utf-8")
    tree = cli_surface(sys.executable)
    assert tree is not None
    assert tree_typos(referenced_invocations(invocation_sources(tmp_path)), tree) == []


# -- the entry point ---------------------------------------------------------------
#
# The units above are all called directly. That is not the same as the entry point calling them: a
# reviewer found `main()` reporting `resolved` and exiting 0 for a plugin naming a subcommand that
# exists nowhere, because `main()` ran `compare()` and never `tree_typos()`. Full coverage of a
# function and an entry point that never calls it look identical from outside, so `main()` gets its
# own cases -- a must-fire and a must-not-fire, as always.


def test_main_resolves_the_real_plugin_against_this_checkout():
    assert main(["--released-python", sys.executable]) == 0


def test_main_flags_a_subcommand_that_exists_nowhere_rather_than_reporting_resolved(tmp_path, capsys):
    """The regression the reviewer caught. `compare()` deliberately drops a bare word the tree does
    not call a subcommand, so this plugin's only defect is invisible to it; `main()` has to ask
    `tree_typos()` as well or it prints a clean bill over a broken invocation."""
    skill = tmp_path / "skills" / "typo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Fix it with `requivo model rebase <slug>`.", encoding="utf-8")

    code = main(["--released-python", sys.executable, "--plugin", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "requivo model rebase" in out, out
    # And the headline state must not contradict the list underneath it.
    assert "state         : drift" in out, out
    assert "resolves against the released CLI" not in out, out


def test_main_reports_could_not_look_rather_than_a_verdict_when_the_release_is_unreachable(tmp_path, capsys):
    code = main(["--released-python", str(ROOT / "no" / "such" / "python")])
    out = capsys.readouterr().out
    assert code == 3, out
    assert "could-not-look" in out
    assert "not a clean result" in out


def test_an_unreadable_skill_file_is_could_not_look_not_drift(tmp_path, capsys):
    """An unhandled exception exits 1, and 1 is the drift code -- so a `SKILL.md` this process cannot
    read would have been reported by the CI leg as "drift, annotated above" for a run that annotated
    nothing. A crash is could-not-look: the question was not answered and we know it was not."""
    skill = tmp_path / "skills" / "broken"
    skill.mkdir(parents=True)
    target = skill / "SKILL.md"
    target.write_text("requivo status", encoding="utf-8")
    target.chmod(0o000)
    if os.access(target, os.R_OK):        # root, or a filesystem that ignores the mode
        target.chmod(0o644)
        pytest.skip("this user can read a 0o000 file, so the unreadable case cannot be staged here. "
                    "UNTESTED ON THIS RUN: that an unreadable skill maps to could-not-look.")
    try:
        code = main(["--released-python", sys.executable, "--plugin", str(tmp_path)])
        captured = capsys.readouterr()
    finally:
        target.chmod(0o644)
    assert code == 3, captured
    assert "not evidence of drift" in captured.err, captured.err


def test_a_non_ascii_word_after_requivo_is_not_captured_at_all(tmp_path):
    """Python's word-character class is Unicode-aware by default, so without `re.ASCII` this captures
    a token that is then printed -- and on a Windows console at cp1252 that `print` raises
    `UnicodeEncodeError` and kills the process after the comparison was already done (invariant 16).
    Every real verb is ASCII, because they are argparse choices this project declares, so nothing is
    lost by refusing here."""
    skill = tmp_path / "skills" / "prose"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Le CLI requivo ecrit la session, et requivo ecrase le modele.",
                                    encoding="utf-8")
    ascii_only = referenced_invocations(invocation_sources(tmp_path))
    # The control, and note what it shows: ASCII prose after `requivo` IS captured, second token and
    # all. That fragility is inherited from the regex `tests/test_plugin.py` has always used, and it
    # is what `test_every_verb_the_plugin_names_exists_in_this_checkout` below exists to catch.
    assert {verb for verb, _ in ascii_only} == {"ecrit", "ecrase"}, ascii_only

    (skill / "SKILL.md").write_text("Le CLI requivo écrit la session.", encoding="utf-8")
    found = referenced_invocations(invocation_sources(tmp_path))
    assert found == {}, f"a non-ASCII token was captured and would be printed: {found}"


def test_every_verb_the_plugin_names_exists_in_this_checkout():
    """The first-token counterpart of `tree_typos`, and it covers a file the existing gate does not.

    `tests/test_plugin.py::test_skills_reference_only_real_cli_commands` makes this assertion, but
    only over `skills/*/SKILL.md`. This module also walks `REASONING.md`, so prose there such as
    "requivo requires an API key" would be captured as a verb named `requires`, sail past that gate,
    and be reported by the advisory leg as drift against the release -- a false positive in the one
    file the older test never opens. Asserted here, where the walked set is defined."""
    tree = cli_surface(sys.executable)
    assert tree is not None
    referenced = plugin_invocations()
    assert referenced, "nothing extracted -- a silent pass would be the harness, not the plugin"
    unknown = sorted({verb for verb, _ in referenced if verb not in tree.verbs})
    assert not unknown, (
        f"the plugin names {unknown}, which this checkout's CLI does not have. If that is prose "
        f"rather than a command, rewrite it: the extractor cannot tell them apart.")


def test_the_phantom_verb_guard_fires_on_prose(tmp_path):
    """The must-fire half of the case above. Without it, a guard that can no longer see anything
    reports the same clean result as a plugin with no phantom verbs."""
    (tmp_path / "REASONING.md").write_text(
        "Note that requivo requires an API key for provider verbs.", encoding="utf-8")
    (tmp_path / "skills").mkdir()
    tree = cli_surface(sys.executable)
    assert tree is not None
    referenced = referenced_invocations(invocation_sources(tmp_path))
    unknown = sorted({verb for verb, _ in referenced if verb not in tree.verbs})
    assert unknown == ["requires"], f"expected the phantom verb to be caught; got {referenced}"


# -- the probe --------------------------------------------------------------------


def test_the_probe_returns_none_rather_than_an_empty_surface_when_it_cannot_run():
    missing = str(ROOT / "no" / "such" / "python")
    assert cli_surface(missing) is None


def test_this_checkouts_own_cli_introspects():
    """The other half of the probe: it has to actually work somewhere, or the test above passes for a
    reason that has nothing to do with the code."""
    surface = cli_surface(sys.executable)
    assert surface is not None, "could not introspect this checkout's own CLI"
    assert "model" in surface.verbs and surface.verbs["model"], "expected `requivo model` to have subcommands"
    assert surface.verbs["status"] is None, "expected `requivo status` to take no subcommands"


def test_the_plugin_resolves_against_this_checkouts_cli():
    """Offline end-to-end, on the real files. This is the in-tree half of #96's question and it is what
    `tests/test_plugin.py` already asserts at the top level; asserting it here too keeps the drift
    script honest about a comparison whose answer we independently know."""
    surface = cli_surface(sys.executable)
    assert surface is not None
    report = compare(plugin_invocations(), tree=surface, released=surface)
    assert report.state == RESOLVED, [f"{f.invocation}: {f.reason}" for f in report.findings]


@pytest.mark.skipif(
    not os.environ.get("REQUIVO_RELEASED_PYTHON"),
    reason="needs a released Requivo already installed elsewhere: set REQUIVO_RELEASED_PYTHON to its "
           "interpreter. Not run by default because it needs network access to have happened. The CI "
           "leg in .github/workflows/plugin-validate.yml provisions one and runs the script directly.")
def test_the_plugin_resolves_against_a_released_cli_when_one_is_provisioned():
    released = cli_surface(os.environ["REQUIVO_RELEASED_PYTHON"])
    tree = cli_surface(sys.executable)
    assert tree is not None
    report = compare(plugin_invocations(), tree=tree, released=released)
    assert report.state != DRIFT, [f"{f.invocation}: {f.reason}" for f in report.findings]
