from __future__ import annotations

import argparse
import sys
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

from product_copilot.core.adapters import epic_export_json, to_github_json, to_gitlab_json
from product_copilot.core.contracts import EngineOutput
from product_copilot.core.discovery import answer_turn, run
from product_copilot.core.generators import (
    advise, derive_stories, estimate, generate_criteria, generate_epic,
    generate_prd, generate_release,
)
from product_copilot.core.persistence import (
    _slug, load_model, load_request, save_model, save_request, write_artifact,
)
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


def _absorb_reasoning(out: EngineOutput, brief) -> None:
    """Persist the assessment's reasoning into the model so every generator inherits it,
    not just the facts. Called wherever advise() runs, before the model is saved."""
    out.decisions = brief.decisions
    out.challenges = brief.challenges
    out.opportunities = brief.opportunities


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

    # The solution assessment is the default deliverable (skipped on a quick --once pass).
    if not quick:
        print("\nGenerating the solution assessment…")
        brief = advise(client, out)
        _absorb_reasoning(out, brief)  # bake the reasoning into the model
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


# ── Subcommand CLI (`pc`) ─────────────────────────────────────────────────────
# The modern surface. A thin layer over the same core the legacy flag CLI above
# uses: each handler parses, calls core, renders, writes — no business logic here.
# `app()` takes an optional client so tests can inject a stub; only verbs that hit
# the API build one, so `pc status` runs fully offline.


def _load(model_path: str) -> tuple[EngineOutput, str]:
    """Load a saved model and recover its slug (the out/<slug>/ folder name)."""
    path = Path(model_path)
    return load_model(path), path.parent.name


def _emit(slug: str, filename: str, markdown: str, label: str) -> None:
    path = write_artifact(slug, filename, markdown)
    print(markdown)
    print(f"\nWrote {label} → {path}")


def _cmd_discover(a, client) -> None:
    client = client or Anthropic()
    is_file = Path(a.request).exists()
    request = Path(a.request).read_text() if is_file else a.request
    slug = _slug(Path(a.request).stem if is_file else request)
    quick = a.once or not sys.stdin.isatty()
    if quick:
        out = run(client, [{"role": "user", "content": request}])
        render_turn(out)
    else:
        out = converse(client, request)
    if not out:
        return
    save_request(slug, request)  # so `pc answer` can resume this discovery statelessly
    if not quick:
        print("\nGenerating the solution assessment…")
        brief = advise(client, out)
        _absorb_reasoning(out, brief)  # bake the reasoning into the model before saving
        render_brief(out, brief)
    print(f"\nSaved model → {save_model(out, slug)}")
    if quick and out.questions:
        print(f'\n→ Answer and refine: pc answer out/{slug}/model.json "<your answers>"')


def _cmd_answer(a, client) -> None:
    client = client or Anthropic()
    out, slug = _load(a.model)
    out = answer_turn(client, out, load_request(Path(a.model)), a.answers)
    render_turn(out)
    print(f"\nSaved model → {save_model(out, slug)}")
    if not out.questions:
        print("\n✅ Discovery converged — run `pc brief` for the assessment.")
    else:
        print(f'\n→ Keep going: pc answer {a.model} "<your answers>"')


def _cmd_status(a, client) -> None:
    out, _ = _load(a.model)
    render_turn(out)


def _cmd_brief(a, client) -> None:
    client = client or Anthropic()
    out, slug = _load(a.model)
    brief = advise(client, out)
    _absorb_reasoning(out, brief)
    save_model(out, slug)  # backfill: persist the reasoning back into the saved model
    render_brief(out, brief)


def _cmd_prd(a, client) -> None:
    client = client or Anthropic()
    out, slug = _load(a.model)
    _emit(slug, "prd.md", prd_markdown(generate_prd(client, out)), "PRD")


def _cmd_stories(a, client) -> None:
    client = client or Anthropic()
    out, _ = _load(a.model)
    render_stories(derive_stories(client, out))


def _cmd_estimate(a, client) -> None:
    client = client or Anthropic()
    out, _ = _load(a.model)
    stories = derive_stories(client, out)
    render_stories(stories)
    draft, soft, confidence = estimate(client, out, stories)
    render_estimate(draft, soft, confidence)


def _cmd_criteria(a, client) -> None:
    client = client or Anthropic()
    out, slug = _load(a.model)
    _emit(slug, "acceptance-criteria.md", criteria_markdown(generate_criteria(client, out)), "acceptance criteria")


def _cmd_epic(a, client) -> None:
    client = client or Anthropic()
    out, slug = _load(a.model)
    epic = generate_epic(client, out)  # one model call; every view renders from it
    _emit(slug, "epic.md", epic_markdown(epic), "epic")
    if a.json:
        print(f"Wrote neutral epic export → {write_artifact(slug, 'epic.json', epic_export_json(epic))}")
    if a.github:
        print(f"Wrote GitHub issue-creation plan → {write_artifact(slug, 'epic.github.json', to_github_json(epic, slug))}")
    if a.gitlab:
        print(f"Wrote GitLab issue-creation plan → {write_artifact(slug, 'epic.gitlab.json', to_gitlab_json(epic, slug))}")


def _cmd_release(a, client) -> None:
    client = client or Anthropic()
    out, slug = _load(a.model)
    _emit(slug, "release-notes.md", release_markdown(generate_release(client, out, a.version)), "release notes")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pc",
        description="Product Copilot — turns a vague request into a structured solution model.",
    )
    sub = p.add_subparsers(dest="command", required=True, metavar="<command>")

    d = sub.add_parser("discover", help="run discovery on a request (a string or a file path)")
    d.add_argument("request", help="the client request, or a path to a file containing it")
    d.add_argument("--once", action="store_true", help="single pass (status + questions), no interactive loop")
    d.set_defaults(func=_cmd_discover)

    def model_cmd(name: str, help_: str, func, extra=None):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("model", help="path to a saved out/<slug>/model.json")
        if extra:
            extra(sp)
        sp.set_defaults(func=func)

    model_cmd("answer", "feed the client's answers back and refine the model one more turn",
              _cmd_answer, lambda sp: sp.add_argument("answers", help="the client's answers, as free text"))
    model_cmd("status", "show the understanding checklist + open questions", _cmd_status)
    model_cmd("brief", "generate the solution assessment", _cmd_brief)
    model_cmd("prd", "generate the PRD", _cmd_prd)
    model_cmd("stories", "derive user stories", _cmd_stories)
    model_cmd("estimate", "derive stories and estimate them (day ranges)", _cmd_estimate)
    model_cmd("criteria", "generate Given/When/Then acceptance criteria", _cmd_criteria)

    def epic_flags(sp):
        sp.add_argument("--json", action="store_true", help="also write the neutral epic.json export")
        sp.add_argument("--github", action="store_true", help="also write a GitHub issue-creation plan")
        sp.add_argument("--gitlab", action="store_true", help="also write a GitLab issue-creation plan")

    model_cmd("epic", "generate the delivery epic (+ optional tracker plans)", _cmd_epic, epic_flags)
    model_cmd("release", "generate client-facing release notes", _cmd_release,
              lambda sp: sp.add_argument("version", nargs="?", default="", help="optional version label to stamp"))

    return p


def app(argv: list[str] | None = None, client: Anthropic | None = None) -> None:
    """Entry point for the `pc` command and `python -m product_copilot`."""
    args = _build_parser().parse_args(argv)
    args.func(args, client)
