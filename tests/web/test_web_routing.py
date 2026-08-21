"""Requivo Web routing: the routes that answer, and the status each error reaches the browser as.

Split out of `test_web.py` by #142, along the five subjects that file's own docstring named. Offline
(a fake provider), isolated workspace per test; the fixtures and the seeded-session helper live in
`tests/web/conftest.py`.

Two subjects in one file because they are halves of one question. A route is only as good as the
status it answers with, so the error-code to HTTP-status contract (#34) sits beside the pages it is a
contract about.
"""

from __future__ import annotations

import pytest

from requivo.cli import app as cli_app
from tests.web.conftest import HIGH_EXPLICIT, HIGH_INFERRED, _make_session

# ── packaging / smoke ─────────────────────────────────────────────────────────

def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_web_help_exits_cleanly():
    with pytest.raises(SystemExit) as ei:
        cli_app(["web", "--help"])
    assert ei.value.code == 0


def test_static_assets_are_served_locally(client):
    # HTMX is vendored — served from the package, never a CDN.
    assert client.get("/static/vendor/htmx.min.js").status_code == 200
    assert client.get("/static/css/app.css").status_code == 200
    assert client.get("/static/js/app.js").status_code == 200


# ── app / pages ───────────────────────────────────────────────────────────────

def test_home_without_sessions(client):
    r = client.get("/")
    assert r.status_code == 200 and "Nothing here yet" in r.text


def test_home_lists_an_existing_session(client):
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    r = client.get("/")
    assert r.status_code == 200 and "leave-approval" in r.text


def test_session_page_renders_understanding(client):
    _make_session("leave-approval", problem=HIGH_EXPLICIT, business_rules=HIGH_INFERRED)
    r = client.get("/sessions/leave-approval")
    assert r.status_code == 200
    assert "What Requivo understood" in r.text and "Are we ready?" in r.text


def test_missing_session_is_404(client):
    r = client.get("/sessions/does-not-exist")
    assert r.status_code == 404 and "Not found" in r.text


def test_export_returns_model_json(client):
    _make_session("leave-approval", problem=HIGH_EXPLICIT)
    r = client.get("/sessions/leave-approval/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert "leave-approval.model.json" in r.headers["content-disposition"]
    assert "problem" in r.json()["model"]


# ── the error-code → HTTP status contract (#34) ───────────────────────────────
#
# `_STATUS_BY_CODE` used to fall back to 400 for anything unlisted, so a code added in one lane got a
# plausible-but-wrong status rather than an obvious gap: `context_unreadable` — a permissions fault on
# the install's own card directory, entirely the operator's environment — reached the browser as
# "HTTP 400: your request was bad". The reader was told they did something wrong; the server could not
# read its own assets. The missing row is the symptom, the silent default is the defect, so the guard
# below is a completeness test rather than one more row.


def _all_error_codes() -> dict[str, str]:
    """Every `RequivoError` code the package can raise, mapped to the class carrying it.

    Discovered by walking subclasses after importing every module, not by reading one file: three of
    them live outside `core/errors.py` (`web.security`, `services.artifacts`, `providers.anthropic`),
    and two of those are already in the status table — so "every code in core/errors.py" would be the
    wrong scan set in both directions.
    """
    import importlib
    import pkgutil

    import requivo
    from requivo.core.errors import RequivoError

    unimportable = {}
    for m in pkgutil.walk_packages(requivo.__path__, "requivo."):
        try:
            importlib.import_module(m.name)
        except Exception as e:  # noqa: BLE001
            unimportable[m.name] = f"{type(e).__name__}: {e}"
    # A module that would not import is a hole in the scan set, and a hole here is invisible: the
    # test would pass by not looking. `tests/test_boundaries.py` makes the same point about an empty
    # glob — `assert not []` is an all-clear nobody earned.
    assert not unimportable, f"scan set incomplete, cannot speak for these codes: {unimportable}"

    found = {RequivoError.code: "requivo.core.errors.RequivoError"}

    def walk(cls):
        for sub in cls.__subclasses__():
            found[sub.code] = f"{sub.__module__}.{sub.__qualname__}"
            walk(sub)

    walk(RequivoError)
    return found


# Codes deliberately absent from `_STATUS_BY_CODE`. An entry here is a claim that the code cannot
# reach the table, and the reason is the part a reviewer checks — an allowlist without one is just a
# quieter version of the default this test exists to remove.
_NOT_STATUS_MAPPED = {
    "provider_unavailable":
        "EngineError is classified by the isinstance branch in the handler, ahead of the table, so a "
        "row here would never be read. Pinned by test_a_provider_transport_failure_is_still_502.",
    "requivo_error":
        "the abstract base. Nothing raises it directly; a bare one reaching the handler is a defect, "
        "and the unclassified default is what surfaces it rather than dressing it as the caller's.",
}


def test_every_error_code_has_an_explicit_http_status():
    """The compounding half of #34: a new code is a red leg here, not a wrong answer to a user.

    The table's default was 400, so the only thing standing between a newly added code and "your
    request was bad" was somebody remembering a file in another lane. Four codes were sitting on that
    default when this was written — `context_unreadable`, `session_exists`, `session_locked` and
    `provider_output_invalid` — and only the first had been filed.
    """
    from requivo.web.app import _STATUS_BY_CODE

    codes = _all_error_codes()
    # must fire: the walk really found the vocabulary, so the assertions below mean something
    assert len(codes) >= 15, f"scan set looks blind: {sorted(codes)}"
    assert {"session_not_found", "invalid_slug", "cross_site_request"} <= set(codes)

    unclassified = sorted(set(codes) - set(_STATUS_BY_CODE) - set(_NOT_STATUS_MAPPED))
    assert not unclassified, (
        "these error codes have no explicit HTTP status and would fall through to the unclassified "
        f"default: { {c: codes[c] for c in unclassified} }")

    both = sorted(set(_STATUS_BY_CODE) & set(_NOT_STATUS_MAPPED))
    assert not both, f"claimed unreachable by the table and also listed in it: {both}"

    dead = sorted(set(_STATUS_BY_CODE) - set(codes))
    assert not dead, f"status rows for codes nothing raises any more: {dead}"

    stale = sorted(set(_NOT_STATUS_MAPPED) - set(codes))
    assert not stale, f"allowlisted codes that no longer exist: {stale}"


@pytest.mark.parametrize("code, status, why", [
    ("context_unreadable", 500,
     "the server cannot read its own card directory — the operator's environment, not the request"),
    ("no_context_cards", 500,
     "the install shipped no context cards; nothing the caller sent could have avoided it"),
    ("provider_output_invalid", 502,
     "the upstream model would not hold the contract after every retry — same family as a transport "
     "failure, which is already 502"),
    ("session_exists", 409,
     "a conflict with the current state of the store, like revision_conflict and stale_artifact"),
    ("session_locked", 503,
     "nothing raced to a conclusion; the write never started and retrying it unchanged is correct"),
    ("empty_selector_token", 400, "a stray comma in what the caller typed"),
    ("empty_selection", 400, "a selection the caller supplied that selects nothing"),
    ("invalid_filename", 400, "a path target the caller supplied"),
    # #101. The archive-shape refusals sit between `unreadable_archive` and `inconsistent_archive` on
    # one code path, and all three answer the same question the same way: the caller handed us this
    # archive. A 5xx here would say the store is broken when the store has not been touched — nothing
    # is written until the archive has passed. 409 would be wrong for the opposite reason: nothing in
    # the store conflicts with anything, and re-sending the same zip unchanged can never succeed.
    ("invalid_archive", 400, "the caller handed us this archive — the same answer its two siblings "
                             "on the import path already give"),
])
def test_a_server_side_fault_is_not_reported_as_the_users_bad_request(code, status, why):
    """The five decisions this change makes, each pinned with its reason, plus the three the issue
    confirms were already right at 400. Asserted as a table so a future edit has to argue with the
    sentence rather than only with the number."""
    from requivo.web.app import _STATUS_BY_CODE
    assert _STATUS_BY_CODE[code] == status, why


def test_an_unclassified_code_is_a_server_fault_not_a_bad_request():
    """The default itself. With every known code explicitly mapped, the fallback only fires for a
    code this version has never heard of — and "we do not know what this is" is not evidence the
    caller erred. It used to be 400, which is the misreport in #34 generalised."""
    from requivo.core.errors import RequivoError, SessionNotFoundError
    from requivo.web.app import _UNCLASSIFIED_STATUS, _status_for

    class _FromTheFuture(RequivoError):
        code = "a_code_this_version_has_never_heard_of"

    assert _UNCLASSIFIED_STATUS >= 500
    assert _status_for(_FromTheFuture("...")) == _UNCLASSIFIED_STATUS
    # must fire: a code it *has* heard of is still classified from the table, not defaulted
    assert _status_for(SessionNotFoundError("...")) == 404


def test_a_provider_transport_failure_is_still_502():
    """The one allowlist entry that is about ordering: EngineError is classified by isinstance ahead
    of the table, so `provider_unavailable` deliberately has no row. If that branch were ever
    removed, this fails rather than the code quietly picking up the unclassified default."""
    from requivo.providers.anthropic import EngineError
    from requivo.web.app import _STATUS_BY_CODE, _status_for
    assert _status_for(EngineError("upstream is down")) == 502
    assert "provider_unavailable" not in _STATUS_BY_CODE


def test_context_unreadable_reaches_the_browser_as_a_server_error(client, monkeypatch):
    """End to end, through the real handler and the real templates — the mapping test above pins the
    number, this pins that the number is what a reader actually gets."""
    from requivo.core.errors import ContextUnreadableError

    # must fire: the page is fine before the fault is injected
    assert client.get("/").status_code == 200

    def _unreadable():
        raise ContextUnreadableError(
            "the context-card directory /x exists but cannot be read: denied",
            details={"directory": "/x"})

    monkeypatch.setattr("requivo.web.routes.home.available_cards", _unreadable)
    r = client.get("/")
    assert r.status_code == 500, "a permissions fault on the install's own assets is not a 400"
    assert "context_unreadable" in r.text


def test_a_taken_session_name_is_suffixed_rather_than_refused(client):
    """Why `session_exists` gets a status row but no end-to-end test — and a finding recorded where
    the next reader will look for it.

    Posting a name that is already taken by a *different* request does not raise `session_exists`:
    `create_session` falls through to a `<base>-<identity hash>` candidate, which is free, so the
    reader is redirected to a session with a name they did not choose and nothing says so. The raise
    at `services/sessions.py:163` needs *both* candidates taken with different identities, and the
    suffix is a hash of the request, so from this route that is close to unreachable.

    This pins the behaviour as it is rather than asserting it is right — the silent rename is
    reported as an adjacent finding, not fixed here, because choosing between refusing, suffixing
    loudly, and re-rendering the form is a design decision this change was not briefed to make.
    """
    first = client.post("/sessions", data={"request_text": "A leave approval request",
                                           "slug": "leave-approval", "provider": "create_only"},
                        follow_redirects=False)
    assert first.status_code == 303
    assert first.headers["location"] == "/sessions/leave-approval"

    second = client.post("/sessions", data={"request_text": "A different request entirely",
                                            "slug": "leave-approval", "provider": "create_only"},
                         follow_redirects=False)
    assert second.status_code == 303
    landed = second.headers["location"]
    assert landed.startswith("/sessions/leave-approval-"), landed
    assert landed != "/sessions/leave-approval", (
        "must fire: the second request really did get its own session, so the rename is real")
