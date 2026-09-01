"""Suite-wide guarantees — what every test gets without asking, and deliberately nothing else.

`tests/_fakes.py` records why this file did not exist: an autouse *workspace* fixture at the root
would silently change what every unrelated test runs against, so workspace isolation stays local to
each file. That reasoning survives this file, because it is about fixtures that alter test
semantics — and the net below alters the run of no correct test: no test may depend on the
developer's own credential, and a test that wants one sets its own after the scrub (a per-test
`monkeypatch` layers over an autouse one).

#419 is the incident that funds it: `cli.py` loaded the repo's `.env` at import time, `client=None`
meant "build the default client", and one journey test made a real paid Anthropic call on every
machine with a resolvable credential — red locally, green in keyless CI, billed either way. The
sentence this file makes structural was prose before: "the whole suite: no API calls, no network".

Three layers, because #419 measured what one layer costs. The tests that exercise the SDK's own
discovery chain (profile/federation, in `test_provider.py` and `test_cli_doctor.py`) are untouched:
the net clears the *environment* and re-routes the *wire*, but leaves `default_credentials` real —
those tests re-set their own sources on top and keep asserting against the SDK, not a stub.

Must-fire pair in `tests/test_suite_hermeticity.py`:
`test_no_ambient_credential_reaches_a_test` (the probe) and
`test_the_net_fires_when_a_credential_is_ambient` (the probe re-run under a planted key).
"""
import pytest
from _credentials import _CREDENTIAL_ENV, SINKHOLE_BASE_URL


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    # Layer 1: no credential variable survives into a test, whatever shell ran the suite.
    for var in _CREDENTIAL_ENV:
        monkeypatch.delenv(var, raising=False)
    # Layer 2: `app()` reads `.env` per run (#419 moved it out of import time) — a no-op here, or
    # every in-process CLI test running from the repo root would put the developer's real key
    # straight back after layer 1. The contract that `app()` *does* load `.env` is covered where a
    # subprocess owns its own environment: `test_a_verb_still_reads_the_dotenv_file`.
    monkeypatch.setattr("requivo.cli.load_dotenv", lambda *a, **kw: False, raising=False)
    # Layer 3: a call that still escapes — an on-disk profile resolves without a single variable
    # set, and a future path may hand the SDK a key some other way — dies on an unroutable loopback
    # port in milliseconds, unpaid, instead of reaching Anthropic.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", SINKHOLE_BASE_URL)
