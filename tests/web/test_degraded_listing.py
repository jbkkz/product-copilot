"""Invariant 15 — a listing survives its own members (#7).

The bug this invariant was written about shipped once already, and the fix was narrower than the
failure: `session_list` guards `status()` per row, but the *source* of the rows is a single-shot
comprehension above that guard, and the guard names one exception family. So there are three
distinct ways one broken session takes the whole home page down, and a fix that closes one of them
looks exactly like a fix that closes all three.

That is the vacuity trap here, and it is why this module is shaped the way it is:

* every degradation test runs against a fixture that also holds **healthy** sessions, and asserts
  they still render in full — a guard that degrades everything passes the degradation half;
* the three break modes are exercised **separately** as well as together, because a guard that
  catches `status()` and not `read_meta` is the defect, not a partial fix;
* each breaker asserts that it really broke something, via the strict read that is *supposed* to
  raise. A fixture that quietly stopped breaking anything would otherwise turn this whole file
  green while proving nothing.
"""

from __future__ import annotations

import json

import pytest

from requivo.core.errors import InvalidSessionError
from requivo.core.persistence import SESSION_FORMAT_VERSION, canonical_dir
from requivo.services.sessions import SessionService
from tests.web.conftest import full_model

HEALTHY_ANALYSED = "healthy-analysed"
HEALTHY_AWAITING = "healthy-awaiting"
BROKEN_META = "broken-meta"
BROKEN_REQUEST = "broken-request"
BROKEN_MODEL = "broken-model"


def _seed(slug: str, *, analysed: bool = True) -> str:
    """A session, offline. `analysed` decides whether it has a model — a session at revision 0 is a
    normal row (`Awaiting analysis`), not a broken one, and the two must never render alike."""
    svc = SessionService()
    svc.create_session(f"A request about {slug}", slug=slug)
    if analysed:
        svc.update_model(slug, json.dumps({
            "model": full_model(), "questions": [],
            "summary": {"objective": f"Objective for {slug}"}}))
    return slug


# ── the three break modes ─────────────────────────────────────────────────────
# Each is a real on-disk state a user can reach, not a monkeypatched raise: the point of the issue is
# which *layer* the failure comes out of, and a patched method would let the test agree with an
# implementation that guards the wrong layer.

def break_meta(slug: str) -> None:
    """A session written by a newer Requivo — the downgrade path in scenario A.

    `read_meta` refuses it with `InvalidSessionError`, and it does so from inside
    `list_sessions()`'s own comprehension: the failure is raised *before any row exists to degrade*,
    which is the half of #7 that sits above the existing guard.
    """
    p = canonical_dir(slug) / "session.json"
    # Explicit encoding on both halves. `read_text()` defaults to the *locale* encoding, which is
    # cp1252 on a default Windows console — the store writes UTF-8, so a session carrying anything
    # outside ASCII would fail here for a reason that has nothing to do with what is being tested.
    data = json.loads(p.read_text(encoding="utf-8"))
    data["format_version"] = SESSION_FORMAT_VERSION + 1
    p.write_text(json.dumps(data), encoding="utf-8")


def break_request(slug: str) -> None:
    """`request.md` replaced by a directory, so `request_text` cannot read it.

    The exact exception differs by platform — `IsADirectoryError` on POSIX, `PermissionError` on
    Windows — which is precisely why nothing here asserts on the type. Both are `OSError`, neither is
    a `RequivoError`, and the row has to degrade on either.
    """
    p = canonical_dir(slug) / "request.md"
    p.unlink()
    p.mkdir()


def break_model(slug: str) -> None:
    """A crash mid-write leaves `model.json` truncated — scenario B, and the sharpest of the three.

    `status()` reaches `PersistedEngineOutput.model_validate_json`, which raises a pydantic `ValidationError`.
    That is not a `RequivoError`, so it misses the viewmodel's `SessionNotFoundError` catch *and*
    `create_app`'s `RequivoError` handler, and lands on the bare `Exception` handler as a 500 over
    the whole page.
    """
    (canonical_dir(slug) / "model.json").write_text('{"summary": {"objec', encoding="utf-8")


BREAKERS = {BROKEN_META: break_meta, BROKEN_REQUEST: break_request, BROKEN_MODEL: break_model}


@pytest.fixture
def mixed_workspace():
    """Two healthy sessions and three broken ones, each broken a different way.

    The healthy pair is not decoration. It is the must-fire control: without it every assertion in
    this file is satisfied by an implementation that degrades every row it is handed.
    """
    _seed(HEALTHY_ANALYSED, analysed=True)
    _seed(HEALTHY_AWAITING, analysed=False)
    for slug, breaker in BREAKERS.items():
        _seed(slug, analysed=True)
        breaker(slug)
    return SessionService()


# ── the breakers really break something ───────────────────────────────────────

@pytest.mark.parametrize("slug", sorted(BREAKERS))
def test_each_breaker_defeats_the_strict_read(slug):
    """Must fire. `list_sessions()` is the strict read and is *supposed* to raise here.

    Without this, a breaker that silently stopped breaking anything — a renamed file, a format
    version this build grew to accept — would turn every degradation test in this module green while
    proving nothing at all.
    """
    _seed(slug, analysed=True)
    BREAKERS[slug](slug)
    svc = SessionService()
    with pytest.raises(Exception) as ei:      # noqa: PT011 - the type is the platform's, not ours
        svc.list_sessions()
        svc.request_text(slug)
        svc.status(slug)
    assert ei.value is not None


# ── the service: the source of the rows degrades per member ───────────────────

def test_list_entries_degrades_only_the_unreadable_member(mixed_workspace):
    svc = mixed_workspace
    entries = {e.slug: e for e in svc.list_entries()}

    # must fire: the healthy pair is readable and carries real metadata
    assert entries[HEALTHY_ANALYSED].readable and entries[HEALTHY_ANALYSED].meta is not None
    assert entries[HEALTHY_AWAITING].readable and entries[HEALTHY_AWAITING].meta is not None

    # the member whose own metadata will not load is reported, not raised
    assert not entries[BROKEN_META].readable
    assert entries[BROKEN_META].meta is None
    assert entries[BROKEN_META].error                      # the reason is stated, not dropped
    assert entries[BROKEN_META].slug == BROKEN_META        # …and it names which session

    # a member broken *below* the metadata is still readable at this layer — the row-level guard is
    # what covers those, and conflating the two would hide which layer failed
    assert entries[BROKEN_REQUEST].readable
    assert entries[BROKEN_MODEL].readable


def test_list_entries_is_a_complete_census(mixed_workspace):
    """Every slug on disk gets an entry. A degrading listing that *drops* the broken member is the
    same absence one step quieter: the reader is told nothing is wrong, and the session is gone."""
    assert {e.slug for e in mixed_workspace.list_entries()} == set(BREAKERS) | {
        HEALTHY_ANALYSED, HEALTHY_AWAITING}


def test_a_healthy_workspace_has_no_degraded_entries():
    """The clean-path control at the service layer: no session is reported unreadable when none is."""
    _seed(HEALTHY_ANALYSED)
    _seed(HEALTHY_AWAITING, analysed=False)
    entries = SessionService().list_entries()
    assert len(entries) == 2
    assert all(e.readable and e.error is None for e in entries)


# ── the home page: one bad session cannot take the list down ──────────────────

def test_the_home_page_renders_every_row_when_three_are_broken(client, mixed_workspace):
    r = client.get("/")
    assert r.status_code == 200, r.text[:400]

    # must fire — the healthy rows are *fully* rendered, not degraded alongside the broken ones
    assert f"/sessions/{HEALTHY_ANALYSED}" in r.text
    assert f"A request about {HEALTHY_ANALYSED}" in r.text
    assert "Awaiting analysis" in r.text                  # the revision-0 row kept its own state

    # every broken session is on the page, named
    for slug in BREAKERS:
        assert slug in r.text, f"{slug} vanished from the listing"


@pytest.mark.parametrize("slug", sorted(BREAKERS))
def test_one_break_mode_at_a_time_still_leaves_the_healthy_row(client, slug):
    """The test that separates a real fix from a partial one.

    A guard around `status()` alone passes for `broken-model` and fails for the other two. Running
    the three together would let a two-thirds fix look like a bug in the fixture; running them apart
    names which layer is unguarded.
    """
    _seed(HEALTHY_ANALYSED, analysed=True)
    _seed(slug, analysed=True)
    BREAKERS[slug](slug)

    r = client.get("/")
    assert r.status_code == 200, f"{slug} took the whole page down: {r.text[:400]}"
    assert f"A request about {HEALTHY_ANALYSED}" in r.text   # must fire: the healthy row is intact
    assert slug in r.text                                     # …and the broken one names itself


@pytest.mark.parametrize("slug", sorted(BREAKERS))
def test_a_degraded_row_says_it_could_not_be_read_and_names_the_slug(client, slug):
    """Neither surface named the failing session before this. A user with one bad session could see
    that *something* was wrong and had no way to learn which — which is most of the cost."""
    _seed(slug, analysed=True)
    BREAKERS[slug](slug)

    r = client.get("/")
    assert r.status_code == 200
    row = [line for line in r.text.splitlines() if slug in line]
    assert row, f"{slug} is not on the page at all"
    assert "could not be read" in r.text.lower()


def test_a_degraded_row_is_not_dressed_as_an_ordinary_state(client):
    """`unreadable` must not render as `awaiting`, `ready` or `in progress`.

    This is the third state, and the whole argument for it: *we could not look* has to read
    differently from *we looked and it is early*, or the reader is confidently misinformed about a
    session nobody managed to open.
    """
    _seed(HEALTHY_AWAITING, analysed=False)
    _seed(BROKEN_MODEL, analysed=True)
    break_model(BROKEN_MODEL)

    from requivo.web.viewmodels.sessions import session_list
    rows = {r["slug"]: r for r in session_list(SessionService())}

    assert rows[HEALTHY_AWAITING]["state"] == "awaiting"          # must fire
    assert rows[BROKEN_MODEL]["state"] == "unreadable"
    assert rows[BROKEN_MODEL]["state"] != rows[HEALTHY_AWAITING]["state"]
    assert rows[BROKEN_MODEL]["error"]


def test_a_degraded_row_states_no_facts_it_could_not_read(client):
    """A row we could not read must not claim a timestamp, a question count or a freshness verdict.

    `0 open questions` and `updated just now` are both *answers*, and we have none — inventing a
    plausible one is the quiet-wrong-answer form of the same bug.
    """
    _seed(BROKEN_META, analysed=True)
    break_meta(BROKEN_META)

    from requivo.web.viewmodels.sessions import session_list
    row = session_list(SessionService())[0]
    assert row["slug"] == BROKEN_META
    assert row["updated_at"] == ""            # not a fabricated timestamp
    assert row["open_questions"] is None      # not 0 — we did not count, we could not
    assert row["needs_update"] is False       # no artifact claim either way


def test_the_metadata_failure_is_reported_as_the_error_it_was(mixed_workspace):
    """The degraded entry keeps the reason. `InvalidSessionError` says *newer format* here; folding
    every failure into a bare 'unreadable' with no text loses the one line that tells a user to
    upgrade rather than to delete the session."""
    entry = next(e for e in mixed_workspace.list_entries() if e.slug == BROKEN_META)
    assert "format" in entry.error.lower()
    with pytest.raises(InvalidSessionError):
        SessionService().meta(BROKEN_META)     # must fire: the strict read still refuses
