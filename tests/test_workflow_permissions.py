"""Every workflow states the token it wants, and every `write` it wants is written down (#178).

A workflow with no `permissions:` block takes the repository or organisation default. In this
repository that default is read/write, because its owner has never changed it -- so a step that only
ever checks out and runs pytest has been holding a token that can push to `main`, and a step added
later inherits the same thing without anybody deciding.

Nothing is known to be exploitable through it today, and that is the honest weight of this file:
#177 and #176 are where the reachable vectors were. This is defence in depth. What it buys is that a
future step reaching for a token it should not have fails rather than succeeding quietly.

**Why a test rather than four edits.** Doing it to one workflow and not the one beside it is exactly
the shape #147 was: a class swept in one file and missed in its neighbour, inside the same feature.
The edits are four lines; the thing worth keeping is that the fifth workflow cannot be added without
answering the question. So the check is over the directory, and a new file goes red under its own
name.

Text, not YAML. PyYAML is not a dependency of this project and adding one to read four blocks would
be the larger change -- the same call `tests/test_workflow_untrusted_output.py` makes for the same
reason.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# Workflows that scope permissions per job instead of at the top. Naming any permission on a job
# REPLACES the workflow-level set rather than adding to it, so for a workflow whose one job needs a
# write scope, a top-level block would be discarded by the job's own and read as a guarantee it does
# not give. Each entry carries the reason, and the test below asserts the mapping in both
# directions -- an entry whose file is gone is stale prose, which is the rule
# `tests/test_boundaries.py` applies to its own allowlist.
_JOB_SCOPED = {
    "publish.yml": "the publish job needs `id-token: write` for PyPI Trusted Publishing, and a "
                   "workflow-level block would be replaced wholesale by that job's own",
}

# Every `write` scope granted anywhere, with why. A `write` that is not here is either a mistake or
# a decision nobody wrote down, and the two are indistinguishable at review time.
_WRITE_GRANTS = {
    ("publish.yml", "id-token"): "mints the short-lived OIDC token PyPI Trusted Publishing "
                                 "verifies; it is what replaces a stored API token",
    ("secret-scan.yml", "pull-requests"): "gitleaks-action's only use of GITHUB_TOKEN is "
                                          "`pulls.createReviewComment`, and its "
                                          "GITLEAKS_ENABLE_COMMENTS defaults to true",
}


def _workflow_files():
    files = sorted(list(WORKFLOWS.glob("*.yml")) + list(WORKFLOWS.glob("*.yaml")))
    # A glob over a directory that has moved returns [], and every assertion below would then pass
    # over nothing. `tests/test_boundaries.py` refuses an empty scan set for the same reason: an
    # all-clear nobody earned.
    assert files, f"no workflow files under {WORKFLOWS} -- the scan set is empty"
    return files


def _permission_blocks(text):
    """Every `permissions:` block in one workflow, as `(indent, {scope: value})`.

    Indent is what tells a workflow-level block from a job-level one, and the two mean different
    things: the first is inherited by every job that states nothing, the second replaces it.
    """
    lines = text.splitlines()
    blocks, i = [], 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped.startswith("permissions:"):
            i += 1
            continue
        indent = len(raw) - len(raw.lstrip())
        inline = stripped[len("permissions:"):].strip()
        if inline:
            # `permissions: read-all`, `permissions: write-all`, `permissions: {}`.
            blocks.append((indent, {"<inline>": inline}))
            i += 1
            continue
        grants, j = {}, i + 1
        while j < len(lines):
            line = lines[j]
            if not line.strip() or line.lstrip().startswith("#"):
                j += 1
                continue
            if len(line) - len(line.lstrip()) <= indent:
                break
            scope, _, value = line.strip().partition(":")
            grants[scope.strip()] = value.split("#")[0].strip()
            j += 1
        blocks.append((indent, grants))
        i = j
    return blocks


def test_the_job_scoped_exception_list_names_files_that_exist():
    """Both directions. An entry pointing at a workflow that has been renamed or deleted is prose
    claiming to describe a decision nobody can find, and it exempts nothing."""
    present = {p.name for p in _workflow_files()}
    stale = sorted(set(_JOB_SCOPED) - present)
    assert stale == [], f"_JOB_SCOPED names workflows that no longer exist: {stale}"


def _declaration_offence(name, text):
    """Why `name` fails the declaration rule, or None.

    Split out from the test so the control at the foot of this file can put text in front of it
    that really does offend. Three must-not-fire assertions over a tree that is already clean
    cannot tell a working guard from one that returns an empty list whatever it is shown."""
    blocks = _permission_blocks(text)
    if not blocks:
        return (f"{name}: no `permissions:` block, so every job in it takes the repository "
                f"default -- read/write here. Add `permissions:` with `contents: read`, or scope "
                f"it per job and record the reason in _JOB_SCOPED.")
    if name in _JOB_SCOPED:
        if not any(indent > 0 for indent, _ in blocks):
            return (f"{name}: _JOB_SCOPED says its grant is per job ({_JOB_SCOPED[name]}) but "
                    f"every `permissions:` block in it is at workflow level.")
        return None
    if not any(indent == 0 for indent, _ in blocks):
        return (f"{name}: declares permissions per job only. A job added later still falls back "
                f"to the repository default. Either declare one at workflow level, or record in "
                f"_JOB_SCOPED why this file cannot.")
    return None


def _write_scopes(name, text):
    """`(granted, offences)` for one workflow: which `<scope>: write` it grants, and which of those
    nobody wrote a reason for."""
    granted, offences = set(), []
    for _, grants in _permission_blocks(text):
        for scope, value in grants.items():
            if scope == "<inline>":
                if "write" in value:
                    offences.append(
                        f"{name}: `permissions: {value}` grants write across every scope. List "
                        f"the scopes it actually needs instead.")
                continue
            if value != "write":
                continue
            granted.add((name, scope))
            if (name, scope) not in _WRITE_GRANTS:
                offences.append(
                    f"{name}: grants `{scope}: write` with no entry in _WRITE_GRANTS. Say what "
                    f"needs it and why, or drop the scope.")
    return granted, offences


def test_every_workflow_declares_its_own_permissions():
    offenders = []
    for path in _workflow_files():
        offence = _declaration_offence(path.name, path.read_text(encoding="utf-8"))
        if offence:
            offenders.append(offence)
    assert offenders == [], "\n".join(offenders)


def test_every_write_scope_a_workflow_grants_is_written_down():
    """Declaring `permissions:` is not the same as declaring a narrow one -- `write-all` satisfies
    the test above and grants more than the default it replaced. So each `write` is named here with
    its reason, and both directions are checked: an unlisted grant is a finding, and a listed one
    that no workflow makes any more is stale prose the next reader would take for a live decision."""
    granted, offenders = set(), []
    for path in _workflow_files():
        seen, offences = _write_scopes(path.name, path.read_text(encoding="utf-8"))
        granted |= seen
        offenders += offences
    assert offenders == [], "\n".join(offenders)

    stale = sorted(set(_WRITE_GRANTS) - granted)
    assert stale == [], (
        f"_WRITE_GRANTS explains write scopes no workflow grants any more: {stale}. Remove the "
        f"entries -- an explanation with no call site reads as a live decision.")


def test_the_permission_guards_fire_on_a_workflow_that_offends():
    """The must-fire half, and the reason the two checks above are worth having.

    Every shape that has to be caught, plus a clean one -- because a guard that flagged everything
    would satisfy the offending cases and report nothing usable about the real tree."""
    silent = "name: X\non:\n  pull_request:\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
    assert _declaration_offence("silent.yml", silent), (
        "a workflow with no `permissions:` at all has to be caught")

    per_job_only = silent.replace(
        "    runs-on: ubuntu-latest\n",
        "    permissions:\n      contents: read\n    runs-on: ubuntu-latest\n")
    assert _declaration_offence("per-job.yml", per_job_only), (
        "a workflow whose only grant is inside one job leaves the next job on the default")

    assert _declaration_offence("publish.yml", per_job_only) is None, (
        "_JOB_SCOPED must actually exempt the file it names, or the entry is decoration")
    assert _declaration_offence("publish.yml", "permissions:\n  contents: read\njobs:\n"), (
        "a _JOB_SCOPED workflow that moved its grant to the top no longer matches its own reason")

    top_level = "permissions:\n  contents: read\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
    assert _declaration_offence("fine.yml", top_level) is None, top_level

    _, blanket = _write_scopes("blanket.yml", "permissions: write-all\njobs:\n")
    assert blanket, "`permissions: write-all` grants more than the default it replaced"

    _, undeclared = _write_scopes("new.yml", "permissions:\n  contents: write\njobs:\n")
    assert undeclared, "a write scope with no entry in _WRITE_GRANTS has to be caught"

    assert _write_scopes("fine.yml", top_level) == (set(), []), (
        "a read-only workflow must come back clean, or the guard above flags everything")
