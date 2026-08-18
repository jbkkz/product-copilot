"""Text IO encoding guards -- every text read declares its codec, every print survives its console.

Why this file exists (#11, #29)
-------------------------------
`_atomic_write` has always written `encoding="utf-8"`. Nothing read that way back: 29 call sites
across `src/` and `scripts/` called `Path.read_text()` with no `encoding`, which decodes with
`locale.getpreferredencoding(False)` -- UTF-8 on macOS and on Linux CI, the ANSI codepage (cp1252)
on Windows unless `PYTHONUTF8=1`. `pyproject.toml` is `requires-python = ">=3.9"`, so UTF-8
mode-by-default (new in 3.15) covers none of the supported range.

That asymmetry has three consequences and all of them are quiet:

  - a French `problem` slot round-trips into mojibake that is still valid JSON, so nothing fails and
    the PRD ships it;
  - `integrity.py` rehashes the mis-decoded string and reports `revision_hash_mismatch` -- the verb
    whose whole job is answering *is this session intact* accuses the user of editing a file nobody
    touched, and `session import` refuses a legitimate archive on it;
  - twelve bundled assets are not cp1252-decodable, so `requivo discover`, `demo`, `schema` and
    `context` raise `UnicodeDecodeError` before any API call is made.

The same class runs the other way on output (#29): a glyph the console cannot *encode* raises
`UnicodeEncodeError` at the `print` -- after the mutation that print was reporting has already
landed. The operator sees a crash, re-runs, and pays for a second provider call on top of the first.

The three guards here
---------------------
1. `test_every_text_read_declares_its_encoding` -- a static AST walk over `src/` and `scripts/`.
   This is the part that removes the *class* rather than the 29 instances: passing `encoding=` at 29
   call sites leaves the 30th, written next week, unguarded. This repo has already watched that
   happen twice -- a bare `read_text()` appeared three lines from #33's fix in a neighbouring
   module, and the guard written for #10 had the identical defect *in the guard itself*.
2. `test_the_cli_survives_a_console_that_cannot_encode_its_glyphs` -- a subprocess under
   `PYTHONIOENCODING=ascii`. #29 observed that this suite captures to `io.StringIO`, so no
   in-process test can ever reach the console encoder even once a Windows leg exists. A subprocess
   with a forced narrow encoder reaches the real one, and it does so identically on every platform,
   so this control fires on the Linux legs too rather than waiting for Windows.
3. `test_the_cli_reads_its_assets_with_an_explicit_encoding` -- a subprocess under
   `PYTHONWARNDEFAULTENCODING=1` with `-W error::EncodingWarning`, which turns every locale-default
   text read into an exception identically on every platform and in every locale. 3.10+ only; on 3.9
   it skips loudly, naming what went untested, rather than passing.

Every runtime control is paired with a control on the *lever*, because "the process did not crash"
also passes when the environment variable did nothing at all. A control that cannot fire is worse
than no control, so where a lever does not bite the test names the claim that went unmade.

What these guards cannot see
----------------------------
Stated rather than left to read as clean:

  - an aliased or dynamically built call -- `getattr(path, "read_text")()`, `f = p.read_text; f()`;
  - a third-party library reading text with the locale codec inside itself;
  - a console that *encodes* a glyph and then draws it as something else. Encoding is all this can
    reach; what the terminal renders afterwards is the terminal's business.
"""
from __future__ import annotations

import ast
import io
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from requivo import streams
from requivo.core.errors import InvalidModelError
from requivo.deterministic import read_user_text

REPO_ROOT = Path(__file__).resolve().parents[1]

# Both trees CI already lints (`ruff check src tests scripts`). `scripts/` is in scope because the
# golden harness reads provider-written prose out of `fixtures/golden/*.runs.json` -- em dashes and
# curly quotes by construction -- and mis-decoding a baseline reports a prompt regression that never
# happened, which is the one failure mode a regression lens must not have.
SCAN_ROOTS = {
    "src/requivo": "the package",
    "scripts": "the golden harness and the install-free launcher",
}

# One stable anchor per root, so adding a module does not fail this file while a rename still does.
# `Path.rglob` on a directory that does not exist returns `[]`, and `assert not []` is an all-clear
# nobody earned -- the failure #10 exists in order not to repeat.
SCAN_ANCHORS = {
    "src/requivo": ("core/persistence.py", "deterministic.py"),
    "scripts": ("golden_lib.py",),
}

# The methods whose default codec is the *locale's*, not the file's.
_TEXT_METHODS = {
    "read_text": "Path.read_text() with no encoding decodes with the locale codepage, not the file's",
    "write_text": "Path.write_text() with no encoding encodes with the locale codepage, not UTF-8",
    "open": "Path.open() in text mode with no encoding uses the locale codepage in both directions",
}

# Receivers whose `.open()` has no text layer at all, so demanding an `encoding` of them would fail
# correct code. Each carries its reason, so the next person argues with a named line rather than
# deleting the table.
_EXEMPT_RECEIVERS = {
    "os": "os.open returns a raw file descriptor -- there is no text layer and no codec to declare",
    "webbrowser": "webbrowser.open takes a URL and opens a browser, not a file",
    "zipfile": "zipfile members are opened as bytes unless wrapped explicitly",
    "tarfile": "tarfile members are opened as bytes unless wrapped explicitly",
    "shutil": "shutil has no text-mode open",
    "subprocess": "subprocess streams take their codec from the Popen call, not from open()",
}


def _parse(path: Path) -> ast.Module:
    """Parse a source file.

    Explicitly UTF-8, and the reason is this file's own subject: every module in the package carries
    at least one em dash, so a guard that read its own scan set with a bare `read_text()` would die
    instead of running under exactly the locale it exists to protect against. That is not
    hypothetical -- it is what happened to the #10 guard, which had the defect it was written to
    catch.
    """
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def scan(root: Path) -> list[Path]:
    """Every Python file under `root`, recursively. An empty result is an error, not an answer."""
    if not root.is_dir():
        raise AssertionError(
            f"the encoding guard could not scan {root}: no such directory. That is 'could not "
            f"look', not 'looked and found nothing' -- fix the path, never the assertion."
        )
    found = sorted(root.rglob("*.py"))
    if not found:
        raise AssertionError(
            f"the encoding guard scanned {root} and found no Python files. An empty scan set "
            f"cannot support a 'no offenders' verdict."
        )
    return found


def _has_keyword(node: ast.Call, name: str) -> bool:
    return any(kw.arg == name for kw in node.keywords)


def _mode_of(node: ast.Call, positional_index: int) -> tuple[str | None, bool]:
    """The literal `mode` string of an open() call, and whether it could be determined at all.

    Returns `(mode, known)`. A mode assembled at runtime is `(None, False)` -- the third state, which
    the caller reports as a finding rather than waving through, because a guard that cannot tell text
    from binary must not answer as though it could.
    """
    for kw in node.keywords:
        if kw.arg == "mode":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value, True
            return None, False
    if len(node.args) > positional_index:
        arg = node.args[positional_index]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value, True
        return None, False
    return "r", True  # the default, and it is text


_UNKNOWN_MODE = (
    "whose mode is not a literal -- this guard cannot tell text from binary here, so pass "
    "encoding= explicitly or read bytes"
)


def encoding_violations(path: Path) -> list[str]:
    """Every text read or write in `path` that takes whatever codec the locale happens to offer.

    One string per violation, carrying the line, the construct and the reason. A guard that says only
    "False" costs the next reader the whole search again.
    """
    out: list = []
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.Call):
            continue
        # The bare builtin: `open(p)`. An attribute call is handled below.
        if isinstance(node.func, ast.Name) and node.func.id == "open":
            if _has_keyword(node, "encoding"):
                continue
            mode, known = _mode_of(node, positional_index=1)
            if not known:
                out.append(f"line {node.lineno}: open() {_UNKNOWN_MODE}")
            elif "b" not in mode:
                out.append(f"line {node.lineno}: open() in text mode -- {_TEXT_METHODS['open']}")
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in _TEXT_METHODS:
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Name) and receiver.id in _EXEMPT_RECEIVERS:
            continue
        if _has_keyword(node, "encoding"):
            continue
        if node.func.attr == "open":
            mode, known = _mode_of(node, positional_index=0)
            if known and "b" in mode:
                continue
            if not known:
                out.append(f"line {node.lineno}: .open() {_UNKNOWN_MODE}")
                continue
        out.append(f"line {node.lineno}: .{node.func.attr}() -- {_TEXT_METHODS[node.func.attr]}")
    return sorted(out)


def _nonascii_literals(node: ast.AST) -> list:
    """Every string constant reachable from `node` that is not pure ASCII."""
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and any(ord(c) > 127 for c in n.value)]


def fixture_violations(path: Path) -> list:
    """Encoding-less text IO in a *test* whose content is not ASCII.

    A narrower rule than `encoding_violations`, applied to `tests/` for a stated reason. Most of the
    ~90 encoding-less reads and writes in this suite move pure ASCII between a fixture and an
    assertion in the same process: the locale's codec is used on both sides, so they agree, and
    demanding `encoding=` of all of them would be a diff far larger than the bug with no defect
    behind it.

    The ones that matter are the fixtures carrying a character outside ASCII, because those cross a
    real boundary: the *test* writes with the locale's codec and the *product* now reads UTF-8. On
    Windows those disagree, and the leg goes red about a product that is behaving correctly. That is
    the trap #3 names -- a harness rendering an environment limit as a product verdict -- and it is
    worth catching here, where it costs a second, rather than one multi-leg CI matrix at a time.

    Found exactly one instance when this was written (`test_selection.py`, a context card with an em
    dash, read back through `load_context`), which is the evidence that the narrow rule is worth
    having and the broad one is not yet.
    """
    out: list = []
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in ("read_text", "write_text"):
            continue
        if _has_keyword(node, "encoding"):
            continue
        found = [s for arg in node.args for s in _nonascii_literals(arg)]
        if found:
            out.append(
                f"line {node.lineno}: .{node.func.attr}() with non-ASCII content and no encoding -- "
                f"the fixture is written with the locale's codec and read back by a product that "
                f"decodes UTF-8; they disagree on Windows. Content: {found[0][:60]!r}"
            )
    return sorted(out)


def test_no_test_fixture_writes_non_ascii_with_the_locale_codec():
    """The harness half of #3, kept honest by the same walk that keeps the product honest."""
    offenders: dict = {}
    for path in scan(REPO_ROOT / "tests"):
        found = fixture_violations(path)
        if found:
            offenders[path.relative_to(REPO_ROOT).as_posix()] = found
    assert not offenders, (
        "a test fixture carrying non-ASCII text must name its codec, or the Windows leg reddens "
        "about a product that is correct: " + repr(offenders)
    )


_FIXTURE_CASES = {
    "must_fire.py": """
        from pathlib import Path

        def fixture(root: Path) -> None:
            (root / "card.md").write_text("ACME \\u2014 the context")
    """,
    "must_not_fire_ascii.py": """
        from pathlib import Path

        def fixture(root: Path) -> None:
            (root / "card.md").write_text("ACME - the context")
    """,
    "must_not_fire_explicit.py": """
        from pathlib import Path

        def fixture(root: Path) -> None:
            (root / "card.md").write_text("ACME \\u2014 the context", encoding="utf-8")
    """,
}


def test_the_fixture_rule_fires_exactly_where_it_should(tmp_path):
    """Both edges of the narrow rule, because a rule this narrow is the easy one to get backwards."""
    root = tmp_path / "tests"
    _write_tree(root, _FIXTURE_CASES)
    verdicts = {p.name: bool(fixture_violations(p)) for p in scan(root)}
    assert verdicts == {
        "must_fire.py": True,
        "must_not_fire_ascii.py": False,
        "must_not_fire_explicit.py": False,
    }, verdicts


def _write_tree(root: Path, sources: dict) -> None:
    for name, source in sources.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(source), encoding="utf-8")


# --------------------------------------------------------------------------------------------------
# The scan set itself: "could not look" must never render as "looked and found nothing".
# --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("relative", sorted(SCAN_ROOTS))
def test_the_guard_scans_the_real_trees(relative):
    """Name what was scanned. Everything below this line is a negative assertion, and a negative
    assertion over an empty set is an all-clear nobody earned."""
    root = REPO_ROOT / relative
    names = sorted(p.relative_to(root).as_posix() for p in scan(root))
    missing = [a for a in SCAN_ANCHORS[relative] if a not in names]
    assert not missing, (
        f"the encoding guard scanned {root} and did not find {missing}; it is not looking at "
        f"{SCAN_ROOTS[relative]}. Scanned: {names}"
    )


def test_the_guard_refuses_a_scan_it_could_not_make(tmp_path):
    """The #10 shape, in both of its forms: a path that resolves to nothing, and one that resolves
    to nothing useful. `rglob` raises on neither."""
    missing = tmp_path / "not-here"
    assert list(missing.rglob("*.py")) == [], "the shape guarded against: rglob returns [], not an error"
    with pytest.raises(AssertionError, match="no such directory"):
        scan(missing)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(AssertionError, match="no Python files"):
        scan(empty)


# --------------------------------------------------------------------------------------------------
# The static guard, and controls on both of its edges.
# --------------------------------------------------------------------------------------------------

def test_every_text_read_declares_its_encoding():
    """#11 itself. Every text read and write in the shipped trees names its codec, so a session
    written as UTF-8 reads back as what was written, on every platform Requivo installs on."""
    offenders: dict = {}
    for relative in sorted(SCAN_ROOTS):
        for path in scan(REPO_ROOT / relative):
            found = encoding_violations(path)
            if found:
                offenders[path.relative_to(REPO_ROOT).as_posix()] = found
    assert not offenders, (
        "every text read and write must pass encoding= explicitly: the default is the *locale's* "
        "codec, so a file this project wrote as UTF-8 is decoded as cp1252 on Windows and the "
        "round-trip corrupts silently while still validating (#11). Offenders: " + repr(offenders)
    )


_ENCODING_VIOLATIONS = {
    "bare_read.py": """
        from pathlib import Path

        def load(p: Path) -> str:
            return p.read_text()
    """,
    "bare_write.py": """
        from pathlib import Path

        def save(p: Path, text: str) -> None:
            p.write_text(text)
    """,
    "bare_builtin_open.py": """
        def load(name):
            with open(name) as f:
                return f.read()
    """,
    "builtin_open_explicit_text_mode.py": """
        def load(name):
            with open(name, "r") as f:
                return f.read()
    """,
    "path_open_text.py": """
        from pathlib import Path

        def load(p: Path) -> str:
            with p.open() as f:
                return f.read()
    """,
    "open_with_computed_mode.py": """
        def load(name, mode):
            with open(name, mode) as f:
                return f.read()
    """,
    "chained_read.py": """
        import json
        from pathlib import Path

        def load(p: Path) -> dict:
            return json.loads((p / "session.json").read_text())
    """,
    "buried/deeper.py": """
        from pathlib import Path

        def load(p: Path) -> str:
            return p.read_text()
    """,
}


def test_the_guard_sees_each_way_of_writing_the_violation(tmp_path):
    """Positive control, one fixture per shape. "No offenders" also passes when the guard has gone
    blind, so every forbidden form gets a fixture the guard must flag -- including the buried one,
    which is what proves the walk is recursive rather than a flat glob."""
    root = tmp_path / "src"
    _write_tree(root, _ENCODING_VIOLATIONS)
    missed = [p.relative_to(root).as_posix() for p in scan(root) if not encoding_violations(p)]
    assert not missed, f"the encoding guard is blind to these: {missed}"


_LEGITIMATE_IO = """
    from __future__ import annotations

    import os
    import webbrowser
    from pathlib import Path


    def load(p: Path) -> str:
        return p.read_text(encoding="utf-8")


    def save(p: Path, text: str) -> None:
        p.write_text(text, encoding="utf-8")


    def load_bytes(p: Path) -> bytes:
        return p.read_bytes()


    def save_bytes(p: Path, blob: bytes) -> None:
        p.write_bytes(blob)


    def binary_stream(p: Path):
        return p.open("rb")


    def builtin_binary(name):
        return open(name, "rb")


    def builtin_text(name):
        return open(name, encoding="utf-8")


    def lock(d: Path) -> int:
        # a raw file descriptor: no text layer, so no codec to declare
        return os.open(d / ".lock", os.O_RDWR | os.O_CREAT, 0o600)


    def show(url: str) -> None:
        webbrowser.open(url)
"""


def test_the_guard_does_not_fire_on_correct_io(tmp_path):
    """The must-fire cases above are only meaningful next to a must-not-fire case: a detector that
    flags everything is as useless as one that flags nothing, and this one gets deleted by the next
    person the first time it reddens correct code."""
    root = tmp_path / "src"
    _write_tree(root, {"ordinary.py": _LEGITIMATE_IO})
    assert encoding_violations(scan(root)[0]) == []


# --------------------------------------------------------------------------------------------------
# The runtime half. #29 measured that this suite captures to io.StringIO, so nothing in-process can
# reach the console encoder even once a Windows leg exists. A subprocess with a forced narrow
# encoder reaches the real one, on every platform.
# --------------------------------------------------------------------------------------------------

def _clean_env(extra: dict) -> dict:
    env = dict(os.environ)
    # Any of these, inherited from uv or a parent shell, would override the levers below and turn
    # every control in this section green for the wrong reason.
    for name in ("PYTHONUTF8", "PYTHONIOENCODING", "PYTHONWARNDEFAULTENCODING"):
        env.pop(name, None)
    env.update(extra)
    return env


def _cli(args: list, extra_env: dict, cwd: Path, python_args: list = None):
    env = _clean_env(extra_env)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, *(python_args or []), "-m", "requivo", *args],
        cwd=str(cwd), env=env, capture_output=True, timeout=300,
    )


_ASCII_CONSOLE = {"PYTHONIOENCODING": "ascii"}

# The two glyphs doctor leads with, named by codepoint so this source file stays pure ASCII: a
# console whose codepage cannot represent a check mark must not be able to kill a failure report.
_CHECK_MARK = chr(0x2705)
_WARNING_SIGN = chr(0x26A0)


def test_the_ascii_console_lever_actually_bites():
    """The positive control for the console tests below. `PYTHONIOENCODING=ascii` is a *narrower*
    console than Windows cp1252 -- cp1252 encodes an em dash and ascii does not -- so a process that
    survives it survives cp1252 for these glyphs. But an environment variable that silently did
    nothing would make "the process did not crash" pass for entirely the wrong reason, so whether
    the lever bites is measured rather than assumed."""
    probe = subprocess.run(
        [sys.executable, "-c", "print(chr(0x2705))"],
        env=_clean_env(_ASCII_CONSOLE), capture_output=True, text=True, timeout=60,
    )
    assert probe.returncode != 0, (
        "PYTHONIOENCODING=ascii did not take on this interpreter; the console controls below cannot "
        "fire and must not be read as evidence."
    )
    assert "UnicodeEncodeError" in probe.stderr, probe.stderr


@pytest.mark.parametrize("verb", [["doctor"], ["schema"], ["demo"]])
def test_the_cli_survives_a_console_that_cannot_encode_its_glyphs(verb, tmp_path):
    """#29. `doctor` prints a check mark on its very first line and dies there on a console that
    cannot encode it -- after the whole diagnosis it exists to report has already been computed, so
    the exit code describes the crash rather than the finding.

    The ordering is what makes this a correctness bug rather than a cosmetic one, and it is worst on
    the write verbs: `requivo brief` dies in the renderer after the paid provider call has already
    landed a revision, so the operator re-runs and pays twice."""
    r = _cli(verb, _ASCII_CONSOLE, tmp_path)
    detail = r.stderr.decode("ascii", "backslashreplace")
    assert b"UnicodeEncodeError" not in r.stderr, (
        f"`requivo {' '.join(verb)}` died in its own renderer on a console that cannot encode its "
        f"glyphs: {detail}"
    )
    assert r.returncode == 0, detail
    assert r.stdout.strip(), "the command exited 0 and printed nothing at all"


def test_the_console_chokepoint_degrades_rather_than_dropping_the_glyph(tmp_path):
    """What survival must *not* mean: silently printing nothing where a glyph was. A renderer that
    swallows the character it could not encode trades the loud failure for the quiet one -- the
    reader sees a well-formed line with a word missing from it, and nothing says so. An escape is
    ugly and honest; a hole is neither."""
    r = _cli(["doctor"], _ASCII_CONSOLE, tmp_path)
    assert r.returncode == 0, r.stderr.decode("ascii", "backslashreplace")
    text = r.stdout.decode("ascii", "strict")  # it must be pure ascii on an ascii console
    escaped = [g.encode("ascii", "backslashreplace").decode("ascii") for g in (_CHECK_MARK, _WARNING_SIGN)]
    assert any(e in text for e in escaped), (
        "the glyphs were dropped rather than escaped; a reader cannot tell a missing character from "
        "a character that was never there. Expected one of " + repr(escaped) + " in: " + text
    )


# --------------------------------------------------------------------------------------------------
# The chokepoint's own three states. `doctor` reports these, and a state that is only ever produced
# by an environment no test can reach is a state nobody has checked.
# --------------------------------------------------------------------------------------------------

def _wrapper(encoding: str, errors: str):
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors=errors)


def test_describe_stream_separates_safe_from_will_crash_from_unknown():
    """Three answers, not two. `will-crash` and `unknown` are different findings with different
    remedies, and both differ from `safe` — a check that returned the same value for *I looked and it
    is fine* and *I could not look* would put an absence into `doctor`'s own report."""
    safe = streams.describe_stream(_wrapper("ascii", "backslashreplace"), "stdout")
    assert safe["state"] == "safe" and safe["detail"] is None

    crashy = streams.describe_stream(_wrapper("ascii", "strict"), "stdout")
    assert crashy["state"] == "will-crash"
    assert "UnicodeEncodeError" in crashy["detail"], crashy

    blind = streams.describe_stream(io.BytesIO(), "stdout")  # no .encoding at all
    assert blind["state"] == "unknown"
    assert "cannot look" in blind["detail"], blind

    assert streams.describe_stream(None, "stdout")["state"] == "unknown"


def test_configure_stream_reports_a_stream_it_could_not_reach():
    """The third state on the *configuring* side. A stream that refused to be configured is exactly
    the one that can still kill the process later, so it has to be nameable rather than silent."""
    reached = streams.configure_stream(_wrapper("ascii", "strict"), "stdout")
    assert reached["state"] in ("configured", "unchanged"), reached
    assert reached["errors"] == streams.ERRORS

    unreachable = streams.configure_stream(io.BytesIO(), "stdout")  # no .reconfigure
    assert unreachable["state"] == "could-not"
    assert "reconfigure" in unreachable["reason"], unreachable

    closed = _wrapper("utf-8", "strict")
    closed.close()
    assert streams.configure_stream(closed, "stdout")["state"] == "could-not"


def test_configure_stream_does_not_overrule_an_operator_who_named_a_codec(monkeypatch):
    """`PYTHONIOENCODING` is a decision about somebody's pipeline. This module guarantees their
    stream cannot crash; it does not get to decide what their stream is for."""
    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    stream = _wrapper("ascii", "strict")
    report = streams.configure_stream(stream, "stdout")
    assert stream.encoding == "ascii", "the operator's codec was overruled"
    assert stream.errors == streams.ERRORS, "the no-crash guarantee was not applied"
    assert report["state"] == "unchanged" and "PYTHONIOENCODING" in report["reason"]


def test_safe_write_never_raises_on_a_character_it_cannot_encode():
    """The belt to `configure_streams`' braces. The message this writes is usually an error report,
    and an error report that dies on its own em dash is the whole failure being fixed here."""
    stream = _wrapper("ascii", "strict")
    streams.safe_write(stream, "verdict " + _CHECK_MARK + " done")
    stream.seek(0)
    written = stream.buffer.getvalue().decode("ascii")
    assert "verdict" in written and "done" in written, written
    assert _CHECK_MARK.encode("ascii", "backslashreplace").decode("ascii") in written, written


def test_safe_write_gives_up_quietly_on_a_stream_that_is_gone():
    """A closed stream is not a reason to raise from inside an error handler: there is nowhere left
    to report to, and raising here would replace the message with a traceback about the message."""
    closed = _wrapper("utf-8", "strict")
    closed.close()
    streams.safe_write(closed, "anything")  # must not raise


# --------------------------------------------------------------------------------------------------
# A file the *user* named. The one read whose bytes this project did not write.
# --------------------------------------------------------------------------------------------------

def test_a_user_file_that_is_not_utf8_is_refused_by_name_not_by_traceback(tmp_path):
    """Refusing is right — mojibake validates, and half a request reads exactly like a whole one. But
    refusing with a bare `UnicodeDecodeError` would trade a silently wrong answer for an unexplained
    crash, which is the same trade one step along. The refusal has to be an answer."""
    brief = tmp_path / "brief.md"
    # A French sentence saved as cp1252 by a Windows editor: the realistic input, not an exotic one.
    brief.write_bytes("Système de validation des congés.".encode("cp1252"))

    with pytest.raises(InvalidModelError) as ei:
        read_user_text(brief)

    message = str(ei.value)
    assert "not valid UTF-8" in message, message
    assert "0xe8" in message, ("the offending byte is not named, so the user cannot tell which "
                              f"character to look for: {message}")
    assert ei.value.details["path"] == str(brief)
    assert ei.value.details["expected_encoding"] == "utf-8"
    assert isinstance(ei.value.details["position"], int)


def test_a_user_file_that_is_utf8_is_read_unchanged(tmp_path):
    """The must-not-fire half. A refusal that fires on correct input is worse than no refusal, and
    this is the case that matters most on this project: French client prose, correctly encoded."""
    brief = tmp_path / "brief.md"
    original = "Système de validation des congés — 5 000 salariés."
    brief.write_bytes(original.encode("utf-8"))
    assert read_user_text(brief) == original


def test_the_refusal_does_not_let_a_path_forge_a_line_of_output(tmp_path):
    """The message interpolates a path the user supplied. A path carrying a newline must not be able
    to write what looks like a second, authoritative line of Requivo's own output -- the shape #40
    found in `doctor`."""
    sneaky = tmp_path / "brief\nERROR: session verified OK.md"
    try:
        sneaky.write_bytes(b"\xe8")
    except (OSError, ValueError):
        pytest.skip("this filesystem refuses a newline in a filename; the forging path is untested here")
    with pytest.raises(InvalidModelError) as ei:
        read_user_text(sneaky)
    body = str(ei.value)
    assert not any(line.startswith("ERROR:") for line in body.splitlines()), (
        "a user-supplied path forged a line at column 0 of Requivo's own message: " + repr(body))


_WARN_DEFAULT_ENCODING = {"PYTHONWARNDEFAULTENCODING": "1"}
_ERROR_ON_DEFAULT_ENCODING = ["-W", "error::EncodingWarning"]

_NO_LEVER_ON_39 = (
    "EncodingWarning and PYTHONWARNDEFAULTENCODING are 3.10+, so this lever cannot fire on 3.9. "
    "UNTESTED ON THIS INTERPRETER: that the CLI reads its bundled assets with an explicit codec "
    "rather than the locale's. The static guard above covers the same claim on every interpreter, "
    "and the 3.10-3.13 legs of the CI matrix do run this one."
)


def test_the_default_encoding_lever_actually_bites(tmp_path):
    """Positive control for the read-side test below, on the same reasoning as the console one."""
    if sys.version_info < (3, 10):
        pytest.skip(_NO_LEVER_ON_39)
    probe = tmp_path / "probe.py"
    probe.write_text(textwrap.dedent("""
        import sys
        from pathlib import Path

        Path(sys.argv[1]).read_text()
    """), encoding="utf-8")
    target = tmp_path / "data.txt"
    target.write_text("plain", encoding="utf-8")
    r = subprocess.run(
        [sys.executable, *_ERROR_ON_DEFAULT_ENCODING, str(probe), str(target)],
        env=_clean_env(_WARN_DEFAULT_ENCODING), capture_output=True, text=True, timeout=60,
    )
    assert r.returncode != 0 and "EncodingWarning" in r.stderr, (
        "the default-encoding lever did not take; the read-side control below cannot fire and must "
        "not be read as evidence: " + r.stderr
    )


@pytest.mark.parametrize("verb", [["schema"], ["schema", "--framework"], ["context"], ["demo"], ["doctor"]])
def test_the_cli_reads_its_assets_with_an_explicit_encoding(verb, tmp_path):
    """#11's read half, at runtime rather than statically.

    `-W error::EncodingWarning` turns *any* locale-default text read into an exception, identically
    on every platform and in every locale -- a sharper lever than forcing a locale, because it fires
    on a read that happens to succeed today. All five bundled context cards are cp1252-decodable,
    which is exactly why that path corrupts silently on Windows instead of crashing, and why a
    crash-based control would miss it."""
    if sys.version_info < (3, 10):
        pytest.skip(_NO_LEVER_ON_39)
    r = _cli(verb, {**_WARN_DEFAULT_ENCODING, **_ASCII_CONSOLE}, tmp_path,
             python_args=_ERROR_ON_DEFAULT_ENCODING)
    detail = r.stderr.decode("ascii", "backslashreplace")
    assert b"EncodingWarning" not in r.stderr, (
        f"`requivo {' '.join(verb)}` read text with the locale's codec rather than an explicit "
        f"one: {detail}"
    )
    assert r.returncode == 0, detail
