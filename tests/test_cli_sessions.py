"""End-to-end tests of `requivo.deterministic.sessions` — `session init`, `list`, `show`, `migrate`
and `verify`.

Split out of `test_cli_deterministic.py` by #141. `session export` and `session import` are the other
half of the same module and live in `test_cli_session_archives.py`; the reason they are a file of
their own is written there. The shared harness is `tests/_cli_harness.py`.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest
from _cli_harness import _full_model, _run, _run_json, _run_stdin, _slot

from requivo.cli import app
from requivo.core import persistence as store


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    return tmp_path


# ── the acceptance scenario ─────────────────────────────────────────────────────


def test_session_init_creates_a_session(workspace):
    r = _run_json(["session", "init", "Build a leave approval system.", "--slug", "leave", "--json"])
    assert r["slug"] == "leave"
    assert store.session_exists("leave")
    assert store.read_meta("leave").current_revision == 0


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
    from requivo.core.errors import ContextUnreadableError
    from requivo.deterministic import doctor as det

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
