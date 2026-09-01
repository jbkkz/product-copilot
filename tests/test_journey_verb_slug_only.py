"""#402 -- eight of the ten journey verbs advertised a model.json path they could not open, and
`SessionService.resolve_slug` mined one anyway: `p.parent.name`, unconditionally, whether or not the
file existed. A nonexistent path was reported on under a slug carved out of its own parent
directory -- a name the user never typed -- and worse, a session that happened to share that name
was operated on silently.

`status` and `impact` are unaffected: they resolve a real path through `cli.py`'s `_resolve_ref`,
which reads the file's own bytes directly and never goes near `resolve_slug`. The other eight --
`answer`, `brief`, `prd`, `stories`, `estimate`, `criteria`, `epic`, `release` -- never open the file
they are handed at all: they resolve a *slug* and then read and write the store's own copy
(`ArtifactService.save` refuses anything that is not `has_meta(slug)`, so a loose file has nowhere to
file an artifact against). So a path was never a meaningful input for these eight, and the fix is to
say so before ever mining one: `SessionService.resolve_slug(..., accept_path=False)` refuses a
path-shaped reference outright, naming exactly what was given.

The shared harness is `tests/_cli_harness.py`.
"""
from __future__ import annotations

import pytest
from _cli_harness import _full_model

from requivo.cli import app
from requivo.core import persistence as store
from requivo.core.errors import SessionNotFoundError
from requivo.services.sessions import SessionService


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path


def _fails(argv, capsys) -> str:
    with pytest.raises(SystemExit) as exc:
        app(argv, client=None)   # client=None -> an accidental API call would blow up
    assert exc.value.code == 1
    return capsys.readouterr().err


# ── `SessionService.resolve_slug` itself ──────────────────────────────────────


def test_resolve_slug_refuses_a_model_json_path_when_the_caller_opted_out(tmp_path):
    """The eight write verbs pass `accept_path=False`. A model.json-shaped reference is refused
    outright, naming exactly what was given -- never mined for a slug."""
    ref = str(tmp_path / "loose" / "model.json")
    with pytest.raises(SessionNotFoundError) as exc:
        SessionService().resolve_slug(ref, accept_path=False)
    assert exc.value.details["ref"] == ref
    assert "loose" not in str(exc.value).replace(ref, ""), (
        "the refusal must not name a slug mined from the path's parent directory")


def test_resolve_slug_still_accepts_a_bare_slug_when_paths_are_refused():
    """Must-fire control: refusing paths must not refuse an ordinary slug too."""
    assert SessionService().resolve_slug("leave-approval", accept_path=False) == "leave-approval"


def test_resolve_slug_no_longer_mines_a_nonexistent_model_json_path(tmp_path):
    """The root cause, independent of `accept_path`: a caller that *does* still want path support
    (every `deterministic/` verb) must not get a slug carved out of a file that was never written --
    that slug can coincidentally name a real session, which is the worse half of #402."""
    ref = str(tmp_path / "loose" / "model.json")
    assert SessionService().resolve_slug(ref) == ref   # named as given, not mined


def test_resolve_slug_still_mines_a_real_saved_model_json(tmp_path):
    """Must-fire control: the existence gate must not break the legitimate case -- a real model.json
    really does live under its own session's directory name."""
    d = tmp_path / "leave-approval"
    d.mkdir()
    (d / "model.json").write_text("{}", encoding="utf-8")
    assert SessionService().resolve_slug(str(d / "model.json")) == "leave-approval"


# ── the eight write verbs, end to end ─────────────────────────────────────────

_WRITE_VERB_ARGV = [
    ["answer", "some answers"],
    ["brief"],
    ["prd"],
    ["stories"],
    ["estimate"],
    ["criteria"],
    ["epic"],
    ["release"],
]


@pytest.mark.parametrize("tail", _WRITE_VERB_ARGV, ids=lambda a: a[0])
def test_a_nonexistent_model_json_path_is_refused_naming_the_path(tail, workspace, capsys):
    """The reproduction in the issue, run through every one of the eight verbs: the path given, not
    a slug carved out of it, is what the refusal names."""
    verb, *rest = tail
    ref = str(workspace / "loose" / "model.json")
    err = _fails([verb, ref, *rest], capsys)
    assert ref in err, f"`requivo {verb}` does not name the path it was given: {err!r}"


@pytest.mark.parametrize("tail", _WRITE_VERB_ARGV, ids=lambda a: a[0])
def test_a_nonexistent_model_json_path_does_not_silently_use_an_unrelated_real_session(
        tail, workspace, capsys):
    """The worse half. A session named `loose` really exists elsewhere in the workspace; the path
    the user gave points nowhere and its parent happens to share that name. This must never resolve
    to the real `loose` session -- paired with the must-fire positive below, which proves `loose` is
    reachable by its own slug, so this is not merely a harness that never runs anything."""
    store.create_session("loose", "an unrelated real session")
    verb, *rest = tail
    ref = str(workspace / "loose" / "model.json")   # does not exist; "loose" only coincides in name
    err = _fails([verb, ref, *rest], capsys)
    assert "no session named loose" not in err.lower(), (
        f"`requivo {verb}` reported on the unrelated real session instead of the given path: {err!r}")
    assert ref in err


@pytest.mark.parametrize("tail", _WRITE_VERB_ARGV, ids=lambda a: a[0])
def test_the_real_session_is_still_reachable_by_its_own_slug(tail, workspace, capsys):
    """Must-fire control for the pair above: resolution by slug still succeeds, so the negative
    result above is not an artifact of a harness that refuses everything. Resolution succeeding means
    the failure (if any, past this point) is no longer `session_not_found`."""
    store.create_session("loose", "a real session, referenced by its own slug")
    verb, *rest = tail
    with pytest.raises(SystemExit):
        app([verb, "loose", *rest], client=None)
    err = capsys.readouterr().err
    assert "no session named" not in err.lower(), (
        f"`requivo {verb} loose` failed to resolve its own real session: {err!r}")


def test_status_and_impact_still_open_a_model_json_path_directly(workspace):
    """The two verbs this issue leaves untouched: they read the file's own bytes and never go near
    `resolve_slug`, so they are immune to this bug by construction and their wider help stays true."""
    import io
    from contextlib import redirect_stdout

    from requivo.core.contracts import EngineOutput

    loose = workspace / "elsewhere" / "model.json"
    loose.parent.mkdir(parents=True)
    loose.write_text(EngineOutput.model_validate(_full_model()).model_dump_json(), encoding="utf-8")

    buf = io.StringIO()
    with redirect_stdout(buf):
        app(["status", str(loose)], client=None)
    assert "UNDERSTANDING" in buf.getvalue()

    buf = io.StringIO()
    with redirect_stdout(buf):
        app(["impact", str(loose)], client=None)
    assert "DEPENDENCY MAP" in buf.getvalue()
