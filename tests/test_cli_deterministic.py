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
    empty_text = _run(["doctor"])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(det.store, "list_session_slugs", _unreadable)
        unreadable = _run_json(["doctor", "--json"])["sessions"]
        unreadable_text = _run(["doctor"])
    assert unreadable["readable"] is False
    assert unreadable["total"] is None, "0 is a claim about the workspace; we could not look"
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
    # The code names the omission rather than the session since #57. `invalid_session` was inherited
    # while `web/app.py` was held by another lane, and a caller across this boundary sees the code,
    # never the type — so the one handle it had could not tell "you left a flag off" from "this
    # session is broken".
    assert envelope["code"] == "unstated_source_revision"
    # and nothing was recorded against the guess: the PRD on disk is still the one saved above.
    assert _run_json(["artifact", "list", "s", "--json"])["prd"]["revision"] == 2


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
