"""The deterministic CLI surface — every command here runs with no LLM and no API key.

These verbs (`doctor`, `session …`, `model …`, `artifact …`) are the offline half of Requivo: they
create and inspect sessions, validate and apply proposed models, and record artifacts, all through
the same `SessionService`/`ArtifactService` the provider path uses. Claude Code drives *these* — it
reasons with its own Claude, pipes the proposal in on stdin, and calls `model validate`/`model apply` — so no
`ANTHROPIC_API_KEY` is ever required in that mode.

`register(sub)` attaches the parsers to the main `requivo` argparse tree; each handler takes
`(args, client)` to match the CLI's uniform dispatch (the deterministic handlers ignore `client`).
Handlers raise structured `RequivoError`s; `cli.app()` turns them into a clean message or a JSON error
envelope (`--json`).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from requivo.core import persistence as store
from requivo.core.context import available_cards, check_selection, resolve_cards
from requivo.core.errors import InvalidModelError, InvalidSessionError, SessionExistsError, SessionNotFoundError
from requivo.core.integrity import IntegrityProblem, check_session, check_session_dir
from requivo.core.selectors import display_token
from requivo.core.validation import validate_proposal
from requivo.paths import ASSETS, CONTEXT, session_root, user_context_dir, workspace_root
from requivo.services.artifacts import ARTIFACT_FILENAMES, ArtifactService
from requivo.services.sessions import SessionService
from requivo.streams import describe_streams


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2))


def read_user_text(path: Path) -> str:
    """A file the *user* named, decoded as UTF-8, with a structured refusal when it is not UTF-8.

    Every file Requivo writes is UTF-8 (`_atomic_write`), so UTF-8 is the right codec to read one
    back with — that is #11. But these particular reads take a path the user typed, and that file was
    written by something else: a French client brief saved out of a Windows editor is genuinely
    cp1252 often enough to matter.

    The old behaviour decoded it with the locale's codec, which on Windows silently produced mojibake
    that then validated, persisted, and shipped in the PRD. Refusing is right — invariant 3, refuse
    rather than truncate, because half a request reads exactly like a whole one, and mojibake reads
    exactly like prose. What is *not* right is refusing with a bare `UnicodeDecodeError` traceback:
    that would trade a silently wrong answer for an unexplained crash, which is the same trade one
    step along. So the refusal names the file, the codec and the way out.
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        # `display_token`, not the bare path: the path is untrusted input, and a filename carrying a
        # newline would otherwise write what reads as a second, authoritative line of Requivo's own
        # output at column 0 — the shape #40 found in `doctor`, in a message added by the fix for
        # #11. Found by this file's own guard test rather than in review, which is the argument for
        # the guard: the class reaches code written today, not only legacy paths.
        raise InvalidModelError(
            f"{display_token(str(path))} is not valid UTF-8 "
            f"(byte 0x{e.object[e.start]:02x} at position {e.start}). "
            f"Requivo reads and writes UTF-8 throughout, so a file in another encoding is refused "
            f"rather than decoded into text that would look like prose and be wrong. Re-save it as "
            f"UTF-8 and try again.",
            details={"path": str(path), "expected_encoding": "utf-8", "position": e.start},
        ) from None


def _read_source(arg: str) -> str:
    """A request/answers argument that may be an inline string, a path to a file, or `-` for stdin."""
    if arg == "-":
        return _read_stdin()
    try:
        is_file = bool(arg.strip()) and Path(arg).is_file()
    except OSError:
        is_file = False
    return read_user_text(Path(arg)) if is_file else arg


def _read_stdin() -> str:
    """Everything on stdin, as text. Refused when stdin is a terminal, which would otherwise hang
    waiting for input the caller never meant to type."""
    if sys.stdin is None or sys.stdin.isatty():
        raise InvalidModelError(
            "'-' means read from stdin, but stdin is a terminal — pipe the content in, "
            "or pass a file path instead")
    return sys.stdin.read()


def _read_document(arg: str) -> str:
    """A *document* argument: a path, or `-` for stdin. Unlike `_read_source`, the text is never
    itself the content — `model apply <session> proposal.json` takes a path, so a non-existent path is
    a mistake to report, not a proposal whose body happens to be a filename.

    Stdin exists so a caller with content in hand does not have to invent a temp file for it. The
    Claude Code skills used to write `/tmp/requivo:prd.md`: a path that is not writable on Windows
    (`:` is illegal in a filename there), that needed `rm` to clean up — a command the plugin does not
    grant itself — and that two concurrent sessions would have shared."""
    if arg == "-":
        return _read_stdin()
    p = Path(arg)
    if not p.is_file():
        raise InvalidModelError(f"no such file: {display_token(arg)} (use '-' to read from stdin)",
                                details={"path": arg})
    return read_user_text(p)


# ── doctor ──────────────────────────────────────────────────────────────────────


def doctor_report() -> dict:
    """A self-diagnosis of the install: Python, Requivo, assets, schema, provider availability, and
    the workspace. Absence of the Anthropic SDK / API key is reported as informational, NOT an error —
    Claude Code mode needs neither."""
    from requivo import __version__

    # Assets + schema.
    schema_ok, slot_count, schema_err = True, 0, None
    try:
        from requivo.core.contracts import schema_slot_ids
        allowed, _ = schema_slot_ids()
        slot_count = len(allowed)
    except Exception as e:  # noqa: BLE001 - doctor reports any failure rather than raising
        schema_ok, schema_err = False, str(e)

    # Context cards get their own check, with their own three states. They used to have none: a
    # failure of `available_cards()` was written into `schema_err` — a *different* check's field —
    # with `schema_ok` left True and the message printed nowhere, while the card line printed a tick
    # whatever the count was. A wheel that ships `assets/` but loses `assets/context/` therefore
    # showed three green ticks (#12). `status` is the distinction that was missing: `ok` (cards
    # loaded), `empty` (we looked and there are none — a broken install, because impact estimation
    # is the product's central idea and it runs on these cards), `unreadable` (we could not look,
    # which is not the same answer and must not render like the clean one).
    cards, cards_err = [], None
    try:
        cards = available_cards()
    except Exception as e:  # noqa: BLE001 - doctor reports any failure rather than raising
        cards_err = str(e)
    cards_status = "unreadable" if cards_err else ("ok" if cards else "empty")

    # Provider (optional).
    provider_installed, provider_version = False, None
    try:
        import anthropic
        provider_installed = True
        provider_version = getattr(anthropic, "__version__", "unknown")
    except ImportError:
        pass

    # The console's own codec, which `doctor` is the right verb to answer about: it is the thing
    # that decides whether any *other* line of this report can be printed at all (#29). `cli.app()`
    # has already run `configure_streams()` by the time this is reached, so what is reported is the
    # state after that — including the case where a stream refused to be configured, which is the
    # one state in which a glyph can still kill the process mid-report.
    output = describe_streams()

    return {
        "requivo_version": __version__,
        "python_version": platform.python_version(),
        "assets": {"root": str(ASSETS), "present": ASSETS.exists()},
        "output": {"ok": all(s["state"] == "safe" for s in output), "streams": output},
        "schema": {"ok": schema_ok, "slots": slot_count, "error": schema_err},
        # `context_cards` stays the plain list it has always been — it is a published `--json` key
        # and a consumer reading `len(...)` off it must keep working. The verdict is the new sibling.
        "context_cards": cards,
        "context": {"ok": cards_status == "ok", "status": cards_status, "count": len(cards),
                    "error": cards_err, "roots": [str(CONTEXT), str(user_context_dir())]},
        "provider_anthropic": {
            "installed": provider_installed,
            "version": provider_version,
            "api_key_present": bool(os.getenv("ANTHROPIC_API_KEY")),
        },
        "workspace": {"root": str(workspace_root()), "sessions": str(session_root())},
        # Sessions that no longer add up. Cheap (a session is a handful of small files) and this is
        # where a user asks "is anything wrong?" — a broken history is exactly that, and it otherwise
        # only surfaces later, as a refused artifact save with no obvious cause.
        "sessions": _session_health(cards_readable=cards_err is None),
    }


# Which card findings are repaired by *restoring a file*, and which by *fixing the stored selection*.
# Two different remedies, and printing the first under the second is the quiet-wrong-answer form of
# the bug #40 is about: the verb names a real problem and then tells you to do something that cannot
# fix it. Stated once and read by both surfaces, because `doctor` and `session verify` printing
# different advice for the same finding is how they drift.
#
# `context_unreadable` is deliberately NOT a member, for the same reason `_SELECTION_REFUSALS` in
# `core/context.py` deliberately excludes it: `check_selection` lets it propagate rather than
# returning it, so `_card_health` reports it as `{"checked": False, "problem": None}` and it can
# never arrive here as a `problem["code"]` at all. Listing it would be a branch that cannot run,
# which reads to the next person as coverage this does not have. `test_the_two_card_code_tables_
# agree` pins the pair, so adding it to the refusals tuple later fails loudly here instead of
# silently routing a permissions fault to the wrong remedy.
_RESTORABLE_CARD_CODES = frozenset({"unknown_context_card", "no_context_cards"})

_RESTORE_HINT = ("Put the card back, or point REQUIVO_CONTEXT_DIR at where it now lives — until "
                 "then these sessions refuse their next reasoning turn.")
_REPAIR_HINT = ("Repair the `context_cards` list in the session's session.json — the selection "
                "itself is malformed, so no card you install will resolve it.")


def _card_health(slug: str) -> dict:
    """Does this session's persisted context-card selection still load *here*? Three states, because
    a checker that could not look must not answer like one that looked and found nothing:

    - `{"checked": True,  "problem": None}`  — it loads;
    - `{"checked": True,  "problem": {…}}`   — it does not, and the envelope names the cards;
    - `{"checked": False, "error": "…"}`     — neither the session's metadata nor the card directory
      could be read, so this session's context is simply unknown.

    **Why this lives here and not in `core/integrity.py`.** That module answers one question — does
    a session directory tell the truth *about itself* — and a context card is not in the directory;
    it is in the installed package or in `user_context_dir()`. Reporting a lost card as an integrity
    problem would make the same directory coherent on one machine and broken on another, which is
    not a property an integrity check can have. It would also break `session import`, which refuses
    an archive on exactly those problems: a colleague's perfectly good session would become
    unimportable because you happen not to have one of their cards. So it is an *environment*
    finding, reported by the two verbs that ask about the environment — `doctor` and
    `session verify` — over `core.context.check_selection`, which is the guard `load_context`
    itself applies rather than a second implementation of it.
    """
    try:
        problem = check_selection(store.read_meta(slug).context_cards)
    except Exception as e:  # noqa: BLE001 - a health check reports that it could not look; it never raises
        return {"checked": False, "problem": None, "error": str(e)}
    return {"checked": True, "problem": problem.to_dict() if problem else None, "error": None}


def _session_health(*, cards_readable: bool = True) -> dict:
    """The workspace's sessions, with a third state on each question it asks.

    - `readable` / `total` / `error` — could the session root be listed at all? A bare `except`
      returning `{"total": 0}` was the whole of #12's F3: twelve unreachable sessions rendered
      byte-identically to a genuinely empty workspace, and a user reads that as "my sessions were
      deleted". When we could not look, `total` is `None`, because `0` is a claim about the
      workspace and we do not have one.
    - `inconsistent` — {slug: [integrity codes]}, from `check_session`. A slug whose own files
      cannot be read gets the `unreadable` code the inner loop already synthesised, now as a real
      `IntegrityProblem` rather than an ad-hoc stand-in class.
    - `unresolved_cards` — {slug: error envelope} for a session whose persisted card selection no
      longer loads (see `_card_health` for why that is not an integrity code). `cards_checked` is
      false when the card layer itself was unreadable — then nobody looked, and an empty map here
      means nothing at all.
    """
    inconsistent: dict[str, list[str]] = {}
    unresolved: dict[str, dict] = {}
    try:
        slugs = store.list_session_slugs()
    except Exception as e:  # noqa: BLE001 - doctor reports, it does not fail — but it must say what it hit
        return {"total": None, "readable": False, "error": str(e),
                "inconsistent": {}, "unresolved_cards": {}, "cards_checked": False}
    for slug in slugs:
        try:
            problems = check_session(slug)
        except Exception as e:  # noqa: BLE001
            problems = [IntegrityProblem("unreadable", str(e))]
        codes = [p.code for p in problems]
        if cards_readable:
            health = _card_health(slug)
            if not health["checked"] and "unreadable" not in codes:
                codes.append("unreadable")
            if health["problem"]:
                unresolved[slug] = health["problem"]
        if codes:
            inconsistent[slug] = codes
    return {"total": len(slugs), "readable": True, "error": None,
            "inconsistent": inconsistent, "unresolved_cards": unresolved,
            "cards_checked": cards_readable}


def _cmd_schema(a, client) -> None:
    """Print the slot schema (and optionally the human framework spec) so a reasoning caller — Claude
    Code, above all — has the exact slot vocabulary + driver rule to produce a valid proposal offline."""
    from requivo.paths import FRAMEWORK
    print((FRAMEWORK / "model_schema.json").read_text(encoding="utf-8"))
    if a.framework:
        print("\n\n<!-- framework/elicitation.md (human spec) -->\n")
        print((FRAMEWORK / "elicitation.md").read_text(encoding="utf-8"))


def _cmd_context(a, client) -> None:
    """List or print the context cards — the product knowledge that grounds impact estimation. A
    reasoning caller reads this to weigh information value; pure asset I/O, no LLM.

    `--session` prints the cards *that session* was created with. A session's card selection is held
    constant across its turns on purpose — it is what the impact estimates were made against, and it
    keeps the cached prompt prefix alive — so a later turn that reads all the cards is reasoning from a
    wider context than the one the model was built on. Asking for it by session removes the step where
    a caller has to carry the list by hand and can quietly widen it."""
    from requivo.core.context import load_context
    if a.list:
        for c in available_cards():
            print(c)
        return
    if a.session:
        if a.cards:
            raise InvalidModelError("--session and --cards are alternatives; pass only one")
        svc = SessionService()
        cards = svc.cards(svc.resolve_slug(a.session))   # None == the session uses every card
    else:
        cards = _resolve_cards(a.cards) if a.cards else None
    print(load_context(cards))


def _cmd_doctor(a, client) -> None:
    r = doctor_report()
    if a.json:
        _print_json(r)
        return
    ok = "✅"
    warn = "🟡"
    print("Requivo doctor")
    print(f"  {ok} requivo         {r['requivo_version']}")
    print(f"  {ok} python          {r['python_version']}")
    # The console's codec, reported before anything that depends on it. Only ever a line when there
    # is something to say: on a UTF-8 terminal — every developer's, which is why this shipped — the
    # answer is uninteresting and a clean report should not grow a row per non-finding.
    for stream in r["output"]["streams"]:
        if stream["state"] == "will-crash":
            print(f"  ❌ {stream['stream']:<15} {stream['detail']}")
            print("     └─ Requivo could not configure this stream; set PYTHONIOENCODING=utf-8.")
        elif stream["state"] == "lossy":
            print(f"  {warn} {stream['stream']:<15} {stream['detail']}")
            print("     └─ this handler came from your environment, not from Requivo. Prefer "
                  "errors=backslashreplace.")
        elif stream["state"] == "unknown":
            print(f"  {warn} {stream['stream']:<15} {stream['detail']}")
        elif (stream["encoding"] or "").lower() not in ("utf-8", "utf8"):
            print(f"  {warn} {stream['stream']:<15} {stream['encoding']} — characters it cannot "
                  f"encode are escaped, not dropped, and never crash")
    print(f"  {ok if r['assets']['present'] else '❌'} assets          {r['assets']['root']}")
    s = r["schema"]
    print(f"  {ok if s['ok'] else '❌'} schema          {s['slots']} slots"
          + (f"  (error: {s['error']})" if not s["ok"] else ""))
    c = r["context"]
    if c["status"] == "unreadable":
        print(f"  ❌ context cards   unreadable — {c['error']}")
    elif c["status"] == "empty":
        print("  ❌ context cards   0 available — none found under "
              f"{' or '.join(c['roots'])}")
        print("     └─ impact estimation has no product context to reason from; this install is "
              "incomplete.")
    else:
        print(f"  {ok} context cards   {c['count']} available")
    p = r["provider_anthropic"]
    prov = f"installed (v{p['version']})" if p["installed"] else "not installed"
    key = "API key set" if p["api_key_present"] else "no API key"
    print(f"  {ok if p['installed'] else warn} anthropic       {prov} · {key}")
    if not p["installed"]:
        print("     └─ optional: `pip install 'requivo[anthropic]'` for API-powered discovery.")
        print("        Not needed for Claude Code mode.")
    print(f"  {ok} workspace       {r['workspace']['root']}")
    print(f"     sessions        {r['workspace']['sessions']}")
    h = r["sessions"]
    if not h["readable"]:
        # Not "0 sessions". We could not look, and saying nothing found is the failure this verb
        # exists to prevent: a user told they have no sessions concludes they were deleted.
        print(f"  ❌ sessions        unreadable — {h['error']}")
        print(f"     └─ {r['workspace']['sessions']} could not be listed. This is not the same "
              "thing as having no sessions.")
        return
    bad, lost = h["inconsistent"], h["unresolved_cards"]
    # Only worth saying when there are sessions at all. Since #33 a session with no card selection is
    # *not* exempt: `check_selection(None)` reads the card directory now, because an install with no
    # cards refuses every load, `only=None` included — so "every card" is a selection that can fail
    # like any other.
    unchecked = not h["cards_checked"] and bool(h["total"])
    notes = ([f"{len(bad)} inconsistent"] if bad else []) \
        + ([f"{len(lost)} with product context that no longer loads"] if lost else []) \
        + (["product context not checked"] if unchecked else [])
    # Three glyphs for three states, on the line a reader actually scans. Leaving "not checked" to
    # a trailing note put a tick on this line while nobody had looked — the defect this whole change
    # is about, one line further down than where it was filed.
    glyph = "❌" if (bad or lost) else (warn if unchecked else ok)
    print(f"  {glyph} sessions        {h['total']} in this workspace"
          + (f" · {' · '.join(notes)}" if notes else ""))
    for slug, codes in bad.items():
        print(f"     └─ {slug}: {', '.join(codes)} — run `requivo session verify {slug}`")
    for slug, problem in lost.items():
        print(f"     └─ {slug}: {problem['message']}")
    # One hint per remedy actually present, rather than one hint for whichever remedy came first.
    codes = {p["code"] for p in lost.values()}
    if codes & _RESTORABLE_CARD_CODES:
        print(f"        {_RESTORE_HINT}")
    if codes - _RESTORABLE_CARD_CODES:
        print(f"        {_REPAIR_HINT}")
    if unchecked:
        print("     └─ the card directory could not be read (see above), so nothing is known about "
              "whether these sessions' product context still loads.")


# ── session ──────────────────────────────────────────────────────────────────────


def _resolve_cards(spec: str | None) -> list[str] | None:
    """A comma-separated --context spec → validated card stems (None == all cards). The resolution and
    the unknown-card error live in Core (`resolve_cards`), shared with the Web, so a typo can never
    silently widen the context on one surface and fail on another."""
    return resolve_cards(spec.split(",")) if spec else None


def _cmd_session_init(a, client) -> None:
    request = _read_source(a.request)
    if not request.strip():
        raise InvalidModelError("session init needs a request (a sentence or a file path)")
    cards = _resolve_cards(a.context)
    meta = SessionService().create_session(
        request, context_cards=cards, slug=a.slug, provider=a.provider)
    if a.json:
        # `revision` is 0 for a genuinely new session — but init is idempotent, so re-running it on the
        # same request returns an *existing* session that may already carry a model. A caller about to
        # apply needs to know which of the two it got, and this is where it finds out.
        _print_json({"slug": meta.slug, "session_id": meta.session_id,
                     "path": str(store.canonical_dir(meta.slug)), "context_cards": meta.context_cards,
                     "revision": meta.current_revision})
        return
    print(f"Created session '{meta.slug}' → {store.canonical_dir(meta.slug)}")
    print("  No model yet. Produce a proposal and run:")
    print(f"    requivo model apply {meta.slug} proposal.json")


def _cmd_session_list(a, client) -> None:
    sessions = SessionService().list_sessions()
    if a.json:
        _print_json([{"slug": m.slug, "revision": m.current_revision, "provider": m.provider,
                      "updated_at": m.updated_at} for m in sessions])
        return
    if not sessions:
        print(f"No sessions under {session_root()}.")
        return
    print(f"Sessions under {session_root()}:")
    for m in sessions:
        print(f"  {m.slug:<40} rev {m.current_revision}  ({m.provider or '—'}, {m.updated_at})")


def _cmd_session_show(a, client) -> None:
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    if not store.session_exists(slug):
        raise SessionNotFoundError(f"no canonical session {display_token(slug)}", details={"slug": slug})
    meta = store.read_meta(slug)
    if a.json:
        _print_json(meta.model_dump())
        return
    print(f"Session '{meta.slug}'  (id {meta.session_id[:12]}…)")
    print(f"  created  {meta.created_at}")
    print(f"  updated  {meta.updated_at}")
    print(f"  revision {meta.current_revision}")
    print(f"  provider {meta.provider or '—'}   model {meta.model_name or '—'}")
    # `display_token`, not a bare join (#40). This is the one card-name render site the selector
    # guard cannot reach: nothing here is *selecting*, so `normalize_tokens` never runs and a name
    # persisted by `session import` arrives unexamined. A clean name is returned byte-for-byte, so
    # this line is unchanged for every session that was not tampered with.
    print("  context  " + (", ".join(display_token(c) for c in meta.context_cards)
                           if meta.context_cards else "all cards"))
    if meta.artifact_status:
        print("  artifacts:")
        for t, st in meta.artifact_status.items():
            # The explicit stale flag is the whole rule — the source revision is provenance, not an
            # invalidation signal (see ArtifactService.list). An artifact produced two revisions ago
            # whose inputs never moved is still fresh, and saying otherwise here contradicted both
            # `artifact list` and the status JSON every other surface reads.
            print(f"    {t:<12} {st.filename:<26} rev {st.revision}  {'STALE' if st.stale else 'fresh'}")


def _cmd_session_migrate(a, client) -> None:
    """The bulk migration of every legacy out/<slug>/ session into the canonical store. Since 0.9.8
    this is the *only* thing that reads that layout — there is no automatic migrate-on-first-write.

    The `session_exists` check below is **reporting, not the guard**: it is what fills the
    `skipped_already_present` row, and it is kept because a sweep that names what it declined is worth
    a cheap stat call. The guard is `migrate_legacy`'s own atomic claim on the slug — which is why the
    `SessionExistsError` arm exists. A session that appears between the check and the migration is the
    TOCTOU window the check cannot close, and the correct outcome there is the same skip.

    **That arm covers the occupied-slug case and nothing else, deliberately, and the gap is stated
    here rather than left to be discovered.** A legacy session whose `model.json` does not parse still
    aborts the whole pass: `migrate_legacy` raises before it claims the slug, nothing catches it, and
    the run ends with no output at all — so slugs sorted after the bad one are neither migrated nor
    reported, and the ones already done are never printed. That is invariant 15's shape and this loop
    does not yet satisfy it. It is left loud on purpose rather than widened to `except Exception`
    here: turning a corrupt session into one row of a list is the calm-wrong-answer direction, and
    doing it properly means designing what the receipt says, which is a decision and not a catch."""
    from requivo.paths import output_root
    root = output_root()
    slugs = sorted(p.name for p in root.iterdir() if (p / "model.json").exists()) if root.exists() else []
    migrated, skipped = [], []
    for slug in slugs:
        if store.session_exists(slug):
            skipped.append(slug)
            continue
        try:
            store.migrate_legacy(slug)
        except SessionExistsError:
            skipped.append(slug)
            continue
        migrated.append(slug)
    if a.json:
        _print_json({"migrated": migrated, "skipped_already_present": skipped, "source": str(root)})
        return
    print(f"Legacy sessions under {root}:")
    print(f"  migrated: {', '.join(migrated) or '(none)'}")
    if skipped:
        print(f"  skipped (already in canonical store): {', '.join(skipped)}")
    print("  Legacy files were preserved (read-only).")


def _cmd_session_export(a, client) -> None:
    """Archive a session as a .zip — under its lock, and complete or not at all.

    A session is a handful of files that must agree with each other: session.json's revision count,
    the revision files it names, the model that should equal the last of them. Reading them one by one
    while another surface applies a revision produces an archive that combines an old metadata with a
    new model — internally inconsistent, and only discovered on import. So the read happens under the
    session lock, the same one every writer takes.

    `.lock` and the scratch files of an interrupted write are excluded: they are local artefacts of
    *this* machine's coordination, meaningless in an archive, and the lock file in particular would
    import as a session component. The archive itself is written beside its destination and renamed
    into place, so an interrupted export leaves no half-written .zip looking like a real one."""
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    if not store.session_exists(slug):
        raise SessionNotFoundError(f"no canonical session {display_token(slug)}", details={"slug": slug})
    d = store.canonical_dir(slug)
    dest = Path(a.output) if a.output else Path.cwd() / f"{slug}.requivo.zip"
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.part")
    try:
        with store.session_lock(slug):
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                for f in sorted(d.rglob("*")):
                    if f.is_file() and not any(part.startswith(".") for part in f.relative_to(d).parts):
                        z.write(f, f.relative_to(d.parent))
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    if a.json:
        _print_json({"slug": slug, "archive": str(dest)})
        return
    print(f"Exported session '{slug}' → {dest}")


def _cmd_session_verify(a, client) -> None:
    """Check that a session tells the truth about itself, and that the product context it names is
    still there. Exits non-zero when either is wrong, so it can gate a script.

    The two are reported side by side and kept apart on purpose. `problems` are *internal*: the
    relationships between the session's own files, which validating each file on its own cannot see.
    `context_cards` is an *environment* finding — the cards a session was created against live
    outside its directory, so a lost one says nothing about the session and everything about this
    machine. Keeping it out of `check_session_dir` is what stops `session import` refusing a
    colleague's perfectly good archive over a card you do not have; see `_card_health`.

    It is nonetheless part of `ok`, because a session whose cards are gone is refused at its next
    reasoning turn, and a verb that answers "is this session usable" with a tick right up to that
    moment is the failure this whole change is about."""
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    if not store.session_exists(slug):
        raise SessionNotFoundError(f"no canonical session {display_token(slug)}", details={"slug": slug})
    problems = check_session(slug)
    cards = _card_health(slug)
    ok = not problems and cards["checked"] and cards["problem"] is None
    if a.json:
        _print_json({"slug": slug, "ok": ok, "problems": [p.to_dict() for p in problems],
                     "context_cards": cards})
        if not ok:
            raise SystemExit(1)
        return
    if ok:
        print(f"✅ Session '{slug}' is internally consistent and its product context still loads.")
    if problems:
        print(f"❌ Session '{slug}' has {len(problems)} problem(s):")
        for p in problems:
            print(f"  · [{p.code}] {p.message}")
    if cards["problem"]:
        code = cards["problem"]["code"]
        restorable = code in _RESTORABLE_CARD_CODES
        print(f"❌ Session '{slug}' " + ("names product context that no longer loads:" if restorable
                                         else "has a product-context selection that cannot be read:"))
        print(f"  · [{code}] {cards['problem']['message']}")
        print(f"    {_RESTORE_HINT if restorable else _REPAIR_HINT}")
    elif not cards["checked"]:
        print(f"🟡 Could not check '{slug}'s product context: {cards['error']}")
    if not ok:
        raise SystemExit(1)


# Ceilings for an imported archive. A session is a handful of small JSON and Markdown files; anything
# near these is not one. They exist so a hostile or corrupt archive fails on a bound rather than on the
# filesystem filling up, and so decompression cannot be used as an amplifier.
MAX_ARCHIVE_FILES = 2_000
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


def _inspect_archive(z: zipfile.ZipFile) -> str:
    """Validate an export archive *before* anything is written, and return the single session slug it
    contains. Raises `InvalidModelError` on anything unexpected.

    Checking names by string prefix (the previous guard: `str(target).startswith(str(root))`) is not a
    containment test — `/…/sessions-evil` starts with `/…/sessions`. Here every entry is decomposed
    into path components instead, so a separator, a drive letter, a root, or a `..` segment is
    unrepresentable rather than merely unlikely."""
    infos = [i for i in z.infolist() if not i.is_dir()]
    if not infos:
        raise InvalidModelError("the archive contains no files")
    if len(infos) > MAX_ARCHIVE_FILES:
        raise InvalidModelError(
            f"the archive holds {len(infos)} files; the maximum is {MAX_ARCHIVE_FILES}",
            details={"files": len(infos), "max_files": MAX_ARCHIVE_FILES})
    total = sum(i.file_size for i in infos)
    if total > MAX_ARCHIVE_BYTES:
        raise InvalidModelError(
            f"the archive expands to {total} bytes; the maximum is {MAX_ARCHIVE_BYTES}",
            details={"bytes": total, "max_bytes": MAX_ARCHIVE_BYTES})

    slugs = set()
    for i in infos:
        name = i.filename
        if "\\" in name:  # a Windows-style separator is not a component boundary to zipfile
            raise InvalidModelError(f"unsafe path in archive: {name!r}", details={"entry": name})
        parts = PurePosixPath(name).parts
        if len(parts) < 2:
            raise InvalidModelError(
                f"archive entry {name!r} is not inside a session directory; an export contains "
                "<slug>/session.json and friends", details={"entry": name})
        if any(p in ("", ".", "..") for p in parts) or PurePosixPath(name).is_absolute():
            raise InvalidModelError(f"unsafe path in archive: {name!r}", details={"entry": name})
        slugs.add(parts[0])

    if len(slugs) != 1:
        raise InvalidModelError(
            f"the archive holds {len(slugs)} session directories ({', '.join(sorted(slugs))}); "
            "import takes exactly one", details={"slugs": sorted(slugs)})
    slug = slugs.pop()
    # The directory name becomes a session slug, so it faces the same validation as any other — this is
    # what stopped an archive whose folder was called `bad slug` from being unpacked into the store and
    # breaking every later `session list`.
    return store.validate_slug(slug)


def _validate_extracted(d: Path, slug: str) -> None:
    """Confirm an extracted directory really is a *coherent* session before it is allowed in.

    This used to check that session.json parsed, that its slug agreed, and that a claimed revision had
    a model.json — which is shape, not truth. An archive announcing revision 2 with no `revisions/` at
    all passed, and so did one whose model.json had been swapped for a different model: nothing is
    malformed in either, only the relationships are broken. `check_session_dir` is the same check
    `requivo session verify` runs, so an archive is held to exactly the standard a live session is."""
    problems = check_session_dir(d, expected_slug=slug)
    if problems:
        raise InvalidSessionError(
            f"the archive's session '{slug}' is not internally consistent: "
            + "; ".join(p.message for p in problems),
            details={"slug": slug, "problems": [p.to_dict() for p in problems]})


def _cmd_session_import(a, client) -> None:
    """Import a session archive: inspect → extract to a scratch directory → validate → move into place.

    Nothing lands in the session store until the whole archive has been checked and what came out of it
    has been confirmed to be a session. The old flow did the reverse — `extractall` straight into the
    store, then report success — so a bad archive was already unpacked by the time anyone could object.
    (If a second surface ever needs this, it moves to core; today the CLI is the only importer.)"""
    archive = Path(a.archive)
    if not archive.is_file():
        raise SessionNotFoundError(f"archive not found: {display_token(str(archive))}",
                                   details={"archive": str(archive)})
    root = session_root()
    root.mkdir(parents=True, exist_ok=True)

    try:
        z = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as e:
        raise InvalidSessionError(f"{display_token(str(archive))} is not a readable .zip archive: {e}",
                                  details={"archive": str(archive)}) from e
    with z:
        slug = _inspect_archive(z)
        if store.session_exists(slug) and not a.force:
            raise InvalidModelError(
                f"session '{slug}' already exists in this workspace — pass --force to replace it",
                details={"slug": slug})
        # Scratch space beside the store, not inside it: same filesystem, so the final move is a
        # rename, but never visible to `session list` while it is still half-written.
        scratch = Path(tempfile.mkdtemp(prefix=".import-", dir=root.parent))
        try:
            for info in z.infolist():
                z.extract(info, scratch)
            extracted = scratch / slug
            _validate_extracted(extracted, slug)
            target = store.canonical_dir(slug)
            replaced = target.exists()
            # Replacement is a swap, not a delete-then-move. `rmtree` followed by a rename leaves
            # nothing at all if the rename fails — the archive is refused *and* the session the user
            # already had is gone. The old one steps aside first and only dies once the new one is in
            # place; anything going wrong in between puts it back.
            backup = target.with_name(f".{target.name}.replaced-{os.getpid()}") if replaced else None
            if backup is not None:
                target.replace(backup)
            try:
                extracted.replace(target)
            except OSError as e:
                if backup is not None:
                    backup.replace(target)
                raise InvalidSessionError(
                    f"could not move the imported session into place: {e}"
                    + (" — the session that was already here has been restored" if backup else ""),
                    details={"slug": slug}) from e
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    if a.json:
        _print_json({"imported": slug, "into": str(root), "replaced": replaced})
        return
    print(f"Imported session '{slug}' → {store.canonical_dir(slug)}"
          + (" (replaced an existing session)" if replaced else ""))


# ── model ────────────────────────────────────────────────────────────────────────


def _cmd_model_show(a, client) -> None:
    svc = SessionService()
    model = svc.load_model(svc.resolve_slug(a.session))
    print(model.model_dump_json(indent=2))


def _cmd_model_validate(a, client) -> None:
    """Validate a proposal file — the gate Claude Code runs before applying. On success prints a tiny
    confirmation (or `--json` {status: valid}); on failure the structured error surfaces via app()."""
    data = _read_document(a.proposal)
    require = not a.allow_partial
    out = validate_proposal(data, require_complete=require)
    n_slots = len(out.model)
    if a.json:
        _print_json({"status": "valid", "slots": n_slots})
        return
    print(f"✅ Proposal is valid ({n_slots} slots).")


def _cmd_model_apply(a, client) -> None:
    """Apply a proposal as a new revision. Always the complete slot set: `apply` *replaces* the model,
    and `--allow-partial` used to read as if it merged — it did not, so applying one slot left a
    one-slot model where fifteen had been. Validating a projection is `model validate --allow-partial`;
    a real partial update needs a merge semantics this command never had."""
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    data = _read_document(a.proposal)
    result = svc.update_model(slug, data, expected_revision=a.expected_revision,
                              provenance={"provider": "claude-code", "surface": "cli-apply"})
    if a.json:
        _print_json(result.to_dict())
        return
    print(f"✅ Applied → revision {result.revision}")
    print(f"   changed slots: {', '.join(result.changed_slots) or '(none)'}")
    if result.invalidated_decisions:
        print(f"   decisions to re-validate: {len(result.invalidated_decisions)}")
    if result.invalidated_challenges:
        print(f"   premises to re-examine: {len(result.invalidated_challenges)}")
    if result.stale_artifacts:
        print(f"   now stale: {', '.join(result.stale_artifacts)}")
    rd = result.readiness
    print(f"   readiness: {'READY' if rd.ready else 'not ready'}"
          + (f" — blocking: {', '.join(rd.blocking_slots)}" if rd.blocking_slots else ""))


def _cmd_model_diff(a, client) -> None:
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    data = _read_document(a.proposal)
    # `diff` is the dry run of `apply`, so it holds the proposal to the same bar — a projection that
    # `apply` would refuse must not be previewed here as though it would land.
    result = svc.diff(slug, data)
    if a.json:
        _print_json(result.to_dict())
        return
    print(f"Would apply as revision {result.revision}")
    print(f"  changed slots: {', '.join(result.changed_slots) or '(none)'}")
    if result.stale_artifacts:
        print(f"  would go stale: {', '.join(result.stale_artifacts)}")


# ── artifact ─────────────────────────────────────────────────────────────────────


def _cmd_artifact_save(a, client) -> None:
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    content = _read_document(a.file)
    st = ArtifactService().save(slug, a.type, content, source_revision=a.revision)
    if a.json:
        # `stale` is reported on the *save*, not only on a later `artifact list`. Saving an artifact
        # reasoned from a superseded revision is legitimate and now recorded honestly — but the caller
        # that just did it is the one who can act on it, and it should not have to ask again to find out.
        _print_json({"type": a.type, "filename": st.filename, "revision": st.revision,
                     "stale": st.stale})
        return
    # Through the chokepoint, not re-joined here (#36). This line only prints the path, which is
    # exactly how it survived the sweeps that closed the writes and the read — `artifact_path` says
    # why display is not exempt, and which caller can actually hand this an unvalidated `st.filename`.
    where = store.artifact_path(slug, st.filename)
    print(f"Saved {a.type} → {where} (from revision {st.revision})")
    if st.stale:
        print(f"  Marked stale: the model has moved past revision {st.revision} in ways this "
              f"{a.type} rests on. Regenerate it to bring it current.")


def _cmd_artifact_list(a, client) -> None:
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    items = ArtifactService().list(slug)
    if a.json:
        _print_json(items)
        return
    if not items:
        print(f"No artifacts saved for '{slug}'.")
        return
    print(f"Artifacts for '{slug}':")
    for t, info in items.items():
        print(f"  {t:<12} {info['filename']:<26} rev {info['revision']}  {'STALE' if info['stale'] else 'fresh'}")


def _cmd_artifact_show(a, client) -> None:
    svc = SessionService()
    print(ArtifactService().show(svc.resolve_slug(a.session), a.type))


# ── registration ─────────────────────────────────────────────────────────────────


def register(sub) -> None:
    """Attach the deterministic verb groups to the main `requivo` subparser."""
    # doctor
    dr = sub.add_parser("doctor", help="diagnose the install (no API key needed)")
    dr.add_argument("--json", action="store_true", help="emit the report as JSON")
    dr.set_defaults(func=_cmd_doctor)

    # schema / context — read-only knowledge for a reasoning caller (Claude Code)
    sc = sub.add_parser("schema", help="print the slot schema (the model vocabulary + driver rule)")
    sc.add_argument("--framework", action="store_true", help="also print the human framework spec")
    sc.set_defaults(func=_cmd_schema)

    cx = sub.add_parser("context", help="list or print the product context cards")
    cx.add_argument("--list", action="store_true", help="list available card stems instead of content")
    cx.add_argument("--cards", metavar="CARDS", help="comma-separated subset to print (default: all)")
    cx.add_argument("--session", metavar="SESSION",
                    help="print exactly the cards this session was created with")
    cx.set_defaults(func=_cmd_context)

    # session
    sp = sub.add_parser("session", help="create, list, show, verify, migrate, export/import sessions")
    ss = sp.add_subparsers(dest="subcommand", required=True, metavar="<action>")

    si = ss.add_parser("init", help="create a session from a request (no LLM)")
    si.add_argument("request", help="the request, a path to a file containing it, or '-' for stdin")
    si.add_argument("--slug", help="explicit session slug (default: derived from the request)")
    si.add_argument("--context", metavar="CARDS", help="comma-separated context cards to record")
    si.add_argument("--provider", default=None, help="informational provider tag (e.g. claude-code)")
    si.add_argument("--json", action="store_true")
    si.set_defaults(func=_cmd_session_init)

    sl = ss.add_parser("list", help="list canonical sessions")
    sl.add_argument("--json", action="store_true")
    sl.set_defaults(func=_cmd_session_list)

    sh = ss.add_parser("show", help="show a session's metadata + artifacts")
    sh.add_argument("session", help="session slug or path")
    sh.add_argument("--json", action="store_true")
    sh.set_defaults(func=_cmd_session_show)

    sm = ss.add_parser("migrate", help="migrate ALL legacy out/ sessions into .requivo/sessions/")
    sm.add_argument("--json", action="store_true")
    sm.set_defaults(func=_cmd_session_migrate)

    se = ss.add_parser("export", help="export a session as a .zip archive")
    se.add_argument("session", help="session slug or path")
    se.add_argument("-o", "--output", help="destination archive path")
    se.add_argument("--json", action="store_true")
    se.set_defaults(func=_cmd_session_export)

    sv = ss.add_parser("verify", help="check that a session's files agree with each other")
    sv.add_argument("session", help="session slug or path")
    sv.add_argument("--json", action="store_true")
    sv.set_defaults(func=_cmd_session_verify)

    sig = ss.add_parser("import", help="import a session archive into the workspace")
    sig.add_argument("archive", help="path to a .zip produced by `session export`")
    sig.add_argument("--force", action="store_true",
                     help="replace a session of the same slug that already exists here")
    sig.add_argument("--json", action="store_true")
    sig.set_defaults(func=_cmd_session_import)

    # model
    mp = sub.add_parser("model", help="show, validate, apply, or diff a model")
    ms = mp.add_subparsers(dest="subcommand", required=True, metavar="<action>")

    msh = ms.add_parser("show", help="print a session's current model")
    msh.add_argument("session", help="session slug or path")
    msh.set_defaults(func=_cmd_model_show)

    mv = ms.add_parser("validate", help="validate a proposal file (no session write)")
    mv.add_argument("proposal", help="path to a proposed model JSON, or '-' to read it from stdin")
    # (A `--session` flag lived here, promising validation "against a session's context", and was read
    # by nothing. Whatever it was going to mean, `model diff <slug> <proposal>` already means it: it
    # reports exactly what applying the proposal to that session would change, without writing.)
    mv.add_argument("--allow-partial", action="store_true",
                    help="check a partial projection for well-formedness only — `apply` and `diff` "
                         "always require the full slot set, because applying replaces the model")
    mv.add_argument("--json", action="store_true")
    mv.set_defaults(func=_cmd_model_validate)

    ma = ms.add_parser("apply", help="validate a proposal and apply it as a new revision")
    ma.add_argument("session", help="session slug or path")
    ma.add_argument("proposal", help="path to a proposed model JSON, or '-' to read it from stdin")
    ma.add_argument("--expected-revision", type=int, default=None,
                    help="only apply if the session is still at this revision (optimistic lock)")
    ma.add_argument("--json", action="store_true")
    ma.set_defaults(func=_cmd_model_apply)

    md = ms.add_parser("diff", help="show what a proposal would change (no write)")
    md.add_argument("session", help="session slug or path")
    md.add_argument("proposal", help="path to a proposed model JSON, or '-' to read it from stdin")
    md.add_argument("--json", action="store_true")
    md.set_defaults(func=_cmd_model_diff)

    # artifact
    ap = sub.add_parser("artifact", help="save, list, or show generated artifacts")
    aps = ap.add_subparsers(dest="subcommand", required=True, metavar="<action>")

    asv = aps.add_parser("save", help="save an artifact against a session")
    asv.add_argument("session", help="session slug or path")
    asv.add_argument("--type", required=True, choices=sorted(ARTIFACT_FILENAMES),
                     help="artifact type")
    asv.add_argument("--file", required=True, help="path to the artifact content, or '-' to read it from stdin")
    asv.add_argument("--revision", type=int, default=None,
                     help="source model revision (default: the session's current revision)")
    asv.add_argument("--json", action="store_true")
    asv.set_defaults(func=_cmd_artifact_save)

    al = aps.add_parser("list", help="list a session's artifacts + freshness")
    al.add_argument("session", help="session slug or path")
    al.add_argument("--json", action="store_true")
    al.set_defaults(func=_cmd_artifact_list)

    ash = aps.add_parser("show", help="print a saved artifact's content")
    ash.add_argument("session", help="session slug or path")
    ash.add_argument("--type", required=True, choices=sorted(ARTIFACT_FILENAMES))
    ash.set_defaults(func=_cmd_artifact_show)
