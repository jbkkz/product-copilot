"""#296: `cli.py` hosts three no-LLM verbs alongside the provider ones, and the split's own prose has
to keep saying so or it drifts back to being wrong the way the issue found it.

Not a test of behaviour -- moving `status`/`demo`/`impact` into `deterministic/` was the other
legitimate resolution the issue named, and is not the one taken this round (see the changelog
fragment for #296, and `deterministic/__init__.py`'s own docstring for the reasoning). What this
guards is the *documented* axis staying true of the *actual* one: the three names appear, by name, in
the two places that make the claim, and the claim about where the code lives is checked directly
rather than only in prose.
"""
from __future__ import annotations

from pathlib import Path

from requivo import cli, deterministic

# The three journey verbs that never construct a provider client, kept beside the ones that do
# rather than moved into `deterministic/` -- see CLAUDE.md's tree entry for `cli.py` and
# `deterministic/__init__.py`'s docstring, both of which this file pins against drifting apart from
# the code again.
NO_LLM_JOURNEY_VERBS = ("_cmd_status", "_cmd_demo", "_cmd_impact")


def test_the_three_no_llm_journey_verbs_still_live_in_cli_py():
    """The claim is a fact about *where the code is*, checked directly: each handler is defined in
    `cli.py`'s own module, not imported from `deterministic/` or anywhere else."""
    for name in NO_LLM_JOURNEY_VERBS:
        func = getattr(cli, name)
        assert func.__module__ == "requivo.cli", (
            f"{name} moved out of cli.py -- the split's documented axis (CLAUDE.md's tree, "
            f"deterministic/__init__.py's docstring) needs to move with it, or be rewritten (#296)"
        )


def test_claude_md_names_the_three_exceptions_in_the_repo_tree():
    """The tree entries for `cli.py` and `deterministic/` are where CLAUDE.md states the split's
    axis. Anchored on the `requivo/` line the tree code-fence opens with, rather than on the first
    ``` fence in the file, which belongs to an unrelated shell example higher up."""
    page = (Path(__file__).resolve().parents[1] / "CLAUDE.md").read_text(encoding="utf-8")
    start = page.index("\nrequivo/\n")
    tree = page[start:page.index("\n```", start)]
    for verb in ("status", "demo", "impact"):
        assert verb in tree, (
            f"CLAUDE.md's repo tree no longer names {verb!r} as a no-LLM verb kept in cli.py (#296)"
        )


def test_the_deterministic_docstring_names_the_three_exceptions():
    doc = deterministic.__doc__ or ""
    for verb in ("status", "demo", "impact"):
        assert verb in doc, (
            f"deterministic/__init__.py's docstring no longer names {verb!r} as a journey verb kept "
            f"in cli.py rather than moved here (#296)"
        )
