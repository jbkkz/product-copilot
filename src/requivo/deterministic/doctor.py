"""`requivo doctor`, `schema` and `context`: the verbs that answer for the install, not for a session.

Three verbs, one subject. `doctor` reports whether this install can work at all, `schema` prints the
slot vocabulary and the driver rule a reasoning caller needs, and `context` prints the product
context cards that impact estimation is read against. None of them needs a session to do its own
job, and all three read the same bundled assets, which is why a change to one is usually a change to
its neighbours.

Two things here are deliberately *not* integrity checks, and the distinction is load-bearing.
`core/integrity.py` answers whether a session directory tells the truth about itself, and its
evidence is the directory and only the directory. A context card lives in the installed package or
in `user_context_dir()`, so a lost card is an environment finding rather than a broken session. That
is why `_card_health` and the two remedy hints live here and are imported from this module by
`session verify`, which asks the same environment question from the other side. They are stated once
so that the two surfaces cannot print different advice for the same finding.

Part of the deterministic surface, so no LLM and no API key. `register_doctor(sub)` is composed into
the package's single `register()` by `deterministic/__init__.py`.
"""

from __future__ import annotations

import os
import platform

from requivo.core import persistence as store
from requivo.core.context import available_cards, check_selection
from requivo.core.errors import InvalidModelError
from requivo.core.integrity import IntegrityProblem, check_session
from requivo.core.selectors import display_token
from requivo.deterministic._shared import _NO_DETAIL, _print_json, _resolve_cards
from requivo.paths import ASSETS, CONTEXT, session_root, user_context_dir, workspace_root
from requivo.services.sessions import SessionService
from requivo.streams import describe_streams


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
        # `SessionService.meta`, not `repo.context_cards`: the two differ on the case that matters
        # here. `context_cards` answers None for a session it cannot find, and None means *all
        # cards* — so an unreadable session would be reported as healthy. `meta` raises, the
        # `except` below turns that into `checked: False`, and "could not look" stays distinct from
        # "looked and found nothing" (#80, #86).
        problem = check_selection(SessionService().meta(slug).context_cards)
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
        # Which is also why this one call stays direct rather than going through the repository
        # (#76): `list_slugs` and `list_unexaminable` are deliberately two scans there, and two
        # scans are the very thing this key exists to avoid.
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
          + (f"  (error: {display_token(s['error'])})" if not s["ok"] else ""))
    c = r["context"]
    if c["status"] == "unreadable":
        print(f"  ❌ context cards   unreadable — {display_token(c['error'])}")
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
        print(f"  ❌ sessions        unreadable — {display_token(h['error'])}")
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


def register_doctor(sub) -> None:
    """Attach `doctor`, `schema` and `context` to the main `requivo` subparser."""
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
