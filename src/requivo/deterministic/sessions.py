"""`requivo session`: create, list, show, migrate, export, verify and import sessions.

The session directory is the interface between every surface and it is public at `format_version` 1
(invariant 8), so this is the largest of the deterministic modules and the one whose output shape is
hardest to change. Two rules run through all of it.

**A listing survives its own members** (invariant 15). `session list` renders every row it can and
degrades the ones it cannot, and *could not be read* is a different answer from *not analysed yet*.
`EXIT_DEGRADED` is the exit code that says so. It lives in `_shared` rather than here because it
names a shape of answer rather than a verb: `session verify` reaches the same state from the other
side when it cannot read a session's product context, and minting a code per verb would rebuild the
collapse the code exists to undo.

**A value read off disk is untrusted input** (invariant 14). Every slug, error string and filename
that comes back from the store goes through `display_token` before it reaches a printed line,
because a stored value carrying a newline would otherwise write what reads as a second,
authoritative line of Requivo's own output at column 0.

`session verify` also asks whether a session's context cards still load on this machine. That is an
environment finding rather than an integrity one, so the check and its two remedy hints are imported
from `doctor`, which owns them, instead of being restated here: the two surfaces printing different
advice for the same finding is how they drift.

Part of the deterministic surface, so no LLM and no API key. `register_sessions(sub)` is composed
into the package's single `register()` by `deterministic/__init__.py`.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional

from requivo.core import persistence as store
from requivo.core.errors import (
    ImportDestinationOccupiedError,
    ImportMoveFailedError,
    InconsistentArchiveError,
    InvalidArchiveError,
    InvalidModelError,
    SessionExistsError,
    SessionNotFoundError,
    SessionUnreadableError,
    UnreadableArchiveError,
)
from requivo.core.integrity import check_session, check_session_dir
from requivo.core.selectors import display_token
from requivo.deterministic._shared import _NO_DETAIL, EXIT_DEGRADED, _print_json, _read_source, _resolve_cards
from requivo.deterministic.doctor import _REPAIR_HINT, _RESTORABLE_CARD_CODES, _RESTORE_HINT, _card_health
from requivo.paths import session_root
from requivo.services.repository import SessionRepository
from requivo.services.sessions import SessionService


def _cmd_session_init(a, client) -> None:
    request = _read_source(a.request)
    if not request.strip():
        raise InvalidModelError("session init needs a request (a sentence or a file path)")
    cards = _resolve_cards(a.context)
    meta = SessionService().create_session(
        request, context_cards=cards, slug=a.slug, provider=a.provider)
    # Both lines below reach `canonical_dir` directly, and that is the justified kind (#76): where
    # the session landed on this machine is the answer the caller asked for, in `--json` for a script
    # and in prose for a reader. `SessionRepository` deliberately exposes no path — a Postgres
    # backing has none to expose — so there is no seam to route this through, and a CLI that talks
    # about files is entitled to know about them.
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
    if not svc.exists(slug):
        raise SessionNotFoundError(f"no canonical session {display_token(slug)}", details={"slug": slug})
    meta = svc.meta(slug)
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
    repo = SessionService().repo
    for slug in slugs:
        if repo.exists(slug):
            skipped.append(slug)
            continue
        try:
            # No repository equivalent, and there cannot be one: this converts a directory in the
            # retired `out/` layout into one in `.requivo/sessions/`. It is a statement about two
            # filesystem layouts, which is what the verb *is* — a Postgres backing has neither.
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
    if not svc.exists(slug):
        raise SessionNotFoundError(f"no canonical session {display_token(slug)}", details={"slug": slug})
    # Direct, and legitimately so: this verb archives the session's *directory*. A path is the
    # subject of the command, not an implementation detail leaking through it.
    d = store.canonical_dir(slug)
    dest = Path(a.output) if a.output else Path.cwd() / f"{slug}.requivo.zip"
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.part")
    try:
        with svc.repo.lock(slug):
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
        found = svc.exists(slug)
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
        print(f"🟡 Could not check '{slug}'s product context: {display_token(cards['error'])}")
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
    # breaking every later `session list`. Direct on purpose: this is the *name* rule the file
    # backing enforces, asked before any session exists to ask a repository about.
    return store.validate_slug(slug)


def _swap_in(extracted: Path, target: Path, slug: str) -> None:
    """Replace an existing session directory with a freshly extracted one, reversibly.

    A swap, not a delete-then-move. `rmtree` followed by a rename leaves nothing at all if the
    rename fails — the archive is refused *and* the session the user already had is gone. The old
    one steps aside first and only dies once the new one is in place; anything going wrong in
    between puts it back.

    **Only ever called for a session the caller passed `--force` for**, and never for a slug that was
    free at the guard — that arm claims by rename instead, because a session that appeared during the
    extraction window has an owner who never asked for it to be replaced (#111).

    It does not run under `session_lock`, and cannot: the lock is an open handle on `.lock` inside
    the very directory being renamed, which Windows refuses. See the call site for what that costs
    and what it does not."""
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


def _refuse_a_non_session_destination(target: Path, slug: str, repo: SessionRepository,
                                      cause: Optional[BaseException] = None) -> None:
    """Refuse when something that is **not a session** already occupies the slug's directory.

    `os.replace` answers this differently per platform, so without this guard the same stray `mkdir`
    imported on POSIX and failed on Windows, and the Windows refusal read `import_move_failed` — a
    sentence about a move, naming a cause that is not the cause. Enforced by
    `test_a_stray_directory_at_the_slug_is_refused_by_name_on_every_platform`.

    **It only ever refuses**, so invariant 11 is intact: the rename is still the claim, and nothing
    here authorises an import the rename would have lost. It is called on *both* sides of that rename
    because the two sides catch different windows — before it for a stray already on disk, and from
    the `except OSError` arm for one that landed while the archive was being read
    (`test_a_stray_appearing_in_the_rename_window_is_named_rather_than_called_a_move_failure`).

    The session half of the question goes through `repo.exists` rather than `store.session_exists`:
    *is this slug a session* is not a question about a path, so it has a backing-neutral form and
    `test_the_surfaces_reach_the_store_only_through_the_named_filesystem_concerns` is right to refuse
    the direct call. `target` is a path because the import moves a directory onto it, which is the
    justification the sibling `canonical_dir` call above already carries.

    **Three answers, not two.** Both probes re-raise rather than answering `False` — `Path.exists` on
    EACCES, `repo.exists` as `SessionUnreadableError` — and a probe that could not look has not
    established anything about the destination, so it says nothing and lets the rename decide, which
    is the only decision that was ever authoritative. `is_symlink` rides with `exists` for the reason
    invariant 17 gives: `exists()` follows the link, so a dangling symlink at the slug is a stray this
    would otherwise call an empty space.

    **A destination that really is a session is not this function's answer**, and reading the name
    without the body is how that gets lost. It belongs to `SessionExistsError` — *created while this
    archive was being read; pass `--force`* — which is a different remedy from this one and is raised
    by the caller. Answering here instead would replace a code a consumer already branches on with a
    new one, in the window `test_that_window_refusal_names_the_conflict_rather_than_a_move_failure`
    exists to pin.
    """
    try:
        if not (target.exists() or target.is_symlink()):
            return
        if repo.exists(slug):
            return
    except (OSError, SessionUnreadableError):
        return
    raise ImportDestinationOccupiedError(
        f"cannot import session '{slug}': {display_token(str(target))} already exists and is not a "
        "session — nothing was imported and nothing was removed. Move or delete it and import again; "
        "--force replaces a session and does not apply here.",
        details={"slug": slug, "path": str(target)}) from cause


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
    repo = SessionService().repo

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
        occupied = repo.exists(slug)
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
            # Direct: the import moves a directory into place, so the destination *is* a path.
            target = store.canonical_dir(slug)
            if not occupied:
                # The slug was free when it was checked, so **the rename is the claim** and nothing
                # steps aside — invariant 11's rule, and the only thing that makes the window above
                # safe rather than merely narrow. `os.replace` refuses a non-empty destination
                # directory — POSIX on `ENOTEMPTY`, Windows on any existing directory at all, which
                # is stricter still — so a session that appeared while the archive was unzipping
                # stops this import instead of being destroyed by it. The caller gets the refusal
                # the guard would have given them, which is the answer they were entitled to either
                # way. Read without the platform qualifier, that sentence is how #114 happened: the
                # safety claim holds on both, and the *answer* did not.
                #
                # **What `os.replace` does *not* answer the same way on every platform is a
                # destination that holds no session at all** (#114), which is what
                # `_refuse_a_non_session_destination` is for. It is called on both sides of the
                # rename: neither side alone converges the platforms, because a stray already on
                # disk never reaches the `except` on POSIX and one that lands mid-window never
                # reaches the pre-check.
                _refuse_a_non_session_destination(target, slug, repo)
                try:
                    extracted.replace(target)
                except OSError as e:
                    if repo.exists(slug):
                        raise SessionExistsError(
                            f"session '{slug}' was created while this archive was being read — "
                            "nothing was imported and nothing was replaced; pass --force to replace it",
                            details={"slug": slug}) from e
                    _refuse_a_non_session_destination(target, slug, repo, e)
                    raise ImportMoveFailedError(
                        f"could not move the imported session into place: {e}",
                        details={"slug": slug}) from e
            else:
                # `--force` was given against a session that is really there.
                #
                # **This swap deliberately does not hold `session_lock`, and the reason is
                # structural rather than a preference.** The lock is an open handle on `.lock`
                # *inside* the session directory, and Windows refuses to rename a directory that
                # contains an open handle — `os.replace` returns `WinError 5`, on all four legs,
                # every time. Taking the lock here does not serialise the swap; it makes the swap
                # impossible on half the supported platforms. Holding it somewhere else instead
                # would be a lock no other writer takes, which serialises nothing.
                #
                # So what closes #111 is the arm above and the single decision it rests on, not a
                # lock. What is no longer possible is losing a session the caller was never asked
                # about.
                #
                # **The residue is wider than "the concurrent writer loses its work", which is what
                # this comment claimed for one release** (#113). `save_revision` resolves the session
                # directory once and then writes by *pathname*, while `session_lock` holds an fd on
                # an *inode*. So a writer sitting inside `save_revision` while the swap happens goes
                # on writing into the newly imported directory — the import silently inherits another
                # session's revision files and identity — and a third process then locks
                # `target/.lock`, a different inode from the one that writer holds, and acquires it.
                # Two writers hold the lock for one slug, which is invariant 9's own failure mode.
                # Pre-existing, byte-identical at 1.0.0 and 0.11.0, and filed rather than fixed here
                # because the fix is a change to the swap mechanism and not to this decision.
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
    # Same as `session init`: the line's subject is where the session landed on this machine.
    print(f"Imported session '{slug}' → {store.canonical_dir(slug)}"
          + (" (replaced an existing session)" if occupied else ""))


def register_sessions(sub) -> None:
    """Attach the `session` verb group to the main `requivo` subparser."""
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
