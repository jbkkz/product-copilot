from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
from pathlib import Path

from dotenv import load_dotenv

from requivo.core import persistence as store
from requivo.core.adapters import epic_export_json, to_github_json, to_gitlab_json
from requivo.core.analysis import _label, _readiness_blockers
from requivo.core.context import available_cards
from requivo.core.contracts import EngineOutput
from requivo.core.dependencies import propagate, resolve_slots
from requivo.core.errors import RequivoError, SessionNotFoundError
from requivo.core.persistence import _slug, load_model, save_model, write_artifact
from requivo.deterministic import register as register_deterministic
from requivo.paths import DEMO
from requivo.providers.anthropic import (
    EngineError,
    advise,
    answer_turn,
    current_model_name,
    derive_stories,
    estimate,
    generate_criteria,
    generate_epic,
    generate_prd,
    generate_release,
    new_client,
    run,
    track_usage,
)
from requivo.render.markdown import criteria_markdown, epic_markdown, prd_markdown, release_markdown
from requivo.render.terminal import (
    render_brief,
    render_dependency_map,
    render_estimate,
    render_impact,
    render_stale,
    render_stories,
    render_turn,
    render_usage,
)
from requivo.services.artifacts import ARTIFACT_FILENAMES, ArtifactService
from requivo.services.sessions import SessionService

load_dotenv()


MAX_TURNS = 8


def converse(client, request: str, only: list[str] | None = None) -> EngineOutput | None:
    """Fill the model, ask, feed answers back, until no high-value question remains.
    Returns the final model (None if the user stopped early). Finalization (brief, save) is
    handled by the caller so the interactive and --from paths share it. `only` restricts the context
    cards for every turn — held constant across the loop so the cached system prefix survives."""
    messages = [{"role": "user", "content": request}]
    out = None
    for turn in range(1, MAX_TURNS + 1):
        print(f"\n──────────── TURN {turn} ────────────")
        out = run(client, messages, only=only)
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
    """Legacy flag CLI entry — thin wrapper turning a clean EngineError into a tidy exit."""
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
# the API build one, so `requivo status` runs fully offline.


def _is_file_arg(arg: str) -> bool:
    """True if arg names an existing file. Two pathlib traps to sidestep: a blank string makes
    Path("") resolve to the current directory (`.`), which *exists* and then blows up on read_text;
    and a request longer than the OS filename limit makes Path.exists() *raise* instead of returning
    False. Both must read as 'not a file' so the request is used as text — the point of discover."""
    if not arg.strip():
        return False
    try:
        return Path(arg).exists()
    except OSError:
        return False


def _resolve_cards(spec: str) -> tuple[list[str], list[str]]:
    """Map a comma-separated --context spec to context-card stems. Returns (picked, unknown)."""
    avail = {c.lower(): c for c in available_cards()}
    picked, unknown = [], []
    for tok in spec.split(","):
        key = tok.strip().lower()
        if not key:
            continue
        (picked if key in avail else unknown).append(avail.get(key, tok.strip()))
    return picked, unknown


def _cmd_discover(a, client) -> None:
    if not a.request or not a.request.strip():
        print("discover needs a request: a sentence describing what to build, or a path to a file "
              "containing one.", file=sys.stderr)
        raise SystemExit(2)
    client = client or new_client()
    is_file = _is_file_arg(a.request)
    request = Path(a.request).read_text() if is_file else a.request

    only = None
    if a.context:
        picked, unknown = _resolve_cards(a.context)
        if unknown:
            print(f"Unknown context card(s): {', '.join(unknown)}. "
                  f"Available: {', '.join(available_cards())}", file=sys.stderr)
        if picked:  # if nothing valid was named, fall back to all cards rather than none
            only = picked
            print(f"Context cards: {', '.join(only)}")

    quick = a.once or not sys.stdin.isatty()
    if quick:
        out = run(client, [{"role": "user", "content": request}], only=only)
        render_turn(out)
    else:
        out = converse(client, request, only=only)
    if not out:
        return
    brief = None
    if not quick:
        print("\nGenerating the solution assessment…")
        brief = advise(client, out, only=only)
        _absorb_reasoning(out, brief)  # bake the reasoning into the model before it is applied
    # The provider's model is a *proposal* — it flows through the SAME validated apply path as a
    # Claude Code proposal (SessionService.update_model), writing the canonical `.requivo/sessions/`
    # store. There is no second save path.
    svc = SessionService()
    meta = svc.create_session(request, context_cards=only,
                              slug=(Path(a.request).stem if is_file else None),
                              provider="anthropic", model_name=current_model_name())
    svc.update_model(meta.slug, out.model_dump_json())
    if brief is not None:
        render_brief(out, brief)
    print(f"\nSaved session → {store.canonical_dir(meta.slug)}")
    if quick and out.questions:
        print(f'\n→ Answer and refine: requivo answer {meta.slug} "<your answers>"')


def _cmd_answer(a, client) -> None:
    client = client or new_client()
    svc = SessionService()
    slug = svc.resolve_slug(a.model)
    if not svc.exists(slug):
        raise SessionNotFoundError(f"no session '{slug}' to answer", details={"slug": slug})
    before = svc.load_model(slug)
    only = svc.cards(slug)
    out = answer_turn(client, before, svc.request_text(slug), a.answers, only=only)
    render_turn(out)
    # The refined model goes through the same validated apply path — which migrates a legacy session,
    # diffs against the prior revision, and flags any generated artifact that just went stale.
    result = svc.update_model(slug, out.model_dump_json())
    if result.stale_artifacts:
        pairs = [(t, ARTIFACT_FILENAMES[t]) for t in result.stale_artifacts]
        render_stale(pairs, [_label(sid) for sid in result.changed_slots])
    n_reasoning = len(result.invalidated_decisions) + len(result.invalidated_challenges)
    if n_reasoning:
        print(f"\n⚠  This change unseats {n_reasoning} piece(s) of the assessment's reasoning "
              f"({len(result.invalidated_decisions)} decision(s), {len(result.invalidated_challenges)} "
              f"premise(s)) — regenerate the brief to refresh it.")
    print(f"\nSaved session → {store.canonical_dir(slug)}")
    if not out.questions:
        print(f"\n✅ Discovery converged — run `requivo brief {slug}` for the assessment.")
    else:
        print(f'\n→ Keep going: requivo answer {slug} "<your answers>"')


def _resolve_ref(ref: str) -> tuple[EngineOutput, str]:
    """Resolve a reference to (model, slug). Accepts a model.json path (legacy or direct) OR a session
    slug in the canonical/legacy store — so the read verbs work both on a raw file and on a session."""
    p = Path(ref)
    if p.is_file():
        return load_model(p), p.parent.name
    svc = SessionService()
    if svc.exists(ref):
        return svc.load_model(ref), svc.resolve_slug(ref)
    raise SessionNotFoundError(f"no model file or session found for '{ref}'", details={"ref": ref})


def _status_payload(ref: str) -> tuple[EngineOutput, dict]:
    """(model, machine status). Readiness is always computed; revision + artifact freshness are added
    when the reference resolves to a canonical session."""
    out, slug = _resolve_ref(ref)
    blockers = _readiness_blockers(out)
    payload: dict = {
        "slug": slug,
        "readiness": {"ready": not blockers,
                      "blocking_slots": [{"slot": s, "label": _label(s)} for s in blockers]},
    }
    if store.session_exists(slug):
        meta = store.read_meta(slug)
        payload["revision"] = meta.current_revision
        payload["artifacts"] = {
            t: {"revision": st.revision, "filename": st.filename,
                "stale": st.stale or st.revision != meta.current_revision}
            for t, st in meta.artifact_status.items()
        }
    return out, payload


def _cmd_status(a, client) -> None:
    out, payload = _status_payload(a.model)
    if getattr(a, "json", False):
        print(json.dumps(payload, indent=2))
        return
    render_turn(out)


DEMO_SLUG = "event-checkin-reconciliation"


def _fenced_text(markdown: str) -> str:
    """Pull the terminal output back out of a saved assessment's ```text … ``` block, so the demo
    shows the clean assessment rather than the markdown wrapper. Falls back to the whole text."""
    m = re.search(r"```text\s*\n(.*?)```", markdown, re.DOTALL)
    return m.group(1).rstrip() if m else markdown.strip()


def _cmd_demo(a, client) -> None:
    """A no-API-key walkthrough of a real run, replayed from the saved event-check-in example.

    A visitor shouldn't need a key, a clone, and a venv before feeling what the product does. This
    renders the understanding + questions LIVE from the saved model (pure Python, proving the engine
    runs offline) and shows the assessment it produced — the differentiator — from disk. No network."""
    # Read from the frozen payload bundled in the package (so `requivo demo` works from a wheel, no clone),
    # but point the visitor at the browsable copy under examples/ at the repo root.
    demo = DEMO
    request = (demo / "request.md").read_text().strip()
    out = load_model(demo / "model.json")
    assessment = _fenced_text((demo / "solution-assessment.md").read_text())

    bar = "═" * 72
    print(bar)
    print("  REQUIVO — DEMO   (no API key needed)")
    print("  A real run, replayed from saved output: this is what the engine made")
    print("  of the messy request below — nothing is called.")
    print(bar)

    print("\n\n① THE REQUEST  — a rambling, multi-feature client email\n")
    print(textwrap.indent(request, "  "))

    print("\n\n② WHAT THE ENGINE MADE OF IT  — computed live from the saved model, no API\n")
    render_turn(out)

    print("\n\n③ THE SOLUTION ASSESSMENT  — a judgment, not a recap (the differentiator)\n")
    print(assessment)

    print("\n\n" + bar)
    print("  ④ EVERYTHING ELSE IS A VIEW OF THE SAME MODEL")
    print("     Regenerated from this one model.json, no re-discovery:")
    for name in ("epic.md", "acceptance-criteria.md"):
        if (demo / name).exists():
            print(f"       • examples/{DEMO_SLUG}/{name}")
    print('\n  Your turn:   requivo discover "<your own request>"')
    print(bar)


def _cmd_impact(a, client) -> None:
    """Offline query over the dependency DAG — no API call. With slots, show their blast
    radius; without, map every slot's downstream."""
    out, _ = _resolve_ref(a.model)
    if not a.slots:
        render_dependency_map(out)
        return
    resolved, unmatched = resolve_slots(a.slots)
    if unmatched:
        print(f"Unknown slot(s): {', '.join(unmatched)} — use a slot id or a label word "
              f"(e.g. 'permissions', 'workflow', 'reporting').")
    if resolved:
        render_impact(propagate(out, resolved))


# Provider-backed generators. Each resolves a session (slug or model.json path), reasons via the
# provider, and saves the artifact into the canonical store through ArtifactService — the same place
# the deterministic `artifact save` verb writes, and never the legacy `out/`. A legacy session is
# migrated on this first artifact write.


def _generator_session(a, client) -> tuple[object, EngineOutput, str, list[str] | None]:
    """Shared preamble for the provider generators: (client, model, slug, cards). Ensures the session
    is canonical so the produced artifact is tracked in `.requivo/sessions/`."""
    client = client or new_client()
    svc = SessionService()
    slug = svc.resolve_slug(a.model)
    if not svc.exists(slug):
        raise SessionNotFoundError(f"no session '{slug}'", details={"slug": slug})
    out = svc.load_model(slug)
    cards = svc.cards(slug)
    svc.ensure_canonical(slug)  # migrate a legacy session before writing its first artifact
    return client, out, slug, cards


def _save_artifact(slug: str, artifact_type: str, markdown: str, label: str) -> None:
    print(markdown)
    st = ArtifactService().save(slug, artifact_type, markdown)
    print(f"\nWrote {label} → {store.canonical_dir(slug) / 'artifacts' / st.filename}")


def _cmd_brief(a, client) -> None:
    client, out, slug, cards = _generator_session(a, client)
    brief = advise(client, out, only=cards)
    _absorb_reasoning(out, brief)
    # Backfilling the reasoning into the model is a model change — it goes through the apply path,
    # so downstream generators inherit the reasoning from a proper new revision.
    SessionService().update_model(slug, out.model_dump_json())
    render_brief(out, brief)


def _cmd_prd(a, client) -> None:
    client, out, slug, cards = _generator_session(a, client)
    _save_artifact(slug, "prd", prd_markdown(generate_prd(client, out, only=cards)), "PRD")


def _cmd_stories(a, client) -> None:
    client, out, slug, cards = _generator_session(a, client)
    render_stories(derive_stories(client, out, only=cards))  # terminal-only view (no saved artifact)


def _cmd_estimate(a, client) -> None:
    client, out, slug, cards = _generator_session(a, client)
    stories = derive_stories(client, out, only=cards)
    render_stories(stories)
    draft, soft, confidence = estimate(client, out, stories, only=cards)
    render_estimate(draft, soft, confidence)


def _cmd_criteria(a, client) -> None:
    client, out, slug, cards = _generator_session(a, client)
    _save_artifact(slug, "criteria",
                   criteria_markdown(generate_criteria(client, out, only=cards)), "acceptance criteria")


def _cmd_epic(a, client) -> None:
    client, out, slug, cards = _generator_session(a, client)
    epic = generate_epic(client, out, only=cards)  # one model call; every view renders from it
    _save_artifact(slug, "epic", epic_markdown(epic), "epic")
    if a.json:
        print(f"Wrote neutral epic export → {store.write_artifact_file(slug, 'epic.json', epic_export_json(epic))}")
    if a.github:
        print(f"Wrote GitHub issue-creation plan → "
              f"{store.write_artifact_file(slug, 'epic.github.json', to_github_json(epic, slug))}")
    if a.gitlab:
        print(f"Wrote GitLab issue-creation plan → "
              f"{store.write_artifact_file(slug, 'epic.gitlab.json', to_gitlab_json(epic, slug))}")


def _cmd_release(a, client) -> None:
    client, out, slug, cards = _generator_session(a, client)
    _save_artifact(slug, "release",
                   release_markdown(generate_release(client, out, a.version, only=cards)), "release notes")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="requivo",
        description="Requivo — turns a vague request into a structured solution model.",
    )
    p.add_argument("--workspace", metavar="DIR",
                   help="workspace root for sessions (default: cwd). Sessions live in "
                        "<workspace>/.requivo/sessions/. Place before the command.")
    sub = p.add_subparsers(dest="command", required=True, metavar="<command>")

    # The deterministic surface (doctor / session / model / artifact) — no LLM, no API key.
    register_deterministic(sub)

    d = sub.add_parser("discover", help="run discovery on a request (a string or a file path)")
    d.add_argument("request", help="the client request, or a path to a file containing it")
    d.add_argument("--once", action="store_true", help="single pass (status + questions), no interactive loop")
    d.add_argument("--context", metavar="CARDS",
                   help="comma-separated context cards to load instead of all "
                        "(e.g. b2b-platform,financial-reporting); sharpens discovery by dropping "
                        "irrelevant cards. Applies to this discovery only.")
    d.set_defaults(func=_cmd_discover)

    demo = sub.add_parser("demo", help="replay a real run from saved output — no API key needed")
    demo.set_defaults(func=_cmd_demo)

    def model_cmd(name: str, help_: str, func, extra=None):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("model", help="a session slug, or a path to a saved model.json")
        if extra:
            extra(sp)
        sp.set_defaults(func=func)

    model_cmd("answer", "feed the client's answers back and refine the model one more turn",
              _cmd_answer, lambda sp: sp.add_argument("answers", help="the client's answers, as free text"))
    model_cmd("status", "show the understanding checklist + open questions", _cmd_status,
              lambda sp: sp.add_argument("--json", action="store_true", help="emit a machine status snapshot"))
    model_cmd("impact", "show what depends on given slots (blast radius); no slots = full map",
              _cmd_impact, lambda sp: sp.add_argument("slots", nargs="*",
              help="slot ids or label words (e.g. permissions workflow); omit for the full map"))
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


def app(argv: list[str] | None = None, client=None) -> None:
    """Entry point for the `requivo` command (and its `pc` alias, and `python -m requivo`)."""
    args = _build_parser().parse_args(argv)
    # A global --workspace redirects where sessions are read/written, for the duration of this run.
    if getattr(args, "workspace", None):
        os.environ["REQUIVO_WORKSPACE"] = args.workspace
    want_json = getattr(args, "json", False)
    # Track the run's API footprint and print it after the command. Offline verbs make no call, so
    # the ledger stays empty and render_usage() prints nothing.
    with track_usage() as ledger:
        try:
            args.func(args, client)
        except RequivoError as e:
            # Every clean, expected failure — a core validation/session error OR a provider transport
            # error (EngineError is a RequivoError) — surfaces without a traceback. With --json the
            # caller (e.g. Claude Code) gets the structured envelope; otherwise a one-line message.
            render_usage(ledger)
            if want_json:
                print(json.dumps(e.to_dict(), indent=2))
            else:
                print(f"\n{e}", file=sys.stderr)
            raise SystemExit(1) from None
    render_usage(ledger)
