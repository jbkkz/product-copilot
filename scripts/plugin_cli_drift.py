"""Resolve the plugin's `requivo` invocations against a RELEASED CLI, not against this checkout.

Why this exists (#96)
---------------------
`tests/test_plugin.py::test_skills_reference_only_real_cli_commands` introspects
`requivo.cli._build_parser()` from the working tree and asserts every `requivo <verb>` a skill
mentions is in it. That is a checkout-against-checkout comparison, and it is green by construction:
the plugin and the parser it is checked against are the same commit.

What a user installs is two artifacts that were never compared. The plugin comes from a marketplace
listing, and a community listing pins it to a commit SHA that Anthropic's CI advances as commits land
on `main` -- so the plugin a stranger installs is routinely `main`. The CLI comes from
`uv tool install requivo`, which is the last PyPI release. Between releases the two drift by
construction, and until this script nothing measured the gap.

The three states, and the third one is the point
------------------------------------------------
  resolved        every invocation the plugin makes exists in the released CLI.
  drift           at least one does not. A stranger installing today gets a skill that fails.
  could-not-look  the released surface is unknown -- PyPI unreachable, no release published, the
                  install failed, the probe could not introspect.

`could-not-look` is not a pass and it is not drift either. A released CLI read as an empty verb set
would report every single invocation as drift, which is a confident answer to a question nobody
answered; read as a pass it hides the check being broken. So a surface is `None` or a populated
`Surface`, never an empty one that grades as either.

Why the working tree classifies, and the release only answers
-------------------------------------------------------------
A skill writes `requivo model apply <slug>` and `requivo status <slug>`. In the first, `apply` is a
subcommand; in the second, `<slug>` is an argument. Deciding which is which from the RELEASED CLI
gets it wrong in the case that matters most: a release that kept `model` but dropped its subcommand
group would make `apply` look like a positional argument, and `requivo model apply` -- the one call
in the whole plugin that mutates a model -- would grade clean while being broken for every user.

So the working tree is the classifier. The tree and the plugin are the same commit, which makes the
tree the authority on what the plugin *meant*; the release is then only asked whether that meaning
still resolves. It also removes a false-positive class for free: a bare word the tree does not call a
subcommand is prose or a positional, and is never flagged.

Advisory by design -- the policy lives in the workflow, not here
---------------------------------------------------------------
This script exits 0 resolved / 1 drift / 3 could-not-look, because a caller that cannot tell them
apart is the failure it exists to prevent. Exit 2 is left to `argparse`, which spends it on a usage
error -- a run that never reached a verdict must not be spellable as one of the three.

Whether a non-zero exit should redden a build is a *policy* question, and it is answered one
directory over in `.github/workflows/plugin-validate.yml`, where the step is `continue-on-error`
and turns the code into an annotation. That split is deliberate: this
repo's normal state between releases is a pull request that adds a CLI verb and a skill using it
before the next release exists on PyPI, and a required check that reddens for that is a check that
gets overridden, then ignored, then deleted -- the same argument `plugin-validate.yml` makes at
length for running `--strict` against a pinned CLI. Keeping the policy in the workflow means changing
it is one reviewed line rather than an edit to a script other callers may rely on.

Stdlib only, and deliberately so: the released CLI is probed in a separate interpreter, and the leg
must be able to run this before deciding anything about what installs.

Usage
-----
    python scripts/plugin_cli_drift.py --released-python /path/to/released/venv/bin/python [--github]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "claude-code"
SKILLS_DIR = PLUGIN_ROOT / "skills"

RESOLVED = "resolved"
DRIFT = "drift"
COULD_NOT_LOOK = "could-not-look"

# 0, 1, 3 -- not 0, 1, 2. `argparse` exits 2 of its own accord on a usage error, so a
# could-not-look of 2 would be indistinguishable from "you called this script wrong", and the
# caller would report a verdict for a run that never got as far as having one. Leaving 2 to
# argparse means an unrecognised code is unambiguously a usage error, which is what the workflow's
# fallback arm says.
EXIT_RESOLVED = 0
EXIT_DRIFT = 1
EXIT_COULD_NOT_LOOK = 3

PROBE_TIMEOUT_S = 60

# The same shape `tests/test_plugin.py` has always used for the first token, plus an optional second
# one. The second is only *read* here; whether it means anything is decided in `compare()` against
# the working tree. A flag (`--json`), a placeholder (`<slug>`) and a bare stdin dash are therefore
# never captured at all, and a bare word that turns out to be a positional argument is dropped later.
#
# The character classes are also what makes the annotations below safe. A SKILL.md is a file, and a
# captured token is interpolated into a GitHub Actions `::warning` command; `[\w-]` admits no colon
# and no newline, so a crafted skill cannot close the annotation and open an `::error` or a
# `::set-output` of its own. Widening this pattern means revisiting `_annotate`, which otherwise
# leans on a guarantee made here and not where it is relied on.
#
# `re.ASCII` is load-bearing for the same reason. Python's `\w` is Unicode-aware by default, so
# without the flag `requivo 日本語` captures a token, which is then printed -- and this script is
# stdlib-only and never imports `requivo.streams`, so nothing has reconfigured stdout. On a Windows
# console at cp1252 that `print` raises `UnicodeEncodeError` and kills the process after the work it
# was reporting was already done, which is invariant 16 in this repo's own CLAUDE.md. Nothing is
# lost by the restriction: every verb this CLI has is ASCII, because they are argparse choices we
# declare. `_harden_streams()` covers the paths and version strings the flag cannot.
INVOCATION_RE = re.compile(r"requivo (\w[\w-]*)(?:[ \t]+([A-Za-z][\w-]*))?", re.ASCII)

# Run in the TARGET interpreter, which may hold a released Requivo of any age. It prints one JSON
# object and nothing else; any failure is a non-zero exit with a sentence on stderr, which the caller
# turns into could-not-look rather than into a verdict.
PROBE = """
import argparse, json, sys

def _subcommands(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return None

try:
    from requivo.cli import _build_parser
except Exception as exc:
    sys.stderr.write("probe: cannot import requivo.cli._build_parser: " + repr(exc))
    raise SystemExit(1)

try:
    from importlib.metadata import version
    installed = version("requivo")
except Exception:
    installed = "unknown"

top = _subcommands(_build_parser())
if top is None:
    sys.stderr.write("probe: the root parser exposes no subcommands")
    raise SystemExit(1)

json.dump({"version": installed,
           "verbs": {name: (None if _subcommands(p) is None else sorted(_subcommands(p)))
                     for name, p in top.items()}}, sys.stdout)
"""


class Surface(NamedTuple):
    """A CLI's verb tree. `verbs[verb]` is that verb's set of subcommands, or None when it has none
    at all -- a different fact from an empty set, and the one that tells an argument from a
    subcommand."""

    version: str
    verbs: dict[str, set[str] | None]


class Finding(NamedTuple):
    invocation: str
    sources: list[str]
    reason: str


class Report(NamedTuple):
    state: str
    findings: list[Finding]
    checked: int
    detail: str


# An "invocation" throughout this module is `(verb, second_token_or_None)` -- spelled out in the
# signatures rather than aliased, because an alias is evaluated at runtime and `str | None` inside
# one would not survive the Python 3.9 leg of the matrix. Annotations are strings here (see the
# `__future__` import) and are never evaluated, so builtin generics are free in them.


def parse_surface(payload: str) -> Surface | None:
    """Read the probe's stdout. Returns None -- never an empty Surface -- for anything unreadable."""
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("verbs")
    if not isinstance(raw, dict) or not raw:
        return None
    verbs: dict[str, set[str] | None] = {}
    for name, subs in raw.items():
        verbs[str(name)] = None if subs is None else {str(s) for s in subs}
    return Surface(version=str(data.get("version") or "unknown"), verbs=verbs)


def cli_surface(python_executable: str) -> Surface | None:
    """Introspect the Requivo installed for `python_executable`. None means could-not-look."""
    try:
        proc = subprocess.run([python_executable, "-c", PROBE], capture_output=True,
                              encoding="utf-8", timeout=PROBE_TIMEOUT_S)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return parse_surface(proc.stdout or "")


def invocation_sources(plugin_root) -> list[Path]:
    """The plugin files whose `requivo` calls are *executed*, which is not the same set as the files
    that mention one.

    The six `SKILL.md` bodies, plus `REASONING.md`. That second one is not a reader's document: it
    holds the shared preflight every skill is required to run before its first `requivo` call (#93),
    so a command named there runs on all six paths and is the most-executed invocation in the
    plugin. It happens to introduce no verb the skills do not already name, which is exactly why it
    needs to be in the walked set rather than left to keep happening to be redundant.

    `plugins/claude-code/README.md` is deliberately NOT here, and that is a real gap rather than a
    settled question: it names `requivo estimate`, `requivo stories` and `requivo session list`,
    which no skill does, and a reader who types one after they are dropped from a release gets a
    command that does not exist. Left out because it is a human-facing document rather than an
    instruction Claude follows -- the same line `tests/test_plugin.py` draws -- and because it was
    being rewritten in another branch when this landed. Reported for filing rather than decided here.
    """
    root = Path(plugin_root)
    sources = sorted((root / "skills").glob("*/SKILL.md"))
    reasoning = root / "REASONING.md"
    if reasoning.is_file():
        sources.append(reasoning)
    return sources


def _label(path: Path) -> str:
    """What a finding calls the file it came from. `skills/brief/SKILL.md` is "brief"; anything else
    is its own name, so `REASONING.md` reads as itself rather than as the directory above it."""
    return path.parent.name if path.name == "SKILL.md" else path.name


def referenced_invocations(paths) -> dict[tuple[str, str | None], list[str]]:
    """Every `requivo ...` the given files mention, mapped to the files that mention it.

    The sources are carried because a finding nobody can locate is a finding nobody acts on.
    """
    found: dict[tuple[str, str | None], list[str]] = {}
    for raw in paths:
        path = Path(raw)
        label = _label(path)
        for match in INVOCATION_RE.finditer(path.read_text(encoding="utf-8")):
            sources = found.setdefault((match.group(1), match.group(2)), [])
            if label not in sources:
                sources.append(label)
    return found


def tree_typos(referenced: dict[tuple[str, str | None], list[str]], tree: Surface) -> list[Finding]:
    """The in-tree half, which is a different question from drift and needs a different rule.

    `compare()` drops a second word the tree does not call a subcommand, because it cannot tell a
    positional argument from prose -- correct for the drift question, and it means a skill that
    misspells `requivo model rebase` is flagged by nothing at all.

    A verb that *has* a subcommand group is the case where the ambiguity disappears: argparse gives
    that group the next positional, so `model`, `session` and `artifact` accept no bare word of their
    own and any bare word after them is a subcommand claim. That is checkable against the tree with
    no false positives, and it is what `tests/test_plugin_cli_drift.py` asserts. Verbs with no group
    are left alone here, deliberately -- that is where the prose lives.
    """
    findings: list[Finding] = []
    for verb, token in sorted(referenced, key=lambda k: (k[0], k[1] or "")):
        subs = tree.verbs.get(verb)
        if token is None or subs is None or token in subs:
            continue
        findings.append(Finding(f"requivo {verb} {token}", sorted(referenced[(verb, token)]),
                                "`requivo {}` has no `{}` subcommand in this checkout; it offers {}".format(
                                    verb, token, ", ".join(sorted(subs)))))
    return findings


def compare(referenced: dict[tuple[str, str | None], list[str]], tree: Surface | None,
            released: Surface | None) -> Report:
    """Three states. See the module docstring for why the tree classifies and the release answers.

    A second word the tree does not call a subcommand is dropped rather than flagged; `tree_typos()`
    above is the check that covers that case, against the checkout where it can be answered."""
    if tree is None or not tree.verbs:
        return Report(COULD_NOT_LOOK, [], 0,
                      "the working tree's own CLI could not be introspected, so there is nothing to "
                      "read the plugin's invocations as. This says nothing about the release.")
    if released is None or not released.verbs:
        return Report(COULD_NOT_LOOK, [], 0,
                      "the released CLI could not be introspected. This is not a clean result and it "
                      "is not evidence of drift either -- the question is unanswered for this run.")
    if not referenced:
        return Report(COULD_NOT_LOOK, [], 0,
                      "no `requivo` invocations were found in the plugin's files. An empty set "
                      "passes every check by having nothing to check, so it is reported as a failure "
                      "to look rather than as a clean bill.")

    findings: list[Finding] = []
    for verb, token in sorted(referenced, key=lambda k: (k[0], k[1] or "")):
        sources = sorted(referenced[(verb, token)])
        tree_subs = tree.verbs.get(verb)
        is_subcommand = token is not None and tree_subs is not None and token in tree_subs
        name = f"requivo {verb} {token}" if is_subcommand else f"requivo {verb}"

        if verb not in released.verbs:
            findings.append(Finding(name, sources, f"the released CLI ({released.version}) has no `requivo {verb}`"))
            continue
        if not is_subcommand:
            continue
        released_subs = released.verbs[verb]
        if released_subs is None:
            findings.append(Finding(name, sources, (
                f"`requivo {verb}` takes no subcommands in the released CLI ({released.version}), but the plugin calls "
                f"`{name}`")))
        elif token not in released_subs:
            findings.append(Finding(name, sources, (
                "`{}` is not a subcommand of `requivo {}` in the released CLI ({}), which offers "
                "{}").format(token, verb, released.version, ", ".join(sorted(released_subs)))))

    return Report(DRIFT if findings else RESOLVED, findings, len(referenced), "")


def _harden_streams() -> None:
    """What `requivo/streams.py` does for the product, for a script that cannot import it.

    A plugin path or a version string can hold a character the console's codepage cannot encode --
    on Windows that is typically cp1252 -- and an unhandled `UnicodeEncodeError` would kill this
    process at a `print`, after the comparison it was reporting had already been made.
    `backslashreplace` and never `replace`, because a reader cannot tell a substituted character
    from one that was never there.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:          # a stream someone replaced with a plain object
            continue
        try:
            reconfigure(errors="backslashreplace")
        except (OSError, ValueError):    # already detached, or a stream that refuses
            pass


def _annotate(github: bool, title: str, message: str) -> None:
    """A GitHub Actions annotation is one line; a newline inside it truncates the message."""
    if github:
        print("::warning title={}::{}".format(title, " ".join(message.split())))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--released-python", required=True,
                        help="interpreter of an environment with a RELEASED requivo installed")
    parser.add_argument("--tree-python", default=sys.executable,
                        help="interpreter that can import this checkout's requivo (default: this one)")
    parser.add_argument("--plugin", default=str(PLUGIN_ROOT),
                        help="the plugin root (its skills and REASONING.md are what get walked)")
    parser.add_argument("--github", action="store_true",
                        help="also emit GitHub Actions annotations")
    args = parser.parse_args(argv)
    _harden_streams()

    # Every unhandled exception below would otherwise exit 1, which is EXIT_DRIFT -- so an
    # unreadable `SKILL.md` (a permission error, a bad byte) would be reported by the caller as
    # "drift, annotated above" for a run that annotated nothing and reached no verdict at all. That
    # is this file's own defect class turned on itself. A crash is precisely could-not-look: the
    # question was not answered and we know it was not.
    try:
        return _run(args)
    except Exception as exc:                       # noqa: BLE001 - the point is that it is total
        detail = (f"the drift check raised {type(exc).__name__} and could not complete, so nothing was compared. This "
                  f"is not a clean result and it is not evidence of drift: {exc}")
        print(detail, file=sys.stderr)
        _annotate(args.github, "Plugin/CLI drift check could not look", detail)
        return EXIT_COULD_NOT_LOOK


def _run(args) -> int:
    tree = cli_surface(args.tree_python)
    released = cli_surface(args.released_python)
    sources = invocation_sources(args.plugin)
    referenced = referenced_invocations(sources)
    report = compare(referenced, tree=tree, released=released)

    # The other half of the question, and it has to be asked HERE rather than only in the tests.
    # `compare()` drops a bare word the tree does not call a subcommand, so a plugin whose only
    # defect is `requivo model rebase` -- a subcommand in neither the release nor the checkout --
    # made this function print `resolved` and exit 0. `tests/test_plugin_cli_drift.py` calls
    # `tree_typos()` on the real plugin and is a required check, so such a defect could not have
    # shipped; but a run of this script reported a clean bill for a plugin that has one, and a
    # verdict that is clean for the wrong reason is the thing this whole file exists to refuse.
    # Both groups are invocations that do not resolve, so both count toward the same exit code --
    # and the state line below has to be computed after this, or it prints `resolved` above a list
    # of things that did not resolve.
    typos = tree_typos(referenced, tree) if report.state != COULD_NOT_LOOK else []
    state = DRIFT if (report.state == RESOLVED and typos) else report.state

    print(f"plugin root   : {args.plugin}")
    print(f"files walked  : {len(sources)}")
    print("tree CLI      : {}".format(tree.version if tree else "COULD NOT INTROSPECT"))
    print("released CLI  : {}".format(released.version if released else "COULD NOT INTROSPECT"))
    print(f"invocations   : {len(referenced)}")
    print(f"state         : {state}")
    print("")

    if report.state == COULD_NOT_LOOK:
        print(report.detail)
        _annotate(args.github, "Plugin/CLI drift check could not look", report.detail)
        return EXIT_COULD_NOT_LOOK

    def _show(findings, heading):
        if not findings:
            return
        print(heading)
        for finding in findings:
            print("  {}   (referenced by: {})".format(finding.invocation, ", ".join(finding.sources)))
            print(f"      {finding.reason}")
        print("")

    _show(report.findings, "Present in the plugin, absent from the released CLI:")
    _show(typos, "Present in the plugin, absent from BOTH the released CLI and this checkout:")

    if report.findings:
        summary = ("The plugin on this branch makes {} invocation(s) the released CLI ({}) does not "
                   "have: {}. A user who installs the plugin from a marketplace and requivo from "
                   "PyPI today gets a skill that fails. If these ship in the next release that is "
                   "expected and needs no action; if they were removed from the CLI, the plugin is "
                   "broken now.").format(len(report.findings), released.version,
                                         "; ".join(f.invocation for f in report.findings))
        print(summary)
        _annotate(args.github, "Plugin/CLI drift", summary)

    if typos:
        summary = ("The plugin names {} subcommand(s) that exist in neither the released CLI ({}) "
                   "nor this checkout: {}. That is not release skew -- it is an invocation that "
                   "resolves nowhere, so it is broken on this branch as well as for anyone "
                   "installing today.").format(len(typos), released.version,
                                               "; ".join(f.invocation for f in typos))
        print(summary)
        _annotate(args.github, "Plugin names a command that does not exist", summary)

    if report.findings or typos:
        return EXIT_DRIFT

    print(f"Every invocation the plugin makes resolves against the released CLI ({released.version}).")
    return EXIT_RESOLVED


if __name__ == "__main__":
    sys.exit(main())
