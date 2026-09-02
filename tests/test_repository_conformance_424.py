"""The repository conformance suite is genuinely a public, wheel-shipped, out-of-repo-runnable
artifact -- not merely a class that happens to sit under `src/` (#424).

CLAUDE.md claims "a Postgres repository reuses [the service orchestration] verbatim", and the proof
used to be real but private: `InMemorySessionRepository` inside `tests/test_sessions.py`, runnable
by nothing outside this repository. This file pins the three shapes that make the extracted suite
(`requivo.testing.repository_conformance.SessionRepositoryConformance`) actually usable by an
external implementer, none of which `tests/test_sessions.py`'s own use of the suite (as a subclass)
can itself prove:

- it ships in the built wheel, not only in this checkout;
- the base class is importable and not collected as a test on its own (no `Test*` name -- an
  external suite that merely imports it must not pick up phantom failures for a class with no
  `make_repository()`);
- both implementations this repo carries (`FileSessionRepository`, the in-memory fake) are wired to
  it as subclasses, which is the "shrinks to the factory wiring" acceptance criterion.

Would this pass if #424 did nothing? No: before this change there was no `requivo.testing` package
at all, so every assertion below raises `ModuleNotFoundError` or `ImportError` at collection.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_the_suite_is_importable_and_not_collected_as_a_test_on_its_own():
    from requivo.testing import SessionRepositoryConformance
    from requivo.testing.repository_conformance import SessionRepositoryConformance as direct

    assert SessionRepositoryConformance is direct
    # pytest's default `python_classes = Test*` -- a bare import of this module must add no tests.
    assert not SessionRepositoryConformance.__name__.startswith("Test")
    with pytest.raises(NotImplementedError):
        SessionRepositoryConformance().make_repository()


def test_full_model_is_re_exported_and_documented():
    """A reviewer finding (#424): `full_model` was importable from the submodule and used by the
    suite's own test methods, but the package's `__init__.py` re-exported only the base class and
    neither `docs/compatibility.md` nor this file's own SEAM-shaped guards mentioned it -- an
    out-of-repo subclass following the submodule's own docstring advice ("so an out-of-repo subclass
    of this suite can call it too") was relying on a name nothing pinned. Fixed alongside this test:
    `requivo.testing.__init__`'s `__all__` and import now both name it."""
    from requivo.testing import SessionRepositoryConformance, full_model
    from requivo.testing.repository_conformance import full_model as direct

    assert full_model is direct
    model = full_model()
    assert model.summary.objective
    assert SessionRepositoryConformance  # both names exercised in one test, deliberately

    text = (REPO_ROOT / "docs" / "compatibility.md").read_text(encoding="utf-8")
    assert "full_model" in text, "full_model is exported but not named in the declared seam"


def test_both_shipped_implementations_are_wired_to_the_suite():
    # Bare module name, not `tests.test_sessions` -- there is no `tests/__init__.py`, so pytest's own
    # rootdir import mode puts `tests/` directly on `sys.path` (the same reason `_fakes` is imported
    # by bare name throughout this suite rather than as `tests._fakes`).
    from test_sessions import TestFileRepositoryConformance, TestInMemoryRepositoryConformance

    from requivo.testing.repository_conformance import SessionRepositoryConformance

    assert issubclass(TestInMemoryRepositoryConformance, SessionRepositoryConformance)
    assert issubclass(TestFileRepositoryConformance, SessionRepositoryConformance)


def test_the_suite_ships_in_the_built_wheel():
    pytest.importorskip("setuptools", reason="setuptools is a dev-only addition (see #431)")
    import contextlib
    import io
    import os
    import tempfile

    from setuptools.build_meta import build_wheel

    cwd = os.getcwd()
    os.chdir(REPO_ROOT)
    try:
        with tempfile.TemporaryDirectory() as td:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                name = build_wheel(td)
            with zipfile.ZipFile(Path(td) / name) as zf:
                members = zf.namelist()
    finally:
        os.chdir(cwd)

    assert "requivo/testing/__init__.py" in members
    assert "requivo/testing/repository_conformance.py" in members


def test_compatibility_md_declares_the_suite():
    text = (REPO_ROOT / "docs" / "compatibility.md").read_text(encoding="utf-8")
    assert "SessionRepositoryConformance" in text
    assert "requivo[testing]" in text


def test_the_testing_extra_is_declared():
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'testing = ["pytest' in text, (
        "no [project.optional-dependencies] testing extra -- requivo.testing needs pytest to be "
        "usable, and the base install deliberately does not carry it"
    )
