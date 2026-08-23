"""The drift workflow prints text this repository did not write, and a CI log is parsed (#147).

`.github/workflows/plugin-validate.yml`'s advisory step echoes the combined stdout and stderr of a
third-party binary -- `claude plugin validate --strict`, over two manifests -- and interpolates
`claude --version` into six lines that begin at column 0, five of them GitHub Actions workflow
commands. `scripts/plugin_cli_drift.py` had been hardened against *one form* of this class inside
the same feature (#96): `_annotate` squashes its message, and the module comment at `INVOCATION_RE`
says why. The shell beside it did neither. One half of a change knew about the hazard and the other
half did not, which is the finding rather than the specific hole.

That "already hardened" read as *fully* hardened when this was written, and it was not: the script's
own `_one_line` squashed whitespace, which is a defence against `TryParseV2` and none at all against
the unanchored `TryParse` below, so a skills directory name forged through it with no newline in it.
That was reported from this pull request rather than fixed in it, and is #176 -- where `_log_safe`
now breaks both keys at the point the value enters. The sentence is corrected here because a
neighbour claiming a guarantee its neighbour does not provide is how this hole survived a review.

Two containments, because the runner's parser has two forms and the two obvious fixes cover neither
of them fully. Read out of `actions/runner` at `src/Runner.Common/ActionCommand.cs` (main branch,
2026-08-23), which is the evidence for both sentences below:

- `TryParseV2` runs `message = message.TrimStart()` **before** testing `StartsWith("::")`. Indenting
  untrusted output therefore does not contain it. #147's own measurement leaned on *the line is
  indented two characters* as though it did; what actually saved that measurement was the newline
  being collapsed to a space, and the indent was incidental.
- `TryParse`, the legacy `##[name]data` form, is `message.IndexOf("##[")` with **no anchor at all**.
  Collapsing the captured output onto a single line therefore does not contain it either: `##[error]`
  is parsed in the middle of a line.

What holds is `::stop-commands::<token>`, the mechanism GitHub documents for logging untrusted
input: while stopped the runner parses no command, in either form, at any column, until it sees the
token again. It is also the only candidate that leaves the log exactly as readable as it was, which
matters here -- the step's own annotation tells a reader to go and read that output, so a hardening
that squashed it would have traded a theoretical problem for a real one.

`claude --version` cannot be fenced -- it is interpolated into six lines emitted *outside* the
fence -- so it is sanitised at capture instead, and it needs both halves. The newline goes because a
`::` mid-line is data (and inside an authored `::warning` it is doubly inert: the parser has already
taken that line as our command and everything past the second `::` is data). The `##[` is spaced
apart because that form needs no line start at all, and one of the six sites is the plain `pinned=`
echo, which is not a command the parser can consume first.

The test that can actually go red here is this one -- the pytest suite does not otherwise execute
shell out of a workflow file, so the step is extracted from the YAML and run under `bash` against a
`claude` that forges. Several of the tests below are must-fire controls that strip one containment
back out of the extracted script and assert the checker then reports a forgery, because a
"nothing was parsed" assertion passes just as well against a harness that never ran.

#177 is the second half and it is the reason this module is no longer about one step. The two
**gate** steps and the CLI install step ran `claude` unfenced too, and they are not
`continue-on-error`: a gate's verdict IS its exit code, so the fence had to carry that code out
past the `echo` that closes it. Everything from `_GATE_STEPS` down is about that, including the
class guard that asks the question of every step in the file rather than of the four we know about.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "plugin-validate.yml"
STEP_NAME = "Spec drift against the current CLI (advisory, never fails)"

# Every command name the runner registers, from `ActionCommandManager`. A whitelist of only the ones
# this workflow emits would be the wrong shape for the other side of the check: the injectable
# vocabulary is all of them, and a denylist of `error` ages badly -- the same argument
# `_assert_no_forged_workflow_command` in tests/test_plugin_cli_drift.py already makes.
_REGISTERED = frozenset({
    "set-env", "set-output", "add-mask", "add-path", "debug", "warning", "error", "notice",
    "group", "endgroup", "save-state", "echo", "add-matcher", "remove-matcher", "stop-commands",
})

# The three command lines this step authors. Anything else the runner would act on is forged.
_AUTHORED = (
    "::stop-commands::",
    "::warning title=Drift check could not look::",
    "::warning title=Plugin spec drift::",
)

# A marker no line of this workflow contains, carried by every line the fake `claude` prints. It is
# what tells "the guard held" from "the harness never reached the hostile output".
_MARK = "FORGED"

# The two must-fire controls below strip one containment back out of the extracted script, so each
# needs a key that matches the *effective* line and nothing else. A comment marker would not do: it
# matches the paragraph above the line and leaves the line itself in place, which is a control that
# removes nothing and then reports the guard working.
#
# The squash key is the bare `$latest`, which appears only where the value is re-read for squashing.
# Every one of the six interpolation sites spells it `${latest}`, and that does not contain `$latest`
# -- there is a brace between the sigil and the name.
_SQUASH_KEY = "$latest"
_FENCE_KEYS = ("::stop-commands::", "::${fence}::")

_STUB_TEMPLATE = """#!/bin/sh
if [ "$1" = "--version" ]; then
cat <<'VER'
2.1.238 ##[error]FORGED-VIA-VERSION-LEGACY in the middle of the first line
::error title=FORGED-VIA-VERSION::a version string that broke its own line
VER
exit @EXIT@
fi
cat <<'OUT'
Checking manifest
::error title=FORGED-AT-COLUMN-0::validator output starting at column 0
  ::error title=FORGED-BEHIND-AN-INDENT::the runner trims leading space before matching
plain ##[error]FORGED-IN-THE-LEGACY-FORM which is matched anywhere in a line
OUT
exit @EXIT@
"""


def _claude_stub(exit_code: int = 0) -> str:
    """A `claude` that forges in both of the runner's parser forms and then exits `exit_code`.

    The exit code is a parameter because of #177 rather than for symmetry. The three steps this
    module gained there are gates: their verdict IS the process exit code, so a fence around one has
    to carry that code out past the fence. A stub that only ever exits 0 cannot tell a fence that
    preserved the verdict from one that swallowed it, and a fence that swallows it turns a required
    check into one that always passes -- strictly worse than the forging it was added to contain."""
    return _STUB_TEMPLATE.replace("@EXIT@", str(exit_code))


_CLAUDE_STUB = _claude_stub()

_NPM_STUB = "#!/bin/sh\nexit 0\n"


def _all_run_steps():
    """Every step in the workflow that carries a `run:`, as `(name, script)` pairs, in file order.

    No YAML parser: PyYAML is not a dependency of this project and adding one to read a handful of
    shell blocks would be the larger change.

    **Both forms of `run:`, and that is the point rather than completeness for its own sake.** A
    block scalar is what the advisory step uses; a plain one-line `run: claude plugin validate
    --strict .` is what the two gate steps were before #177. A scanner that understood only
    `run: |` would have reported the very defect this module exists to catch as absent, which is the
    empty-scan-set all-clear `tests/test_boundaries.py` refuses for the same reason.
    """
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    steps, name, i = [], None, 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if stripped.startswith("- "):
            # A new step begins here. Its name is known only if `name:` is the key it opens with.
            name = stripped[len("- name:"):].strip() if stripped.startswith("- name:") else None
        key = stripped[2:].strip() if stripped.startswith("- ") else stripped
        label = name or f"<unnamed step at line {i + 1}>"
        if key == "run: |":
            indent = len(raw) - len(raw.lstrip())
            body, k = [], i + 1
            while k < len(lines):
                if not lines[k].strip():
                    body.append("")
                elif len(lines[k]) - len(lines[k].lstrip()) <= indent:
                    break
                else:
                    body.append(lines[k])
                k += 1
            steps.append((label, textwrap.dedent("\n".join(body)).rstrip() + "\n"))
            i = k                     # past the block, so a `run:` inside a heredoc is not a step
            continue
        if key.startswith("run: "):
            steps.append((label, key[len("run: "):].strip() + "\n"))
        i += 1
    if not steps:
        pytest.fail(f"no `run:` step found in {WORKFLOW} at all -- the extractor is broken, and an "
                    f"empty scan set is an all-clear nobody earned")
    return steps


def _step_script(step_name: str = STEP_NAME) -> str:
    """One named step's shell, dedented. Every way this can fail to find the block is a hard failure
    rather than an empty string, because an extractor that quietly returns nothing turns every test
    below green."""
    found = [script for name, script in _all_run_steps() if name == step_name]
    if len(found) != 1:
        pytest.fail(f"expected exactly one step named {step_name!r} in {WORKFLOW}, found {len(found)}")
    script = found[0]
    # Weaker than the `claude plugin validate` this used to test, because #177 brought in a step
    # that runs `claude --version` and no validator. It still catches what it was written for: an
    # extractor that grabbed a neighbouring block returns shell with no `claude` in it at all.
    assert "claude" in script, "extracted the wrong block:\n" + script
    return script


def _pinned_version() -> str:
    match = re.search(r'^\s*CLAUDE_CLI_VERSION:\s*"([^"]+)"',
                      WORKFLOW.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, "the workflow no longer sets CLAUDE_CLI_VERSION; this harness supplies it"
    return match.group(1)


def _parse(line, resume_token):
    """What `ActionCommand.TryParseV2` and then `TryParse` would make of one log line. Both branches
    are modelled, including the two asymmetries that make the obvious fixes insufficient: V2 trims
    leading whitespace first, and V1 is unanchored."""
    known = _REGISTERED | ({resume_token} if resume_token else set())
    stripped = line.lstrip()
    if stripped.startswith("::"):
        end = stripped.find("::", 2)
        if end >= 0:
            name = stripped[2:end].split(" ", 1)[0]
            if name in known:
                return name, stripped[end + 2:]
    start = line.find("##[")
    if start >= 0:
        closing = line.find("]", start)
        if closing >= 0:
            name = line[start + 3:closing].split(" ", 1)[0]
            if name in known:
                return name, line[closing + 1:]
    return None


def _processed(log):
    """The lines the runner would act on, and the fence token still open at the end (None when the
    step closed it). A token left open is its own finding: annotations after it are suppressed."""
    acted_on, token = [], None
    for line in log.splitlines():
        parsed = _parse(line, token)
        if parsed is None:
            continue
        name, data = parsed
        if token is not None:
            if name == token:
                token = None
            continue
        acted_on.append(line)
        if name == "stop-commands":
            token = data.strip()
    return acted_on, token


def _forged(acted_on):
    return [line for line in acted_on
            if not any(line.lstrip().startswith(prefix) for prefix in _AUTHORED)]


def _exec(script, tmp_path, claude_stub=None, shell=("bash", "-e")):
    """Execute an extracted step with a `claude` that forges and an `npm` that does nothing, and
    hand back the whole `CompletedProcess` -- exit code included, because for a gate step that code
    is the thing under test.

    `bash -e` with the script on stdin: that is the shell GitHub runs a `run:` block with when the
    workflow names none, and stdin sidesteps path translation on the one leg of this matrix whose
    bash is Git Bash. `shell` is a parameter so the exit-code tests can run the same script under a
    plain `bash` as well. That is not belt-and-braces: *the default shell is `bash -e`* is a claim
    about GitHub's runner that nothing in this repository can go red on, so the fences #177 added
    carry the verdict out with an explicit `exit` instead of leaning on errexit -- and running both
    ways is how that independence gets measured rather than asserted in a comment.
    """
    if shutil.which("bash") is None:
        pytest.skip("no bash on this platform. UNTESTED HERE: that the workflow's own shell contains "
                    "the validator's output end to end. The workflow itself runs on ubuntu-latest "
                    "only, and the structural tests assert their half on every leg.")
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    for name, body in (("claude", _CLAUDE_STUB), ("npm", _NPM_STUB)):
        path = stub_dir / name
        path.write_text(body, encoding="utf-8")
        os.chmod(path, 0o755)
    env = dict(os.environ)
    env["PATH"] = str(stub_dir) + os.pathsep + env.get("PATH", "")
    env["CLAUDE_CLI_VERSION"] = _pinned_version()

    # Probe the harness before asserting anything with it. Staging an extensionless shell script and
    # reaching it by name off PATH is the platform-dependent part of this whole file -- the workflow
    # itself is ubuntu-latest only, and the bash on the Windows leg of this matrix is Git Bash. A
    # probe that is the staging step itself cannot pass for a reason unrelated to what it checks,
    # which is the same argument `_forging_dir` in tests/test_plugin_cli_drift.py makes for a
    # filesystem that refuses a newline in a name. It cannot mask a real failure either: everything
    # it could hide is a reason the guard was never exercised, and the guard is only asserted after
    # this returns.
    #
    # Always probed with the exit-0 stub, whatever the caller went on to ask for. Probing with a
    # stub that exits non-zero on purpose would read as "this platform cannot stage a stub" and skip
    # the one test that needs the failing one -- a skip manufactured by the harness, which is
    # not-checked rendering as checked.
    probe = subprocess.run(["bash", "-e"], input="claude --version\n", env=env,
                           cwd=str(tmp_path), capture_output=True, text=True)
    if probe.returncode != 0 or _MARK not in probe.stdout:
        pytest.skip(
            f"this platform's bash cannot reach a staged stub by name (exit {probe.returncode}, "
            f"stderr {probe.stderr[:200]!r}). UNTESTED HERE: that the workflow's own shell contains "
            f"the validator's output end to end. The workflow runs on ubuntu-latest only, and the "
            f"structural tests assert their half everywhere.")

    if claude_stub is not None:
        (stub_dir / "claude").write_text(claude_stub, encoding="utf-8")
        os.chmod(stub_dir / "claude", 0o755)

    return subprocess.run(list(shell), input=script, env=env, cwd=str(tmp_path),
                          capture_output=True, text=True)


def _run(script, tmp_path):
    """`_exec` for the advisory step, whose contract is that it always exits 0."""
    proc = _exec(script, tmp_path)
    log = proc.stdout + proc.stderr
    assert proc.returncode == 0, "the step is meant to exit 0 always:\n" + log
    assert _MARK in log, "the harness never reached the hostile output at all:\n" + log
    return log


def test_the_step_still_carries_both_containments():
    """The half that runs on every leg, including one with no bash. Structural rather than
    behavioural on purpose: it says the two containments are still in the file and still bracket the
    output they contain, and the three tests below say what they do."""
    script = _step_script()
    assert "::stop-commands::" in script, script
    fence_open = script.index("::stop-commands::")
    loop = script.index("claude plugin validate")
    assert fence_open < loop, "the fence opens after the output it contains:\n" + script
    resume = script.index("::${fence}::", fence_open)
    assert resume > loop, "the fence closes before the output it contains:\n" + script
    assert script.count(_SQUASH_KEY) == 1, (
        "the version string is no longer squashed at capture, or is squashed somewhere the "
        "controls below cannot find:\n" + script)


def test_the_validator_output_cannot_forge_a_workflow_command(tmp_path):
    """The must-not-fire half, end to end through the real shell in the real workflow file."""
    log = _run(_step_script(), tmp_path)
    acted_on, open_token = _processed(log)
    assert _forged(acted_on) == [], "a third-party binary forged a workflow command:\n" + log
    assert open_token is None, "the fence was never closed, so later annotations are lost:\n" + log
    assert any(line.lstrip().startswith("::stop-commands::") for line in acted_on), \
        "the fence never opened, so the assertion above passed for the wrong reason:\n" + log
    # And the log still reads as it did: every line the validator printed survives, verbatim.
    for expected in ("Checking manifest", "FORGED-AT-COLUMN-0", "FORGED-BEHIND-AN-INDENT",
                     "FORGED-IN-THE-LEGACY-FORM"):
        assert expected in log, "the hardening ate the output it was meant to contain:\n" + log


def test_removing_the_fence_lets_the_validator_forge_one(tmp_path):
    """Must-fire control for the fence. Without it the assertion above would pass against a runner
    model that parses nothing, a stub that printed nothing, or an extractor that returned nothing."""
    script = _step_script()
    stripped = "\n".join(line for line in script.splitlines()
                         if not any(key in line for key in _FENCE_KEYS)) + "\n"
    removed = len(script.splitlines()) - len(stripped.splitlines())
    assert removed == 2, f"expected to strip exactly the two fence lines, stripped {removed}"

    acted_on, _ = _processed(_run(stripped, tmp_path))
    forged = _forged(acted_on)
    # Three shapes, and the loop runs the validator over both manifests, so six lines.
    assert len(forged) == 6, f"expected all six forged lines unfenced, got: {forged}"
    assert any("FORGED-AT-COLUMN-0" in line for line in forged), forged
    assert any("FORGED-BEHIND-AN-INDENT" in line for line in forged), \
        "an indented `::` must count: the runner trims before it matches"
    assert any("FORGED-IN-THE-LEGACY-FORM" in line for line in forged), \
        "`##[error]` mid-line must count: the legacy parser is unanchored"


def test_removing_the_version_squash_lets_the_version_string_forge_one(tmp_path):
    """Must-fire control for the other containment. `claude --version` is interpolated into six
    lines that begin at column 0, all of them outside the fence, so the fence cannot cover it."""
    script = _step_script()
    stripped = "\n".join(line for line in script.splitlines() if _SQUASH_KEY not in line) + "\n"
    removed = len(script.splitlines()) - len(stripped.splitlines())
    assert removed == 1, f"expected to strip exactly the squash line, stripped {removed}"

    acted_on, _ = _processed(_run(stripped, tmp_path))
    forged = _forged(acted_on)
    # Both forms, because the one line this value reaches at column 0 unwrapped -- the `pinned=`
    # echo -- can be forged by either: a newline gives the `::` form a line start of its own, and
    # the `##[` form needs no line start at all.
    assert any("FORGED-VIA-VERSION::" in line for line in forged), \
        f"the version string could not forge a `::` line even unsquashed: {forged}"
    assert any("FORGED-VIA-VERSION-LEGACY" in line for line in forged), \
        f"the version string could not forge a `##[` line even unsquashed: {forged}"


# -- The three steps that are NOT advisory (#177) ---------------------------------------------
#
# Everything above is about one `continue-on-error: true` step that ends `exit 0`. The install step
# and the two gate steps are the other shape: the gates are required, branch-protected checks whose
# verdict IS the process exit code, and the install step is what puts the binary on PATH for them.
# All three ran `claude` straight into the log, uncaptured and unfenced.
#
# Measured 2026-08-23 against `claude` 2.1.241, and re-measured on this branch rather than taken
# from the issue: a plugin manifest field name is echoed VERBATIM, twice, in
#
#   > <name>: Unknown field '<name>'. Claude Code ignores it at load time.
#
# so a name containing `##[error]title=X` puts `##[` at column 10 of a line the runner parses with
# an unanchored `IndexOf("##[")`. No newline is needed and no line start is needed. A fork pull
# request edits that manifest, and the same holds for `.claude-plugin/marketplace.json` on the
# second gate. The `::` form stays contained for a field name -- a newline inside one comes back
# collapsed to a space, which is what #147 measured -- but the legacy form needs neither.
#
# The bound, so nothing here is over-read: a fork pull request carries a read-only token and no
# secrets, and a gate takes its verdict from the exit code rather than from the log. The worst case
# is forged annotations and log-command effects on a check a reader is more likely to believe
# BECAUSE it is required.
#
# Why a fence rather than sanitising at capture, as the version string above is handled: a gate's
# output is what a human reads when the gate is red, so squashing or spacing it would trade a
# forging problem for an unreadable failure. The fence leaves the log byte-identical.

_GATE_STEPS = (
    "Install the pinned Claude Code CLI",
    "Validate the plugin manifest (gate)",
    "Validate the marketplace catalog (gate)",
)

_FENCE_OPEN, _FENCE_CLOSE = _FENCE_KEYS

# A `claude` invocation at command position. Deliberately not `"claude" in line`: the install step
# spells the package `@anthropic-ai/claude-code@...`, which is a string this workflow wrote and not
# a process it starts.
_CLAUDE_INVOCATION = re.compile(r"(?:^|[;&|(\s])claude\s", re.M)


def _without_comments(script):
    """The script with whole-line `#` comments blanked to spaces, so every offset is unchanged.

    A prose line inside a `run:` block that says `claude ...` is not an invocation, and without this
    the guard below would report the paragraph explaining the fence as the thing the fence misses.
    """
    return "\n".join(" " * len(line) if line.lstrip().startswith("#") else line
                     for line in script.splitlines())


def _steps_running_claude():
    return [(name, script) for name, script in _all_run_steps()
            if _CLAUDE_INVOCATION.search(_without_comments(script))]


def test_every_step_that_runs_the_cli_contains_what_it_prints():
    """The class guard, and the reason this is not three per-step assertions.

    #147 was one half of a feature hardened and the other half not; #176 was the same class one file
    along. Naming the three steps #177 found would leave the fourth to be discovered the same way.
    So the check is over every step in the workflow that starts `claude`, whatever it is called: it
    must either fence the output or capture it, and a new step that does neither goes red under its
    own name.

    Structural rather than behavioural, so it also runs on a leg with no usable bash."""
    steps = _steps_running_claude()
    assert len(steps) == 4, (
        "the set of steps that run the Claude CLI changed. Every one of them needs a decision: "
        "fence its output, or capture and sanitise it, and if it is a gate, carry its exit code out "
        f"past the fence. Add it to _GATE_STEPS if it is one. Found: {[n for n, _ in steps]}")

    offenders = []
    for name, script in steps:
        code = _without_comments(script)
        if _FENCE_OPEN not in code:
            offenders.append((name, "runs the CLI with no ::stop-commands:: fence anywhere"))
            continue
        open_at = code.index(_FENCE_OPEN)
        close_at = code.find(_FENCE_CLOSE, open_at)
        if close_at < 0:
            offenders.append((name, "opens a fence and never closes it"))
            continue
        for match in _CLAUDE_INVOCATION.finditer(code):
            if open_at < match.start() < close_at:
                continue
            line_start = code.rfind("\n", 0, match.start()) + 1
            line_end = code.find("\n", match.start())
            line = code[line_start:line_end if line_end != -1 else len(code)]
            # Outside the fence the only other containment this file accepts is capture: the value
            # goes into a variable, and the site that assigns it is then responsible for sanitising
            # it. The version string in the advisory step is the one instance, and
            # test_removing_the_version_squash_lets_the_version_string_forge_one holds the second
            # half of that bargain.
            if "$(claude" in line:
                continue
            offenders.append((name, line.strip()))
    assert offenders == [], (
        "a step runs the Claude CLI straight into the log, outside any fence (#177). Wrap it: open "
        "::stop-commands:: with an unguessable token, run the command, echo the token back.\n"
        + "\n".join(f"  {name}: {detail}" for name, detail in offenders))


@pytest.mark.parametrize("step_name", _GATE_STEPS)
def test_a_gate_step_still_carries_its_exit_code_past_the_fence(step_name):
    """The structural half of the exit-code question, so it runs on every leg.

    `echo` clobbers `$?`, so closing the fence destroys the verdict unless it was captured first.
    The two lines below are what puts it back, and a fence written without them turns a required
    check into one that always passes -- which is worse than the defect it was fixing."""
    script = _step_script(step_name)
    assert "|| code=$?" in script, (
        f"{step_name!r} does not capture the CLI's exit code, so closing the fence loses it:\n"
        + script)
    assert script.rstrip().endswith('exit "$code"'), (
        f"{step_name!r} does not re-raise the captured exit code as its own, so the step reports "
        f"the exit status of the echo that closed the fence:\n" + script)


@pytest.mark.parametrize("step_name", [name for name, _ in _steps_running_claude()])
def test_no_step_lets_the_cli_forge_a_workflow_command(step_name, tmp_path):
    """The behavioural half of the class guard: every step that runs the CLI, through the real
    shell, against a `claude` that forges in both parser forms."""
    proc = _exec(_step_script(step_name), tmp_path)
    log = proc.stdout + proc.stderr
    assert _MARK in log, "the harness never reached the hostile output at all:\n" + log
    acted_on, open_token = _processed(log)
    assert _forged(acted_on) == [], "a third-party binary forged a workflow command:\n" + log
    assert open_token is None, "the fence was never closed, so later annotations are lost:\n" + log


@pytest.mark.parametrize("step_name", _GATE_STEPS)
@pytest.mark.parametrize("shell", [("bash", "-e"), ("bash",)], ids=["errexit", "no-errexit"])
def test_a_gate_step_reports_the_cli_failure_through_its_fence(step_name, shell, tmp_path):
    """The must-fire half, and the one that decides whether this fix was worth making.

    A `claude` that forges AND exits 7. The step has to come back 7 -- not merely non-zero, because
    a fence that lost the code and then failed for some other reason would satisfy `!= 0` and tell
    us nothing -- with nothing forged on the way.

    Run under `bash -e` and under a plain `bash`, because *GitHub's default shell is `bash -e`* is a
    claim about somebody else's runner that no test here can go red on. The fences are written not
    to depend on it, and this is where that gets measured."""
    proc = _exec(_step_script(step_name), tmp_path, claude_stub=_claude_stub(7), shell=shell)
    log = proc.stdout + proc.stderr
    assert _MARK in log, "the harness never reached the hostile output at all:\n" + log
    assert proc.returncode == 7, (
        f"the gate's verdict did not survive its fence: expected exit 7, got {proc.returncode}. A "
        f"required check that cannot fail is worse than one that can be forged.\n" + log)
    acted_on, open_token = _processed(log)
    assert _forged(acted_on) == [], "a third-party binary forged a workflow command:\n" + log
    assert open_token is None, "the fence was never closed:\n" + log


@pytest.mark.parametrize("step_name", _GATE_STEPS)
def test_a_gate_step_still_passes_when_the_cli_passes(step_name, tmp_path):
    """The other half of the same question. A fence that always fails is not a gate either, and the
    test above cannot tell one from a fence that works."""
    proc = _exec(_step_script(step_name), tmp_path)
    log = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        f"the step failed against a CLI that exited 0: {proc.returncode}\n" + log)


def test_removing_the_exit_line_lets_a_failing_gate_report_success(tmp_path):
    """Must-fire control for the exit-code half. Take the re-raise back out and the identical run
    against a CLI that exits 7 has to come back 0 -- otherwise the assertions above were passing for
    some reason other than the line they name."""
    script = _step_script("Validate the plugin manifest (gate)")
    stripped = "\n".join(line for line in script.splitlines()
                         if 'exit "$code"' not in line) + "\n"
    removed = len(script.splitlines()) - len(stripped.splitlines())
    assert removed == 1, f"expected to strip exactly the re-raise line, stripped {removed}"

    proc = _exec(stripped, tmp_path, claude_stub=_claude_stub(7))
    assert proc.returncode == 0, (
        "the control removed nothing: the step failed anyway, so the exit-code assertions above "
        f"prove nothing about the line they name (exit {proc.returncode})\n"
        + proc.stdout + proc.stderr)


def test_removing_the_fence_lets_a_gate_step_forge_one(tmp_path):
    """Must-fire control for the fence on a gate step. The advisory step has its own above; this is
    the same control on the step whose output nobody had contained."""
    script = _step_script("Validate the marketplace catalog (gate)")
    stripped = "\n".join(line for line in script.splitlines()
                         if not any(key in line for key in _FENCE_KEYS)) + "\n"
    removed = len(script.splitlines()) - len(stripped.splitlines())
    assert removed == 2, f"expected to strip exactly the two fence lines, stripped {removed}"

    proc = _exec(stripped, tmp_path)
    acted_on, _ = _processed(proc.stdout + proc.stderr)
    forged = _forged(acted_on)
    # One manifest, three forged shapes -- unlike the advisory step, which loops over two.
    assert len(forged) == 3, f"expected all three forged lines unfenced, got: {forged}"
    assert any("FORGED-AT-COLUMN-0" in line for line in forged), forged
    assert any("FORGED-BEHIND-AN-INDENT" in line for line in forged), \
        "an indented `::` must count: the runner trims before it matches"
    assert any("FORGED-IN-THE-LEGACY-FORM" in line for line in forged), \
        "`##[error]` mid-line must count: the legacy parser is unanchored"
