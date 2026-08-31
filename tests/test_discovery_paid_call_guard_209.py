"""#209: guard paid first-discovery calls against cross-tab / refresh double-submission, server-side.

Driven directly against `DiscoveryService`, not the Web -- the guard lives in the service layer
(invariant 14), so a caller reaching past every surface still gets it. The contending holder opens its
own file descriptor on the guard's own lock file rather than racing a second thread -- `flock` is
scoped to the *open file description*, not the thread or the process, so a second `os.open` in this
same test process contends for real without needing real concurrency to prove it (the same technique
`test_persistence_guards.py::test_a_contended_lock_raises_within_the_deadline_instead_of_hanging` uses
for `session_lock` itself).
"""

from __future__ import annotations

import os

import pytest
from _fakes import out, slot

from requivo.core.errors import SessionLockedError
from requivo.core.persistence import ensure_store_dir
from requivo.services.discovery import DiscoveryService, _discovery_guard_path, fcntl
from requivo.services.sessions import SessionService


@pytest.fixture(autouse=True)
def _isolate_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))


class _CountingProvider:
    """A stub `ReasoningProvider` that just counts how many times it was actually called."""

    name = "stub"

    def __init__(self):
        self.calls = 0

    def analyze(self, request, *, current_model=None, answers=None, only=None, reuse_system=False):
        self.calls += 1
        return out({"problem": slot(80, "explicit", "high")})

    def generate(self, *a, **k):  # pragma: no cover - unused by run_discovery
        raise NotImplementedError

    def model_name(self):
        return "stub-model"

    def provenance(self, op, *, only=None):
        return {"provider": self.name, "model_name": self.model_name(), "surface": "test"}


@pytest.mark.skipif(fcntl is None, reason="POSIX-only branch: fcntl.flock has no Windows equivalent "
                    "here, and the msvcrt branch takes the same non-blocking path. "
                    "REASONED, NOT OBSERVED on Windows -- see #209.")
def test_a_concurrent_first_discovery_is_refused_before_any_provider_call():
    sessions = SessionService()
    slug = sessions.create_session("a leave approval system").slug

    guard_path = _discovery_guard_path(slug)
    ensure_store_dir(guard_path.parent)
    holder_fd = os.open(guard_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX)
    try:
        provider = _CountingProvider()
        disco = DiscoveryService(provider=provider, sessions=sessions)
        with pytest.raises(SessionLockedError) as exc_info:
            disco.run_discovery(slug, surface="test")
        assert exc_info.value.code == "session_locked"
        assert slug in str(exc_info.value)
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)

    # The loser made zero provider calls, and nothing was written -- the whole point of #209.
    assert provider.calls == 0
    meta = sessions.repo.read_meta(slug)
    assert meta.current_revision == 0


def test_run_discovery_still_succeeds_once_the_guard_is_free():
    """Must-fire control: without it, a guard that refused *everything* would also pass the test
    above, telling us nothing about whether an uncontended caller can still proceed."""
    sessions = SessionService()
    slug = sessions.create_session("a leave approval system").slug
    provider = _CountingProvider()
    disco = DiscoveryService(provider=provider, sessions=sessions)

    disco.run_discovery(slug, surface="test")

    assert provider.calls == 1
    meta = sessions.repo.read_meta(slug)
    assert meta.current_revision == 1


@pytest.mark.skipif(fcntl is None, reason="POSIX-only branch: fcntl.flock has no Windows equivalent "
                    "here, and the msvcrt branch takes the same non-blocking path. "
                    "REASONED, NOT OBSERVED on Windows -- see #209.")
def test_start_is_guarded_the_same_way_as_run_discovery():
    """`start()` (the direct-request entry point `POST /sessions` uses when it discovers straight
    away) is the other first-discovery door #209 names -- guarded on the *derived* slug, since a
    caller of `start()` may not have named one."""
    sessions = SessionService()
    provider = _CountingProvider()
    disco = DiscoveryService(provider=provider, sessions=sessions)
    slug = sessions.slug_hint("a leave approval system")
    # `claim_session` derives the same slug `start()` will use for this request -- reproduced here
    # only to find the guard file `start()` itself will contend on.
    meta = disco.claim_session("a leave approval system", cards=None, slug=None)
    assert meta.slug == slug or meta.slug.startswith(slug)

    guard_path = _discovery_guard_path(meta.slug)
    ensure_store_dir(guard_path.parent)
    holder_fd = os.open(guard_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(SessionLockedError):
            disco.start("a leave approval system", slug=meta.slug)
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)

    assert provider.calls == 0
    assert sessions.repo.read_meta(meta.slug).current_revision == 0
