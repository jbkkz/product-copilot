"""The shared harness for the deterministic-CLI test modules split out of `test_cli_deterministic.py`
(#141).

Every command those modules drive must run with no LLM and no API key. Output is captured through
`app()` (the real entry point) against a temp workspace; a `--json` variant is asserted where the spec
fixes a machine format, so Claude Code can rely on it.

Not a test module and not a `conftest.py`, for the reasons `tests/_fakes.py` sets out at length: the
name starts with `_` and not with `test_`, so pytest never collects it looking for tests, and a
fixture declared in a root `conftest.py` would apply to every file under `tests/` rather than to the
seven that ask for it.

What is *not* here is the `workspace` fixture, and the reason is worth a line because sharing it was
tried first. `_fakes.py` says fixtures stay local to each module and values are what get shared; a
fixture imported into a module is also a module-level name that every test taking it as a parameter
then shadows, so ruff answered the design question directly — 68 × F811, one per test signature.
Each module below therefore carries its own two-line `workspace`, which is what `tests/test_sessions.py`
already does.
"""
from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stdout

from requivo.cli import app
from requivo.core import persistence as store
from requivo.core.contracts import _schema_order, schema_slot_ids


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
        # client=None is "build the default client", NOT a poison pill — with a credential it
        # would call out and bill (#419). The autouse net in `tests/conftest.py` is what makes an
        # accidental provider path refuse cleanly instead, on every machine and not only keyless CI.
        app(argv, client=None)
    return buf.getvalue()


def _run_json(argv):
    return json.loads(_run(argv))


_SESSIONS_ROW = re.compile(r"^  [✅❌🟡] sessions\b")


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


def _run_stdin(argv, text, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    return _run(argv)
