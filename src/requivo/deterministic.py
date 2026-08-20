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
from requivo.core.errors import (
    ImportMoveFailedError,
    InconsistentArchiveError,
    InvalidArchiveError,
    InvalidModelError,
    SessionExistsError,
    SessionNotFoundError,
    SessionUnreadableError,
    UnreadableArchiveError,
)
from requivo.core.integrity import IntegrityProblem, check_session, check_session_dir
from requivo.core.selectors import display_token
from requivo.core.validation import validate_proposal
from requivo.paths import ASSETS, CONTEXT, session_root, user_context_dir, workspace_root
from requivo.services.artifacts import ARTIFACT_FILENAMES, ArtifactService
from requivo.services.sessions import SessionService
from requivo.streams import describe_streams

# The work was done and part of the answer was unreachable. Neither 0 nor 1 is true of that: 0 says
# nothing is wrong, 1 says there is no answer at all, and a script that does not parse stdout reads
# the exit code alone — so collapsing it into either neighbour is invariant 15's own defect in the
# one channel left. It is safe to make non-zero *because* stdout is complete: unlike the error path,
# nothing is withheld, so a caller that only wants what was produced still gets all of it.
#
# It was `EXIT_DEGRADED_LISTING` and it is `EXIT_DEGRADED` (#86): an exit code describes a shape of
# answer, not a verb. `session list` rendering every row it could was the first instance;
# `session verify` unable to read a session's product context is the second, and minting a number
# per verb would rebuild the problem this one was introduced to solve.
#
# 0/1/2 are success, `RequivoError` and argparse; 3 is `cli.EXIT_RENDER_FAILED`. It cannot be
# imported from there — `cli` imports this module, so the dependency runs one way only — and
# `test_the_degraded_code_collides_with_nothing` is what stops the two numbers drifting into each
# other.
EXIT_DEGRADED = 4


def _print_json(obj) -> None:
    # `ensure_ascii` is left at its default and that is load-bearing (#70) — for a narrower set of
    # characters than `session list` and `session show` claim between them. JSON's own grammar
    # forbids a literal control character below U+0020 inside a string, so a newline is escaped
    # whatever this flag says. What the flag decides is everything *non-ASCII*: U+007F–U+009F, where
    # NEL and CSI live, and also U+2028/U+2029, which the terminal-side guard deliberately does not
    # cover. So this path is the *stricter* of the two and stays that way only while the default
    # does; turning it off to make accented output readable would reopen the forgery by that route,
    # and `test_session_show_json_escapes_a_control_character_before_it_reaches_a_line` is what
    # objects.
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
    - `non_sessions` — what is under the session root and is *not* a session: the name, what kind of
      thing it is and what it holds, from `list_non_session_entries`. Nothing could see one of these
      at all (#67), and the symptom is not in this report — it is the next `create_session` on that
      name quietly landing under `<slug>-<hash>` instead. `None`, never `[]`, in the arm where the
      root could not be listed: an empty list there reads as *we looked and there is nothing else*,
      which is this function's own defect class one key along.
    - `unexaminable` — names under the root that could not be examined at all, so nothing above knows
      whether they are sessions (#80). Kept out of `non_sessions` because that key states a fact —
      *this is not a session* — and here nobody established one; kept out of `total` for the same
      reason, so the count stays what could be confirmed. `None` in the unreadable-root arm, on the
      same terms as its neighbour.

      This is the narrow claim `readable: False` used to swallow: one directory the process could
      not stat into made the *whole root* read as unlistable, which was broader than what failed
      and also, on the surface a user actually runs, fatal.
    """
    inconsistent: dict[str, list[str]] = {}
    unresolved: dict[str, dict] = {}
    try:
        # One listing for all three parts. Calling `list_session_slugs`, `list_non_session_entries`
        # and `list_unexaminable_entries` separately reads the directory at three instants, and a
        # `session.json` landing between them puts a name in *no* answer at all — the invisible
        # state this key exists to end, reintroduced by the key itself. Neither
        # `_describe_non_session` nor the partition's third bucket raises, so what this `except`
        # catches is the listing, which is genuinely the whole root.
        slugs, entries, blind = store.scan_session_root()
        non_sessions = [e.to_dict() for e in entries]
        unexaminable = [e.to_dict() for e in blind]
    except Exception as e:  # noqa: BLE001 - doctor reports, it does not fail — but it must say what it hit
        return {"total": None, "readable": False, "error": str(e),
                "inconsistent": {}, "unresolved_cards": {}, "cards_checked": False,
                "non_sessions": None, "unexaminable": None}
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
            "cards_checked": cards_readable, "non_sessions": non_sessions,
            "unexaminable": unexaminable}


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
            # Both spellings named, because #85 made them aliases: a refusal that names one of two
            # accepted flags reads as a claim that the other is not the flag you passed.
            raise InvalidModelError(
                "--session and --cards/--context are alternatives; pass only one")
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
        if stream["state"] == "will_crash":
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
    blind = h["unexaminable"] or []
    notes = ([f"{len(bad)} inconsistent"] if bad else []) \
        + ([f"{len(lost)} with product context that no longer loads"] if lost else []) \
        + (["product context not checked"] if unchecked else []) \
        + ([f"{len(blind)} entr{'y' if len(blind) == 1 else 'ies'} that could not be examined"]
           if blind else [])
    # Three glyphs for three states, on the line a reader actually scans. Leaving "not checked" to
    # a trailing note put a tick on this line while nobody had looked — the defect this whole change
    # is about, one line further down than where it was filed.
    # An unexaminable entry is a *could not look*, so it takes the same middle glyph as an unchecked
    # card layer rather than the failure glyph: nothing here is known to be broken, and spelling
    # "we could not tell" the same way as "this is wrong" is the merge the third state exists to
    # prevent. It must not be the clean tick either, which was the whole finding.
    glyph = "❌" if (bad or lost) else (warn if (unchecked or blind) else ok)
    print(f"  {glyph} sessions        {h['total']} in this workspace"
          + (f" · {' · '.join(notes)}" if notes else ""))
    # `display_token` on every slug, for the reason `_print_unexaminable` states two functions down
    # and this loop did not: the name is a raw directory entry. `_scan_session_root` puts it in the
    # *sessions* bucket on `(p/"session.json").exists()` alone, and the `except Exception` above turns
    # a name `validate_slug` would refuse into an ordinary `unreadable` row — so a directory whose
    # name carries a newline and holds a session.json wrote two further lines of this report at
    # column 0, in the shape of real ones. Reproduced before it was fixed; #40 is the same defect on
    # the card-name half of this verb, and `_print_non_sessions` already covers the sibling bucket.
    #
    # `problem['message']` needs no wrap and is left bare deliberately: the card names inside it have
    # been through `normalize_tokens`, which refuses a control character outright
    # (`unsafe_selector_token`) — invariant 14's second door. Wrapping it would say that guard gave us
    # nothing, which is the reading that makes the next person wrap what does not need it.
    for slug, codes in bad.items():
        safe = display_token(slug)
        print(f"     └─ {safe}: {', '.join(codes)} — run `requivo session verify {safe}`")
    for slug, problem in lost.items():
        print(f"     └─ {display_token(slug)}: {problem['message']}")
    # One hint per remedy actually present, rather than one hint for whichever remedy came first.
    codes = {p["code"] for p in lost.values()}
    if codes & _RESTORABLE_CARD_CODES:
        print(f"        {_RESTORE_HINT}")
    if codes - _RESTORABLE_CARD_CODES:
        print(f"        {_REPAIR_HINT}")
    if unchecked:
        print("     └─ the card directory could not be read (see above), so nothing is known about "
              "whether these sessions' product context still loads.")
    _print_unexaminable(blind, h["total"])
    _print_non_sessions(h["non_sessions"])


def _print_unexaminable(entries: list[dict], total: int | None) -> None:
    """Names under the session root that could not be examined, under the sessions check (#80).

    Under *sessions* and not under *other entries*, because that is the one thing the failed probe
    did not settle: this may be a session and it may not. `_print_non_sessions` says `Requivo does
    not read these`, which would be a claim, and would be the wrong one on the reading where it
    matters — a user's own session, invisible.

    The count on the line above stays what could be *confirmed*, so this says so rather than leaving
    a reader to reconcile `1 in this workspace` with a second entry named beneath it. A count that
    silently absorbed these would be the quiet-wrong-answer form of the same bug.

    The name and the error text both come off disk and both go through `display_token`: a new site
    for the guard, and a fresh one — the name is a raw directory entry, and the `read_meta` that
    would have refused a name carrying a newline is exactly what could not run (#40).
    """
    if not entries:
        # `[]` is a clean workspace and earns no row. The unreadable-root arm passes `None`, but it
        # has already returned above with a line of its own, so it never reaches here.
        return
    n = len(entries)
    # Detail lines under the sessions check, not a check row of its own. The row above already
    # carries the count as one of its notes and already wears the middle glyph for it; a second
    # line at check indent reading `sessions` would be two rows answering one question, and a
    # reader scanning the glyph column could not tell which was the verdict.
    for entry in entries:
        print(f"     └─ {display_token(entry['name'])} — could not be examined: "
              f"{display_token(entry['error'] or _NO_DETAIL)}")
    thing = "this is a session" if n == 1 else "these are sessions"
    print(f"     Requivo cannot tell whether {thing}, so the count above ({total}) is what it could "
          "confirm, not what is there. Nothing has been read, moved or changed; Requivo does not "
          "alter permissions in your workspace.")


def _non_session_detail(entry: dict) -> str:
    """One entry of `sessions.non_sessions`, as a clause naming what is there and nothing else.

    Every branch is an observation. There is no arm that says *a leftover lock directory*, because
    that is a conclusion the directory cannot support — `.lock` and nothing else is what an older
    `session_lock` left (#22) and also what an interrupted unzip leaves, and this verb's evidence is
    the directory and only the directory (invariant 14).

    **Every value interpolated here comes off disk, so every one goes through `display_token`** —
    the names and the error text alike. One carrying a newline would otherwise end this line and
    start another at column 0 of `doctor`'s own report, which is exactly what a stored context-card
    name could do before #40.

    This docstring used to state the rule for the *names* only, on a line that carries two classes of
    value, and the two `error` interpolations below went unwrapped for a release (#90). `error` is
    `str(e)` from a deliberately wide `except Exception` in the store, where the docstring beside it
    says the set of ways a member can be broken is open — an open set of causes feeding an unescaped
    interpolation is the shape #40 was. Today it misreports rather than forges, because the reachable
    exceptions are the `OSError` family and CPython's `OSError.__str__` already `repr()`s the
    filename; that is a fact about today's exception space and not a property of this line, which is
    why the wrap is here and not in a comment saying it is unnecessary."""
    kind, error = entry["kind"], entry["error"]
    if kind == "unknown":
        return f"could not be examined — {display_token(error)}"
    if kind == "file":
        return "a file, not a directory"
    if kind == "symlink":
        # Not followed, and not described as whatever it points at. Reporting a symlink's target
        # contents would list another directory's filenames into a report about this workspace,
        # and would answer `directory` about something that is not one.
        return "a symbolic link, not followed; nothing here is read from its target"
    if kind == "other":
        return "neither a file nor a directory"
    if error:
        # Not "an empty directory". We could not look inside, and an empty directory is the one
        # shape that costs nothing on POSIX (`rename(2)` replaces an empty destination) — so the two
        # answers must not be spelled the same way.
        return f"a directory whose contents could not be listed — {display_token(error)}"
    total, shown = entry["entry_count"], entry["entries"] or []
    if not total:
        return "an empty directory"
    names = ", ".join(display_token(n) for n in shown)
    more = f", … ({total} in total)" if total > len(shown) else ""
    return f"a directory holding {total} entr{'y' if total == 1 else 'ies'}: {names}{more}"


def _print_non_sessions(entries: list[dict] | None) -> None:
    """Things under the session root that are not sessions, named with what they cost.

    `doctor` owns this rather than `session verify` for the reason the state exists: `verify` is
    per-session and takes a slug, and the defining property of one of these is that no listing
    produces its name, so there is no slug for anybody to type. `doctor` already answers about the
    workspace as a whole and already carries the three-state discipline this needs (#67).

    Its own row rather than a note on the sessions row, because `0 in this workspace` stays true —
    none of this is a session — and folding it in would trade a correct count for a vague one.

    The consequence is printed and not left to be inferred. A finding with no remedy is a line
    people learn to scroll past, and this one is invisible until it strikes: the rename that claims
    a slug (invariant 11) loses to anything already occupying the name, and `SessionService` then
    falls through to its hash-suffixed candidate without a word. It is printed only for a name
    `create_session` can actually be asked for — `canonical_dir` refuses anything else long before a
    rename — because a consequence that cannot happen is noise on a report that is already a
    judgement call."""
    if not entries:
        # `None` here is the unreadable-root arm, which has already returned above with its own
        # line; `[]` is a clean workspace, and a clean check earns no row on this report.
        return
    n = len(entries)
    print(f"  🟡 other entries   {n} entr{'y' if n == 1 else 'ies'} under this directory that "
          "Requivo does not read")
    for entry in entries:
        taken = "  [name taken]" if entry["slug_shaped"] else ""
        print(f"     └─ {display_token(entry['name'])} — {_non_session_detail(entry)}{taken}")
    # Marked per row and explained once. Repeating the mechanism under every row buried the rows
    # themselves the moment there was more than one, and the rows are the finding.
    if any(e["slug_shaped"] for e in entries):
        print("     [name taken]: a new session asked for that name will not get it. The rename "
              "that claims a slug loses to anything already occupying it, so the session is "
              "created under that name plus a hash — which is the only symptom any of this has.")
    print("     Requivo has not read, moved or deleted any of these, and does not say what they "
          "are: an interrupted copy and a directory an older version left behind look the same "
          "from here. Check before removing anything.")


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


# Said once rather than at each of the sites that need it, because they have to agree: an entry with
# no error text still has to read as *we could not look*, and an empty string there would render as a
# row that failed for no reason.
_NO_DETAIL = "no further detail"


def _session_list_row(entry) -> dict:
    """One `--json` row, with the **same key set** whether the session could be read or not.

    That is the compatibility decision, and it is why a degraded row is not simply a shorter dict:
    `session list --json` is a public output (invariant 8), and a consumer looping over
    `payload["sessions"]` reading `row["revision"]` would get a `KeyError` from a row it was handed
    deliberately — trading a command that fails loudly for a caller that fails obscurely, one layer
    along.

    So the fields are always present and `null` where the fact is missing. `null`, never `0` or `""`:
    we did not read revision 0, we failed to read the revision, and a plausible value on a session
    nobody could open is the quiet-wrong-answer form of the bug this whole guard exists for.
    `readable` is what a consumer should branch on; `error` carries the reason, because *written by a
    newer Requivo, upgrade* is a remedy and a flattened code is not.
    """
    if not entry.readable:
        return {"slug": entry.slug, "revision": None, "provider": None, "updated_at": None,
                "readable": False, "error": entry.error or _NO_DETAIL}
    m = entry.meta
    return {"slug": m.slug, "revision": m.current_revision, "provider": m.provider,
            "updated_at": m.updated_at, "readable": True, "error": None}


def _session_list_line(entry) -> str:
    """One terminal row. A session that could not be read still gets one, and still names itself.

    **Every text field on both branches is untrusted, and all of them go through `display_token`**
    (#40). An earlier draft of this docstring wrapped only the degraded branch and argued the
    readable one was safe because "the slug comes back through `read_meta`, which validates it".
    That was wrong, and wrong in the way this codebase keeps finding: `read_meta` validates the slug
    it is *called with* — the directory name, via `canonical_dir` — and then returns
    `SessionMeta.slug`, which is the `"slug"` field inside `session.json`'s own body, declared a bare
    `str` with no pattern. The two are not the same value and nothing checks that they agree outside
    `session import`. Reproduced on this branch: a `session.json` whose `slug` carries a newline
    printed a second, entirely fabricated row — `rev 999 (trusted, …)` — into the listing, and the
    command exited 0.

    That is invariant 14's second door. A persisted `session.json` is untrusted input every time it
    is read back, exactly as a persisted `context_cards` is; creation resolving a value is a
    guarantee about creation, never about what is on disk. So:

    * **the degraded row's slug** is the raw directory name — `list_session_slugs` returns `p.name`
      filtered only on a leading dot, and the `read_meta` that would have refused a non-kebab name is
      precisely why this row is degraded, so it never ran;
    * **the readable row's `slug`, `provider` and `updated_at`** all come out of the file's body.
      `current_revision` does not need wrapping: it is an `int`, so `read_meta` refuses a string
      there already;
    * **the error text** is whatever the failure said. `read_meta` refusing a `session.json` whose
      `current_revision` is a string raises a pydantic `ValidationError` whose message is four lines
      long; printed raw that is four rows of listing for one session, with the reader unable to tell
      where the row ends. `display_token` collapses it to one escaped line — the same `!r` treatment
      `core/integrity.py` gives the recorded artifact filename, its sibling untrusted field.

    A value that is already one safe line comes back byte-for-byte, so every real session's row is
    unchanged and no reader learns a new shape for the normal case.

    **`session show` had the same defect and is fixed in #70** — this paragraph used to say it was
    deliberately left for its own change, which it was, and the pointer is kept rather than deleted
    because the count it gave was wrong: five, where the verb turned out to print **eight** untrusted
    strings. #62 counted the `SessionMeta` scalars and missed `slug` plus the two fields that live on
    `ArtifactStatus` and its dict key. Read `_cmd_session_show`'s docstring for the surface-specific
    half; the argument is this one.

    The `--json` path needs none of this, **for a narrower reason than this file used to give**.
    `json.dumps` defaults to `ensure_ascii=True`, and that default is load-bearing — but not for the
    newline both issues reproduced with. A control character below U+0020 is escaped by JSON's own
    grammar whatever the flag says; what the flag decides is the *non-ASCII* half of `_CONTROL_CHARS`,
    U+007F–U+009F, which carries NEL and CSI. Measured, and pinned by
    `test_session_show_json_escapes_a_control_character_before_it_reaches_a_line`, which probes both
    halves because a newline probe is green either way and pins nothing (#70).

    The reason rides the row rather than being replaced by a pointer, because for the commonest break
    mode the reason *is* the remedy. `requivo session verify <slug>` is the acting surface the footer
    points at for the cases where one line is not enough: measured against each way `read_meta` can
    refuse — a newer `format_version`, an unparseable `session.json`, a field of the wrong type — it
    reports an integrity code and exits 1 rather than raising.

    **Two** cases it does not report on, and they fail in opposite directions. A slug that is not a
    slug is refused by name, and there the row's own text is already the whole story because the name
    is the defect. An entry the partition could not *examine* is the other, and it is the one worth
    knowing about: `session_exists` probes `session.json` with the same unguarded `.exists()` this
    file's own listing had to stop using in #80, so `verify` raised a bare `PermissionError` on the
    very row this footer sent the reader to. **Fixed in #97**, one release after it was filed here:
    `session_exists` raises `SessionUnreadableError` rather than widening a bool that has two states
    for a question with three, and `verify` folds that into `unchecked` and exits **4** — the footer
    now reaches a verb that says it could not look, which is what this line always promised.
    """
    if not entry.readable:
        return (f"  {display_token(entry.slug):<40} could not be read — "
                f"{display_token(entry.error or _NO_DETAIL)}")
    m = entry.meta
    return (f"  {display_token(m.slug):<40} rev {m.current_revision}  "
            f"({display_token(m.provider or '—')}, {display_token(m.updated_at)})")


def _cmd_session_list(a, client) -> None:
    """Every session, degrading the ones that cannot be read rather than failing for the set.

    Invariant 15 — *a listing survives its own members* — and this is the surface that did not get
    the fix when the web half shipped (#7, #62). `list_sessions()` is the strict read: a single
    comprehension over `read_meta`, so one `session.json` written by a newer Requivo raised before
    any row existed, the command exited 1 with a single message, every other session was invisible,
    and nothing named which session was the problem. `list_entries()` is the same read degrading per
    member, and it is where the guard belongs — above the rows, not around them.

    **This row needs no second `except` and the web's does.** Everything rendered here comes off the
    metadata `list_entries` has already loaded and guarded; the web row additionally calls
    `request_text` and `status()`, which is why it wraps its row builder as well. That is a fact
    about the current row shape rather than a promise: adding a read to this row means adding that
    guard too, and `test_a_break_below_the_metadata_does_not_reach_this_listing` is what will say so.
    """
    entries = SessionService().list_entries()
    degraded = [e for e in entries if not e.readable]
    if a.json:
        # An **object**, not the bare array this was until #87. It was the only array among the
        # fourteen JSON payloads this CLI prints, and an array has no top level, so no field could
        # ever be added to it without the type change made here once, in the 1.0 release itself.
        #
        # `degraded` recovers no fact. Every row carries `readable` and `error` whether it could be
        # read or not, so the count has always been derivable from the rows. What the key buys is
        # that exit 4 is readable on stdout rather than only signalled, which is the same argument
        # that makes a degraded row name its session instead of disappearing.
        _print_json({"sessions": [_session_list_row(e) for e in entries],
                     "degraded": len(degraded), "session_root": str(session_root())})
    elif not entries:
        print(f"No sessions under {session_root()}.")
    else:
        print(f"Sessions under {session_root()}:")
        for e in entries:
            print(_session_list_line(e))
        if degraded:
            n = len(degraded)
            print()
            # `entr{y,ies}` and not `session{,s}` since #80. A degraded row used to be a name that
            # certainly had a `session.json` behind it, because every row came from
            # `list_session_slugs`; one of them can now be an entry nobody could examine, and calling
            # that a session is the single claim this whole change exists to refuse. The word also
            # matches what `doctor` says about the same entry, so the two surfaces stop describing
            # one thing two ways. `session verify <slug>` stays the remedy: it is right for every
            # mode it was written for, and where it is not, the fix belongs in that verb.
            print(f"{n} entr{'y' if n == 1 else 'ies'} could not be read. "
                  f"`requivo session verify <slug>` reports what is wrong in full.")
    # Raised after the listing is printed, never instead of it: the rows are the answer, and the exit
    # code is the third state in the one channel a script that does not parse stdout can read.
    if degraded:
        raise SystemExit(EXIT_DEGRADED)


def _cmd_session_show(a, client) -> None:
    """One session's metadata. **Every string on this path comes out of `session.json`'s body and is
    untrusted**, so all eight of them go through `display_token` (#70).

    The argument is `_session_list_line`'s, in full, and is not repeated here — read that docstring.
    Only two things differ, and both make this verb the worse of the pair rather than the safer one:

    * **It is eight fields, not the five the issue counted.** #62 named the five that happen to be
      `SessionMeta` scalars. The other three are `meta.slug` — which #62's own fix caught on the
      listing and which is the same bare `str` here — plus two that are not `SessionMeta` fields at
      all: the **keys** of `artifact_status`, a `dict[str, …]` whose keys are whatever the file says,
      and `ArtifactStatus.filename`. `core/integrity.py` already treats that recorded filename as
      untrusted input; a render site that does not is the exception that makes the rule unreliable.
    * **Every line here is one Requivo writes itself**, in a fixed shape, at a fixed column. On the
      listing a forged row at least has to imitate a row; here a stored value can print
      `  revision 0` under a session that is at revision 12, and nothing in the render distinguishes
      the two. Reproduced on this branch: a `session.json` forged in all eight fields printed sixteen
      lines instead of eight, including its own `revision 999` and `provider trusted`, and the
      command exited 0.

    `meta.current_revision`, `st.revision` and `st.stale` are deliberately **not** wrapped, and that
    is stated rather than hedged: they are `int`/`int`/`bool`, so `read_meta` refuses a string there
    before this function runs. Wrapping them defensively would say the type gave us nothing, which is
    the reading that makes the next person wrap something that genuinely does not need it.

    `session_id` is **sliced before it is escaped**, and the order is load-bearing: escaping first
    would produce a quoted, backslash-escaped string, and truncating *that* to twelve characters can
    cut an escape sequence in half and leave the quote unclosed — a neutralised value rendered as
    garbage, which is a second defect bought with the fix for the first.

    The `--json` path needs none of this and is left alone — but **not for the reason #62 and #70
    both give**, which is worth stating here because that reason is what a later reader will act on.
    It is not that `json.dumps` defaults to `ensure_ascii=True`: a control character below U+0020 is
    escaped by JSON's own grammar whatever that flag says, so a *newline* — the character both issues
    reproduced with — is safe either way. The default is still load-bearing, for the non-ASCII half of
    `_CONTROL_CHARS` (U+007F–U+009F), which carries NEL, a line terminator `str.splitlines()` honours,
    and CSI. Measured rather than argued, and pinned by
    `test_session_show_json_escapes_a_control_character_before_it_reaches_a_line`, which probes both
    halves precisely because a newline probe is green under either setting and pins nothing.

    A value that is already one safe line comes back byte-for-byte, so no real session's output
    changes — `test_session_show_leaves_an_ordinary_session_byte_for_byte` pins every line of it.

    **What this does not cover, said here rather than left to be discovered.** `display_token`'s
    `_CONTROL_CHARS` is C0, DEL and C1 — the class that can move a terminal's cursor or end its line.
    `str.splitlines()` also breaks on U+2028 and U+2029, which are *not* in that class and come back
    from `display_token` byte-for-byte. On a terminal that is correct: xterm and the VT sequences it
    descends from answer to CR and LF, not to Unicode `Zl`/`Zp`. It is not correct for anything that
    parses this human-readable output line by line — which is what `--json` is for, and which is
    covered there, since `ensure_ascii=True` escapes those two as well. Widening `_CONTROL_CHARS`
    would also change what `normalize_tokens` *refuses*, i.e. the public `unsafe_selector_token`
    surface, and that module's own comment scopes it deliberately — so it is a decision for its
    owner, reported rather than taken here (#70).

    **One cosmetic cost, accepted rather than overlooked.** The first line wraps the slug in literal
    quotes of its own, so a slug that has to be escaped renders nested — an apostrophe in the stored
    value puts a `repr` in double quotes inside this line's single ones. Ugly, still one line, still
    incapable of forging anything. Both available fixes are worse: dropping the literal quotes changes
    the output of every clean session, which is the guarantee above and worth more than the nesting;
    and quoting conditionally on whether `display_token` escaped puts a branch on that function's
    *return shape* rather than on its contract, which is the coupling that survives until somebody
    changes the escaper.
    """
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    if not store.session_exists(slug):
        raise SessionNotFoundError(f"no canonical session {display_token(slug)}", details={"slug": slug})
    meta = store.read_meta(slug)
    if a.json:
        _print_json(meta.model_dump())
        return
    print(f"Session '{display_token(meta.slug)}'  (id {display_token(meta.session_id[:12])}…)")
    print(f"  created  {display_token(meta.created_at)}")
    print(f"  updated  {display_token(meta.updated_at)}")
    print(f"  revision {meta.current_revision}")
    print(f"  provider {display_token(meta.provider or '—')}   "
          f"model {display_token(meta.model_name or '—')}")
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
            #
            # Padded *after* escaping, which is the only order that works: the column widths exist so
            # a reader can scan the block, and padding a value that is about to grow quotes lines the
            # block up against a length the render does not have.
            print(f"    {display_token(t):<12} {display_token(st.filename):<26} "
                  f"rev {st.revision}  {'STALE' if st.stale else 'fresh'}")


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
    moment is the failure this whole change is about.

    **Three answers, three exit codes (#86).** The rendering has always distinguished them and the
    exit code distinguished two, in the verb whose whole job is to answer *is this session sound*:

    - `problems` — checked, the session is inconsistent. A complete answer. **1**.
    - `cards["problem"]` — checked, its product context is broken. Also complete. **1**.
    - `not cards["checked"]` — the context could not be checked. Not an answer at all. **4**.

    4 rather than a code of this verb's own: it already means *the work was done and part of the
    answer was unreachable*, and an exit code describes a shape of answer, not a verb. A code per
    verb rebuilds the problem 4 was introduced to solve.

    **A firm negative outranks a partial one**, so a session that is both inconsistent *and* whose
    cards could not be read exits 1. A script gating on *is this usable* wants the definite answer,
    and there is one. Nothing is withheld at either code: `--json` carries the whole story either
    way, and `ok` keeps the meaning it always had — it is false in all three failing states.
    """
    svc = SessionService()
    slug = svc.resolve_slug(a.session)
    # The probe itself is a third source of `unchecked` (#97). `session_exists` no longer escapes as a
    # bare traceback when it cannot stat — it raises `SessionUnreadableError` — and letting that
    # propagate would exit 1, which says *I checked and it is broken* about a session nothing looked
    # at. That is the collapse #86 removed from this verb; it must not come back through a different
    # door. Nothing below this line can run either: `check_session` and `_card_health` both read the
    # directory this call could not stat.
    session_probe: dict = {"checked": True, "error": None}
    try:
        found = store.session_exists(slug)
    except SessionUnreadableError as e:
        session_probe = {"checked": False, "error": str(e)}
        found = True
    else:
        if not found:
            raise SessionNotFoundError(f"no canonical session {display_token(slug)}",
                                       details={"slug": slug})
    problems = check_session(slug) if session_probe["checked"] else []
    cards = _card_health(slug) if session_probe["checked"] else {"checked": False, "problem": None,
                                                                 "error": session_probe["error"]}
    unsound = bool(problems) or cards["problem"] is not None
    unchecked = not cards["checked"] or not session_probe["checked"]
    ok = not unsound and not unchecked
    # `exit_code`, not `code`: the rendering below already binds `code` to a card-problem *code*
    # string, and the collision reached the raise as `SystemExit('unknown_context_card')`, which
    # CPython prints to stderr and turns into status 1 — the number this change is about replaced by
    # a stray line, on the branch where the shadowing happens and only there. Caught by an existing
    # test, not by this one, which is why the name rather than the number is the fix.
    exit_code = 1 if unsound else (EXIT_DEGRADED if unchecked else 0)
    if a.json:
        # `session` is additive and always present (#97). It is a sibling of `context_cards` and
        # carries the same two keys for the same reason: a consumer reading `problems: []` has to be
        # able to tell *checked, nothing wrong* from *nothing was checked*, and an empty list spells
        # both. Branch on `session.checked`, never on the emptiness of `problems`.
        _print_json({"slug": slug, "ok": ok, "session": session_probe,
                     "problems": [p.to_dict() for p in problems], "context_cards": cards})
        if exit_code:
            raise SystemExit(exit_code)
        return
    if ok:
        print(f"✅ Session '{slug}' is internally consistent and its product context still loads.")
    if not session_probe["checked"]:
        print(f"🟡 Could not examine '{slug}': {display_token(session_probe['error'])}")
        print("   Nothing about this session was checked — this is not a report that it is sound.")
        raise SystemExit(exit_code)
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
    if exit_code:
        raise SystemExit(exit_code)


# Ceilings for an imported archive. A session is a handful of small JSON and Markdown files; anything
# near these is not one. They exist so a hostile or corrupt archive fails on a bound rather than on the
# filesystem filling up, and so decompression cannot be used as an amplifier.
MAX_ARCHIVE_FILES = 2_000
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


def _inspect_archive(z: zipfile.ZipFile) -> str:
    """Validate an export archive *before* anything is written, and return the single session slug it
    contains. Raises `InvalidArchiveError` on anything unexpected, with `details["problem"]` naming
    which shape check refused it — see that class for the vocabulary and for why the seven conditions
    are one code (#101). It answered `invalid_model` until then, on a path where nobody had proposed
    a model and where the arm on either side of it already named the archive.

    Checking names by string prefix (the previous guard: `str(target).startswith(str(root))`) is not a
    containment test — `/…/sessions-evil` starts with `/…/sessions`. Here every entry is decomposed
    into path components instead, so a separator, a drive letter, a root, or a `..` segment is
    unrepresentable rather than merely unlikely."""
    infos = [i for i in z.infolist() if not i.is_dir()]
    if not infos:
        raise InvalidArchiveError("the archive contains no files", details={"problem": "empty"})
    if len(infos) > MAX_ARCHIVE_FILES:
        raise InvalidArchiveError(
            f"the archive holds {len(infos)} files; the maximum is {MAX_ARCHIVE_FILES}",
            details={"problem": "too_many_files",
                     "files": len(infos), "max_files": MAX_ARCHIVE_FILES})
    total = sum(i.file_size for i in infos)
    if total > MAX_ARCHIVE_BYTES:
        raise InvalidArchiveError(
            f"the archive expands to {total} bytes; the maximum is {MAX_ARCHIVE_BYTES}",
            details={"problem": "too_large",
                     "bytes": total, "max_bytes": MAX_ARCHIVE_BYTES})

    slugs = set()
    for i in infos:
        name = i.filename
        if "\\" in name:  # a Windows-style separator is not a component boundary to zipfile
            raise InvalidArchiveError(f"unsafe path in archive: {name!r}",
                                      details={"problem": "unsafe_entry", "entry": name})
        parts = PurePosixPath(name).parts
        if len(parts) < 2:
            raise InvalidArchiveError(
                f"archive entry {name!r} is not inside a session directory; an export contains "
                "<slug>/session.json and friends",
                details={"problem": "entry_outside_session_directory", "entry": name})
        if any(p in ("", ".", "..") for p in parts) or PurePosixPath(name).is_absolute():
            raise InvalidArchiveError(f"unsafe path in archive: {name!r}",
                                      details={"problem": "unsafe_entry", "entry": name})
        slugs.add(parts[0])

    if len(slugs) != 1:
        # `display_token` per name, and this is the one arm on this path that needs it: the two
        # entry-name refusals above render with `!r`, and every message *after* this point names a
        # slug that `validate_slug` has already made kebab-safe. Here the names are raw archive text
        # — a directory called "ok\nAll clear." ends the line and writes the next at column 0, in the
        # refusal that exists to report it. Same class as #40 and #98, one function along. A name
        # with nothing to escape comes back byte-for-byte, so an ordinary archive reads unchanged,
        # and `details["slugs"]` stays raw because `json.dumps` escapes it on the way out.
        shown = ", ".join(display_token(s) for s in sorted(slugs))
        raise InvalidArchiveError(
            f"the archive holds {len(slugs)} session directories ({shown}); "
            "import takes exactly one",
            details={"problem": "multiple_sessions", "slugs": sorted(slugs)})
    slug = slugs.pop()
    # The directory name becomes a session slug, so it faces the same validation as any other — this is
    # what stopped an archive whose folder was called `bad slug` from being unpacked into the store and
    # breaking every later `session list`.
    return store.validate_slug(slug)


def _swap_in(extracted: Path, target: Path, slug: str) -> None:
    """Replace an existing session directory with a freshly extracted one, reversibly.

    A swap, not a delete-then-move. `rmtree` followed by a rename leaves nothing at all if the
    rename fails — the archive is refused *and* the session the user already had is gone. The old
    one steps aside first and only dies once the new one is in place; anything going wrong in
    between puts it back.

    **Only ever called for a session the caller has already been shown to own the right to replace**,
    under `session_lock(slug)`, and never for a slug that was free at the guard — that arm claims by
    rename instead, because a session that appeared during the extraction window has an owner who
    never passed `--force` (#111)."""
    backup = target.with_name(f".{target.name}.replaced-{os.getpid()}")
    target.replace(backup)
    try:
        extracted.replace(target)
    except OSError as e:
        backup.replace(target)
        raise ImportMoveFailedError(
            f"could not move the imported session into place: {e}"
            " — the session that was already here has been restored",
            details={"slug": slug}) from e
    shutil.rmtree(backup, ignore_errors=True)


def _validate_extracted(d: Path, slug: str) -> None:
    """Confirm an extracted directory really is a *coherent* session before it is allowed in.

    This used to check that session.json parsed, that its slug agreed, and that a claimed revision had
    a model.json — which is shape, not truth. An archive announcing revision 2 with no `revisions/` at
    all passed, and so did one whose model.json had been swapped for a different model: nothing is
    malformed in either, only the relationships are broken. `check_session_dir` is the same check
    `requivo session verify` runs, so an archive is held to exactly the standard a live session is."""
    problems = check_session_dir(d, expected_slug=slug)
    if problems:
        raise InconsistentArchiveError(
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
        raise UnreadableArchiveError(f"{display_token(str(archive))} is not a readable .zip archive: {e}",
                                     details={"archive": str(archive)}) from e
    with z:
        slug = _inspect_archive(z)
        # A conflict with the store's current state, not a malformed proposal: `session_exists`
        # exists for exactly this fact and answers 409 where `invalid_model` answered 400 (#101).
        #
        # **This answer is remembered, never asked twice** (#111). It used to be re-decided as
        # `replaced = target.exists()` *after* the whole archive had been unzipped, and the two
        # decisions disagreeing is a session created during that window being moved aside and then
        # `rmtree`d — destroyed without `--force`, because at the moment the user would have been
        # asked to force there was nothing to force past. That is invariant 9 ("a precondition is
        # held across the writes it authorises") in the one verb that writes a whole session, and it
        # is why the two arms below are two arms rather than one flag.
        occupied = store.session_exists(slug)
        if occupied and not a.force:
            raise SessionExistsError(
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
            if not occupied:
                # The slug was free when it was checked, so **the rename is the claim** and nothing
                # steps aside — invariant 11's rule, and the only thing that makes the window above
                # safe rather than merely narrow. `os.replace` refuses a non-empty destination
                # directory, so a session that appeared while the archive was unzipping stops this
                # import instead of being destroyed by it. The caller gets the refusal the guard
                # would have given them, which is the answer they were entitled to either way.
                try:
                    extracted.replace(target)
                except OSError as e:
                    if store.session_exists(slug):
                        raise SessionExistsError(
                            f"session '{slug}' was created while this archive was being read — "
                            "nothing was imported and nothing was replaced; pass --force to replace it",
                            details={"slug": slug}) from e
                    raise ImportMoveFailedError(
                        f"could not move the imported session into place: {e}",
                        details={"slug": slug}) from e
            else:
                # `--force` was given against a session that is really there, so the swap runs under
                # that session's own lock: the check above and the four writes below are one unit,
                # and no concurrent writer can be part-way through the directory being moved aside.
                with store.session_lock(slug):
                    _swap_in(extracted, target, slug)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    if a.json:
        # `slug`/`path`, the spelling every sibling session verb uses (#84). It was
        # `imported`/`into`, so a consumer looping over the session verbs and reading `row["slug"]`
        # got a `KeyError` from the one verb that had just put the session there. Both old keys are
        # gone rather than kept as duplicates: removing a key is breaking, so the rename ships
        # in the 1.0 release or never.
        #
        # `path` is the session's own directory, which is what `session init --json` means by the
        # word and what the line below already prints. `into` carried the session *root*; renaming
        # the key over that value would give `path` two meanings across two verbs of one noun, which
        # is this defect back under the harmonised name and harder to see for it.
        # `replaced` keeps the meaning it always had — *did this import replace an existing session*
        # — and is now the guard's own answer rather than a second observation taken after the
        # extraction. Those two used to be able to disagree, and the disagreement was #111.
        _print_json({"slug": slug, "path": str(target), "replaced": occupied})
        return
    print(f"Imported session '{slug}' → {store.canonical_dir(slug)}"
          + (" (replaced an existing session)" if occupied else ""))


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
        # `items` is keyed by artifact type, so printing it bare gave the payload a top level made
        # of data — #87's defect on `session list`, one shape along (#107). Its argument was that
        # an array has no top level, so no field could ever be added to it; a map keyed by data has
        # that property in practice, because the consumer read is `for t, info in payload.items()`
        # and a metadata key added later is both ambiguous with a future artifact type and breaks
        # that loop.
        #
        # Wrap, do not restructure: the rows are untouched, so the migration is one level of
        # indirection. `slug` is the only key the new top level carries — every sibling verb
        # answers it and this one had nowhere to put it — and deliberately the only one, because a
        # top level nobody needs yet is still worth having, and filling it speculatively is not.
        _print_json({"slug": slug, "artifacts": items})
        return
    if not items:
        print(f"No artifacts saved for '{slug}'.")
        return
    print(f"Artifacts for '{slug}':")
    for t, info in items.items():
        # The same two untrusted strings `session show`'s artifact block renders, in the other verb
        # that renders them (#70). `ArtifactService.list` passes `session.json`'s `artifact_status`
        # through, so the key and the filename are whatever the file says; `core/integrity.py`
        # already treats that filename as untrusted input. `slug` above is the resolved directory
        # name, not the body's, and `revision`/`stale` are `int`/`bool` — none of the three needs it.
        # Escape before padding: the widths exist so the block can be scanned, and padding a value
        # that is about to grow quotes aligns it to a length the render does not have.
        print(f"  {display_token(t):<12} {display_token(info['filename']):<26} "
              f"rev {info['revision']}  {'STALE' if info['stale'] else 'fresh'}")


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
    # `--context` is the documented primary spelling of this selector across the CLI (#85) and
    # `--cards` is a permanent alias. Here the dest stays `cards` — the option strings are what the
    # user types, the dest is what `_cmd_context` already reads, and moving it would be a rename
    # dressed up as an alias.
    cx.add_argument("--context", "--cards", metavar="CARDS", dest="cards",
                    help="comma-separated subset to print (default: all). Alias: --cards.")
    cx.add_argument("--session", metavar="SESSION",
                    help="print exactly the cards this session was created with")
    cx.set_defaults(func=_cmd_context)

    # session
    sp = sub.add_parser("session", help="create, list, show, verify, migrate, export/import sessions")
    ss = sp.add_subparsers(dest="subcommand", required=True, metavar="<action>")

    si = ss.add_parser("init", help="create a session from a request (no LLM)")
    si.add_argument("request", help="the request, a path to a file containing it, or '-' for stdin")
    si.add_argument("--slug", help="explicit session slug (default: derived from the request)")
    si.add_argument("--context", "--cards", metavar="CARDS", dest="context",
                    help="comma-separated context cards to record. Alias: --cards.")
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
    # No `required=True`: the omission has to arrive as a structured `UnstatedSourceRevisionError` the
    # `--json` envelope can carry, not as argparse's usage error and exit 2 (see `ArtifactService.save`).
    # The help string is what has to say it, and until #57 it said the opposite — it still advertised
    # the default #6 removed, which is the text a user reads while deciding whether to pass the flag.
    asv.add_argument("--revision", type=int, default=None,
                     help="required: the model revision this content was reasoned from. There is no "
                          "default — the session's current revision is a different fact, and only you "
                          "know what you read")
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
