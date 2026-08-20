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
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from requivo.cli import _build_parser, app
from requivo.core import persistence as store
from requivo.core.contracts import _schema_order, schema_slot_ids
from requivo.core.errors import InvalidSlugError
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

    Deliberately not `_deny_read`'s `chmod 000`. That denies the `session.json` probe in
    `_scan_session_root` as well, and `Path.exists()` re-raises EACCES rather than swallowing it, so
    the *whole root* reads as unlistable and this entry never gets a row of its own to be honest in.
    That behaviour predates this change — `list_session_slugs` has always raised there — and is
    reported separately rather than fixed on this branch."""
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


def test_session_list_does_not_call_one_of_these_a_session(workspace):
    """The other half of the partition, and why this is not `session list`'s finding to report: a
    listing of sessions must not grow a row for something that is not one. The real session beside it
    is the must-fire control — without it this passes against a listing that lists nothing at all."""
    _run(["session", "init", "A real one.", "--slug", "real", "--json"])
    _lock_ghost()

    rows = _run_json(["session", "list", "--json"])
    assert [r["slug"] for r in rows] == ["real"]
    assert store.list_session_slugs() == ["real"]

    text = _run(["session", "list"])
    assert "real" in text and "leave-approval" not in text


def test_the_two_halves_of_the_session_root_are_one_partition(workspace):
    """`list_session_slugs` and `list_non_session_entries` are complements over one predicate, and
    are only worth having as a pair while nothing can fall between them — a name in neither is
    precisely the state #67 is about. Staging directories are in neither on purpose: they are
    `create_session` in flight rather than something left behind, and reporting one is a race the
    reader cannot act on."""
    _run(["session", "init", "A real one.", "--slug", "real", "--json"])
    _lock_ghost()
    (store.session_root() / ".real.new-1-abcdef12").mkdir()

    slugs = set(store.list_session_slugs())
    others = {e.name for e in store.list_non_session_entries()}
    on_disk = {p.name for p in store.session_root().iterdir()}

    assert slugs == {"real"} and others == {"leave-approval"}
    assert slugs & others == set()
    assert on_disk - (slugs | others) == {".real.new-1-abcdef12"}


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
    listed = _run_json(["artifact", "list", "event", "--json"])
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
    assert _run_json(["artifact", "list", "s", "--json"])["prd"]["stale"] is False


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
    assert any(s["slug"] == "one" and s["revision"] == 1 for s in listing)
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
    assert _run_json(["artifact", "list", "s", "--json"])["prd"]["stale"] is True

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
    # and nothing was recorded against the guess: the PRD on disk is still the one saved above.
    assert _run_json(["artifact", "list", "s", "--json"])["prd"]["revision"] == 2


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
    assert r["imported"] == "s" and r["replaced"] is False
    assert store.read_meta("s").current_revision == 1


def test_import_refuses_a_directory_name_that_is_not_a_valid_slug(workspace, tmp_path):
    """The reviewer's case: an archive whose folder is `bad slug` unpacked happily and then broke every
    later `session list`. A directory name becomes a slug, so it faces the same validation as any."""
    _zip(tmp_path / "bad.zip", _good_entries("bad slug"))
    with pytest.raises(SystemExit):
        _run(["session", "import", str(tmp_path / "bad.zip"), "--json"])
    assert store.list_session_slugs() == []          # and nothing was written
    assert _run_json(["session", "list", "--json"]) == []


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
