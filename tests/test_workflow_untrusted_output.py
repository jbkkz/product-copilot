"""The drift workflow prints text this repository did not write, and a CI log is parsed (#147).

`.github/workflows/plugin-validate.yml`'s advisory step echoes the combined stdout and stderr of a
third-party binary -- `claude plugin validate --strict`, over two manifests -- and interpolates
`claude --version` into six lines that begin at column 0, five of them GitHub Actions workflow
commands. `scripts/plugin_cli_drift.py` had already been hardened against exactly this class inside
the same feature (#96): `_annotate` squashes its message, and the module comment at `INVOCATION_RE`
says why. The shell beside it did neither. One half of a change knew about the hazard and the other
half did not, which is the finding rather than the specific hole.

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
`claude` that forges. Two of the four tests below are must-fire controls that strip one containment
back out of the extracted script and assert the checker then reports a forgery, because a
"nothing was parsed" assertion passes just as well against a harness that never ran.
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

_CLAUDE_STUB = """#!/bin/sh
if [ "$1" = "--version" ]; then
cat <<'VER'
2.1.238 ##[error]FORGED-VIA-VERSION-LEGACY in the middle of the first line
::error title=FORGED-VIA-VERSION::a version string that broke its own line
VER
exit 0
fi
cat <<'OUT'
Checking manifest
::error title=FORGED-AT-COLUMN-0::validator output starting at column 0
  ::error title=FORGED-BEHIND-AN-INDENT::the runner trims leading space before matching
plain ##[error]FORGED-IN-THE-LEGACY-FORM which is matched anywhere in a line
OUT
exit 0
"""

_NPM_STUB = "#!/bin/sh\nexit 0\n"


def _step_script(step_name: str = STEP_NAME) -> str:
    """The step's `run: |` block, dedented. No YAML parser: PyYAML is not a dependency of this
    project and adding one to read six lines would be the larger change. Every way this can fail to
    find the block is a hard failure rather than an empty string, because an extractor that quietly
    returns nothing turns all four tests below green."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == f"- name: {step_name}"]
    if len(starts) != 1:
        pytest.fail(f"expected exactly one step named {step_name!r} in {WORKFLOW}, found {len(starts)}")
    run_at = None
    for j in range(starts[0] + 1, len(lines)):
        if lines[j].strip().startswith("- name:"):
            break
        if lines[j].strip() == "run: |":
            run_at = j
            break
    if run_at is None:
        pytest.fail(f"step {step_name!r} has no `run: |` block")
    indent = len(lines[run_at]) - len(lines[run_at].lstrip())
    body = []
    for k in range(run_at + 1, len(lines)):
        if not lines[k].strip():
            body.append("")
            continue
        if len(lines[k]) - len(lines[k].lstrip()) <= indent:
            break
        body.append(lines[k])
    script = textwrap.dedent("\n".join(body)).rstrip() + "\n"
    assert "claude plugin validate" in script, "extracted the wrong block:\n" + script
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


def _run(script, tmp_path):
    """Execute the extracted step with a `claude` that forges and an `npm` that does nothing.

    `bash -e` with the script on stdin: that is the shell GitHub runs a `run:` block with on Linux,
    and stdin sidesteps path translation on the one leg of this matrix whose bash is Git Bash.
    """
    if shutil.which("bash") is None:
        pytest.skip("no bash on this platform. UNTESTED HERE: that the workflow's own shell contains "
                    "the validator's output end to end. The workflow itself runs on ubuntu-latest "
                    "only, and test_the_step_still_carries_both_containments asserts the structural "
                    "half on every leg.")
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
    probe = subprocess.run(["bash", "-e"], input="claude --version\n", env=env,
                           cwd=str(tmp_path), capture_output=True, text=True)
    if probe.returncode != 0 or _MARK not in probe.stdout:
        pytest.skip(
            f"this platform's bash cannot reach a staged stub by name (exit {probe.returncode}, "
            f"stderr {probe.stderr[:200]!r}). UNTESTED HERE: that the workflow's own shell contains "
            f"the validator's output end to end. The workflow runs on ubuntu-latest only, and "
            f"test_the_step_still_carries_both_containments asserts the structural half everywhere.")

    proc = subprocess.run(["bash", "-e"], input=script, env=env, cwd=str(tmp_path),
                          capture_output=True, text=True)
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
