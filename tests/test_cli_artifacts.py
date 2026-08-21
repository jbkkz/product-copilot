"""End-to-end tests of `requivo.deterministic.artifacts` — `artifact save`, `list` and `show`.

Split out of `test_cli_deterministic.py` by #141; the shared harness is `tests/_cli_harness.py`.

The forgery case over `artifact list`'s two untrusted fields is deliberately not here. It lives in
`test_cli_untrusted_output.py` beside the `session show` case it was swept from, because the two
share `_SHOW_FORGERIES` and the argument for that constant is written once, above it.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest
from _cli_harness import _forge_meta, _full_model, _run, _run_json, _slot

from requivo.cli import _build_parser, app


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path


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
