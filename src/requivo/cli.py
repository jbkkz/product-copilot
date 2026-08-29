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
from requivo.core.analysis import _label, model_status
from requivo.core.context import resolve_cards
from requivo.core.contracts import EngineOutput
from requivo.core.dependencies import propagate, resolve_slots
from requivo.core.errors import RequivoError, SessionNotFoundError
from requivo.core.persistence import load_model
from requivo.deterministic import read_user_text
from requivo.deterministic import register as register_deterministic
from requivo.paths import DEMO

# The only two names this surface takes from `requivo.providers`, and each is a *surface* concern
# rather than an orchestration one: `new_client` builds the SDK client that gets handed to
# DiscoveryService, and `EngineError` is an exception type the top-level handler catches — neither
# reasons about anything. `track_usage` was a third until #167 and is no longer a provider name at
# all: the ledger is provider-neutral and lives in `requivo.usage`.
#
# `run`, `advise` and `estimate` used to be here too, and that was #77: the interactive `discover`
# branch drove two provider calls of its own before letting the service do the write, so the primary
# surface held a second orchestration of discovery while CLAUDE.md, the README and
# docs/architecture.md all said it did not. `tests/test_boundaries.py` guards the list now, in both
# directions — an unexpected import fails, and so does an entry here that nothing imports.
from requivo.providers.anthropic import new_client
from requivo.providers.errors import EngineError
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
from requivo.services.artifacts import ARTIFACT_FILENAMES
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService
from requivo.streams import configure_streams, safe_write
from requivo.usage import track_usage

load_dotenv()


MAX_TURNS = 8

# A command whose *work* succeeded and whose *report* could not be encoded. Distinct from 1 (a clean,
# expected failure) so a script can tell "nothing happened" from "it happened and you cannot see it";
# see the `UnicodeEncodeError` arm in `app()` and `streams.py` for why the distinction is the point.
EXIT_RENDER_FAILED = 3

# Two messages, because this arm cannot see how far the handler got and must not pretend it can.
# `app()` wraps the whole handler, and several verbs print before they mutate anything -- `discover`
# echoes its context cards before the provider call, and `doctor`/`status`/`schema` never mutate at
# all. A single message asserting "whatever this changed HAS been applied" is therefore false about
# roughly half the verbs, which is the same misreporting this branch exists to remove, one layer up.
#
# What the arm *can* see is the usage ledger: a non-empty one means a provider call completed and was
# billed. That is the fact worth being precise about, so it is read rather than assumed.
_RENDER_FAILED_HEAD = (
    "\n"
    "Requivo could not encode its output for this console: {error}\n"
    "\n"
    "This failed while *printing*, which happens after the command has done its work.\n"
)

_RENDER_FAILED_PAID = (
    "A provider call HAS completed and been billed on this run, and any revision it produced has\n"
    "already been applied. Do not re-run this command -- you would pay for a second call and stack\n"
    "a second revision on the first. Check with `requivo status <session>`.\n"
)

_RENDER_FAILED_UNPAID = (
    "No provider call was made on this run, so nothing has been billed. A local change may still\n"
    "have been written -- `model apply` and `artifact save` mutate without calling out -- so check\n"
    "with `requivo status <session>` before re-running rather than assuming either way.\n"
)

_USAGE_UNPRINTABLE = (
    "\n(the API usage summary for this run could not be encoded for this console)\n"
)


def _render_usage_safely(ledger) -> None:
    """`render_usage`, made unable to end the process.

    Found by the audit on this branch, and it is the same ordering bug one call further out.
    `render_usage` prints a middle dot and an em dash, and two of its three call sites are *outside*
    the `UnicodeEncodeError` arm below -- one in the `RequivoError` handler, one after a wholly
    successful run. On a stream `configure_streams` could not reach, a successful `requivo brief`
    therefore still died at the usage line: after the provider call was billed and the revision
    applied, which is precisely the failure #29 exists to close.

    A usage summary is never worth that, so it degrades to a stated absence rather than an exception.
    Stated, not silent: a line nobody can read is a different thing from a run that made no calls,
    and the two must not print the same way.
    """
    try:
        render_usage(ledger)
    except UnicodeEncodeError:
        safe_write(sys.stderr, _USAGE_UNPRINTABLE)


_RENDER_FAILED_TAIL = (
    "\n"
    "Set PYTHONIOENCODING=utf-8, or redirect to a file, to see the output itself.\n"
    "Run `requivo doctor` to see which stream could not be configured.\n"
)


class DraftingFailed(Exception):
    """A provider failure (or a Ctrl-C) *during* a draft turn, carrying the work that survived it.

    `converse` drafts up to eight paid turns in memory and writes none of them — deliberate, because
    what is drafted becomes real through the one validated apply path. The cost of that design is that
    an exception anywhere in the loop used to discard every prior turn **and** every answer the user
    typed, leaving the session at revision 0 and printing a transport message that named neither the
    session nor a way back. One transient 529 on turn 8 threw away seven paid calls and ten minutes of
    typing (#202).

    So the loop stops raising the transport error directly. It raises this, which carries the last
    model that succeeded, and `_cmd_discover` persists that model before letting the failure surface.
    `last` is None only when the very first turn failed, where there is genuinely nothing to save.
    Pinned by `test_a_failed_draft_turn_persists_the_turns_that_succeeded`.
    """

    def __init__(self, cause: BaseException, last: EngineOutput | None, turn: int) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.last = last
        self.turn = turn


def converse(disco: DiscoveryService, request: str, only: list[str] | None = None) -> EngineOutput | None:
    """Fill the model, ask, feed answers back, until no high-value question remains.
    Returns the final model (None if the user stopped early). Finalization (brief, save) is
    handled by the caller so the interactive and --from paths share it. `only` restricts the context
    cards for every turn — held constant across the loop so the cached system prefix survives.

    This is the CLI's job and all of it: prompting, rendering, and deciding when to stop. The
    reasoning is `DiscoveryService.draft_turn`, so the loop holds no provider client of its own and a
    second interactive surface reuses the same operation instead of copying this function (#77).

    **The model is the state that is carried, not a transcript.** Each turn hands back the model so
    far plus the answers just given — the same shape `requivo answer` and the Web form already use,
    so there is one turn operation across every surface rather than a conversational one here and a
    stateless one everywhere else. Turns 1 and 2 send exactly what the old in-CLI loop sent; from
    turn 3 the earlier rounds of question-and-answer are no longer re-sent, because the evidence they
    produced is in the model being carried."""
    out = None
    answers = None
    for turn in range(1, MAX_TURNS + 1):
        print(f"\n──────────── TURN {turn} ────────────")
        try:
            out = disco.draft_turn(request, current_model=out, answers=answers, cards=only)
        except (EngineError, KeyboardInterrupt) as e:
            # `out` still holds the last turn that succeeded, because the failed assignment did not
            # land — and that model is every answer the client has given so far, since the model is
            # what this loop carries. Handing it to the caller is the difference between a transient
            # 529 costing one turn and it costing all of them (#202).
            raise DraftingFailed(e, out, turn) from e
        render_turn(out)

        if not out.questions:
            break

        print("\nYour answers (Enter = skip a question · 'q' = stop):")
        replies = []
        try:
            for i, q in enumerate(out.questions, 1):
                ans = input(f"  {i}. {q.q}\n     > ").strip()
                if ans.lower() == "q":
                    print("Stopped.")
                    return None
                if ans:
                    replies.append(f"[slot: {q.slot}] Q: {q.q} → A: {ans}")
        except (EOFError, KeyboardInterrupt):
            print("\nStopped.")
            return None

        if not replies:
            print("No answer provided — stopping.")
            return None

        answers = "\n".join(replies)
    else:
        print(f"\n⚠️  Reached the {MAX_TURNS}-turn limit.")

    return out


# ── Subcommand CLI (`pc`) ─────────────────────────────────────────────────────
# The modern surface. A thin layer over the same core: each handler parses, calls
# the services, renders, writes — no business logic here.
# `app()` takes an optional client so tests can inject a stub; only verbs that hit
# the API build one, so `requivo status` runs fully offline.


def _is_file_arg(arg: str) -> bool:
    """True if arg names an existing *file*. Three pathlib traps to sidestep: a blank string makes
    Path("") resolve to the current directory, which exists; a bare directory name exists too, and
    `.exists()` accepts both — the next line then calls `read_text()` on a directory and raises. And a
    request longer than the OS filename limit makes the check *raise* rather than return False. All
    three must read as 'not a file', so the request is used as text — the point of discover."""
    if not arg.strip():
        return False
    try:
        return Path(arg).is_file()
    except OSError:
        return False


def _say_saved(slug: str) -> None:
    """Where the session landed. `canonical_dir` direct, and justified (#76): the path *is* the
    answer, and `SessionRepository` exposes none because a non-file backing has none. Same at the
    three other display sites in this file and in `deterministic/sessions.py`."""
    print(f"\nSaved session → {store.canonical_dir(slug)}")


def _rescue_drafted(disco, request: str, e: DraftingFailed, *, cards, slug: str):
    """Persist what an interrupted drafting loop had already paid for, then let the failure surface.

    Every abort path after `claim_session` must name the session, and this one must also *keep* the
    work: the turns that succeeded are in `e.last`, because the model is what the loop carries. The
    user retries with `requivo answer`, which works from any revision >= 1, instead of restarting a
    conversation they already had at full price (#202).

    Turn 1 failing is the one case with nothing to save; the session stays at revision 0 and
    `requivo discover` is still the right retry, so it says so rather than pointing at `answer`.
    Never returns. Pinned by `test_a_failed_draft_turn_persists_the_turns_that_succeeded`."""
    if e.last is None:
        print(f"\nSaved request → {store.canonical_dir(slug)}", file=sys.stderr)
        print("Nothing was drafted, so the session is unchanged — re-run `requivo discover` to try "
              "again.", file=sys.stderr)
    else:
        kept = e.turn - 1
        slug = disco.finalize_discovery(request, e.last, cards=cards, slug=slug,
                                        brief=None, surface="cli-discover")
        print(f"\nTurn {e.turn} failed, so the {kept} turn(s) before it were saved rather than "
              f"discarded.", file=sys.stderr)
        print(f"Saved session → {store.canonical_dir(slug)}", file=sys.stderr)
        print(f'Continue where you left off with:\n  requivo answer {slug} "<your answers>"',
              file=sys.stderr)
    if isinstance(e.cause, RequivoError):
        raise e.cause
    # A Ctrl-C landing inside the provider call, which `converse`'s prompt-level catch never saw:
    # it is not a `RequivoError`, so `app()` would have let it out as a traceback (#202).
    print("\nInterrupted.", file=sys.stderr)
    raise SystemExit(1) from e.cause


def _cmd_discover(a, client) -> None:
    if not a.request or not a.request.strip():
        print("discover needs a request: a sentence describing what to build, or a path to a file "
              "containing one.", file=sys.stderr)
        raise SystemExit(2)
    client = client or new_client()
    is_file = _is_file_arg(a.request)
    request = read_user_text(Path(a.request)) if is_file else a.request

    # One resolver, in Core, shared with the deterministic verbs and the Web: an unknown card is a hard
    # error. This used to warn and carry on with `only = None`, which does not mean "the cards you
    # named minus the typo" — it means *every* card. A misspelling widened the context instead of
    # narrowing it, and the run looked like it had honoured the selection.
    only = resolve_cards(a.context.split(",")) if a.context else None
    if only:
        print(f"Context cards: {', '.join(only)}")

    # Discovery orchestration (run the provider → apply through the validated path) lives in the shared
    # DiscoveryService, so the Web drives the exact same pipeline — the CLI only owns the interactive
    # TTY loop and the rendering.
    disco = DiscoveryService(client=client)
    # A filename is a *suggestion* for the slug, not a slug: "Leave Approval v2.md" has a space and a
    # capital, and a slug names a directory under the session store, so it is validated strictly.
    # Passing the raw stem through turned a perfectly ordinary input file into an invalid_slug error.
    slug_hint = SessionService.slug_hint(Path(a.request).stem) if is_file else None
    quick = a.once or not sys.stdin.isatty()
    if quick:
        slug = disco.start(request, cards=only, slug=slug_hint, finalize=False, surface="cli-discover")
        out = disco.sessions.load_model(slug)
        render_turn(out)
        _say_saved(slug)
        if out.questions:
            print(f'\n→ Answer and refine: requivo answer {slug} "<your answers>"')
        return

    # Invariant 13's gate, here rather than only inside `finalize_discovery`: refusing after the
    # loop meant paying for up to nine provider calls first (#133). Pinned by
    # `test_both_discover_entry_points_refuse_a_refined_session_before_paying`.
    slug = disco.claim_session(request, cards=only, slug=slug_hint).slug
    try:
        out = converse(disco, request, only=only)
    except DraftingFailed as e:
        _rescue_drafted(disco, request, e, cards=only, slug=slug)
    if not out:
        # Claiming first means an abandoned discovery leaves the request captured at revision 0
        # rather than nothing, so say where it went — see
        # `test_stopping_early_leaves_the_claimed_session_and_says_where`.
        print(f"\nSaved request → {store.canonical_dir(slug)}")
        return

    # **The write comes before the last paid call, and that ordering is the fix** (#202). The
    # assessment used to be reasoned first and passed into `finalize_discovery`, so an `EngineError`
    # on that ninth call discarded all eight drafted turns along with it. Persisting first costs
    # nothing — `generate(slug, "brief")` reads the session back, absorbs the assessment's reasoning
    # as a revision of its own and saves the document, which is the same path every other surface
    # takes — and it turns the worst failure in the product into one retryable call. Pinned by
    # `test_a_failed_assessment_leaves_the_discovery_saved_and_names_the_retry`.
    slug = disco.finalize_discovery(request, out, cards=only, slug=slug,
                                    brief=None, surface="cli-discover")
    _say_saved(slug)
    print("\nGenerating the decision brief…")
    try:
        gen = disco.generate(slug, "brief", surface="cli-discover")
    except RequivoError as e:
        print(f"\nThe decision brief failed: {e}", file=sys.stderr)
        print(f"Your discovery is saved and nothing was lost — retry just this step with:\n"
              f"  requivo brief {slug}", file=sys.stderr)
        raise SystemExit(1) from e
    # `gen.model`, not `out`: the assessment's reasoning has been absorbed into the model as a
    # revision by now, so this renders what was actually saved rather than the pre-absorption copy.
    render_brief(gen.model, gen.artifact)


def _cmd_answer(a, client) -> None:
    # Same shared orchestration as the Web: DiscoveryService folds the answers in and applies the
    # refined model through the validated path (diff → revision → stale-flag).
    disco = DiscoveryService(client=client)
    svc = disco.sessions
    slug = svc.resolve_slug(a.model)
    if not svc.exists(slug):
        raise SessionNotFoundError(f"no session '{slug}' to answer", details={"slug": slug})
    result = disco.answer(slug, a.answers, surface="cli-answer")
    out = svc.load_model(slug)
    render_turn(out)
    if result.stale_artifacts:
        pairs = [(t, ARTIFACT_FILENAMES[t]) for t in result.stale_artifacts]
        render_stale(pairs, [_label(sid) for sid in result.changed_slots])
    n_reasoning = len(result.invalidated_decisions) + len(result.invalidated_challenges)
    if n_reasoning:
        print(f"\n⚠  This change unseats {n_reasoning} piece(s) of the decision brief's reasoning "
              f"({len(result.invalidated_decisions)} decision(s), {len(result.invalidated_challenges)} "
              f"premise(s)) — regenerate the brief to refresh it.")
    print(f"\nSaved session → {store.canonical_dir(slug)}")
    if not out.questions:
        print(f"\n✅ Discovery converged — run `requivo brief {slug}` for the decision brief.")
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
    """(model, machine status). The model-derived view (readiness, understanding, questions, summary,
    gaps) comes from the shared `model_status` projection — the same one `SessionService.status` uses,
    so there is no second status implementation to drift. Revision, context and artifact freshness are
    layered on when the reference resolves to a canonical session (a bare model.json has none)."""
    out, slug = _resolve_ref(ref)
    payload: dict = {"slug": slug, **model_status(out)}
    svc = SessionService()
    if svc.exists(slug):
        meta = svc.meta(slug)
        payload["revision"] = meta.current_revision
        payload["context_cards"] = meta.context_cards
        # Freshness is the explicit stale flag only — revision is provenance, not an invalidation rule.
        payload["artifacts"] = {
            t: {"revision": st.revision, "filename": st.filename, "stale": st.stale}
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
    request = (demo / "request.md").read_text(encoding="utf-8").strip()
    out = load_model(demo / "model.json")
    assessment = _fenced_text((demo / "solution-assessment.md").read_text(encoding="utf-8"))

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

    print("\n\n③ THE DECISION BRIEF  — a judgment, not a recap (the differentiator)\n")
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


# Provider-backed generators. Each resolves a session (slug or model.json path) and hands off to
# `DiscoveryService`, which is where reasoning, the revision lock, provenance and the artifact write
# actually happen — so a document asked for from the terminal is produced, saved and tracked exactly as
# the same document asked for from the browser or from Claude Code. The CLI's job here is to resolve
# the session, choose the terminal view, and say where the file went.


def _generator_service(a, client) -> tuple[str, DiscoveryService]:
    """Shared preamble: (slug, service). Fails early if the session does not exist, so a typo'd slug
    never reaches the provider and gets billed for it."""
    svc = SessionService()
    slug = svc.resolve_slug(a.model)
    if not svc.exists(slug):
        raise SessionNotFoundError(f"no session '{slug}'", details={"slug": slug})
    return slug, DiscoveryService(client=client, sessions=svc)


def _wrote(slug: str, result, label: str) -> None:
    """Say where a generated document went — the one line five generator verbs share.

    The path goes through `artifact_path` rather than being re-joined here (#36). Printing a path is
    still disclosing one, and `result.status.filename` is a plain `str` off an `ArtifactStatus` that
    nothing re-validates on the way out; that function carries the argument for why a display-only
    join is not exempt from the chokepoint, and which door is actually open."""
    # Through the chokepoint rather than joined here (#36), and direct rather than through the
    # repository (#76): `artifact_path` validates both halves of a name that came *off disk*, and a
    # printed path is a disclosure like any other. The repository's `load_artifact` is the read
    # seam; there is no seam that hands back a path, on purpose.
    print(f"\nWrote {label} → {store.artifact_path(slug, result.status.filename)}")


def _cmd_brief(a, client) -> None:
    slug, disco = _generator_service(a, client)
    # The assessment's reasoning is absorbed back into the model as a new revision inside `generate`,
    # so downstream generators inherit the decisions and challenges, not just the facts.
    result = disco.generate(slug, "brief", surface="cli-brief")
    render_brief(result.model, result.artifact)
    # The caption is "decision brief" everywhere a person reads it; the type, the verb and the file
    # on disk are all still `brief`/`solution-assessment.md` (#166).
    _wrote(slug, result, "decision brief")


def _cmd_prd(a, client) -> None:
    slug, disco = _generator_service(a, client)
    result = disco.generate(slug, "prd", surface="cli-prd")
    print(prd_markdown(result.artifact))
    _wrote(slug, result, "PRD")


def _cmd_stories(a, client) -> None:
    # Terminal-only: stories are an analysis feeding the estimate, not a deliverable with a file.
    slug, disco = _generator_service(a, client)
    render_stories(disco.reason(slug, "stories"))


def _cmd_estimate(a, client) -> None:
    slug, disco = _generator_service(a, client)
    # One snapshot for both calls, decided rather than left as a residual: the estimate is read
    # against these stories, so a snapshot each let them be read against two revisions (#135). Pinned
    # by `test_the_estimate_verb_reads_stories_and_estimate_from_one_snapshot`.
    snap = disco.sessions.snapshot(slug)
    stories = disco.reason_from(snap, "stories")
    render_stories(stories)   # rendered here, not after both calls, so the stories arrive while the estimate runs
    # The estimate is the one call that needs a prior artifact as input, so it does not fit the plain
    # model→artifact shape — but "does not fit" was being spent on a direct provider call, on a second
    # client this verb built for itself (#77). It fits `reason()` perfectly well: `stories` rides the
    # same `**kwargs` a release note's `version` does. Terminal-only, so nothing is written.
    draft, soft, confidence = disco.reason_from(snap, "estimate", stories=stories)
    render_estimate(draft, soft, confidence)


def _cmd_criteria(a, client) -> None:
    slug, disco = _generator_service(a, client)
    result = disco.generate(slug, "criteria", surface="cli-criteria")
    print(criteria_markdown(result.artifact))
    _wrote(slug, result, "acceptance criteria")


def _cmd_epic(a, client) -> None:
    slug, disco = _generator_service(a, client)
    result = disco.generate(slug, "epic", surface="cli-epic")  # one model call; every view renders from it
    epic = result.artifact
    print(epic_markdown(epic))
    _wrote(slug, result, "epic")
    if a.export_json:
        # `write_artifact_file`, not `repo.save_artifact`: these three are extra *views* of one
        # already-saved artifact and are deliberately untracked — no type, no source revision, no
        # staleness. Giving them artifact status would put three rows in `artifact list` that no
        # generator can refresh. Direct, and it stays direct until a second surface writes them.
        print(f"Wrote neutral epic export → {store.write_artifact_file(slug, 'epic.json', epic_export_json(epic))}")
    if a.github:
        print(f"Wrote GitHub issue-creation plan → "
              f"{store.write_artifact_file(slug, 'epic.github.json', to_github_json(epic, slug))}")
    if a.gitlab:
        print(f"Wrote GitLab issue-creation plan → "
              f"{store.write_artifact_file(slug, 'epic.gitlab.json', to_gitlab_json(epic, slug))}")


def _cmd_release(a, client) -> None:
    slug, disco = _generator_service(a, client)
    result = disco.generate(slug, "release", surface="cli-release", version=a.version)
    print(release_markdown(result.artifact))
    _wrote(slug, result, "release notes")


def _cmd_web(a, client) -> None:
    """Launch the local, single-user web interface (the `[web]` extra). Binds to localhost by default;
    the Anthropic key is read from the server environment and is only needed for provider actions —
    consulting existing sessions needs none. Uvicorn is imported and started here, never at module
    import, and the FastAPI app is a factory so nothing binds a port until this runs."""
    host, port = a.host, a.port
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"⚠  Binding to {host}: Requivo Web has NO authentication and must not be exposed on an "
              "untrusted network. Prefer 127.0.0.1 unless you fully control the network.", file=sys.stderr)
        # The app only answers to hosts it recognises (the DNS-rebinding guard in web/security.py), and
        # loopback is all it recognises by default. A deliberate bind elsewhere is the operator saying
        # this address is legitimate, so record it — without silently widening the default.
        os.environ.setdefault("REQUIVO_WEB_ALLOWED_HOSTS", host)
    try:
        import uvicorn

        from requivo.web.app import create_app
    except ImportError as e:
        # `EngineError` for a missing optional dependency is a decision about a published payload, not
        # a leftover: its `code` is `provider_unavailable`, that code travels in the `--json` envelope,
        # and `docs/compatibility.md` makes moving a condition from one code to another a breaking
        # change — a major version from 1.0.0 onward (#135). It is also the vocabulary's existing
        # answer for "an optional install is absent": `new_client()` says the same thing about
        # `[anthropic]`. Pinned by `test_the_missing_web_extra_keeps_its_published_error_code`.
        raise EngineError(
            "The web interface is not installed. Install it with `pip install 'requivo[web]'` "
            f"(or `uv tool install 'requivo[web]'`). You do NOT need it for the CLI or Claude Code. "
            f"(import error: {e})") from e
    url = f"http://{host}:{port}"
    print(f"\nRequivo Web → {url}")
    print("  Sessions stay local under .requivo/sessions/. An Anthropic key (server env) is needed only")
    print("  for provider actions (discovery, generation); consulting existing sessions needs none.\n")
    if not a.no_open:
        import threading
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    if a.reload:
        uvicorn.run("requivo.web.app:create_app", host=host, port=port, reload=True, factory=True)
    else:
        uvicorn.run(create_app(), host=host, port=port)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="requivo",
        description="Requivo — find what could change the solution before you commit to the scope.",
    )
    p.add_argument("--workspace", metavar="DIR",
                   help="workspace root for sessions (default: cwd). Sessions live in "
                        "<workspace>/.requivo/sessions/. Place before the command.")
    sub = p.add_subparsers(dest="command", required=True, metavar="<command>")

    # The deterministic surface (doctor / session / model / artifact) — no LLM, no API key.
    register_deterministic(sub)

    d = sub.add_parser("discover", help="analyse a request (a string or a file path) and start a session")
    d.add_argument("request", help="the client request, or a path to a file containing it")
    d.add_argument("--once", action="store_true", help="single pass (status + questions), no interactive loop")
    # `--cards` is a permanent alias of `--context` (#85): the same selector was spelled two ways
    # across three verbs. One action with two option strings, never two arguments — two arguments
    # would let whichever came last on the command line silently discard the other. `--context` is
    # the documented primary and owns the dest, so no handler moved.
    d.add_argument("--context", "--cards", metavar="CARDS", dest="context",
                   help="comma-separated context cards to load instead of all "
                        "(e.g. b2b-platform,financial-reporting); sharpens discovery by dropping "
                        "irrelevant cards. Applies to this discovery only. Alias: --cards.")
    d.set_defaults(func=_cmd_discover)

    demo = sub.add_parser("demo", help="replay a real run from saved output — no API key needed")
    demo.set_defaults(func=_cmd_demo)

    web = sub.add_parser("web", help="launch the local single-user web interface (needs the [web] extra)")
    web.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1, localhost only)")
    web.add_argument("--port", type=int, default=8765, help="port (default: 8765)")
    # SUPPRESS so an absent web --workspace does not overwrite a global `requivo --workspace … web`.
    web.add_argument("--workspace", metavar="DIR", default=argparse.SUPPRESS,
                     help="workspace root for sessions (default: cwd)")
    web.add_argument("--no-open", action="store_true", help="do not open a browser automatically")
    web.add_argument("--reload", action="store_true", help="auto-reload on code changes (development)")
    web.set_defaults(func=_cmd_web)

    def model_cmd(name: str, help_: str, func, extra=None):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("model", help="a session slug, or a path to a saved model.json")
        if extra:
            extra(sp)
        sp.set_defaults(func=func)

    model_cmd("answer", "fold the client's answers in and report what moved",
              _cmd_answer, lambda sp: sp.add_argument("answers", help="the client's answers, as free text"))
    model_cmd("status", "show the understanding, open questions and readiness", _cmd_status,
              lambda sp: sp.add_argument("--json", action="store_true", help="emit a machine status snapshot"))
    model_cmd("impact", "show what a change to given topics would reach; no topics = full map",
              _cmd_impact, lambda sp: sp.add_argument("slots", nargs="*",
              help="slot ids or label words (e.g. permissions workflow); omit for the full map"))
    model_cmd("brief", "generate the decision brief — what to review before estimating", _cmd_brief)
    model_cmd("prd", "generate the PRD", _cmd_prd)
    model_cmd("stories", "derive user stories", _cmd_stories)
    model_cmd("estimate", "derive stories and estimate them (day ranges)", _cmd_estimate)
    model_cmd("criteria", "generate Given/When/Then acceptance criteria", _cmd_criteria)

    def epic_flags(sp):
        # Three sibling flags of one kind: each writes an export file. `--export-json` was spelled
        # `--json` until #83, where it was the odd one out twice over — on every other verb `--json`
        # means "emit the payload on stdout", and `app()` reads `getattr(args, "json", False)`
        # generically to switch failures to a structured envelope. So the flag that documented
        # itself as writing a file also, silently, changed how failures were reported, while
        # `--github` and `--gitlab` did not. With no `json` dest on this verb that getattr falls
        # through to False and all three report a failure the same way. Do NOT add a stdout
        # `--json` here: it would restore the divergence under a new name.
        sp.add_argument("--export-json", action="store_true",
                        help="also write the neutral epic.json export")
        sp.add_argument("--github", action="store_true", help="also write a GitHub issue-creation plan")
        sp.add_argument("--gitlab", action="store_true", help="also write a GitLab issue-creation plan")

    model_cmd("epic", "generate the delivery epic (+ optional tracker plans)", _cmd_epic, epic_flags)
    model_cmd("release", "generate client-facing release notes", _cmd_release,
              lambda sp: sp.add_argument("version", nargs="?", default="", help="optional version label to stamp"))

    return p


def app(argv: list[str] | None = None, client=None) -> None:
    """Entry point for the `requivo` command (and its `pc` alias, and `python -m requivo`)."""
    # First, before anything can print: make stdout and stderr unable to kill this process on a
    # character they cannot encode (#29). Not at import time — importing `requivo` must not
    # reconfigure the streams of a program that merely imported it.
    configure_streams()
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
            _render_usage_safely(ledger)
            if want_json:
                print(json.dumps(e.to_dict(), indent=2))
            else:
                safe_write(sys.stderr, f"\n{e}\n")
            raise SystemExit(1) from None
        except UnicodeEncodeError as e:
            # The braces to `configure_streams`' belt, and the reason this arm exists at all: a
            # `UnicodeEncodeError` escaping a handler was raised by a `print`, which means the
            # handler had already finished the work it was reporting. Letting it surface as a
            # traceback tells the operator the command failed when the revision has landed and the
            # artifact has been written — so they re-run, and pay for a second provider call on top
            # of the first (#29). Say what actually happened instead.
            #
            # Reached only where `configure_streams` reported `could-not` for this stream, which
            # `requivo doctor` prints. Narrow on purpose: a broad `except Exception` here would
            # swallow real failures and claim they had landed.
            _render_usage_safely(ledger)
            paid = bool(getattr(ledger, "calls", None))
            safe_write(sys.stderr, _RENDER_FAILED_HEAD.format(error=e)
                       + (_RENDER_FAILED_PAID if paid else _RENDER_FAILED_UNPAID)
                       + _RENDER_FAILED_TAIL)
            raise SystemExit(EXIT_RENDER_FAILED) from None
    # Outside the `with`, and therefore outside the arm above -- which is exactly why it needs the
    # safe wrapper. This is the wholly-successful path: the provider call is billed and the revision
    # is applied by the time it runs, so an exception here is the #29 ordering bug on the one route
    # where nothing was wrong in the first place.
    _render_usage_safely(ledger)
