"""#419: the suite's hermeticity is a guarantee of `tests/conftest.py`, not a property of the
machine it runs on.

Before the net existed, "no API calls, no network" was true exactly where no credential was
resolvable: `cli.py` loaded the repo's `.env` at import time, `client=None` meant "build the
default client", and `test_the_real_session_is_still_reachable_by_its_own_slug[answer]` made a
real paid Anthropic call and then went red — on every keyed machine, green in keyless CI, ~$0.07
per full-suite run. These tests are the must-fire pair the net's docstring names, plus the two
halves of the `.env` contract the fix moved.
"""
import os
import subprocess
import sys
from pathlib import Path

from _credentials import _CREDENTIAL_ENV, SINKHOLE_BASE_URL

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_no_ambient_credential_reaches_a_test():
    """The probe: inside a test body, no credential variable survives and the wire points at the
    sinkhole. On a keyless machine this passes vacuously — `test_the_net_fires_when_a_credential_is_ambient`
    is the half that makes it fire everywhere."""
    for var in _CREDENTIAL_ENV:
        assert var not in os.environ, (
            f"{var} survived into a test body — the autouse net in tests/conftest.py is not running"
        )
    assert os.environ.get("ANTHROPIC_BASE_URL") == SINKHOLE_BASE_URL, (
        "an escaped provider call would reach the real API instead of dying on the sinkhole"
    )


def test_the_net_fires_when_a_credential_is_ambient():
    """The must-fire half: run the probe in a child pytest whose environment carries a planted key,
    the exact shape of the developer machine #419 billed. Red if the autouse net is removed —
    which is what makes the probe above more than a keyless-CI tautology."""
    env = dict(os.environ)
    env["ANTHROPIC_API_KEY"] = "sk-test-ambient-should-never-survive"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/test_suite_hermeticity.py::test_no_ambient_credential_reaches_a_test"],
        cwd=_REPO_ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert proc.returncode == 0, (
        "the probe failed under a planted ambient key — the net no longer scrubs:\n"
        + proc.stdout + proc.stderr
    )


def test_importing_the_cli_leaves_the_environment_alone(tmp_path):
    """#419's first mechanism, closed: importing `requivo.cli` from a directory holding a `.env`
    must not load it. A canary lands in `os.environ` only when `app()` runs (the test below)."""
    (tmp_path / ".env").write_text("REQUIVO_HERMETICITY_CANARY=from-dotenv\n", encoding="utf-8")
    script = (
        "import os, sys\n"
        "import requivo.cli\n"
        "sys.exit(1 if 'REQUIVO_HERMETICITY_CANARY' in os.environ else 0)\n"
    )
    proc = _run_in(tmp_path, script)
    assert proc.returncode == 0, (
        "importing requivo.cli loaded the cwd's .env into os.environ:\n" + proc.stdout + proc.stderr
    )


def test_a_verb_still_reads_the_dotenv_file(tmp_path):
    """The contract's other half, unchanged for every CLI user: `app()` itself still honours a
    `.env` in the directory the command runs from. In-process suite runs never see this — the net
    no-ops `load_dotenv` there precisely so the developer's real key cannot come back mid-test —
    so a subprocess, owning its own environment, is where the promise is checked."""
    (tmp_path / ".env").write_text("REQUIVO_HERMETICITY_CANARY=from-dotenv\n", encoding="utf-8")
    script = (
        "import io, os, sys\n"
        "from contextlib import redirect_stdout\n"
        "import requivo.cli\n"
        "buf = io.StringIO()\n"
        "with redirect_stdout(buf):\n"
        "    requivo.cli.app(['schema'])\n"
        "sys.exit(0 if os.environ.get('REQUIVO_HERMETICITY_CANARY') == 'from-dotenv' else 1)\n"
    )
    proc = _run_in(tmp_path, script)
    assert proc.returncode == 0, (
        "app() no longer reads the cwd's .env — the move out of import time went too far:\n"
        + proc.stdout + proc.stderr
    )


def _run_in(cwd, script):
    """A child interpreter running this checkout's `requivo`, wherever the venv's install points.

    `PYTHONPATH` pins the import to this tree's `src/` — in a worktree the venv's editable install
    resolves to the main checkout, which is exactly the wrong tree to assert about."""
    env = {k: v for k, v in os.environ.items() if k != "REQUIVO_HERMETICITY_CANARY"}
    env["PYTHONPATH"] = str(_REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
