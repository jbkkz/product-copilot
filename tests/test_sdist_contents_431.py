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

import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# The floor this project's own `dev` extra declares for setuptools (pyproject.toml's own comment
# there has the full story): below it, `import pkg_resources` itself crashes on Python 3.12 with
# `AttributeError: module 'pkgutil' has no attribute 'ImpImporter'` -- bisected directly against
# Python 3.12.13/3.12.14, not guessed. This is *also* checked at import time here (#453): the
# Dependency floor CI leg installs the literal declared floor via `--resolution lowest-direct`, so a
# future edit that lowers the pyproject.toml floor without updating this constant -- or a consumer
# running this suite with their own older pin -- must not turn into a bare traceback in fixture
# setup. Named and skipped instead, the same shape `tests/test_encoding.py`'s `_NO_LEVER_ON_39`
# already uses for an interpreter-precondition gap.
_MIN_SETUPTOOLS_FOR_SDIST = (66, 1, 0)


def _setuptools_too_old(min_version: tuple) -> str | None:
    """None if the installed setuptools is new enough to build here; otherwise the skip reason.

    Checked by VERSION NUMBER before ever calling into the build backend -- not by catching whatever
    exception a too-old build happens to raise. A real defect in `MANIFEST.in` or the packaging
    config must still fail loudly; only a build backend already known to be unable to run at all
    should turn into a skip, and only for exactly that reason."""
    import setuptools

    try:
        installed = tuple(int(p) for p in setuptools.__version__.split(".")[:3])
    except ValueError:
        return None  # an unparseable version string is not this check's problem to solve
    if installed >= min_version:
        return None
    return (
        f"setuptools {setuptools.__version__} is older than {'.'.join(map(str, min_version))}, the "
        f"floor pyproject.toml documents as the first release that can `import pkg_resources` at all "
        f"on this interpreter (Python {sys.version_info.major}.{sys.version_info.minor}). "
        f"UNTESTED HERE: the sdist's actual member list. Every other CI leg installs the newest "
        f"satisfying setuptools and does cover it; so does a real release build."
    )


def _build_sdist(dest: Path) -> Path:
    """Build a real sdist in-process, exactly as `python -m build --sdist --no-isolation` would --
    no subprocess, no isolated venv, no network. Skips (not fails) when `setuptools` is not
    installed, since it is a `dev`-extra addition made for this test alone and not a runtime
    dependency of the package itself -- and skips the same way when it is installed but too old to
    run at all on this interpreter (see `_setuptools_too_old` above)."""
    pytest.importorskip("setuptools", reason="setuptools is a dev-only addition for this test")
    too_old = _setuptools_too_old(_MIN_SETUPTOOLS_FOR_SDIST)
    if too_old:
        pytest.skip(too_old)
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


# -- the version-floor skip itself (#453): must fire below the floor, must not fire above it ------


def test_setuptools_too_old_fires_below_the_floor(monkeypatch):
    import setuptools

    monkeypatch.setattr(setuptools, "__version__", "64.0.0")
    reason = _setuptools_too_old((66, 1, 0))
    assert reason is not None
    assert "64.0.0" in reason
    assert "UNTESTED HERE" in reason


def test_setuptools_too_old_does_not_fire_at_or_above_the_floor(monkeypatch):
    """The must-not-fire half, paired with the test above -- a version check that always returns a
    reason would pass the first test for the wrong one."""
    import setuptools

    monkeypatch.setattr(setuptools, "__version__", "66.1.0")
    assert _setuptools_too_old((66, 1, 0)) is None
    monkeypatch.setattr(setuptools, "__version__", "84.0.0")
    assert _setuptools_too_old((66, 1, 0)) is None


def test_the_skip_path_actually_skips_rather_than_crashing(monkeypatch):
    """Not just that `_setuptools_too_old` returns a string -- that `_build_sdist` itself turns that
    string into a real `pytest.skip`, end to end, without ever reaching the `setuptools.build_meta`
    import that would crash on a genuinely too-old interpreter combination."""
    import setuptools

    monkeypatch.setattr(setuptools, "__version__", "1.0.0")
    with pytest.raises(pytest.skip.Exception) as ei:
        _build_sdist(REPO_ROOT)  # dest is never used -- the skip fires before any build call
    assert "1.0.0" in str(ei.value)
