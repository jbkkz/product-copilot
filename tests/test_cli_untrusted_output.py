"""A value read off disk cannot write a line of a verb's output (#40, #62, #70, #107).

Split out of `test_cli_deterministic.py` by #141, and the one file in that split which is not a
module of `requivo.deterministic`. It is the class that file swept deliberately *across* verbs, and
the sweep is the finding: `doctor`, `session verify`, `session show`, `artifact list` and `impact`
each render a string somebody else supplied, and each was fixed only after somebody looked at the
neighbour rather than at the issue.

The fixtures say the same thing. `forged_workspace` is read by tests of three verbs and
`_SHOW_FORGERIES` by tests of two, so splitting this file along the package boundary would mean
copying a fixture rather than moving a test — and the comment blocks below, which are the argument
for each constant, would have to be copied with it or orphaned.

The shared harness is `tests/_cli_harness.py`.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest
from _cli_harness import _SESSIONS_ROW, _forge_meta, _full_model, _run, _run_json

from requivo.cli import app
from requivo.core import persistence as store


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path


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
