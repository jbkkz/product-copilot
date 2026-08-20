"""End-to-end tests of the deterministic CLI surface — doctor / session / model / artifact.

Every command here must run with no LLM and no API key. Output is captured through `app()` (the real
entry point) against a temp workspace; a `--json` variant is asserted where the spec fixes a machine
format, so Claude Code can rely on it.
"""
from __future__ import annotations

import io
import json
import os
import re
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from requivo.cli import _build_parser, app
from requivo.core import persistence as store
from requivo.core.contracts import _schema_order, schema_slot_ids
from requivo.core.errors import InvalidSlugError
from requivo.deterministic import MAX_ARCHIVE_FILES
from requivo.services.sessions import SessionService


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path


def _slot(c=0, cf="empty", im="low", v=""):
    return {"completeness": c, "confidence": cf, "impact": im, "value": v}


def _full_model(**overrides):
    _, required = schema_slot_ids()
    model = {sid: _slot() for sid in _schema_order() if sid in required}
    model.update(overrides)
    # A complete model owes an objective as much as it owes its slots (see `completeness_gap`),
    # so the shared fixture carries one.
    return {"model": model, "questions": [], "summary": {"objective": "A leave approval system"}}


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        app(argv, client=None)  # client=None → any accidental API use would blow up
    return buf.getvalue()


def _run_json(argv):
    return json.loads(_run(argv))


# ── doctor ──────────────────────────────────────────────────────────────────────


def test_doctor_runs_without_api_key(workspace, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = _run_json(["doctor", "--json"])
    assert r["schema"]["ok"] and r["schema"]["slots"] > 0
    # Missing key / SDK must never be reported as a hard failure.
    assert r["provider_anthropic"]["api_key_present"] is False
    assert "sessions" in r["workspace"]


# ── doctor's own failures must not render as green ticks (#12) ──────────────────
#
# Every test in this block asserts that the *healthy* and the *broken* case produce **different**
# output. A test that only showed the broken case producing something would pass equally well
# against a doctor that reports a problem for everything — and the defect here was never that doctor
# is silent, it is that two of its states are spelled the same way.


def _check_line(text: str, name: str) -> str:
    """The status line for the named doctor check — the one carrying a tick.

    Matched on the two-space indent a check line has, because the indented detail lines beneath it
    mention the same words (`     sessions        <path>` sits right above `  ✅ sessions …`), and a
    tick asserted against the wrong line is an assertion about nothing."""
    return next(ln for ln in text.splitlines()
                if ln.startswith("  ") and not ln.startswith("   ") and name in ln)


def test_doctor_tells_a_loaded_context_dir_from_a_lost_one_and_from_an_unreadable_one(workspace):
    """Three states, three renderings. `available_cards()` failing used to be written into
    `schema["error"]` — a *different* check's field — with `schema["ok"]` left True and the message
    printed nowhere, while the card line printed a tick unconditionally. A wheel that ships `assets/`
    but loses `assets/context/` therefore showed three green ticks and reasoned with no product
    context at all."""
    import requivo.deterministic as det

    def _unreadable():
        raise OSError("boom")

    healthy = _run_json(["doctor", "--json"])
    assert healthy["context"]["ok"] is True, "fixture is blind: the bundled cards did not load"
    assert healthy["context"]["status"] == "ok"
    assert healthy["context"]["count"] > 0 and healthy["context"]["error"] is None
    healthy_text = _run(["doctor"])

    # (a) the directory is gone — `_card_paths` skips what does not exist and returns nothing.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(det, "available_cards", list)
        empty = _run_json(["doctor", "--json"])
        empty_text = _run(["doctor"])
    assert empty["context"]["ok"] is False
    assert empty["context"]["status"] == "empty"
    assert empty["context"]["count"] == 0
    assert empty["schema"]["ok"] is True and empty["schema"]["error"] is None

    # (b) the directory cannot be read at all — a different answer again, and it must not be
    #     laundered through a neighbouring check's field.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(det, "available_cards", _unreadable)
        broken = _run_json(["doctor", "--json"])
        broken_text = _run(["doctor"])
    assert broken["context"]["ok"] is False
    assert broken["context"]["status"] == "unreadable"
    assert "boom" in (broken["context"]["error"] or "")
    assert broken["schema"]["ok"] is True and broken["schema"]["error"] is None, (
        "a context-card failure must not be reported as a schema failure")

    # The human rendering distinguishes them too — the JSON being right is no use to a reader
    # counting ticks.
    assert "✅" in _check_line(healthy_text, "context cards")
    assert "✅" not in _check_line(empty_text, "context cards")
    assert "✅" not in _check_line(broken_text, "context cards")
    assert "boom" in broken_text, "the captured error was never shown to the reader"
    assert healthy_text != empty_text and empty_text != broken_text


def test_doctor_tells_an_empty_workspace_from_an_unreadable_one(workspace):
    """`_session_health` caught every exception and returned `{"total": 0, "inconsistent": {}}` —
    byte-identical to a genuinely empty workspace. Twelve unreachable sessions then read as "you have
    no sessions", and the user concludes they were deleted rather than that a directory is
    unreadable."""
    import requivo.deterministic as det

    def _unreadable():
        raise PermissionError("Permission denied")

    empty = _run_json(["doctor", "--json"])["sessions"]
    assert empty["total"] == 0 and empty["readable"] is True and empty["error"] is None
    assert empty["non_sessions"] == [], "we looked and there was nothing else here"
    empty_text = _run(["doctor"])

    with pytest.MonkeyPatch.context() as mp:
        # `scan_session_root`, because that is the one listing `_session_health` makes since #67.
        # Patching `list_session_slugs` — which it no longer calls — left this simulating nothing
        # while still asserting; the failure is what said so, which is the point of asserting that
        # the two renderings *differ* rather than that the broken one says something.
        mp.setattr(det.store, "scan_session_root", _unreadable)
        unreadable = _run_json(["doctor", "--json"])["sessions"]
        unreadable_text = _run(["doctor"])
    assert unreadable["readable"] is False
    assert unreadable["total"] is None, "0 is a claim about the workspace; we could not look"
    assert unreadable["non_sessions"] is None, (
        "an empty list here reads as `we looked and found nothing else` — the same conflation one "
        "key along, in the arm where the root could not be listed at all (#67)")
    assert "Permission denied" in (unreadable["error"] or "")

    assert "✅" in _check_line(empty_text, "sessions")
    assert "✅" not in _check_line(unreadable_text, "sessions")
    assert "0 in this workspace" in empty_text
    assert "0 in this workspace" not in unreadable_text
    assert "unreadable" in unreadable_text and "Permission denied" in unreadable_text


def _deny_read(directory: Path) -> None:
    """Make `directory` genuinely unreadable, or skip loudly naming what went untested.

    `chmod 000` is not a read denial everywhere: Windows ignores POSIX mode bits entirely, and root
    bypasses them. Branching silently on that would leave a test that *passes* on those runs while
    asserting nothing — a green leg nobody re-reads, reporting a coverage it does not have. So it
    skips instead, and says which platform or condition the assertion did not reach."""
    if os.name == "nt":
        pytest.skip("POSIX mode bits do not deny reads on Windows — the unreadable-card-directory "
                    "path is untested on this platform")
    directory.chmod(0o000)
    try:
        list(directory.iterdir())
    except OSError:
        return                                  # the denial took: the assertion below is real
    directory.chmod(0o755)
    pytest.skip("chmod 000 did not deny reads here (running as root?) — the "
                "unreadable-card-directory path is untested on this run")


def test_a_card_directory_that_cannot_be_read_is_unreadable_not_empty(workspace, tmp_path):
    """The `unreadable` state has to be reachable by the thing that actually makes a directory
    unreadable, and it was not.

    `_card_paths()` enumerated with `Path.glob("*.md")`, and `glob` **swallows `PermissionError` and
    yields nothing**. So a card directory denied by permissions — the ordinary way one becomes
    unreadable — produced an empty card list and no exception: `doctor` said `empty` (or, with a
    second readable root, a confident `ok` at a smaller count), and a session naming a card in that
    directory was told `unknown_context_card`, whose remedy is "put the card back" when the card is
    right there and merely unreadable. That is #12's own defect class one layer under #12's fix.

    Both halves are here, on the same directory, with only its mode changing.
    """
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "walled-domain.md").write_text("# Walled domain\n")

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("REQUIVO_CONTEXT_DIR", str(cards))
        _run(["session", "init", "Something.", "--slug", "s", "--context", "walled-domain", "--json"])

        # ── readable: the must-fire control ───────────────────────────────────
        healthy = _run_json(["doctor", "--json"])
        assert healthy["context"]["status"] == "ok"
        assert "walled-domain" in healthy["context_cards"]
        assert healthy["sessions"]["cards_checked"] is True
        assert healthy["sessions"]["unresolved_cards"] == {}

        _deny_read(cards)
        try:
            broken = _run_json(["doctor", "--json"])
            broken_text = _run(["doctor"])
        finally:
            cards.chmod(0o755)

    assert broken["context"]["status"] == "unreadable", (
        "a permission-denied card directory is not an install with no cards; the remedy differs")
    assert broken["context"]["ok"] is False
    assert "walled-domain" not in broken["context_cards"]

    # The session must not be accused of naming a card that does not exist — it does exist, and we
    # could not read it. `checked` false is the honest answer, and it must not read as clean.
    assert broken["sessions"]["cards_checked"] is False
    assert broken["sessions"]["unresolved_cards"] == {}
    assert "✅" not in _check_line(broken_text, "context cards")
    assert "✅" not in _check_line(broken_text, "sessions"), (
        "the sessions line ticked while nobody had checked their product context")
    assert "not checked" in _check_line(broken_text, "sessions")


# ── something under the session root that is not a session (#67) ────────────────
#
# The state under test cannot be produced by the current code: #22 stopped `session_lock` creating
# the session directory it opened `.lock` inside, which is exactly why these are only ever found on
# disk and never in a fresh run. So the fixture builds one by hand. Going through `session_lock`
# instead would assert against a state this version cannot reach, and would go green on the day the
# report stopped working.


def _lock_ghost(name: str = "leave-approval") -> Path:
    """A session directory as an older Requivo left one: the name taken, holding only `.lock`."""
    d = store.session_root() / name
    d.mkdir(parents=True)
    (d / ".lock").touch()
    return d


def test_doctor_names_what_is_under_the_session_root_and_is_not_a_session(workspace):
    """Nothing could see one of these. `list_session_slugs` filters on `session.json`, and `doctor`
    and `session verify` both reason over the slugs it returns, so a directory holding only `.lock`
    reached no verb at all — `doctor` printed a green `0 in this workspace` straight over the top of
    it.

    Both halves are on the same workspace with only the directory appearing, so the finding cannot
    become a line that everybody sees.
    """
    clean = _run_json(["doctor", "--json"])["sessions"]
    assert clean["non_sessions"] == [], "the control: an untouched workspace must produce no finding"
    assert "other entries" not in _run(["doctor"])

    _lock_ghost()
    found = _run_json(["doctor", "--json"])["sessions"]

    # What was found, never what it was taken to mean: there is no `is_lock_ghost` key anywhere. A
    # half-extracted archive is this shape too, and the directory is the only evidence there is.
    assert [e["name"] for e in found["non_sessions"]] == ["leave-approval"]
    entry = found["non_sessions"][0]
    assert entry["kind"] == "directory"
    assert entry["entries"] == [".lock"] and entry["entry_count"] == 1
    assert entry["error"] is None
    assert entry["slug_shaped"] is True, "the name is one `create_session` can be asked for"

    # It is still not a session, and the session count must not quietly absorb it.
    assert found["total"] == 0 and found["readable"] is True
    assert found["inconsistent"] == {}

    text = _run(["doctor"])
    assert "leave-approval" in text and ".lock" in text
    assert "✅" in _check_line(text, "sessions"), "0 sessions is still the honest count"
    assert "🟡" in _check_line(text, "other entries")


def test_the_silent_slug_substitution_the_report_names_is_the_one_that_happens(workspace):
    """The finding is only worth a line because of what it costs, so the cost is pinned rather than
    described. `create_session`'s rename is the only claim on a slug (invariant 11) and it loses to a
    non-empty directory, after which `SessionService` falls through to its hash-suffixed candidate:
    the user gets a session under a name they did not ask for, with nothing saying why."""
    _lock_ghost()
    assert "will not get it" in _run(["doctor"]), "the report names the finding but not its cost"

    meta = SessionService().create_session("We would like a leave approval system.",
                                           slug="leave-approval")
    assert meta.slug != "leave-approval"
    assert meta.slug.startswith("leave-approval-")


def test_a_file_where_a_session_name_would_go_costs_the_same_and_is_named_as_a_file(workspace):
    """Swept rather than assumed: the rename onto an existing *file* fails too, `d.exists()` is true,
    and the caller gets the identical substitution. Reporting only directories would have left an
    identical symptom with an identical remedy invisible, so each entry says what it is instead of
    the report assuming they are all directories."""
    store.session_root().mkdir(parents=True)
    (store.session_root() / "leave-approval").write_text("half a download\n", encoding="utf-8")

    entry = _run_json(["doctor", "--json"])["sessions"]["non_sessions"][0]
    assert entry["kind"] == "file"
    assert entry["entries"] is None and entry["entry_count"] is None
    assert entry["error"] is None, "nothing failed here; there is simply nothing to look inside"
    assert entry["slug_shaped"] is True

    meta = SessionService().create_session("We would like a leave approval system.",
                                           slug="leave-approval")
    assert meta.slug.startswith("leave-approval-")


def _deny_listing(directory: Path) -> None:
    """Make `directory` traversable but not listable — `--x`, the mode under which `stat` on a child
    succeeds and `iterdir` does not — or skip loudly naming what went untested.

    Deliberately not `chmod 000`, which denies the `session.json` probe in `_scan_session_root` as
    well and so exercises a *different* state: the entry never reaches `_describe_non_session` at
    all, because the partition above it could not decide what the entry is. That is #80, fixed since,
    and it has its own module — `tests/test_unexaminable_entries.py`. What this fixture is for is the
    entry the partition *did* place, whose contents then could not be listed."""
    if os.name == "nt":
        pytest.skip("POSIX mode bits do not deny listing on Windows — the entry-level "
                    "could-not-look arm is untested on this platform")
    directory.chmod(0o111)
    try:
        list(directory.iterdir())
    except OSError:
        return                                  # the denial took: the assertion below is real
    directory.chmod(0o755)
    pytest.skip("chmod --x did not deny listing here (running as root?) — the entry-level "
                "could-not-look arm is untested on this run")


def test_a_symlink_is_reported_as_one_and_its_target_is_not_read(workspace, tmp_path):
    """`Path.is_dir()` follows a symlink. So a link at a slug name pointing anywhere else reported
    `kind: "directory"`, and the `iterdir` beneath it listed the **target's** filenames into a report
    about this workspace — an answer about something that is not a directory here, carrying names
    from somewhere the user did not ask about. Found by review; a symlink is a third shape, and this
    module already treats one as the single case a containment guard has to answer for (invariant
    17)."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret-project.md").touch()
    store.session_root().mkdir(parents=True)
    try:
        (store.session_root() / "leave-approval").symlink_to(elsewhere, target_is_directory=True)
    except OSError:                            # pragma: no cover - Windows without developer mode
        pytest.skip("this platform refuses an unprivileged symlink — the symlink arm is untested "
                    "on this run")

    entry = _run_json(["doctor", "--json"])["sessions"]["non_sessions"][0]
    assert entry["kind"] == "symlink", "is_dir() follows the link; this answer must not"
    assert entry["entries"] is None and entry["entry_count"] is None
    assert entry["error"] is None, "nothing failed — we declined to follow it"
    assert entry["slug_shaped"] is True, "the name is still taken, whatever it points at"
    assert "secret-project.md" not in _run(["doctor"]), (
        "the target's contents were listed into a report about this workspace")


def test_a_name_too_long_to_be_a_slug_is_not_marked_as_taken(workspace):
    """`slug_shaped` asked `_SLUG_RE` alone, and validity is the pattern **and** the length: an
    81-character kebab-case directory matched the pattern and was marked `[name taken]`, under a
    sentence promising a silent hash-suffixed substitution. `canonical_dir` refuses that name outright
    and loudly instead, so the promise was false in the one direction that matters — it told a reader
    to expect silence from a call that raises. Found by review.

    The 80-character sibling beside it is the must-fire control: same shape, one character shorter,
    and it *is* reachable."""
    over = "a" * (store.MAX_SLUG_LENGTH + 1)
    at_limit = "b" * store.MAX_SLUG_LENGTH
    for name in (over, at_limit):
        (store.session_root() / name).mkdir(parents=True)
        (store.session_root() / name / ".lock").touch()

    by_name = {e["name"]: e for e in _run_json(["doctor", "--json"])["sessions"]["non_sessions"]}
    assert by_name[at_limit]["slug_shaped"] is True, "the control: this one really is reachable"
    assert by_name[over]["slug_shaped"] is False

    # And the claim the flag stands for is the one the code makes: a refusal, not a substitution.
    with pytest.raises(InvalidSlugError):
        store.canonical_dir(over)


def test_an_empty_directory_is_still_reported_and_still_marked(workspace):
    """The one shape whose cost is platform-dependent, and the report deliberately does not try to be
    clever about it. POSIX `rename(2)` replaces an empty destination, so `create_session` still wins
    the name here; on Windows `os.rename` is `MoveFileEx` without `MOVEFILE_REPLACE_EXISTING` and
    refuses any existing destination, so it does not. `slug_shaped` therefore does not exempt an empty
    directory — a marker that is right on one platform and silently absent on another is a worse
    answer than one that is occasionally conservative.

    Both arms below assert a real outcome. Neither is the vacuous kind of platform branch that
    reports coverage it does not have."""
    store.session_root().mkdir(parents=True)
    (store.session_root() / "leave-approval").mkdir()

    entry = _run_json(["doctor", "--json"])["sessions"]["non_sessions"][0]
    assert entry["kind"] == "directory"
    assert entry["entries"] == [] and entry["entry_count"] == 0
    assert entry["error"] is None, "we looked, and it is empty — not the same as could not look"
    assert entry["slug_shaped"] is True
    assert "an empty directory" in _run(["doctor"])

    meta = SessionService().create_session("We would like a leave approval system.",
                                           slug="leave-approval")
    if os.name == "nt":                                     # pragma: no cover - platform-dependent
        assert meta.slug.startswith("leave-approval-"), "os.rename refuses any existing destination"
    else:
        assert meta.slug == "leave-approval", "rename(2) replaces an empty destination directory"


def test_an_entry_that_could_not_be_looked_inside_is_not_reported_as_empty(workspace):
    """The third state one level below the one `_session_health` already has: the root listed fine,
    this directory did not. `entries: []` would say we looked and it holds nothing — the one reading
    that makes the finding worthless, since on POSIX a directory holding nothing is the single shape
    that does not cost the caller its slug at all (`rename(2)` replaces an empty destination)."""
    d = _lock_ghost()

    # The must-fire control, on the same directory, with only its mode changing.
    readable = _run_json(["doctor", "--json"])["sessions"]["non_sessions"][0]
    assert readable["entries"] == [".lock"] and readable["error"] is None

    _deny_listing(d)
    try:
        denied = _run_json(["doctor", "--json"])["sessions"]["non_sessions"][0]
        denied_text = _run(["doctor"])
    finally:
        d.chmod(0o755)

    assert denied["kind"] == "directory", "we can stat it; we cannot list it"
    assert denied["entries"] is None and denied["entry_count"] is None
    assert "Permission denied" in (denied["error"] or "")
    assert "empty directory" not in denied_text
    assert "Permission denied" in denied_text


def test_a_name_read_off_disk_cannot_forge_a_line_of_the_report_that_names_it(workspace):
    """#40 in a new render site. The entry's own name and the names it holds are both read off disk,
    untrusted exactly as a stored context-card name is. Printed bare, one carrying a newline does not
    merely look odd: it ends the line and starts another at whatever column it chooses, immediately
    under a row of `doctor`'s own output."""
    d = _lock_ghost()
    try:
        (d / "x\n  ✅ forged          all clear").touch()
    except OSError:                            # pragma: no cover - filesystem-dependent
        pytest.skip("this filesystem refuses a newline in a filename (Windows, notably) — the "
                    "escaping of an entry name is untested on this run")

    text = _run(["doctor"])
    assert "\\n" in text, "the newline reached the terminal unescaped"
    assert "  ✅ forged          all clear" not in text.splitlines()

    # `--json` was never affected and must stay that way: json.dumps escapes a control character
    # before it can reach a line of its own, so the finding keeps its bytes verbatim.
    entry = _run_json(["doctor", "--json"])["sessions"]["non_sessions"][0]
    assert any("\n" in n for n in entry["entries"])

    # The other permutation, on the entry's *own* name rather than a name it holds. The two reach
    # the report through different f-strings, so one covering the other is an assumption.
    (store.session_root() / "y\n  ✅ forged          all clear").mkdir()
    both = _run(["doctor"])
    assert both.count("\\n") >= 2, "the directory's own name reached the terminal unescaped"
    assert "  ✅ forged          all clear" not in both.splitlines()


def test_a_forged_name_that_holds_a_session_json_cannot_forge_a_line_either(workspace):
    """The permutation the tests either side of this one do not reach, and the gap was real.

    The test above forges a **non-session** name — a bare file, and a directory with no
    `session.json` — so it exercises `_print_non_sessions`, which escapes. The card-name tests further
    down forge a *card* under a legitimate slug. Neither drives a control-charactered name that
    **holds a `session.json`**, and that is the one landing in the *sessions* bucket:
    `_scan_session_root` partitions on `(p / "session.json").exists()` alone, and `_session_health`'s
    `except Exception` turns a name `validate_slug` would refuse into an ordinary `unreadable` row.

    Reproduced against `main` before the fix — the name wrote two further lines of `doctor`'s own
    report at column 0, indented exactly like real rows. Found by the pre-1.0 release audit, which
    reasoned the reachability from four code locations and said it had not executed it; running the
    repro is what settled it.
    """
    forged = "evil\n     └─ ok: all clear"
    try:
        d = store.session_root() / forged
        d.mkdir(parents=True)
    except OSError:                            # pragma: no cover - filesystem-dependent
        pytest.skip("this filesystem refuses a newline in a filename (Windows, notably) — the "
                    "escaping of a session name is untested on this run")
    (d / "session.json").write_text('{"not": "valid session metadata"}', encoding="utf-8")

    text = _run(["doctor"])
    lines = text.splitlines()

    # must fire: the entry really did reach the *sessions* bucket rather than the non-session one.
    # Without this the assertions below would pass against a report that never mentioned it at all.
    assert any("inconsistent" in ln for ln in lines), text

    assert "\\n" in text, "the newline reached the terminal unescaped"
    assert "     └─ ok: all clear`" not in lines, "a stored name wrote a line of doctor's report"
    # One row for one entry. The forged text is built to look like a second, so counting the rows is
    # what separates *escaped* from *merely reordered*.
    assert len([ln for ln in lines if ln.startswith("     └─ ")]) == 1, text


def test_session_list_does_not_call_one_of_these_a_session(workspace):
    """The other half of the partition, and why this is not `session list`'s finding to report: a
    listing of sessions must not grow a row for something that is not one. The real session beside it
    is the must-fire control — without it this passes against a listing that lists nothing at all."""
    _run(["session", "init", "A real one.", "--slug", "real", "--json"])
    _lock_ghost()

    rows = _run_json(["session", "list", "--json"])["sessions"]
    assert [r["slug"] for r in rows] == ["real"]
    assert store.list_session_slugs() == ["real"]

    text = _run(["session", "list"])
    assert "real" in text and "leave-approval" not in text


def test_the_parts_of_the_session_root_are_one_partition(workspace):
    """`list_session_slugs`, `list_non_session_entries` and `list_unexaminable_entries` come out of
    one predicate, and are only worth having as a set while nothing can fall between them — a name
    in none of them is precisely the state #67 is about.

    Three parts rather than two since #80: the predicate can *fail*, and an entry it could not
    decide about belongs in neither of the other two. The third is empty in this fixture and
    asserted as empty for that reason — it is populated in `tests/test_unexaminable_entries.py`,
    which needs a platform skip this test does not.

    Staging directories are in none of the three on purpose: they are `create_session` in flight
    rather than something left behind, and reporting one is a race the reader cannot act on."""
    _run(["session", "init", "A real one.", "--slug", "real", "--json"])
    _lock_ghost()
    (store.session_root() / ".real.new-1-abcdef12").mkdir()

    slugs = set(store.list_session_slugs())
    others = {e.name for e in store.list_non_session_entries()}
    blind = {e.name for e in store.list_unexaminable_entries()}
    on_disk = {p.name for p in store.session_root().iterdir()}

    assert slugs == {"real"} and others == {"leave-approval"}
    assert blind == set(), "nothing here is unexaminable; the populated case is its own module"
    assert slugs & others == set() and slugs & blind == set() and others & blind == set()
    assert on_disk - (slugs | others | blind) == {".real.new-1-abcdef12"}


def test_doctor_and_verify_flag_a_session_whose_context_card_is_gone(workspace, tmp_path):
    """A session's `context_cards` are validated once, at creation. The cards live *outside* the
    session directory, so the answer can change afterwards without the session changing — and since
    `load_context` refuses an unresolvable selection (#13), the session is hard-stopped at its next
    (paid) turn while doctor still calls it healthy.

    Both halves are in this one fixture: the same session, checked twice, with only the card moving.
    """
    cards = tmp_path / "cards"
    cards.mkdir()
    card = cards / "lost-domain.md"
    card.write_text("# Lost domain\n\nSome product context.\n")

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("REQUIVO_CONTEXT_DIR", str(cards))
        _run(["session", "init", "Something.", "--slug", "s", "--context", "lost-domain", "--json"])

        # ── healthy: the card is where the session left it ────────────────────
        healthy_doctor = _run_json(["doctor", "--json"])["sessions"]
        assert healthy_doctor["unresolved_cards"] == {}
        assert healthy_doctor["inconsistent"] == {}
        healthy_verify = _run_json(["session", "verify", "s", "--json"])
        assert healthy_verify["ok"] is True
        assert healthy_verify["context_cards"]["checked"] is True
        assert healthy_verify["context_cards"]["problem"] is None
        healthy_text = _run(["session", "verify", "s"])

        # ── broken: the card is gone, and nothing else changed ────────────────
        card.unlink()

        broken_doctor = _run_json(["doctor", "--json"])["sessions"]
        assert "s" in broken_doctor["unresolved_cards"]
        assert broken_doctor["unresolved_cards"]["s"]["code"] == "unknown_context_card"
        assert "lost-domain" in broken_doctor["unresolved_cards"]["s"]["details"]["unknown"]
        # It is not an *integrity* problem: the directory still tells the truth about itself.
        assert broken_doctor["inconsistent"] == {}
        assert "✅" not in _check_line(_run(["doctor"]), "sessions")

        buf = io.StringIO()
        with redirect_stdout(buf), pytest.raises(SystemExit) as e:
            app(["session", "verify", "s", "--json"], client=None)
        assert e.value.code == 1
        report = json.loads(buf.getvalue())
        assert report["ok"] is False
        assert report["problems"] == []            # nothing is wrong *inside* the directory
        assert report["context_cards"]["checked"] is True
        assert report["context_cards"]["problem"]["code"] == "unknown_context_card"

        buf = io.StringIO()
        with redirect_stdout(buf), pytest.raises(SystemExit):
            app(["session", "verify", "s"], client=None)
        broken_text = buf.getvalue()

    assert healthy_text != broken_text
    assert "lost-domain" in broken_text and "lost-domain" not in healthy_text
    assert "REQUIVO_CONTEXT_DIR" in broken_text, "the reader is not told how to recover"


# ── a receipt forged by the thing it reports on ─────────────────────────────────

# A card name is an unconstrained `str` in `session.json`, and `session import` passes it through
# intact. This one is shaped to forge the very row that would otherwise report it: a first line that
# reads as an ordinary card name, then a claim at column 0, then a byte-identical copy of doctor's
# own `sessions` row saying the opposite of the truth. `.strip()` — the only thing that touched a
# card name before #40 — removes surrounding whitespace and not interior newlines, so all three
# lines survived into the receipt.
_FORGED_CARD = (
    "ok-card\n"
    "All clear, nothing to see.\n"
    "  ✅ sessions        0 in this workspace"
)

_SESSIONS_ROW = re.compile(r"^  [✅❌🟡] sessions\b")


def _forge(slug: str, card: str) -> None:
    """Put an arbitrary card name into a session's persisted metadata, the way an imported archive
    or a hand-edited `session.json` can. Deliberately not through `create_session`, which resolves
    the selection against the installed cards and would refuse this."""
    p = store.canonical_dir(slug) / "session.json"
    meta = json.loads(p.read_text(encoding="utf-8"))
    meta["context_cards"] = [card]
    p.write_text(json.dumps(meta), encoding="utf-8")


@pytest.fixture
def forged_workspace(workspace, tmp_path, monkeypatch):
    """Two sessions in one workspace, differing only in what their card selection says.

    `honest` is the **must-fire** half and it is not optional: every assertion below about the
    forgery *not* appearing would also pass against a doctor that printed nothing at all, a card
    directory that could not be read, or a workspace the fixture failed to populate. So the same
    fixture carries a genuine unresolvable card whose line, glyph and column are asserted present.
    """
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "gone-card.md").write_text("# Gone card\n\nSome product context.\n", encoding="utf-8")
    monkeypatch.setenv("REQUIVO_CONTEXT_DIR", str(cards))

    _run(["session", "init", "Something honest.", "--slug", "honest",
          "--context", "gone-card", "--json"])
    _run(["session", "init", "Something else.", "--slug", "forged",
          "--context", "gone-card", "--json"])
    _forge("forged", _FORGED_CARD)
    (cards / "gone-card.md").unlink()      # now `honest` genuinely cannot resolve its card
    return cards


def test_doctor_cannot_be_made_to_print_a_row_a_session_wrote(forged_workspace):
    """#40 — `doctor` answers *is anything wrong*, and a session it reports on could make it say no.

    The forged name reached `doctor` through `check_selection` and was interpolated into the
    unresolved-card line bare. Its newlines then split that one line into three, two of which land
    at a column the renderer owns: one at column 0, and one that is a byte-identical copy of the
    `sessions` row it is contradicting. The count of `sessions` rows is the assertion, because that
    is the thing forged — a reader scanning glyphs sees two verdicts and no way to tell which is the
    program's.
    """
    out = _run(["doctor"])
    lines = out.splitlines()

    # ── must fire: the genuine finding renders, with its glyph, at its column ──
    rows = [ln for ln in lines if _SESSIONS_ROW.match(ln)]
    assert len(rows) == 1, f"expected exactly one sessions row, got {rows}"
    assert rows[0].startswith("  ❌ sessions"), rows[0]
    assert "2 in this workspace" in rows[0], rows[0]
    honest = [ln for ln in lines if ln.startswith("     └─ honest: ")]
    assert len(honest) == 1 and "gone-card" in honest[0], honest

    # ── must not fire: nothing the session wrote became a line of the receipt ──
    assert "All clear, nothing to see." not in lines, "a card name wrote a line at column 0"
    # Everything the session wrote is confined to the one detail line the renderer owns. Asserted as
    # containment rather than absence: the text is still *shown* — escaped — so "it does not appear"
    # would be the wrong property and would pass on a doctor that had silently dropped the finding.
    assert all(ln.startswith("     └─ forged: ") for ln in lines if "0 in this workspace" in ln), \
        "a card name forged doctor's own sessions row"

    # The session is still *reported* — neutralising must not become dropping. The whole name is
    # there, on one line, in the escaped form `integrity.py` already uses for its sibling field.
    forged = [ln for ln in lines if ln.startswith("     └─ forged: ")]
    assert len(forged) == 1, forged
    assert "ok-card" in forged[0] and "All clear" in forged[0], forged[0]

    # Each finding gets the remedy that can fix it. Both sessions are in `unresolved_cards`, and
    # "put the card back" cannot repair a malformed selection — a receipt that names a real problem
    # and then prints advice that cannot work is the quiet half of this same defect.
    assert any("REQUIVO_CONTEXT_DIR" in ln for ln in lines), lines
    assert any("session.json" in ln and "malformed" in ln for ln in lines), lines

    # `--json` is a machine format and must keep the bytes verbatim: the escaping is a property of
    # the terminal rendering, not of the finding.
    report = _run_json(["doctor", "--json"])["sessions"]
    assert set(report["unresolved_cards"]) == {"honest", "forged"}


def test_session_verify_cannot_be_made_to_print_a_line_a_session_wrote(forged_workspace):
    """The same forgery on the anti-tampering verb, which is the sharper half: `session verify` is
    the command whose entire job is to say whether a session directory is telling the truth, and the
    session under inspection could write into its verdict — while `verify` still exited 1, so the
    exit code and the text disagreed."""
    def _verify(slug: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf), pytest.raises(SystemExit) as e:
            app(["session", "verify", slug], client=None)
        assert e.value.code == 1
        return buf.getvalue()

    honest = _verify("honest").splitlines()
    assert any(ln.startswith("  · [unknown_context_card] ") and "gone-card" in ln
               for ln in honest), honest              # must fire
    assert any("REQUIVO_CONTEXT_DIR" in ln for ln in honest), honest

    forged = _verify("forged").splitlines()
    # The remedy follows the finding: nothing is missing here, so restoring a file cannot help.
    assert not any("REQUIVO_CONTEXT_DIR" in ln for ln in forged), forged
    assert any("session.json" in ln and "malformed" in ln for ln in forged), forged
    assert "All clear, nothing to see." not in forged, "a card name wrote a line at column 0"
    assert not any(_SESSIONS_ROW.match(ln) for ln in forged), forged
    named = [ln for ln in forged if ln.startswith("  · [") and "ok-card" in ln]
    assert len(named) == 1, forged                    # reported, on exactly one line


def test_impact_cannot_be_made_to_print_a_line_by_an_unmatched_slot_token(workspace, tmp_path):
    """The gap the #40 guard left open, found in review of the fix.

    `normalize_tokens` checks the **stripped** token, and `str.strip()` removes every control
    character Python classifies as whitespace — tab, newline, vertical tab, form feed, carriage
    return, the four separator codes U+001C to U+001F, and NEL at U+0085. So a token whose control
    character is *leading or trailing* rather than interior is stripped away before the guard looks
    at it, and is therefore not refused.

    Harmless in the two card selectors, which echo `raw.strip()` — and not harmless in
    `resolve_slots`, which echoed the **unstripped** original into its unmatched list, from where
    `requivo impact` prints it bare. The fix is to echo the same normalized token the guard actually
    checked, which is what the card selectors already do.

    Lower severity than #40 proper: a slot token is a live argv value the same user typed, not
    persisted data a third party supplied. But `core/selectors.py` claims in as many words that the
    value never reaches a render site, and a claim like that has to be true or it should not be
    written down.
    """
    _run(["session", "init", "Something.", "--slug", "imp"])
    proposal = tmp_path / "p.json"
    proposal.write_text(json.dumps(_full_model()), encoding="utf-8")
    _run(["model", "apply", "imp", str(proposal), "--json"])

    # must fire: a real token still resolves, and an ordinary unknown one is still named as typed
    assert "Unknown slot" not in _run(["impact", "imp", "workflow"])
    unknown = _run(["impact", "imp", "zzz"]).splitlines()
    assert any(ln.startswith("Unknown slot(s): zzz") for ln in unknown), unknown

    # must not fire: a leading control character cannot become a line of the output
    forged = _run(["impact", "imp", "\nFORGED AT COLUMN 0"]).splitlines()
    assert "FORGED AT COLUMN 0" not in forged, forged
    named = [ln for ln in forged if ln.startswith("Unknown slot(s): ")]
    assert len(named) == 1 and "FORGED AT COLUMN 0" in named[0], forged


def test_session_show_renders_a_card_name_as_one_line(forged_workspace):
    """The third render site, which #40 does not name and which no selector guard can reach:
    `session show` reads `context_cards` straight out of the metadata and joins it, without asking
    the selector anything. A boundary that refuses a hostile selection still leaves this open,
    because nothing here is selecting."""
    honest = _run(["session", "show", "honest"]).splitlines()
    assert "  context  gone-card" in honest, honest    # must fire, and unquoted

    forged = _run(["session", "show", "forged"]).splitlines()
    assert "All clear, nothing to see." not in forged, "a card name wrote a line at column 0"
    context = [ln for ln in forged if ln.startswith("  context  ")]
    assert len(context) == 1 and "ok-card" in context[0], forged


def _forge_meta(slug: str, fields: dict) -> None:
    """Write arbitrary values into a session's persisted metadata, the way an imported archive or a
    hand-edited `session.json` can. Deliberately not through the services, which would never produce
    these values — that is the point. `read_meta` validates the slug it is *called with*, the
    directory name; every `str` in the body arrives unexamined.

    `fields` is a dict rather than `**kwargs` because one of the keys being forged is `slug` itself,
    which is the whole shape of this defect and would collide with the parameter."""
    p = store.canonical_dir(slug) / "session.json"
    meta = json.loads(p.read_text(encoding="utf-8"))
    meta.update(fields)
    p.write_text(json.dumps(meta), encoding="utf-8")


# One forgery per untrusted `str` on `session show`'s text path (#70). Each value is a plausible one
# followed by a newline and a line shaped exactly like a line `session show` itself prints, so the
# assertion below — that the render is still eight lines — is a statement about forged *rows*, not
# about stray text turning up somewhere.
_SHOW_FORGERIES = {
    "slug": "s\nSession 'trusted'  (id 000000000000…)",
    # Sliced to 12 before it is shown, so the newline has to fall inside the first 12 characters or
    # the forgery is neutralised by the slice rather than by the escaping and proves nothing.
    "session_id": "ab\nFORGED SESSION ID",
    "created_at": "2026-01-01T00:00:00Z\n  revision 999",
    "updated_at": "2026-01-01T00:00:00Z\n  provider trusted   model trusted",
    "provider": "anthropic\n  revision 999",
    "model_name": "claude\n  context  all cards",
    "artifact_status": {
        # The dict *key* is a `str` off disk too, and is printed as the artifact type.
        "prd\n    brief        trusted.md                 rev 9  fresh": {
            "revision": 1,
            "filename": "prd.md\n    stories      trusted.md                 rev 9  fresh",
            "updated_at": "2026-01-01T00:00:00Z",
            "stale": False,
        },
    },
}


def test_session_show_cannot_be_made_to_print_a_line_a_session_wrote(workspace):
    """#70 — the same defect as #62, in a different verb, and in more fields than the issue counted.

    `_session_list_line`'s docstring carries the whole argument and it is not restated here. What is
    different is only the surface: `session show` prints **eight** untrusted strings out of
    `session.json`'s body where `session list` printed three, and two of them are not fields of
    `SessionMeta` at all — an `artifact_status` *key*, and `ArtifactStatus.filename`. The issue says
    five; that is the set #62 happened to name in passing.

    Every line here is one Requivo writes itself, so a forged one is indistinguishable from a real
    one to a reader. That is why the assertion is the *shape* of the render — how many lines, and
    which fact each carries — rather than the absence of a substring.
    """
    _run(["session", "init", "Something.", "--slug", "victim"])
    _forge_meta("victim", _SHOW_FORGERIES)

    out = _run(["session", "show", "victim"])          # must not raise: exit 0, still readable
    lines = out.splitlines()

    # ── must not fire: nothing the session wrote became a line of the render ──
    #
    # Six labelled lines, an `artifacts:` header and exactly one artifact row. Counting is the
    # decisive form: any escape produces a ninth line, wherever it lands and whatever it says.
    assert len(lines) == 8, out
    assert len([ln for ln in lines if ln.startswith("Session '")]) == 1, out
    for label in ("  created  ", "  updated  ", "  revision ", "  provider ", "  context  "):
        assert len([ln for ln in lines if ln.startswith(label)]) == 1, (label, out)
    assert lines[6] == "  artifacts:", out
    assert len([ln for ln in lines if ln.startswith("    ")]) == 1, out
    # The facts stay the session's own. `revision 999` was forged three separate ways above.
    assert lines[3] == "  revision 0", out

    # ── must fire: every forged value is still shown, escaped, on the line that owns it ──
    #
    # Neutralising must not become dropping: a reader has to be able to see exactly what is stored,
    # which is the same treatment `core/integrity.py` gives the recorded artifact filename. Asserted
    # per field, against the line each belongs to, so a fix that dropped one — or moved it onto a
    # neighbour's line — is a failure and not a smaller pass. `session_id` is the exception and is
    # taken separately below, because it is the one value the render truncates.
    st = _SHOW_FORGERIES["artifact_status"]
    ((artifact_type, artifact), ) = st.items()
    for i, value in ((0, _SHOW_FORGERIES["slug"]),
                     (1, _SHOW_FORGERIES["created_at"]),
                     (2, _SHOW_FORGERIES["updated_at"]),
                     (4, _SHOW_FORGERIES["provider"]),
                     (4, _SHOW_FORGERIES["model_name"]),
                     (7, artifact_type),
                     (7, artifact["filename"])):
        assert repr(value) in lines[i], (i, value, lines[i])

    # **Slice first, then escape.** `session_id` is shown truncated; escaping first and slicing after
    # would cut the repr mid-sequence and emit an unterminated quote. The whole repr of the *sliced*
    # value is what must appear — 21 characters, where a truncated escape would be 12.
    assert repr(_SHOW_FORGERIES["session_id"][:12]) in lines[0], lines[0]


def test_session_show_leaves_an_ordinary_session_byte_for_byte(workspace, tmp_path):
    """The other half of #70, and the half that says the fix cost nothing: a value that is already
    one safe line comes back unquoted and unchanged, so no real session's output moves. Without
    this, `display_token` could have been a plain `repr()` on every field, the forgery test above
    would still be green, and every user's terminal would have gained quotes around six values."""
    _run(["session", "init", "Reconcile event check-ins.", "--slug", "plain"])
    proposal = tmp_path / "p.json"
    proposal.write_text(json.dumps(_full_model()), encoding="utf-8")
    _run(["model", "apply", "plain", str(proposal)])
    prd = tmp_path / "prd.md"
    prd.write_text("# PRD\n", encoding="utf-8")
    _run(["artifact", "save", "plain", "--type", "prd", "--file", str(prd), "--revision", "1"])

    m = store.read_meta("plain")
    st = m.artifact_status["prd"]
    assert _run(["session", "show", "plain"]).splitlines() == [
        f"Session '{m.slug}'  (id {m.session_id[:12]}…)",
        f"  created  {m.created_at}",
        f"  updated  {m.updated_at}",
        f"  revision {m.current_revision}",
        f"  provider {m.provider or '—'}   model {m.model_name or '—'}",
        "  context  all cards",
        "  artifacts:",
        f"    {'prd':<12} {st.filename:<26} rev {st.revision}  fresh",
    ]


def test_session_show_json_escapes_a_control_character_before_it_reaches_a_line(workspace):
    """`--json` needs no `display_token`. This is the confirmation, and it **corrects the reason**
    #62 and #70 both give for it.

    The stated reason is that `json.dumps` defaults to `ensure_ascii=True`, so the encoder escapes a
    control character before it can reach a line of its own. Written as one sentence that is not
    true, and it is not true about the exact character both issues reproduced with. Measured:

    | character | `ensure_ascii=True` | `ensure_ascii=False` |
    |---|---|---|
    | LF `U+000A` | escaped | **escaped** |
    | DEL `U+007F` | escaped | raw |
    | NEL `U+0085` | escaped | **raw, and `splitlines()` breaks on it** |
    | CSI `U+009B` | escaped | raw |

    A newline is escaped by **JSON's own grammar** — the format forbids a literal control character
    below `U+0020` inside a string — and `ensure_ascii` has no say in it. What `ensure_ascii` decides
    is the *non-ASCII* half of `core/selectors.py`'s `_CONTROL_CHARS`, `\\x7f-\\x9f`: NEL, which is a
    line terminator `str.splitlines()` and some terminals honour, and CSI, which that module already
    calls "an escape introducer in its own right on terminals that decode it".

    So the default **is** load-bearing, for a different set of characters than anyone wrote down. A
    test probing with a newline is green either way and pins nothing; this one probes with both and
    says which mechanism covers which, so turning the default off fails here rather than in somebody's
    terminal. The bytes survive intact in the *parsed* payload: escaping is a property of rendering,
    not of the data (#40, #62, #70).
    """
    _run(["session", "init", "Something.", "--slug", "j"])
    _forge_meta("j", dict(_SHOW_FORGERIES, model_name="claude\x85FORGED BY A NEL"))

    raw = _run(["session", "show", "j", "--json"])

    # The newline half — safe by the grammar, and asserted so the guarantee is pinned even though
    # this half would survive `ensure_ascii=False`.
    assert "\nSession 'trusted'" not in raw, raw
    assert "\\nSession 'trusted'" in raw, raw

    # The half `ensure_ascii` actually decides. `\x85` is a line terminator: under
    # `ensure_ascii=False` it reaches the payload raw, `splitlines()` breaks on it, and a reader
    # piping `--json` through anything line-oriented sees a fabricated line.
    assert "\x85" not in raw, raw
    assert "\\u0085FORGED BY A NEL" in raw, raw
    assert len(raw.splitlines()) == raw.count("\n"), "a value split a line of the payload"

    # Neither escape is a change to the data.
    parsed = json.loads(raw)
    assert parsed["slug"] == _SHOW_FORGERIES["slug"]
    assert parsed["model_name"] == "claude\x85FORGED BY A NEL"


def test_the_two_output_paths_guard_different_ranges_and_json_is_the_stricter(workspace):
    """Where the terminal guard stops, stated as a test so the claim cannot drift (#70).

    Found by the audit on this branch. `core/selectors.py`'s `_CONTROL_CHARS` is C0, DEL and C1 —
    *the class that can move a terminal's cursor or end its line*, which is what that module says it
    is for. `str.splitlines()` breaks on a wider set: it also breaks on U+2028 and U+2029, and those
    two come back from `display_token` byte-for-byte.

    On a terminal that is the right answer — xterm and the VT sequences behind it answer to CR and
    LF, not to Unicode `Zl`/`Zp` — so nothing here is a forgery on the surface `display_token`
    guards. It matters for two things and both are worth pinning. Anything that reads this
    human-readable output line by line sees a line the render did not write, which is why `--json`
    exists and is asserted to cover it. And **this test suite is such a reader**: every assertion
    about `session show` above counts `splitlines()`, so the boundary between what the guard catches
    and what the harness would notice has to be stated somewhere rather than assumed to coincide.

    Widening `_CONTROL_CHARS` is deliberately *not* done here. It would change what
    `normalize_tokens` refuses — the public `unsafe_selector_token` code — and that module scopes
    itself on purpose, so it is a decision for its owner and is reported rather than taken.
    """
    from requivo.core.selectors import display_token

    # Written as an escape, never as the character. A raw U+2028 in a source file is invisible in
    # every diff and every editor that will ever show this line — which is the property that makes it
    # worth a test, and the property that makes pasting one a bad idea.
    sep = "\u2028"
    assert len(f"a{sep}b".splitlines()) == 2      # must fire: it really does split
    assert display_token(f"a{sep}b") == f"a{sep}b", \
        "the terminal guard is documented as not covering U+2028; if it now does, fix the prose too"

    # …and the machine path is the stricter of the two, which is the half a consumer relies on.
    _run(["session", "init", "Something.", "--slug", "lsep"])
    _forge_meta("lsep", {"provider": f"anthropic{sep}FORGED BY A LINE SEPARATOR"})
    raw = _run(["session", "show", "lsep", "--json"])
    assert sep not in raw, raw
    assert "\\u2028FORGED BY A LINE SEPARATOR" in raw, raw
    assert len(raw.splitlines()) == raw.count("\n"), "a value split a line of the payload"


def test_artifact_list_cannot_be_made_to_print_a_row_a_session_wrote(workspace):
    """The sibling verb, found by sweeping the class rather than the instance (#70).

    `artifact list` renders the *same two untrusted strings* `session show`'s artifact block does —
    an `artifact_status` key and `ArtifactStatus.filename`, both read straight out of `session.json`
    by `ArtifactService.list` — at the same fixed column, and had the identical defect. Fixing one
    verb's copy of a two-field render and leaving the other's is the shape that makes a guard
    unreliable: the rule stops being *a persisted value is escaped where it is shown* and becomes *it
    is escaped in the places somebody happened to look*.

    Not a separate issue on purpose. It is one line, in the same file, over the same two fields as
    the change it rides in on, with the same fixture — but it *is* outside #70's own footprint and is
    called out as such rather than left to read as scope creep.
    """
    _run(["session", "init", "Something.", "--slug", "al"])
    _forge_meta("al", {"artifact_status": _SHOW_FORGERIES["artifact_status"]})

    lines = _run(["artifact", "list", "al"]).splitlines()

    # must not fire: two rows where one artifact is recorded
    assert len(lines) == 2, lines
    assert lines[0] == "Artifacts for 'al':", lines
    assert len([ln for ln in lines if ln.startswith("  ")]) == 1, lines

    # must fire: the one real row is still rendered, and still names what is stored
    ((artifact_type, artifact), ) = _SHOW_FORGERIES["artifact_status"].items()
    assert repr(artifact_type) in lines[1] and repr(artifact["filename"]) in lines[1], lines[1]
    assert lines[1].endswith("rev 1  fresh"), lines[1]

    # and an ordinary artifact row is byte-for-byte what it was
    _run(["session", "init", "Other.", "--slug", "al2"])
    _forge_meta("al2", {"artifact_status": {"prd": {"revision": 1, "filename": "prd.md",
                                                    "updated_at": "2026-01-01T00:00:00Z",
                                                    "stale": False}}})
    assert _run(["artifact", "list", "al2"]).splitlines() == [
        "Artifacts for 'al2':",
        f"  {'prd':<12} {'prd.md':<26} rev 1  fresh",
    ]


def test_artifact_list_json_has_a_top_level_that_is_not_data(workspace):
    """`artifact list --json` printed `ArtifactService.list` straight out, so its top level was a map
    keyed by artifact *type* — every key in the payload a value the session happened to hold (#107).

    This is #87's argument one shape along. That issue moved `session list --json` off a bare array
    because "an array has no top level, so no field could ever be added to it"; a top-level map keyed
    by data has the same property in practice: the consumer read is `for t, info in payload.items()`,
    so any metadata key added later is both ambiguous with a future artifact type and breaks that
    loop. Holding the argument for an array and not for a map is not defensible.

    The envelope is `{"slug": ..., "artifacts": {...}}` and nothing else — a top level nobody needs
    yet is worth having, filling it speculatively is not.
    """
    _run(["session", "init", "Something.", "--slug", "aj"])
    _forge_meta("aj", {"artifact_status": {"prd": {"revision": 1, "filename": "prd.md",
                                                   "updated_at": "2026-01-01T00:00:00Z",
                                                   "stale": False}}})

    payload = _run_json(["artifact", "list", "aj", "--json"])

    # must not fire: an artifact type is not a top-level key
    assert "prd" not in payload, payload
    # must fire, in the same fixture: it is there, one level down. Without this the assertion above
    # passes just as happily on an empty payload, a crash caught upstream, or a session that lost
    # its artifacts — none of which is the thing being asserted.
    assert "prd" in payload["artifacts"], payload

    assert set(payload) == {"slug", "artifacts"}, payload
    assert payload["slug"] == "aj"
    # ...and it names what was asked for, never the value stored inside, which no reader may trust
    # (invariant 14). This top level is the first place the verb states a slug at all, so it is the
    # moment to take it from the right side. `session verify` and `session import` agree.
    _forge_meta("aj", {"slug": "forged"})
    assert _run_json(["artifact", "list", "aj", "--json"])["slug"] == "aj"

    # Wrap, not restructure: the row is what `ArtifactService.list` already returned, same keys in
    # the same order. #87 left its rows untouched too, and that is what keeps the migration to one
    # level of indirection — `jq '.artifacts'` where you had `jq '.'`.
    assert payload["artifacts"] == {"prd": {"revision": 1, "filename": "prd.md",
                                            "updated_at": "2026-01-01T00:00:00Z", "stale": False}}
    assert list(payload["artifacts"]["prd"]) == ["revision", "filename", "updated_at", "stale"]


def test_artifact_list_json_still_names_the_session_when_it_has_no_artifacts(workspace):
    """The empty case is the one the old shape answered worst: it printed `{}`, which states nothing
    at all — not which session was asked about, and not that the question was even answered, so a
    consumer could not tell it from a payload that failed to serialise. It is now a session that
    reports zero artifacts, which is a fact (#107)."""
    _run(["session", "init", "Nothing saved yet.", "--slug", "noart"])

    payload = _run_json(["artifact", "list", "noart", "--json"])

    assert payload == {"slug": "noart", "artifacts": {}}


# ── the acceptance scenario ─────────────────────────────────────────────────────


def test_session_init_creates_a_session(workspace):
    r = _run_json(["session", "init", "Build a leave approval system.", "--slug", "leave", "--json"])
    assert r["slug"] == "leave"
    assert store.session_exists("leave")
    assert store.read_meta("leave").current_revision == 0


def test_model_validate_ok_and_invalid_exit(workspace, tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_full_model()))
    assert _run_json(["model", "validate", str(good), "--json"])["status"] == "valid"

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"model": {"nope": _slot()}, "summary": {}}))
    with pytest.raises(SystemExit) as e:
        _run(["model", "validate", str(bad), "--json"])
    assert e.value.code == 1


def test_apply_refuses_a_partial_model_instead_of_replacing_the_whole_one(workspace, tmp_path):
    """`--allow-partial` on `apply` read as "apply a patch"; it merged nothing. It only relaxed the
    completeness check, and the incomplete model then *replaced* the complete one — a fifteen-slot
    model became a one-slot model, reported as fourteen changed slots. `apply` replaces, so it takes
    the full slot set and nothing else; validating a projection is `model validate --allow-partial`."""
    _run(["session", "init", "Something.", "--slug", "s"])
    full = tmp_path / "full.json"
    full.write_text(json.dumps(_full_model()))
    _run(["model", "apply", "s", str(full)])
    before = len(SessionService().load_model("s").model)

    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"model": {"workflow": _slot(80, "explicit", "high", "scan")},
                                   "summary": {"objective": "Something"}}))
    with pytest.raises(SystemExit) as e:
        _run(["model", "apply", "s", str(partial), "--json"])
    assert e.value.code == 1
    assert len(SessionService().load_model("s").model) == before   # the model is untouched
    # The projection is still checkable on its own — that is what the flag means now, and where it lives.
    assert _run_json(["model", "validate", str(partial), "--allow-partial", "--json"])["slots"] == 1


def test_model_apply_and_status_and_artifact_flow(workspace, tmp_path):
    _run(["session", "init", "Reconcile event check-ins.", "--slug", "event"])
    proposal = tmp_path / "p.json"
    proposal.write_text(json.dumps(_full_model(**{"workflow": _slot(70, "inferred", "high", "scan")})))

    applied = _run_json(["model", "apply", "event", str(proposal), "--json"])
    assert applied["status"] == "applied" and applied["revision"] == 1
    assert "workflow" in applied["readiness"]["blocking_slots"]  # inferred high-impact blocks

    status = _run_json(["status", "event", "--json"])
    assert status["revision"] == 1 and status["readiness"]["ready"] is False

    brief = tmp_path / "brief.md"
    brief.write_text("# Assessment\n")
    _run(["artifact", "save", "event", "--type", "brief", "--file", str(brief), "--revision", "1"])
    listed = _run_json(["artifact", "list", "event", "--json"])["artifacts"]
    assert listed["brief"]["revision"] == 1 and listed["brief"]["stale"] is False


def test_session_show_reads_freshness_from_the_dependency_graph_not_the_revision(workspace, tmp_path):
    # `session show` used to call an artifact stale whenever the session had moved past its source
    # revision — which contradicted `artifact list` and the status JSON in the same binary, and made
    # every artifact look out of date after any unrelated change. The stale flag is the whole rule.
    _run(["session", "init", "X.", "--slug", "s"])
    proposal = tmp_path / "m.json"
    proposal.write_text(json.dumps(_full_model()))
    _run(["model", "apply", "s", str(proposal)])
    prd = tmp_path / "prd.md"
    prd.write_text("# PRD\n")
    _run(["artifact", "save", "s", "--type", "prd", "--file", str(prd), "--revision", "1"])

    # Move the session on via a slot the PRD does not consume: revision 2, PRD inputs untouched.
    proposal.write_text(json.dumps(_full_model(
        **{"current_process": _slot(80, "explicit", "high", "as-is described")})))
    _run(["model", "apply", "s", str(proposal)])

    out = _run(["session", "show", "s"])
    assert "revision 2" in out and "rev 1" in out   # provenance still says where it came from…
    assert "STALE" not in out                       # …but it is not stale, and both views agree
    assert _run_json(["artifact", "list", "s", "--json"])["artifacts"]["prd"]["stale"] is False


def test_model_validate_has_no_flag_it_does_not_honour():
    # `--session` was declared and read by nothing. A flag that parses and changes nothing is worse
    # than a missing one: the caller believes a check ran. `model diff` is the real answer.
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["model", "validate", "p.json", "--session", "s"])
    assert _build_parser().parse_args(["model", "diff", "s", "p.json"]).func.__name__ == "_cmd_model_diff"


def test_apply_invalid_proposal_emits_error_envelope(workspace, tmp_path):
    _run(["session", "init", "X.", "--slug", "s"])
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"model": {"ghost": _slot()}, "summary": {}}))
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit):
        app(["model", "apply", "s", str(bad), "--json"], client=None)
    env = json.loads(buf.getvalue())
    assert env["code"] == "unknown_slot" and env["details"]["slots"] == ["ghost"]


def test_model_diff_does_not_write(workspace, tmp_path):
    _run(["session", "init", "X.", "--slug", "s"])
    p = tmp_path / "p.json"
    p.write_text(json.dumps(_full_model()))
    _run(["model", "apply", "s", str(p)])
    before = store.read_meta("s").current_revision
    r = _run_json(["model", "diff", "s", str(p), "--json"])
    assert r["status"] == "planned"
    assert store.read_meta("s").current_revision == before


def test_session_list_and_show(workspace, tmp_path):
    _run(["session", "init", "First.", "--slug", "one"])
    p = tmp_path / "p.json"
    p.write_text(json.dumps(_full_model()))
    _run(["model", "apply", "one", str(p)])
    listing = _run_json(["session", "list", "--json"])
    assert any(s["slug"] == "one" and s["revision"] == 1 for s in listing["sessions"])
    assert listing["degraded"] == 0
    shown = _run_json(["session", "show", "one", "--json"])
    assert shown["slug"] == "one" and shown["format_version"] == 1


def test_session_migrate_moves_legacy_sessions(workspace, tmp_path):
    # Seed a legacy out/<slug>/ session, then bulk-migrate it into the canonical store.
    legacy = store.legacy_dir("legacy-one")
    legacy.mkdir(parents=True)
    legacy.joinpath("model.json").write_text(json.dumps(_full_model()))
    legacy.joinpath("request.txt").write_text("Legacy request.")

    r = _run_json(["session", "migrate", "--json"])
    assert "legacy-one" in r["migrated"]
    assert store.session_exists("legacy-one")
    assert store.read_meta("legacy-one").current_revision == 1
    assert legacy.joinpath("model.json").exists()  # originals preserved


def test_new_verbs_are_bound_in_the_parser():
    cases = [
        (["doctor"], "_cmd_doctor"),
        (["session", "init", "r"], "_cmd_session_init"),
        (["session", "migrate"], "_cmd_session_migrate"),
        (["model", "apply", "s", "p.json"], "_cmd_model_apply"),
        (["model", "validate", "p.json"], "_cmd_model_validate"),
        (["artifact", "save", "s", "--type", "prd", "--file", "f"], "_cmd_artifact_save"),
    ]
    for argv, fname in cases:
        assert _build_parser().parse_args(argv).func.__name__ == fname


# ── the revision contract on the CLI surface ────────────────────────────────────
# These are the primitives the Claude Code skills drive, so their JSON shape is part of the contract:
# a skill reads `revision`, reasons, then hands it back on apply and on save.


def test_session_init_json_reports_the_revision(workspace, tmp_path):
    r = _run_json(["session", "init", "Build a leave approval system.", "--json"])
    assert r["revision"] == 0  # a fresh session has no model yet

    (tmp_path / "p.json").write_text(json.dumps(_full_model()))
    _run(["model", "apply", r["slug"], str(tmp_path / "p.json"), "--json"])
    # `init` is idempotent: re-running it on the same request returns the session as it now stands,
    # so a caller about to apply learns it is no longer at revision 0.
    again = _run_json(["session", "init", "Build a leave approval system.", "--json"])
    assert again["slug"] == r["slug"]
    assert again["revision"] == 1


def test_model_apply_honours_the_expected_revision_precondition(workspace, tmp_path):
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    p = tmp_path / "p.json"
    p.write_text(json.dumps(_full_model()))
    _run(["model", "apply", "s", str(p), "--expected-revision", "0", "--json"])  # fresh: asserts 0

    p2 = tmp_path / "p2.json"
    p2.write_text(json.dumps(_full_model(**{"workflow": _slot(80, "explicit", "high", "new")})))
    _run(["model", "apply", "s", str(p2), "--expected-revision", "1", "--json"])

    # Applying again from the same base is refused with a structured, actionable error.
    with pytest.raises(SystemExit) as exc:
        _run(["model", "apply", "s", str(p2), "--expected-revision", "1", "--json"])
    assert exc.value.code != 0


def test_artifact_save_reports_staleness_at_save_time(workspace, tmp_path):
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    p = tmp_path / "p.json"
    p.write_text(json.dumps(_full_model()))
    _run(["model", "apply", "s", str(p), "--json"])                       # revision 1
    p2 = tmp_path / "p2.json"
    p2.write_text(json.dumps(_full_model(**{"workflow": _slot(80, "explicit", "high", "new")})))
    _run(["model", "apply", "s", str(p2), "--json"])                      # revision 2

    doc = tmp_path / "prd.md"
    doc.write_text("# PRD\n")
    # Reasoned from revision 1, saved once the session is at 2: the answer is knowable, so it is given
    # here rather than only on a later `artifact list`.
    r = _run_json(["artifact", "save", "s", "--type", "prd", "--file", str(doc),
                   "--revision", "1", "--json"])
    assert r["revision"] == 1 and r["stale"] is True
    assert _run_json(["artifact", "list", "s", "--json"])["artifacts"]["prd"]["stale"] is True

    # This used to omit `--revision` and assert `revision: 2, stale: false` — the defect of #6 pinned
    # as a contract. The service filled the gap with the session's current revision and then answered
    # the freshness question against it, which cannot come out anything but False. The revision it
    # recorded was real, so no reader downstream could tell the claim from a stated one. Saying `2`
    # here asserts the same fresh answer about a revision the caller actually claims to have read.
    fresh = _run_json(["artifact", "save", "s", "--type", "prd", "--file", str(doc),
                       "--revision", "2", "--json"])
    assert fresh["revision"] == 2 and fresh["stale"] is False

    # …and leaving it off is now refused rather than guessed, on the exact surface the Claude Code
    # plugin drives. What the caller gets is the structured envelope, not a traceback — the refusal is
    # raised from inside the session lock, so this also pins that it reaches `cli.py`'s handler.
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        app(["artifact", "save", "s", "--type", "prd", "--file", str(doc), "--json"])
    assert exc.value.code == 1
    envelope = json.loads(buf.getvalue())
    assert envelope["details"]["source_revision"] is None
    assert "--revision" in envelope["message"]
    # The code names the omission rather than the session since #57. `invalid_session` was inherited
    # while `web/app.py` was held by another lane, and a caller across this boundary sees the code,
    # never the type — so the one handle it had could not tell "you left a flag off" from "this
    # session is broken".
    assert envelope["code"] == "unstated_source_revision"
    # and nothing was recorded against the guess: the PRD on disk is still the one saved above.
    assert _run_json(["artifact", "list", "s", "--json"])["artifacts"]["prd"]["revision"] == 2


def test_the_revision_flag_does_not_advertise_a_default_it_no_longer_has():
    """The help text is read *while deciding whether to pass the flag*, and it went on describing the
    behaviour #6 was filed to remove: `(default: the session's current revision)`. There is no default
    — an omitted `--revision` is refused — so the text was telling a user to rely on exactly the
    fabricated provenance the refusal exists to stop. Two reviewers found it independently on the #6
    branch, which is how it reached #57 instead of being fixed there.

    Both halves are asserted. That the flag says it is required is the weaker claim; that no option on
    this subcommand *advertises* a default is the one that catches the next instance, because
    `argparse` renders every option's `help` into that one string. The two forms this repository
    writes a default in are checked — `(default: …)` and "defaults to" — rather than the bare word,
    which the corrected text itself uses to deny having one.
    """
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as ei:
        _build_parser().parse_args(["artifact", "save", "--help"])
    assert ei.value.code == 0
    help_text = buf.getvalue()

    assert "--revision" in help_text, "must fire: this is not the help text that owns the flag"
    # Sliced between the two option names rather than read off a wrapped line: argparse wraps to the
    # terminal width, so a line-based assertion passes or fails on how wide the console happens to be.
    # `rsplit` because the usage line names `--revision` first; the options block is the last mention.
    chunk = help_text.rsplit("--revision", 1)[1].split("--json", 1)[0].lower()
    assert "required" in chunk, f"`--revision` does not say it is required: {chunk!r}"
    for form in ("default:", "defaults to"):
        assert form not in help_text.lower(), (
            f"an `artifact save` option advertises a default ({form!r}); `--revision` has had none "
            f"since #6 and no other option on this subcommand has one either:\n{help_text}")


# ── documents on stdin ──────────────────────────────────────────────────────────
# `-` exists so a caller holding content does not have to invent a file for it. The Claude Code skills
# used to write `/tmp/requivo:prd.md` — a shared path, illegal on Windows, needing `rm` to clean up.


def _run_stdin(argv, text, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    return _run(argv)


def test_a_proposal_can_be_applied_from_stdin(workspace, monkeypatch):
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    proposal = json.dumps(_full_model())
    r = json.loads(_run_stdin(["model", "validate", "-", "--json"], proposal, monkeypatch))
    assert r["status"] == "valid"
    applied = json.loads(_run_stdin(["model", "apply", "s", "-", "--expected-revision", "0", "--json"],
                                    proposal, monkeypatch))
    assert applied["revision"] == 1


def test_an_artifact_can_be_saved_from_stdin(workspace, monkeypatch):
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _run_stdin(["model", "apply", "s", "-", "--json"], json.dumps(_full_model()), monkeypatch)
    r = json.loads(_run_stdin(["artifact", "save", "s", "--type", "prd", "--file", "-",
                               "--revision", "1", "--json"],
                              "# PRD\nwritten straight to stdin\n", monkeypatch))
    assert r["revision"] == 1 and r["stale"] is False
    assert "straight to stdin" in _run(["artifact", "show", "s", "--type", "prd"])


def test_a_request_can_be_created_from_stdin(workspace, monkeypatch):
    r = json.loads(_run_stdin(["session", "init", "-", "--slug", "s", "--json"],
                              "We need a leave approval system.\n", monkeypatch))
    assert r["slug"] == "s"
    assert "leave approval" in store.session_request("s")


def test_a_missing_document_path_is_an_error_not_content(workspace):
    # `model apply <session> <path>` takes a path. Treating an unreadable one as the proposal itself
    # would turn a typo into a confusing schema error about a body that happens to be a filename.
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    with pytest.raises(SystemExit) as exc:
        _run(["model", "apply", "s", "no-such-file.json", "--json"])
    assert exc.value.code != 0


def test_stdin_is_refused_when_it_is_a_terminal(workspace, monkeypatch):
    class _Tty(io.StringIO):
        def isatty(self):
            return True

    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    monkeypatch.setattr("sys.stdin", _Tty(""))
    # Without this guard the command blocks forever waiting for input nobody meant to type.
    with pytest.raises(SystemExit) as exc:
        _run(["model", "apply", "s", "-", "--json"])
    assert exc.value.code != 0


def test_context_can_be_asked_for_by_session(workspace):
    # A session's card selection is held constant across its turns; a later turn that reads every card
    # reasons from a wider context than the model was built on. Asking by session makes that unmissable.
    _run(["session", "init", "Something.", "--slug", "narrow", "--context", "b2b-platform", "--json"])
    _run(["session", "init", "Something else.", "--slug", "wide", "--json"])
    narrow = _run(["context", "--session", "narrow"])
    wide = _run(["context", "--session", "wide"])
    assert "## b2b-platform" in narrow
    assert len(narrow) < len(wide)          # the subset really is a subset
    assert narrow == _run(["context", "--cards", "b2b-platform"])

    with pytest.raises(SystemExit):         # the two selectors are alternatives
        _run(["context", "--session", "narrow", "--cards", "b2b-platform"])


# ── session import ──────────────────────────────────────────────────────────────
# Import takes a file from outside the workspace and turns it into a session, so it is the one command
# whose input is genuinely untrusted. Nothing may land in the store before the archive has been checked.


def _zip(path, entries: dict) -> None:
    with zipfile.ZipFile(path, "w") as z:
        for name, content in entries.items():
            z.writestr(name, content)


def _good_entries(slug="imported", revision=0):
    meta = {"format_version": 1, "session_id": "abc", "slug": slug, "created_at": "t",
            "updated_at": "t", "current_revision": revision}
    entries = {f"{slug}/session.json": json.dumps(meta), f"{slug}/request.md": "A request."}
    if revision:
        entries[f"{slug}/model.json"] = json.dumps(_full_model())
    return entries


def test_export_import_round_trip(workspace, tmp_path, monkeypatch):
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _run_stdin(["model", "apply", "s", "-", "--json"], json.dumps(_full_model()), monkeypatch)
    _run(["session", "export", "s", "-o", str(tmp_path / "s.zip"), "--json"])

    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path / "elsewhere"))
    r = _run_json(["session", "import", str(tmp_path / "s.zip"), "--json"])
    assert r["slug"] == "s" and r["replaced"] is False
    assert store.read_meta("s").current_revision == 1


def test_import_json_names_the_session_and_its_directory_the_way_its_siblings_do(workspace, tmp_path,
                                                                                 monkeypatch):
    """#84. `session import --json` spelled the session `imported` and its location `into`; every
    sibling verb spells them `slug` and (for the one that reports a directory) `path`. A consumer
    looping over session verbs and reading `row["slug"]` got a `KeyError` from the one verb that had
    just put the session there.

    `imported` and `into` are **gone**, not kept as duplicates. Keeping both would leave the wrong
    spelling in the payload a reader copies from, and the whole reason this ships before 1.0 is that
    removing a `--json` key is breaking afterwards.

    **`path` is the session's own directory, not the session root**, and that is a correction to the
    issue as filed, which proposed `str(root)` — the value `into` carried. `session init --json`'s
    `path` is `canonical_dir(slug)`, and `session import`'s own human-readable line already prints
    `canonical_dir(slug)`. Renaming the key while leaving the container underneath it would give
    `path` two meanings across two verbs of the same noun — the defect this issue exists to close,
    reintroduced under the harmonised name and harder to see, because the key would then look right.
    """
    init = _run_json(["session", "init", "Something.", "--slug", "s", "--json"])
    _run(["session", "export", "s", "-o", str(tmp_path / "s.zip"), "--json"])

    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path / "elsewhere"))
    r = _run_json(["session", "import", str(tmp_path / "s.zip"), "--json"])

    assert r.keys() == {"slug", "path", "replaced"}
    assert "imported" not in r and "into" not in r
    assert r["slug"] == "s"
    assert r["path"] == str(store.canonical_dir("s"))
    # the directory, not the root that holds it — and it is the same string the verb prints
    assert Path(r["path"]).name == "s"
    assert r["path"] != str(store.session_root())

    # must fire: `path` means the same thing here as it does on the verb that creates a session
    assert set(init) >= {"slug", "path"}
    assert Path(init["path"]).name == "s"


def test_import_refuses_a_directory_name_that_is_not_a_valid_slug(workspace, tmp_path):
    """The reviewer's case: an archive whose folder is `bad slug` unpacked happily and then broke every
    later `session list`. A directory name becomes a slug, so it faces the same validation as any."""
    _zip(tmp_path / "bad.zip", _good_entries("bad slug"))
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "bad.zip"), "--json"])
    assert store.list_session_slugs() == []          # and nothing was written
    assert _run_json(["session", "list", "--json"])["sessions"] == []


@pytest.mark.parametrize("entry", [
    "../escape/session.json",          # traversal via a parent segment
    "/absolute/session.json",          # an absolute path
    "..\\windows\\session.json",       # a Windows separator zipfile does not treat as a boundary
    "loose.json",                      # not inside a session directory at all
])
def test_import_refuses_unsafe_entries(workspace, tmp_path, entry):
    _zip(tmp_path / "evil.zip", {entry: "{}"})
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "evil.zip"), "--json"])
    assert store.list_session_slugs() == []


def test_import_refuses_an_archive_holding_more_than_one_session(workspace, tmp_path):
    _zip(tmp_path / "two.zip", {**_good_entries("one"), **_good_entries("two")})
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "two.zip"), "--json"])
    assert store.list_session_slugs() == []


def test_import_refuses_an_archive_that_is_too_large_or_too_many_files(workspace, tmp_path):
    from requivo.deterministic import MAX_ARCHIVE_FILES

    many = {f"s/artifacts/f{i}.md": "x" for i in range(MAX_ARCHIVE_FILES + 1)}
    _zip(tmp_path / "many.zip", many)
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "many.zip"), "--json"])

    # A zip bomb compresses to nothing and expands past the ceiling; the cap is on the expanded size.
    _zip(tmp_path / "big.zip", {"s/session.json": "0" * (64 * 1024 * 1024 + 1)})
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "big.zip"), "--json"])
    assert store.list_session_slugs() == []


def test_import_refuses_an_archive_that_is_not_a_session(workspace, tmp_path):
    # Extraction succeeding is not the same as having imported a session. Import used to declare
    # success on the strength of the extraction alone.
    _zip(tmp_path / "nometa.zip", {"s/notes.md": "hello"})
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "nometa.zip"), "--json"])

    _zip(tmp_path / "badjson.zip", {"s/session.json": "{not json"})
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "badjson.zip"), "--json"])

    # A session that disagrees with itself about its own identity.
    _zip(tmp_path / "mismatch.zip", {**_good_entries("claimed")})
    with zipfile.ZipFile(tmp_path / "mismatch2.zip", "w") as z:
        meta = json.loads(_good_entries("claimed")["claimed/session.json"])
        meta["slug"] = "something-else"
        z.writestr("claimed/session.json", json.dumps(meta))
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "mismatch2.zip"), "--json"])

    # A session claiming a model it does not carry.
    _zip(tmp_path / "noModel.zip", {"s/session.json": json.dumps(
        {"format_version": 1, "session_id": "a", "slug": "s", "created_at": "t", "updated_at": "t",
         "current_revision": 3})})
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "noModel.zip"), "--json"])
    assert store.list_session_slugs() == []


def test_import_refuses_a_collision_unless_forced(workspace, tmp_path, monkeypatch):
    _run(["session", "init", "The original.", "--slug", "dup", "--json"])
    _run_stdin(["model", "apply", "dup", "-", "--json"], json.dumps(_full_model()), monkeypatch)
    _zip(tmp_path / "dup.zip", _good_entries("dup"))

    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "dup.zip"), "--json"])
    assert store.read_meta("dup").current_revision == 1        # the original is untouched
    assert "The original." in store.session_request("dup")

    r = _run_json(["session", "import", str(tmp_path / "dup.zip"), "--force", "--json"])
    assert r["replaced"] is True
    assert store.read_meta("dup").current_revision == 0        # genuinely replaced, not merged
    assert "A request." in store.session_request("dup")


# ── the archive-shape refusals name the archive, not the model (#101) ───────────
#
# #82 split `invalid_session` into a nine-arm family on the principle that a code must name its fact,
# and gave `unreadable_archive` and `inconsistent_archive` codes of their own. The seven shape
# refusals *between* those two arms — same function, same code path — kept `InvalidModelError`, whose
# docstring reads "a proposed model is structurally or semantically invalid". `cli.py` serializes
# `to_dict()` on every `--json` verb, so a consumer scripting `session import --json` read one handle
# for *my zip is too big*, *that slug is taken* and *your proposal is malformed*: three remedies
# behind one code, on the page that tells them to assert on the code and never on the message.
#
# One code and not seven, because the seven share a remedy — *give me a different archive*. What a
# single code owes in exchange is the thing #82 was actually about: `details` must not vary silently
# under it. `details["problem"]` is on every arm, so a consumer that needs the distinction branches
# on a key that is always there rather than on a `KeyError`.


def _import_error(archive) -> dict:
    """Drive the real CLI and return the structured envelope a `--json` consumer sees."""
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "import", str(archive), "--json"], client=None)
    assert e.value.code == 1
    return json.loads(buf.getvalue())


def _lower_the_byte_ceiling(mp):
    """The real 64 MiB ceiling is driven end-to-end by
    `test_import_refuses_an_archive_that_is_too_large_or_too_many_files`, which asserts this same
    code. Paying 64 MiB of allocation a second time to re-read the same branch buys nothing, so the
    ceiling moves instead of the archive — `_inspect_archive` reads the module global at call time."""
    import requivo.deterministic as det
    mp.setattr(det, "MAX_ARCHIVE_BYTES", 32)


@pytest.mark.parametrize("label, problem, build", [
    ("no entries at all", "empty",
     lambda p, mp: _zip(p, {})),
    ("more entries than the ceiling", "too_many_files",
     lambda p, mp: _zip(p, {f"s/artifacts/f{i}.md": "x"
                            for i in range(MAX_ARCHIVE_FILES + 1)})),
    ("expands past the byte ceiling", "too_large",
     lambda p, mp: (_lower_the_byte_ceiling(mp), _zip(p, {"s/session.json": "0" * 64}))),
    ("a parent segment", "unsafe_entry",
     lambda p, mp: _zip(p, {"../escape/session.json": "{}"})),
    ("an absolute path", "unsafe_entry",
     lambda p, mp: _zip(p, {"/absolute/session.json": "{}"})),
    ("a Windows separator zipfile does not treat as a boundary", "unsafe_entry",
     lambda p, mp: _zip(p, {"..\\windows\\session.json": "{}"})),
    ("an entry loose at the root", "entry_outside_session_directory",
     lambda p, mp: _zip(p, {"loose.json": "{}"})),
    ("two session directories", "multiple_sessions",
     lambda p, mp: _zip(p, {**_good_entries("one"), **_good_entries("two")})),
])
def test_an_archive_shaped_wrong_is_refused_as_an_archive(workspace, tmp_path, monkeypatch,
                                                          label, problem, build):
    """#101. Every one of the seven shape refusals, driven through the CLI a consumer actually calls."""
    archive = tmp_path / "case.zip"
    build(archive, monkeypatch)

    err = _import_error(archive)
    assert err["code"] == "invalid_archive", f"{label} still answers {err['code']}"
    assert err["details"]["problem"] == problem, label
    assert store.list_session_slugs() == []


def test_the_shape_refusals_are_visible_to_a_consumer_that_did_not_enumerate_them(workspace, tmp_path):
    """The must-fire half of the case above: the harness can see a *good* archive land, so the seven
    reds are the refusal firing and not the fixture failing to build anything at all.

    Also the family question. `InvalidArchiveError` is an `InvalidSessionError`, so the arm on either
    side of it on this code path — `unreadable_archive` and `inconsistent_archive` — is caught by the
    same `except`, which is the asymmetry #101 is about. It is deliberately *not* an
    `InvalidModelError` any more; that is the breaking half, recorded in `changelog.d/101`."""
    from requivo.core.errors import InvalidArchiveError, InvalidModelError, InvalidSessionError

    assert issubclass(InvalidArchiveError, InvalidSessionError)
    assert not issubclass(InvalidArchiveError, InvalidModelError)
    assert InvalidArchiveError.code == "invalid_archive"

    # must fire: a well-formed archive still imports, so the seven refusals above mean something
    _zip(tmp_path / "good.zip", _good_entries("fine"))
    r = _run_json(["session", "import", str(tmp_path / "good.zip"), "--json"])
    assert r["slug"] == "fine"
    assert store.list_session_slugs() == ["fine"]


def test_an_occupied_slug_is_a_conflict_with_the_store_not_an_invalid_model(workspace, tmp_path,
                                                                           monkeypatch):
    """#101, the sharpest row: the vocabulary already had the right code. `session_exists` answers
    409 and its docstring is written for exactly this fact; the import path raised `invalid_model`
    and 400 instead — a *conflict with the store's current state* reported as a malformed proposal.

    The `--force` half is asserted in the same test on purpose: a refusal that cannot be lifted is a
    different product, and a code change must not turn the escape hatch off."""
    _run(["session", "init", "The original.", "--slug", "dup", "--json"])
    _zip(tmp_path / "dup.zip", _good_entries("dup"))

    err = _import_error(tmp_path / "dup.zip")
    assert err["code"] == "session_exists"
    assert err["details"] == {"slug": "dup"}
    assert "The original." in store.session_request("dup")   # and nothing was replaced

    # must fire: --force still lands, so the code above is the refusal and not a broken archive
    assert _run_json(["session", "import", str(tmp_path / "dup.zip"), "--force",
                      "--json"])["replaced"] is True


def test_every_refusal_on_the_import_path_names_what_it_is_about(workspace, tmp_path):
    """The table in `docs/cli.md` under *Importing a session*, asserted rather than described.

    Seven codes reach this verb and each answers a different question. They are checked together
    because the defect #101 fixes was not any one of them being wrong — it was two of them being the
    *same* code while their neighbours on the identical code path had names of their own. A table
    that drifts back into a single handle fails here before a consumer discovers it.
    """
    from requivo.core.errors import (
        InconsistentArchiveError,
        InvalidArchiveError,
        InvalidSessionError,
        SessionExistsError,
        UnreadableArchiveError,
    )

    # not a file at all — before anything is opened
    assert _import_error(tmp_path / "nowhere.zip")["code"] == "session_not_found"

    # a file that is not a zip
    (tmp_path / "text.zip").write_text("not a zip at all", encoding="utf-8")
    assert _import_error(tmp_path / "text.zip")["code"] == "unreadable_archive"

    # a zip whose shape is not an export
    _zip(tmp_path / "loose.zip", {"loose.json": "{}"})
    assert _import_error(tmp_path / "loose.zip")["code"] == "invalid_archive"

    # a zip whose one directory could not be a session
    _zip(tmp_path / "badname.zip", _good_entries("bad slug"))
    assert _import_error(tmp_path / "badname.zip")["code"] == "invalid_slug"

    # a zip whose session does not tell the truth about itself
    broken = _good_entries("claimed")
    broken["claimed/session.json"] = json.dumps(
        {**json.loads(broken["claimed/session.json"]), "slug": "something-else"})
    _zip(tmp_path / "lying.zip", broken)
    assert _import_error(tmp_path / "lying.zip")["code"] == "inconsistent_archive"

    # a zip that is fine, onto a slug that is taken
    _run(["session", "init", "The original.", "--slug", "taken", "--json"])
    _zip(tmp_path / "taken.zip", _good_entries("taken"))
    assert _import_error(tmp_path / "taken.zip")["code"] == "session_exists"

    # a zip that passed every check and could not be moved into place. The seventh code, and the one
    # this test claimed to cover while asserting six — found by the pre-1.0 release audit reading the
    # docstring against the body.
    #
    # Driven by patching `Path.replace` rather than by arranging a filesystem that refuses a rename:
    # the conditions that produce one differ per platform (ENOTEMPTY on POSIX, a held handle on
    # Windows), so a fixture would test the platform on some legs and nothing on others. The patch is
    # narrowed to the one destination under test, so the backup/restore path — which uses the same
    # call — is untouched and a failure here cannot come from the harness.
    _zip(tmp_path / "movefail.zip", _good_entries("move-fails"))
    doomed = store.canonical_dir("move-fails")
    real_replace = Path.replace

    def _refuse_only_the_target(self, dest):
        if Path(dest) == doomed:
            raise OSError(39, "Directory not empty")
        return real_replace(self, dest)

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(Path, "replace", _refuse_only_the_target)
        assert _import_error(tmp_path / "movefail.zip")["code"] == "import_move_failed"
    finally:
        monkeypatch.undo()

    # must fire: the patch really was the cause, so the same archive lands once it is lifted. Without
    # this the assertion above would pass just as well against an import broken some other way.
    assert _run_json(["session", "import", str(tmp_path / "movefail.zip"), "--json"])["slug"] == "move-fails"

    # the three archive codes are one family, so `except InvalidSessionError` still catches every
    # archive refusal without enumerating them; the other two deliberately are not in it
    for cls in (UnreadableArchiveError, InvalidArchiveError, InconsistentArchiveError):
        assert issubclass(cls, InvalidSessionError), cls.__name__
    assert not issubclass(InvalidSlugError, InvalidSessionError)
    assert not issubclass(SessionExistsError, InvalidSessionError), (
        "an occupied slug is a conflict with the store, not a malformed session")

    # must fire: with all seven refusals asserted, a good archive still lands
    _zip(tmp_path / "good.zip", _good_entries("ok-one"))
    assert _run_json(["session", "import", str(tmp_path / "good.zip"), "--json"])["slug"] == "ok-one"


# A directory name inside an archive is caller text that has NOT been validated yet: `validate_slug`
# runs on the one surviving slug, after the count check, so the message that reports *more than one*
# is the single site in `_inspect_archive` that interpolates a raw, unvalidated, attacker-chosen
# string. Its two siblings on the same path already render an entry name with `!r`. Same class as
# #40 and #98, one function along.
_FORGED_SLUG = (
    "ok-session\n"
    "All clear, nothing to see.\n"
    "  ✅ sessions        0 in this workspace"
)


def test_an_archive_directory_name_cannot_write_a_line_of_the_refusal_reporting_it(workspace,
                                                                                   tmp_path):
    """Found by the audit of #101, on a line #101 edits. The refusal naming the directories it found
    is rendered to stderr by `cli.py`, and `safe_write` guards encoding, not control characters — so
    a top-level directory carrying a newline ends the line and writes the next one at column 0.

    Two directories are needed to reach this arm at all, which is why the archive carries an innocent
    second one."""
    _zip(tmp_path / "forged.zip", {f"{_FORGED_SLUG}/session.json": "{}",
                                   "other/session.json": "{}"})

    err = io.StringIO()
    with redirect_stderr(err), pytest.raises(SystemExit):
        app(["session", "import", str(tmp_path / "forged.zip")], client=None)
    rendered = err.getvalue()

    # must fire: the refusal really did run and really did name what it found
    assert "session directories" in rendered, rendered
    assert "other" in rendered

    # the forgery does not reach the terminal as lines of its own
    assert "\nAll clear, nothing to see." not in rendered
    assert not any(_SESSIONS_ROW.match(line) for line in rendered.splitlines()), rendered
    # …and it is escaped rather than dropped, so nothing is hidden from the reader
    assert "ok-session" in rendered

    # `--json` was never exposed — json.dumps escapes a control character before it can reach a line
    out = _import_error(tmp_path / "forged.zip")
    assert out["code"] == "invalid_archive"
    assert _FORGED_SLUG in out["details"]["slugs"], "the raw name is still reported, losslessly"


def test_a_refused_import_leaves_no_scratch_directory(workspace, tmp_path):
    _zip(tmp_path / "bad.zip", _good_entries("bad slug"))
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "bad.zip"), "--json"])
    _zip(tmp_path / "nometa.zip", {"s/notes.md": "hello"})
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "nometa.zip"), "--json"])
    assert list((workspace / ".requivo").glob(".import-*")) == []


# ── session integrity at the boundary ────────────────────────────────────────


def test_session_verify_reports_a_broken_history_and_exits_non_zero(workspace, tmp_path, monkeypatch):
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _run_stdin(["model", "apply", "s", "-", "--json"], json.dumps(_full_model()), monkeypatch)
    assert _run_json(["session", "verify", "s", "--json"])["ok"] is True

    (store.canonical_dir("s") / "revisions" / "0001-model.json").unlink()
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "verify", "s", "--json"], client=None)
    assert e.value.code == 1
    report = json.loads(buf.getvalue())
    assert report["ok"] is False
    assert [p["code"] for p in report["problems"]] == ["missing_revision_file"]


# ── session verify: three answers, three exit codes (#86) ────────────────────────
#
# `_card_health` already renders three states and `_cmd_session_verify` collapsed two of them into
# exit 1. These tests patch `check_selection` rather than denying reads on a real card directory:
# the state under test is "the card layer raised", the raise is what the verb branches on, and a
# POSIX mode bit is one platform's way of producing it. The real filesystem route is already covered
# above by `test_a_card_directory_that_cannot_be_read_is_unreadable_not_empty`, loudly skipped where
# the platform cannot deny a read; pinning the exit code to that route too would leave this rule
# untested on Windows.


def _cards_unreadable(monkeypatch) -> None:
    """The card layer itself cannot be enumerated, so `check_selection` propagates rather than
    returning a verdict — `_card_health`'s `{"checked": False}` arm, which is *we could not look*."""
    import requivo.deterministic as det
    from requivo.core.errors import ContextUnreadableError

    def _boom(only):
        raise ContextUnreadableError(
            "the context-card directory exists but cannot be read: denied",
            details={"directory": "walled"})

    monkeypatch.setattr(det, "check_selection", _boom)


def test_session_verify_exits_four_when_it_could_not_check_the_product_context(workspace, monkeypatch):
    """*Checked, and it is broken* and *could not check* are two different answers and had one exit
    code. The verb already printed them differently — the second has a glyph of its own — and then
    exited 1 beside a session that really is inconsistent, in the command whose whole job is to say
    whether a session is sound.

    4 rather than a new 5: the code already means *the work was done and part of the answer was
    unreachable*, and a code per verb rebuilds the problem 4 was introduced to solve.

    The clean run is asserted first and is the control. Without it this test would pass equally well
    against a verb that exits 4 unconditionally.
    """
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    healthy = _run_json(["session", "verify", "s", "--json"])
    assert healthy["ok"] is True                                    # must fire
    assert healthy["context_cards"]["checked"] is True

    _cards_unreadable(monkeypatch)

    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "verify", "s", "--json"], client=None)
    assert e.value.code == 4
    report = json.loads(buf.getvalue())
    assert report["ok"] is False
    assert report["problems"] == []                                 # nothing is wrong inside it
    assert report["context_cards"]["checked"] is False
    assert report["context_cards"]["problem"] is None

    # the same number on the human surface: the exit code is not a property of `--json`
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "verify", "s"], client=None)
    assert e.value.code == 4
    assert "Could not check" in buf.getvalue()


def test_session_verify_lets_a_firm_negative_outrank_a_partial_one(workspace, monkeypatch):
    """Both at once: a session that really is inconsistent *and* whose product context could not be
    checked. It exits **1**, not 4.

    A script gating on *is this usable* wants the definite answer, and there is one — the session is
    broken, and no reading of the cards could make it sound again. 4 would understate a finding that
    is complete. `--json` still carries both facts, so nothing is withheld at either code.
    """
    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _run_stdin(["model", "apply", "s", "-", "--json"], json.dumps(_full_model()), monkeypatch)
    (store.canonical_dir("s") / "revisions" / "0001-model.json").unlink()

    # must fire: with the cards readable this is the ordinary firm negative
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "verify", "s", "--json"], client=None)
    assert e.value.code == 1
    assert json.loads(buf.getvalue())["context_cards"]["checked"] is True

    _cards_unreadable(monkeypatch)

    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as e:
        app(["session", "verify", "s", "--json"], client=None)
    assert e.value.code == 1, "a partial answer displaced a complete one"
    report = json.loads(buf.getvalue())
    assert [p["code"] for p in report["problems"]] == ["missing_revision_file"]
    assert report["context_cards"]["checked"] is False


def test_session_verify_exits_one_when_the_cards_were_checked_and_are_broken(workspace, tmp_path):
    """The other firm negative, and the one most easily confused with the new 4: the cards *were*
    read and the selection does not resolve. That is a complete answer about a session that is not
    usable, so it stays at 1 — 4 is for the answer nobody could produce."""
    cards = tmp_path / "cards"
    cards.mkdir()
    card = cards / "lost-domain.md"
    card.write_text("# Lost domain\n", encoding="utf-8")

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("REQUIVO_CONTEXT_DIR", str(cards))
        _run(["session", "init", "Something.", "--slug", "s", "--context", "lost-domain", "--json"])
        assert _run_json(["session", "verify", "s", "--json"])["ok"] is True   # must fire
        card.unlink()

        buf = io.StringIO()
        with redirect_stdout(buf), pytest.raises(SystemExit) as e:
            app(["session", "verify", "s", "--json"], client=None)

        # The text branch as well, and not for symmetry: it is the only arm that binds a local to a
        # card-problem *code* string, so an exit status computed into a name that collides with it
        # leaves SystemExit carrying `unknown_context_card` instead of a number. Reproduced while
        # writing this change.
        text = io.StringIO()
        with redirect_stdout(text), pytest.raises(SystemExit) as e_text:
            app(["session", "verify", "s"], client=None)

    assert e.value.code == 1
    assert e_text.value.code == 1
    report = json.loads(buf.getvalue())
    assert report["context_cards"]["checked"] is True
    assert report["context_cards"]["problem"]["code"] == "unknown_context_card"


def test_import_refuses_an_archive_whose_history_is_missing(workspace, tmp_path):
    """An archive can announce revision 2 and carry no `revisions/` at all — every file in it valid,
    every relationship between them false. Import checked shapes, so it accepted this and the damage
    surfaced later, somewhere unrelated. It now runs the same integrity check as `session verify`."""
    entries = _good_entries("s", revision=1)
    entries["s/session.json"] = json.dumps({
        "format_version": 1, "session_id": "abc", "slug": "s", "created_at": "t", "updated_at": "t",
        "current_revision": 2})                        # …with no revision log and no revision files
    _zip(tmp_path / "hollow.zip", entries)

    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "hollow.zip"), "--json"])
    assert store.list_session_slugs() == []


def test_import_refuses_a_file_that_is_not_an_archive(workspace, tmp_path):
    """`zipfile.BadZipFile` reached the user as a traceback. Every way a supplied file can be wrong
    has to arrive as a Requivo error."""
    bad = tmp_path / "notazip.zip"
    bad.write_text("this is not a zip")
    with pytest.raises(SystemExit) as e:
        _run(["session", "import", str(bad), "--json"])
    assert e.value.code == 1


def test_a_failed_forced_replacement_puts_the_original_back(workspace, tmp_path, monkeypatch):
    """`--force` used to `rmtree` the existing session and *then* move the new one in. If the move
    failed the user was left with neither: the archive refused, and the session they already had
    deleted. The old session now steps aside and only dies once the new one is in place."""
    _run(["session", "init", "The original.", "--slug", "dup", "--json"])
    _run_stdin(["model", "apply", "dup", "-", "--json"], json.dumps(_full_model()), monkeypatch)
    _zip(tmp_path / "dup.zip", _good_entries("dup"))

    real_replace = Path.replace

    def failing_replace(self, target):
        # Only the move that brings the *imported* session into place fails; the step-aside and the
        # rollback must still work, which is the whole point.
        if ".import-" in str(self):
            raise OSError("simulated failure moving the imported session into place")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", failing_replace)
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "dup.zip"), "--force", "--json"])

    assert store.session_exists("dup")
    assert store.read_meta("dup").current_revision == 1        # the original, intact
    assert "The original." in store.session_request("dup")
    assert _run_json(["session", "verify", "dup", "--json"])["ok"] is True


def test_export_excludes_the_lock_file_and_waits_for_the_writer(workspace, tmp_path, monkeypatch):
    """An export reads several files that must agree with each other. Read outside the lock, it can
    combine an old session.json with a new model.json — an archive that is internally inconsistent and
    only says so on import. And `.lock` is this machine's coordination, not part of the session: it
    has no meaning in an archive and would import as a session component."""
    import threading
    import time

    _run(["session", "init", "Something.", "--slug", "s", "--json"])
    _run_stdin(["model", "apply", "s", "-", "--json"], json.dumps(_full_model()), monkeypatch)
    assert (store.canonical_dir("s") / ".lock").exists()       # the writer left one behind

    held = threading.Event()

    def hold_the_lock():
        with store.session_lock("s"):
            time.sleep(0.4)
            held.set()

    t = threading.Thread(target=hold_the_lock)
    t.start()
    time.sleep(0.05)                                           # let it take the lock first
    dest = tmp_path / "s.zip"
    _run(["session", "export", "s", "-o", str(dest), "--json"])
    t.join(timeout=10)

    assert held.is_set(), "the export read the session while a writer held it"
    with zipfile.ZipFile(dest) as z:
        names = z.namelist()
    assert not [n for n in names if ".lock" in n]
    assert "s/model.json" in names and "s/revisions/0001-model.json" in names
