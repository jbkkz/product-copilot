"""The sdist ships exactly what it means to ship -- never a half-collectable `tests/` tree (#431).

Filed from the 2026-09 readiness audit (packaging pass, verified against the actual PyPI artifact):
`requivo-3.0.0.tar.gz` shipped `tests/` (66 .py files) but not the underscore helpers
(`_fakes.py`, `_cli_harness.py`, `_scan.py`, `_credentials.py`, `conftest.py`), not `tests/web/`, not
the root `conftest.py`, not `scripts/`, not `fixtures/` -- so `pytest --co` against the extracted
sdist died at collection with `ModuleNotFoundError: No module named '_fakes'`, and even past that the
self-scanning guard tests read `workflows/`, plugin dirs and `fixtures/golden` the sdist never
carried at all.

The decision (see the direction section of #431, and `docs/compatibility.md`): exclude `tests/`
entirely rather than ship a complete second copy of the suite's fixtures/scripts/harnesses. The
guards' subjects are the repo, not the package -- distro packagers verify the wheel via the
wheel-install CI leg and the publish gates, not by re-running this suite inside the extracted sdist.
`MANIFEST.in`'s `prune tests` is the mechanism; this file is the proof it actually does that, built
the same way `python -m build --sdist` would (`setuptools.build_meta.build_sdist`, in-process, no
subprocess, no network -- `setuptools` is a `dev`-extra-only dependency purely for this test).

Would this pass if #431 did nothing? No: before `MANIFEST.in` existed, a real sdist build carried 70
files under `tests/` and this file's own assertion (`test_the_sdist_ships_no_tests_directory_at_all`)
was red against it.
"""
from __future__ import annotations

import tarfile
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _build_sdist(dest: Path) -> Path:
    """Build a real sdist in-process, exactly as `python -m build --sdist --no-isolation` would --
    no subprocess, no isolated venv, no network. Skips (not fails) when `setuptools` is not
    installed, since it is a `dev`-extra addition made for this test alone and not a runtime
    dependency of the package itself."""
    pytest.importorskip("setuptools", reason="setuptools is a dev-only addition for this test")
    import contextlib
    import io
    import os

    from setuptools.build_meta import build_sdist

    cwd = os.getcwd()
    os.chdir(REPO_ROOT)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            name = build_sdist(str(dest))
    finally:
        os.chdir(cwd)
    return dest / name


@pytest.fixture(scope="module")
def sdist_members() -> list[str]:
    with tempfile.TemporaryDirectory() as td:
        archive = _build_sdist(Path(td))
        with tarfile.open(archive) as tf:
            return tf.getnames()


def test_the_sdist_ships_no_tests_directory_at_all(sdist_members):
    """The chosen story is (a): exclude `tests/` entirely. Half-shipped -- some files but not the
    helpers they import -- is the one wrong answer #431 was filed about, so the assertion is not
    "the helpers are present too" (option b) but "there is no tests/ tree to be half of"."""
    offenders = [m for m in sdist_members if "/tests/" in m or m.rstrip("/").endswith("/tests")]
    assert offenders == [], f"the sdist still carries tests/ content: {offenders[:10]}"


def test_the_sdist_still_ships_the_package_and_its_assets(sdist_members):
    """A positive control for the assertion above (CLAUDE.md's own rule: a 'must not fire' case
    needs a paired 'must fire' case) -- an empty member list would pass the exclusion check for the
    wrong reason (nothing built, not nothing-to-prune)."""
    assert any(m.endswith("src/requivo/__init__.py") for m in sdist_members)
    assert any(m.endswith("src/requivo/py.typed") for m in sdist_members)
    assert any("src/requivo/assets/prompts/engine.md" in m for m in sdist_members)


def test_manifest_in_declares_the_prune():
    text = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "prune tests" in text, "MANIFEST.in no longer excludes tests/ -- #431 regressed"
