"""The legacy flag CLI — `python src/engine.py "…" --prd --epic`.

**Deprecated.** This is Requivo's original interface, kept working so that scripts and notes written
against it do not break. It is frozen: it gets no new artifact types, no new flags, and none of the
guarantees the subcommand CLI has grown since — it writes to the legacy `out/<slug>/` root rather than
the versioned session store, so its output carries no revisions, no provenance and no staleness
tracking, and it bypasses `SessionService` entirely.

Scheduled for removal in **1.1.0**. The equivalent modern commands are:

    python src/engine.py "request" --prd     →  requivo discover "request" && requivo prd <slug>
    python src/engine.py --from out/x/model.json --epic  →  requivo epic x

It lives in its own module so the boundary is visible: nothing in the supported surface imports from
here, and deleting this file is the whole removal.
"""

from __future__ import annotations

import sys
from pathlib import Path

from requivo.core.adapters import epic_export_json, to_github_json, to_gitlab_json
from requivo.core.persistence import _slug, load_model, save_model, write_artifact
from requivo.providers.anthropic import (
    EngineError,
    advise,
    derive_stories,
    estimate,
    generate_criteria,
    generate_epic,
    generate_prd,
    generate_release,
    new_client,
    run,
)
from requivo.render.markdown import criteria_markdown, epic_markdown, prd_markdown, release_markdown
from requivo.render.terminal import render_brief, render_estimate, render_stories, render_turn
from requivo.services.discovery import absorb_reasoning

DEPRECATION = (
    "⚠  The flag CLI is deprecated and will be removed in Requivo 1.1.0. It writes to the legacy "
    "out/<slug>/ layout — no revisions, no provenance, no staleness tracking.\n"
    "   Use the subcommand CLI instead: `requivo discover \"…\"`, then `requivo prd <slug>`. "
    "See docs/cli.md."
)

def _flag_value(args: list[str], name: str) -> str | None:
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def main() -> None:
    """Legacy flag CLI entry — announces the deprecation, then runs, turning a clean EngineError
    into a tidy exit."""
    print(DEPRECATION, file=sys.stderr)
    try:
        _run_legacy()
    except EngineError as e:
        print(f"\n{e}", file=sys.stderr)
        raise SystemExit(1) from None


def _run_legacy() -> None:
    args = sys.argv[1:]
    flags = {a for a in args if a.startswith("--")}
    from_path = _flag_value(args, "--from")
    # --release optionally takes a version token (e.g. --release v1.0); ignore a following flag.
    release_version = _flag_value(args, "--release") or ""
    if release_version.startswith("--"):
        release_version = ""
    consumed = {from_path, release_version}
    positional = [a for a in args if not a.startswith("--") and a not in consumed]
    client = new_client()

    if from_path:
        # Regenerate artifacts from a saved model — no discovery.
        out = load_model(Path(from_path))
        slug = Path(from_path).parent.name
        print(f"Loaded model ← {from_path}")
        quick = False
    elif positional:
        arg = positional[0]
        request = Path(arg).read_text() if Path(arg).exists() else arg
        slug = _slug(Path(arg).stem if Path(arg).exists() else request)
        # Interactive loop, or a single quick pass with --once / no TTY.
        quick = "--once" in flags or not sys.stdin.isatty()
        if quick:
            out = run(client, [{"role": "user", "content": request}])
            render_turn(out)
        else:
            # The interactive turn loop is shared with `requivo discover`; imported here rather than at
            # module scope so nothing in the supported surface depends on this module being importable.
            from requivo.cli import converse
            out = converse(client, request)
        if out:
            print(f"\nSaved model → {save_model(out, slug)}")
    else:
        print('Usage: python src/engine.py [--once] [--stories] [--estimate] [--prd] [--criteria] [--epic] [--epic-json] [--epic-github] [--epic-gitlab] [--release [version]] "request" | file.md')
        print('       python src/engine.py --from out/<slug>/model.json [--stories] [--estimate] [--prd] [--criteria] [--epic] [--epic-json] [--epic-github] [--epic-gitlab] [--release [version]]')
        sys.exit(1)

    if not out:
        return

    # The solution assessment is the default deliverable (skipped on a quick --once pass).
    if not quick:
        print("\nGenerating the solution assessment…")
        brief = advise(client, out)
        absorb_reasoning(out, brief)  # bake the reasoning into the model
        save_model(out, slug)          # re-save enriched (backfills the --from path too)
        render_brief(out, brief)

    # Delivery pipeline. --estimate implies stories (it estimates them).
    if "--stories" in flags or "--estimate" in flags:
        stories = derive_stories(client, out)
        render_stories(stories)
        if "--estimate" in flags:
            draft, soft, confidence = estimate(client, out, stories)
            render_estimate(draft, soft, confidence)

    # Artifact generators: model → file.
    if "--prd" in flags:
        print("\nGenerating the PRD…")
        markdown = prd_markdown(generate_prd(client, out))
        path = write_artifact(slug, "prd.md", markdown)
        print(markdown)
        print(f"\nWrote PRD → {path}")

    if "--criteria" in flags:
        print("\nGenerating the acceptance criteria…")
        markdown = criteria_markdown(generate_criteria(client, out))
        path = write_artifact(slug, "acceptance-criteria.md", markdown)
        print(markdown)
        print(f"\nWrote acceptance criteria → {path}")

    if flags & {"--epic", "--epic-json", "--epic-github", "--epic-gitlab"}:
        print("\nGenerating the delivery epic…")
        epic = generate_epic(client, out)  # one model call; every view renders from it
        if "--epic" in flags:
            markdown = epic_markdown(epic)
            path = write_artifact(slug, "epic.md", markdown)
            print(markdown)
            print(f"\nWrote epic → {path}")
        if "--epic-json" in flags:
            path = write_artifact(slug, "epic.json", epic_export_json(epic))
            print(f"Wrote neutral epic export (GitHub/GitLab-importable) → {path}")
        if "--epic-github" in flags:
            path = write_artifact(slug, "epic.github.json", to_github_json(epic, slug))
            print(f"Wrote GitHub issue-creation plan → {path}")
        if "--epic-gitlab" in flags:
            path = write_artifact(slug, "epic.gitlab.json", to_gitlab_json(epic, slug))
            print(f"Wrote GitLab issue-creation plan → {path}")

    if "--release" in flags:
        print("\nGenerating the release notes…")
        markdown = release_markdown(generate_release(client, out, release_version))
        path = write_artifact(slug, "release-notes.md", markdown)
        print(markdown)
        print(f"\nWrote release notes → {path}")


