from __future__ import annotations

import sys
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

from product_copilot.core.adapters import epic_export_json, to_github_json, to_gitlab_json
from product_copilot.core.contracts import EngineOutput
from product_copilot.core.discovery import run
from product_copilot.core.generators import (
    advise, derive_stories, estimate, generate_criteria, generate_epic,
    generate_prd, generate_release,
)
from product_copilot.core.persistence import _slug, load_model, save_model, write_artifact
from product_copilot.render.markdown import criteria_markdown, epic_markdown, prd_markdown, release_markdown
from product_copilot.render.terminal import (
    render_brief, render_estimate, render_stories, render_turn,
)

load_dotenv()


MAX_TURNS = 8


def converse(client: Anthropic, request: str) -> EngineOutput | None:
    """Fill the model, ask, feed answers back, until no high-value question remains.
    Returns the final model (None if the user stopped early). Finalization (brief, save) is
    handled by the caller so the interactive and --from paths share it."""
    messages = [{"role": "user", "content": request}]
    out = None
    for turn in range(1, MAX_TURNS + 1):
        print(f"\n──────────── TURN {turn} ────────────")
        out = run(client, messages)
        render_turn(out)

        if not out.questions:
            break

        print("\nYour answers (Enter = skip a question · 'q' = stop):")
        answers = []
        try:
            for i, q in enumerate(out.questions, 1):
                ans = input(f"  {i}. {q.q}\n     > ").strip()
                if ans.lower() == "q":
                    print("Stopped.")
                    return None
                if ans:
                    answers.append(f"[slot: {q.slot}] Q: {q.q} → A: {ans}")
        except (EOFError, KeyboardInterrupt):
            print("\nStopped.")
            return None

        if not answers:
            print("No answer provided — stopping.")
            return None

        # The assistant's prior model IS the state we refine — carry it in the history.
        messages.append({"role": "assistant", "content": out.model_dump_json()})
        messages.append({"role": "user", "content": "Client answers:\n" + "\n".join(answers)})
    else:
        print(f"\n⚠️  Reached the {MAX_TURNS}-turn limit.")

    return out


def _flag_value(args: list[str], name: str) -> str | None:
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def main() -> None:
    args = sys.argv[1:]
    flags = {a for a in args if a.startswith("--")}
    from_path = _flag_value(args, "--from")
    # --release optionally takes a version token (e.g. --release v1.0); ignore a following flag.
    release_version = _flag_value(args, "--release") or ""
    if release_version.startswith("--"):
        release_version = ""
    consumed = {from_path, release_version}
    positional = [a for a in args if not a.startswith("--") and a not in consumed]
    client = Anthropic()

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
            out = converse(client, request)
        if out:
            print(f"\nSaved model → {save_model(out, slug)}")
    else:
        print('Usage: python src/engine.py [--once] [--stories] [--estimate] [--prd] [--criteria] [--epic] [--epic-json] [--epic-github] [--epic-gitlab] [--release [version]] "request" | file.md')
        print('       python src/engine.py --from out/<slug>/model.json [--stories] [--estimate] [--prd] [--criteria] [--epic] [--epic-json] [--epic-github] [--epic-gitlab] [--release [version]]')
        sys.exit(1)

    if not out:
        return

    # The discovery brief is the default deliverable (skipped on a quick --once pass).
    if not quick:
        print("\nGenerating the discovery brief…")
        render_brief(out, advise(client, out))

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
