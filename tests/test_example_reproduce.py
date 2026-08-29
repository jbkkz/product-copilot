"""The 'Reproduce it' block of each committed example is executable, and this runs it.

Both example READMEs used to open their block with `requivo brief examples/<name>/model.json`, and
every one of the seven generator verbs raised `SessionNotFoundError` on a fresh clone: a generator
writes a revision and an artifact *back into a session*, so `_generator_service` resolves a slug and
requires it to exist, while `.requivo/` is gitignored and no such session ships. It worked only on
the maintainer's machine, where the session already existed — which is the failure mode a docs test
exists for, since the person who wrote the block is the last person able to see it (#222).

Teaching the generators to accept a bare `model.json` was the other candidate fix and was rejected:
they would have to invent a session to file the artifact against, and a session's provenance is its
request and its context cards (invariants 6 and 11). A model file carries neither, so the session
would be real and its provenance would not. The block bootstraps a session instead, offline, in two
commands — which is also an honest demonstration of what the product holds.

The guard is that the commands are read out of the READMEs rather than restated here. A restatement
drifts from the file it claims to pin the moment someone edits the file; these tests go red on the
edit itself.
"""
from __future__ import annotations

import io
import shlex
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from requivo.cli import app
from requivo.core import persistence as store

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = ("leave-approval", "event-checkin-reconciliation")


class ReachedProvider(Exception):
    """Raised in place of the API call. Neither `APIError` nor `RequivoError`, so nothing between
    here and `_complete`'s `client.messages.create` can absorb it and report something friendlier —
    the test needs to see the boundary itself, not a message about it."""


class _SentinelClient:
    def __init__(self):
        self.messages = self

    def create(self, **kwargs):
        raise ReachedProvider


def _reproduce_commands(example: str) -> list[list[str]]:
    """Every `requivo …` line in the example README's 'Reproduce it' section, in order."""
    text = (REPO / "examples" / example / "README.md").read_text(encoding="utf-8")
    section = text.split("## Reproduce it", 1)
    assert len(section) == 2, f"{example}/README.md has no 'Reproduce it' section"
    body = section[1].split("\n## ", 1)[0]
    cmds = []
    in_block = False
    for line in body.splitlines():
        if line.startswith("```"):
            in_block = line.startswith("```bash")
            continue
        if in_block and line.strip().startswith("requivo "):
            # `comments=True` strips the trailing `# what this does` the blocks align on.
            cmds.append(shlex.split(line, comments=True)[1:])
    assert cmds, f"{example}/README.md documents no commands"
    return cmds


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    # cwd stays at the repo root, because the documented commands name `examples/<name>/…` relative
    # to it — that is where a reader runs them from. Only the *store* is redirected, so the run
    # writes nothing into the clone.
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("REQUIVO_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.chdir(REPO)
    return tmp_path


@pytest.mark.parametrize("example", EXAMPLES)
def test_the_documented_reproduce_sequence_runs_on_a_fresh_workspace(example, workspace):
    """Each command in order: the offline ones complete, the paid ones reach the provider.

    Reaching the provider is the whole assertion for a generator. What #222 was — a session that
    does not exist — is refused several layers before the call, so a command that gets as far as
    `messages.create` has resolved its session, taken its snapshot and is spending money on purpose.
    """
    reached = 0
    for argv in _reproduce_commands(example):
        try:
            with redirect_stdout(io.StringIO()):
                app(argv, client=_SentinelClient())
        except ReachedProvider:
            reached += 1
        except SystemExit as e:  # pragma: no cover - only on a real failure
            pytest.fail(f"`requivo {' '.join(argv)}` exited {e.code}")
    assert reached, f"{example}'s block documents no provider-backed command"
    assert store.session_exists(example), (
        f"{example}'s block never created the session its generators are run against"
    )


@pytest.mark.parametrize("example", EXAMPLES)
def test_no_example_documents_a_generator_against_a_bare_model_file(example, workspace):
    """The regression in its own words, so a red run names the defect rather than a stack.

    The test above already fails on this shape, by raising `SessionNotFoundError` out of the first
    generator. It fails the same way for a mistyped slug or a renamed file, though, and this one
    cannot: it is only ever red for the thing #222 was.
    """
    generators = {"brief", "prd", "criteria", "epic", "release", "stories", "estimate"}
    for argv in _reproduce_commands(example):
        if argv and argv[0] in generators:
            assert not argv[1].endswith(".json"), (
                f"`requivo {' '.join(argv)}` passes a model file to a generator; generators resolve "
                f"a session, so this raises SessionNotFoundError on a fresh clone"
            )


def test_no_example_still_promises_the_retired_output_root():
    """`out/<slug>/` was retired in 0.9.8 and is opened by nothing but `session migrate`. The
    leave-approval block promised artifacts there for four releases after that."""
    for example in EXAMPLES:
        text = (REPO / "examples" / example / "README.md").read_text(encoding="utf-8")
        assert "out/" not in text, f"{example}/README.md names the retired out/ root"
