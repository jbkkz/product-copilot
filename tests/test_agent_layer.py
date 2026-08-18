"""The tracked `.claude/` layer must stay inert for anyone without the maintainer's plugins.

Issue #2 reported that `.claude/jit-context/tools/01-oss/supertool-required.md` -- which declares
`tool: Read|Edit|Write|Glob|Grep`, `match: ~.*`, `mode: block` -- blocks every native file operation
for a contributor who clones this repository. Measured, it does not, and the reason is worth pinning
rather than re-deriving:

A jit-context rule is **data**. The only thing that reads it is a `PreToolUse` hook, and that hook is
registered by the `claude-jit-context` plugin, in that plugin's own `hooks/hooks.json`, as
`bash ${CLAUDE_PLUGIN_ROOT}/scripts/pre-tool-hook.sh`. A Claude Code project acquires hooks from
exactly four places: the user's own settings, the project's `.claude/settings.json`, the project's
`.claude/settings.local.json`, and an installed plugin. This repository ships nothing into the first
three. So without the plugin there is no hook, nothing reads the layer, and every native Read, Edit,
Write, Glob and Grep works normally.

`settings.local.json` is the one of those a contributor could commit by accident -- it is where a
personal hook would be written, and `.gitignore` now excludes it. It was *not* excluded when this
file was first written; it merely looked excluded, because the maintainer's machine carries a global
`~/.config/git/ignore` entry for it that no contributor has. The guard below scans whatever is
tracked, so it catches such a commit either way, but the exclusion stops the accident happening.

That inertness is a property of **this repository**, not of the plugin, and it is one commit away
from being false. A `hooks` block added to the tracked `.claude/settings.json`, or a hook script
committed under `.claude/`, would make the barrier #2 described real for every contributor -- and it
would do so silently, because the maintainer, who has the plugin, sees identical behaviour either
way. Nothing in CI would notice. This file is the guard.

Following the rule `test_boundaries.py` sets out: **an empty scan is an error, not an answer.**
`git ls-files` over a directory that has been renamed or removed prints nothing and exits 0, and
`assert not []` is an all-clear nobody earned.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# The layer these guards are about. If it moves, the scan below finds nothing and says so rather
# than passing green over an empty set.
RULE_LAYER = ".claude/jit-context/tools/01-oss/supertool-required.md"
PROJECT_SETTINGS = ".claude/settings.json"

# Suffixes a hook command could plausibly name. A hook is a shell command, so this is a heuristic --
# but a committed script is the only way one reaches a contributor from this repository, and every
# tracked file under `.claude/` today is data (`.md`, `.tsv`, `.json`). The Windows spellings are
# here even though every CI job in this repo runs on ubuntu-latest: a contributor's pull request is
# what this guard reads, and their machine is not this matrix.
EXECUTABLE_SUFFIXES = {
    ".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".ts", ".rb", ".pl",
    ".exe", ".bat", ".cmd", ".ps1",
}


def _looks_executable(rel: str) -> bool:
    """Does a tracked path look like a script a hook could be pointed at?

    Named, like `_declares_hooks`, so the control below can prove it fires. Case-folded because a
    `.SH` committed from a case-insensitive filesystem is the same file.
    """
    return Path(rel).suffix.lower() in EXECUTABLE_SUFFIXES


def _declares_hooks(data: object) -> bool:
    """Does a parsed settings document register hooks?

    Kept as a named function so the positive control below can prove it fires. A guard whose only
    exercise is the clean case reports coverage it does not have.
    """
    return isinstance(data, dict) and bool(data.get("hooks"))


def _tracked_under_dot_claude() -> list[str]:
    """Every path git tracks under `.claude/`, or a loud skip when git cannot be asked.

    `tracked` is the load-bearing word: `.claude/settings.local.json` and
    `.claude/jit-context/.discovery/` exist on the maintainer's disk and never reach a contributor,
    so a filesystem walk would answer a different question than the one asked here.
    """
    if not (REPO / ".git").exists():
        pytest.skip(
            "no .git here, so the tracked file set cannot be enumerated -- "
            "the hook-registration guards for .claude/ went unchecked in this run"
        )
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", ".claude"],
            cwd=REPO, capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover - environment
        pytest.skip(f"git ls-files failed ({exc}); the .claude/ guards went unchecked in this run")
    return [p for p in out.split("\0") if p]


def _tracked_content(rel: str) -> str:
    """The content git tracks at `rel` -- read from the index, never from the working copy.

    The question this file asks is what a *clone* gets, and a working-tree read answers a different
    one. CONTRIBUTING.md tells a contributor they may delete `.claude/` in their working copy; doing
    that leaves the path still listed by `git ls-files` while `Path.read_text` raises
    `FileNotFoundError`, so the documented advice and the guard shipped in the same commit collided
    and turned a local `pytest tests/` into an error. Reading the index is not a workaround for that
    -- it is the reading that matches the claim, and it happens to be immune to the working copy.
    """
    out = subprocess.run(
        ["git", "show", f":{rel}"],
        cwd=REPO, capture_output=True, check=True,
    ).stdout
    return out.decode("utf-8")


# -- the positive controls, first: a guard that cannot fire is not a guard ----------------------


def test_the_guard_refuses_a_scan_it_could_not_make():
    """The scan set must be non-empty and must still contain the two files these guards are about.

    Without this, renaming `.claude/` or deleting the rule layer turns every assertion below into a
    loop over nothing, which passes.
    """
    tracked = _tracked_under_dot_claude()
    assert tracked, "scanned no tracked files under .claude/ -- the guards below would pass vacuously"
    assert PROJECT_SETTINGS in tracked, (
        f"{PROJECT_SETTINGS} is not tracked any more; this guard is checking a file set that has "
        "moved, and its all-clear is meaningless until it is pointed at the new one"
    )
    assert RULE_LAYER in tracked, (
        f"{RULE_LAYER} is not tracked any more. That may well be correct -- issue #2 argued for "
        "exactly that -- but update this file so the reasoning above stops describing a tree that "
        "no longer exists"
    )


def test_the_hook_detector_fires_on_a_settings_file_that_does_register_hooks():
    """The must-fire half of `test_the_repository_registers_no_hooks`.

    That test asserts an absence, and an absence-assertion passes just as happily when the detector
    is broken as when the tree is clean. This is what tells those two apart.
    """
    registers = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "bash x.sh"}]}]}}
    assert _declares_hooks(registers), "the detector missed a settings file that plainly registers a hook"
    assert not _declares_hooks({"enabledPlugins": {"oss@dpt-plugins": True}})
    assert not _declares_hooks({"hooks": {}}), "an empty hooks block registers nothing"
    assert not _declares_hooks(["hooks"]), "a non-mapping document registers nothing"


def test_the_script_detector_fires_on_the_shapes_a_hook_could_point_at():
    """The must-fire half of `test_no_hook_script_is_tracked_under_dot_claude`.

    Every tracked file under `.claude/` today is data, so that guard's clean run exercises none of
    this classifier. Without a control it is a set literal nothing has ever read in anger.
    """
    for rel in (".claude/hooks/pre.sh", ".claude/x.py", ".claude/x.PS1", ".claude/nested/a.bat"):
        assert _looks_executable(rel), f"{rel!r} should be flagged as a script"
    for rel in (".claude/settings.json", ".claude/jit-context/tools/01-oss/00-index.tsv",
                ".claude/remember/identity.md", ".claude/no-suffix"):
        assert not _looks_executable(rel), f"{rel!r} is data and must not be flagged"


# -- the guards themselves ----------------------------------------------------------------------


def test_the_repository_registers_no_hooks():
    """Tracked settings must not register a `PreToolUse` (or any other) hook.

    This is the whole of #2's answer. The `mode: block` rule under `.claude/jit-context/` is inert
    for a contributor precisely because nothing this repository ships can invoke it. Register a hook
    here and that stops being true on the next clone.
    """
    checked = 0
    for rel in _tracked_under_dot_claude():
        if not rel.endswith(".json"):
            continue
        checked += 1
        try:
            data = json.loads(_tracked_content(rel))
        except json.JSONDecodeError as exc:
            pytest.fail(f"{rel!r}: tracked settings must be readable JSON ({exc})")
        except (OSError, subprocess.CalledProcessError) as exc:
            pytest.fail(f"{rel!r}: git lists it but could not produce its content ({exc})")
        assert not _declares_hooks(data), (
            f"{rel!r} registers hooks. A tracked hook runs for everyone who clones this repository, "
            "including a contributor with none of the maintainer's plugins installed -- which is "
            "the barrier issue #2 reported and measurement found absent. If this is deliberate, it "
            "needs to be documented in CONTRIBUTING.md as a hard requirement to contribute."
        )
    assert checked, "scanned no tracked JSON under .claude/ -- expected at least the project settings"


def test_no_hook_script_is_tracked_under_dot_claude():
    """The second route to the same barrier: a committed script for a hook to point at.

    Everything tracked under `.claude/` today is data a plugin may read. Nothing there is code this
    repository asks anyone to run.
    """
    tracked = _tracked_under_dot_claude()
    # The must-fire control lives in a sibling test, which is not enough: run this function alone,
    # under `-k`, or under any test sharding, and an empty scan would report a clean tree it never
    # looked at. The guard belongs in the same function as the assertion it protects.
    assert tracked, "scanned no tracked files under .claude/ -- this guard would pass vacuously"
    offenders = [rel for rel in tracked if _looks_executable(rel)]
    assert not offenders, (
        f"executable-looking files tracked under .claude/: {offenders}. Nothing under .claude/ is "
        "code a contributor runs; if that changed, say so in CONTRIBUTING.md first."
    )


def test_the_contributor_baseline_is_written_down():
    """The measurement is only useful if the person who hits the directory can read it.

    A contributor who opens `.claude/` finds a rule declaring `mode: block` over every file
    operation and has no way to know it cannot reach them. CONTRIBUTING.md says so; this keeps the
    two from drifting apart while the guards above stay green.
    """
    contributing = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    # Two tokens rather than one. `.claude/` alone would be satisfied by any incidental later
    # mention anywhere in the file; requiring the mechanism word as well means the assertion is
    # about the explanation existing, not about the string appearing. Deliberately not pinned to a
    # sentence: prose drifts, and a test that fails on a reworded paragraph gets deleted.
    for token in (".claude/", "jit-context"):
        assert token in contributing, (
            f"CONTRIBUTING.md must tell a contributor what the tracked .claude/ directory is and "
            f"that none of it is required to contribute -- {token!r} is missing (issue #2)"
        )
