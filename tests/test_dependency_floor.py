"""`scripts/dependency_floor.py` — the generator behind the Dependency floor CI leg (#91).

The leg installs Requivo at the oldest release every runtime dependency declares, and runs the suite
against it. That is only worth a job if the floor set is *complete*: a requirement that quietly drops
out leaves pip resolving it to the newest release while the leg still reports having tested the
floor, which is the silent absence the whole leg exists to close, reappearing inside the check for
it. So every way of losing a requirement raises here rather than returning a shorter list.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from dependency_floor import (  # noqa: E402
    RUNTIME_EXTRAS,
    UndeclaredFloor,
    _floor,
    _load_toml,
    constraints,
    runtime_requirements,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _real_pyproject() -> dict:
    return _load_toml((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


# ── reading one requirement ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("requirement, expected", [
    ("pydantic>=2.0,<3", ("pydantic", "2.0")),
    ("python-dotenv>=1.0.0,<2", ("python-dotenv", "1.0.0")),
    ("anthropic >= 0.40.0, <1", ("anthropic", "0.40.0")),
    ("tomli>=1.1.0; python_version < '3.11'", ("tomli", "1.1.0")),
])
def test_the_floor_is_read_off_the_lower_bound(requirement, expected):
    assert _floor(requirement) == expected


@pytest.mark.parametrize("requirement", ["httpx", "ruff", "some-package<2", "  "])
def test_a_requirement_with_no_lower_bound_is_refused(requirement):
    """Refused, not skipped. Skipping is how a dependency ends up outside the constraints file with
    the leg still green — the newest release installed, and a report that the floor was tested."""
    with pytest.raises(UndeclaredFloor):
        _floor(requirement)


# ── assembling the set ─────────────────────────────────────────────────────────────────────────

def test_a_named_runtime_extra_that_is_gone_is_refused():
    """`RUNTIME_EXTRAS` names the extras a user installs. If one is renamed or removed, the script
    must say so: silently covering one fewer extra is a narrower promise reported as the same one."""
    pyproject = {"project": {"dependencies": ["pydantic>=2.0"], "optional-dependencies": {}}}
    with pytest.raises(UndeclaredFloor, match="declares no"):
        runtime_requirements(pyproject)


def test_one_name_with_two_different_floors_is_refused():
    """pip would resolve the contradiction by picking one, and the leg would report a floor nobody
    declared. The manifest is saying two things about one package; that is for a person to settle."""
    pyproject = {"project": {
        "dependencies": ["jinja2>=3.1,<4"],
        "optional-dependencies": {"anthropic": ["anthropic>=0.40.0"], "web": ["jinja2>=3.0,<4"]},
    }}
    with pytest.raises(UndeclaredFloor, match="two different floors"):
        constraints(pyproject)


def test_the_same_floor_declared_twice_is_not_a_contradiction():
    """The must-not-fire half: `web` and `dev` legitimately restate the same requirement, and the
    duplicate is deduplicated rather than treated as a disagreement."""
    pyproject = {"project": {
        "dependencies": ["jinja2>=3.1,<4"],
        "optional-dependencies": {"anthropic": ["anthropic>=0.40.0"], "web": ["jinja2>=3.1,<4"]},
    }}
    assert constraints(pyproject) == ["anthropic==0.40.0", "jinja2==3.1"]


# ── against the real manifest ──────────────────────────────────────────────────────────────────

def test_the_real_manifest_yields_every_runtime_dependency():
    """Named against the manifest rather than a count, so adding a dependency does not fail this
    test while *dropping* one from the floor set still does."""
    pins = dict(line.split("==") for line in constraints(_real_pyproject()))
    for name in ("pydantic", "python-dotenv", "anthropic", "fastapi", "uvicorn", "jinja2",
                 "python-multipart"):
        assert name in pins, f"{name} is a runtime dependency and is missing from the floor set"


def test_the_dev_toolchain_is_not_floored():
    """Scope is the promise a *user's* resolver has to satisfy. Pinning pytest and ruff to their
    floors would test this project's harness, and `tomli`/`packaging` are how the floor is measured
    — flooring the measuring instrument is a leg checking itself."""
    pins = dict(line.split("==") for line in constraints(_real_pyproject()))
    for name in ("pytest", "ruff", "httpx", "tomli", "packaging"):
        assert name not in pins, f"{name} is dev tooling and must not be in the floor set"


def test_every_runtime_extra_named_here_exists_in_the_manifest():
    """The other direction of the same rule: an extra this script claims to cover but the manifest
    no longer declares is a promise about something that is not there."""
    extras = _real_pyproject()["project"]["optional-dependencies"]
    for extra in RUNTIME_EXTRAS:
        assert extra in extras, f"RUNTIME_EXTRAS names '{extra}', which pyproject.toml does not declare"


# ── the verify half ────────────────────────────────────────────────────────────────────────────

def test_verify_compares_versions_and_not_strings():
    """`fastapi==0.110` installs and reports itself as `0.110.0`. A string comparison would fail the
    leg over a difference that is not one, and the first person to hit it would delete the check
    rather than the comparison. `3.1` against `3.1.6` is a real difference and must still fail."""
    from packaging.version import Version
    assert Version("0.110") == Version("0.110.0")
    assert Version("2.0") == Version("2.0.0")
    assert Version("3.1") != Version("3.1.6")


def test_verify_reports_a_dependency_that_is_not_installed_at_all():
    """Absent is not satisfied. A name pip never had to resolve — because nothing imported it, or
    because an extra was installed by a later command without the constraints file — is exactly the
    case that leaves the leg green over nothing."""
    from dependency_floor import verify
    pyproject = {"project": {
        "dependencies": ["requivo-nonexistent-package>=9.9.9"],
        "optional-dependencies": {"anthropic": [], "web": []},
    }}
    wrong = verify(pyproject)
    assert len(wrong) == 1
    assert "not installed at all" in wrong[0]
