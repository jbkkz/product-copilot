"""#396: a session already on disk under a Windows reserved device name is reachable through the Web.

`safe_slug` is the shared `Depends()` behind every `{slug}` route the Web has, and it called
`validate_slug` -- whose reserved-device-name refusal is unconditional by design (#372's creation-time
half). So one unconditional call in one dependency put the *entire* surface out of reach for such a
session: not the two write routes only, but reading the session page, downloading the model and
viewing a saved artifact, all 400 for a session `requivo session show` opens without complaint.

The fixture is built by hand rather than through `SessionService`, which must (and does, per the
control at the foot of the test) still refuse to *create* one. That is exactly what a pre-#221
directory looks like on disk today.
"""

from __future__ import annotations

import json

import pytest

from requivo.core import persistence as store
from tests.web.conftest import engine_reply


def _reserved_session_on_disk(slug: str = "con") -> None:
    """A session directory at a reserved name, written directly. `create_session` cannot build this
    and must not be able to -- see the control in the test below."""
    d = store.session_root() / slug
    (d / "revisions").mkdir(parents=True)
    (d / "artifacts").mkdir()
    (d / "request.md").write_text("A request captured before #221 shipped.", encoding="utf-8")
    (d / "session.json").write_text(json.dumps({
        "session_id": "deadbeef", "slug": slug, "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "provider": None, "model_name": None,
        "context_cards": None, "current_revision": 0, "format_version": 1,
        "revisions": [], "artifact_status": {}}), encoding="utf-8")


@pytest.mark.skipif(store.fcntl is None, reason="the fixture needs a directory literally named "
                    "'con' already on disk, which Windows refuses to create at the OS level "
                    "independent of anything Requivo does -- so a session at a reserved slug is a "
                    "state only a platform that never enforced the restriction can reach. "
                    "REASONED, NOT OBSERVED: the same platform limit the sibling #372 fixtures "
                    "carry.")
def test_a_reserved_slug_already_on_disk_is_reachable_through_the_web_read_routes(client,
                                                                                  with_provider):
    """The read/create asymmetry, both halves in one fixture (#396).

    The widening half must not be provable by simply dropping #221's refusal, so the creation form
    is driven in the same test against a reserved name nothing occupies, and must still be refused.
    Without that control this test would pass against a `safe_slug` that validated nothing at all.
    """
    _reserved_session_on_disk()

    # A read route -- the half nobody had to be told was affected, and the half that made this a
    # whole-surface outage rather than a write-path refusal.
    assert client.get("/sessions/con").status_code == 200

    # A write route reaches past the slug guard too: refining a session that already exists is a
    # read of its *name*. Driven with the offline fake so this asserts the route ran rather than
    # only that it did not 400.
    with_provider(engine_reply(converged=True))
    r = client.post("/sessions/con/discover", follow_redirects=False)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/sessions/con"
    assert store.read_meta("con").current_revision == 1

    # The second read route, asserted only now there is a model to export: at revision 0 it answers
    # 404 for want of a `model.json`, which is a true statement about the session and says nothing
    # about the slug guard -- a 200 here is the one that does.
    assert client.get("/sessions/con/export").status_code == 200

    # Must-not-fire control, same fixture: `POST /sessions` is the one route that can bring a slug
    # into existence, it takes its name from a form field rather than the path (so it never reaches
    # `safe_slug`), and it stays strict. 'nul' is used rather than 'con' precisely because nothing
    # occupies it -- which is the whole of what #372 makes the refusal conditional on.
    created = client.post("/sessions", data={"request_text": "A leave approval system.",
                                             "slug": "nul", "provider": "create_only"})
    assert created.status_code == 400
    assert "reserved Windows device name" in created.text
    assert not (store.session_root() / "nul").exists()
