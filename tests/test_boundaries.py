"""Architectural boundary guards.

These tests fail loudly if the core/provider separation regresses -- the single most important
invariant of the refactor. They are static (they read source), so they hold even in an environment
where the Anthropic SDK is not installed.

Where the line sits
-------------------
Invariant 7 used to read "provider-free and IO-free". The second half was never true and was never
meant to be: `persistence.py` writes sessions, `context.py` reads context cards, `contracts.py` and
`analysis.py` read the framework schema. A guard written against that wording would have to fail on
correct code, so this file encodes what the invariant *means*:

    core may read and write files; core may not talk to a provider, and may not talk to the
    *process* -- its arguments, its standard streams, its environment, or its exit.

Every entry in the `_FORBIDDEN_*` tables below carries the reason it is there, so the next person can
argue with a named line instead of deleting the file. Two things are deliberately *not* banned, and
`test_the_process_guard_allows_what_core_legitimately_does` pins both so that a later tightening
which would fail correct code goes red here first:

  - file IO, per the paragraph above;
  - `logging`, which is the library-correct way of *not* printing. Its default handler writing to
    stderr is the application's configuration, not core's.

What this guard cannot see
--------------------------
Stated rather than left to read as clean:

  - an aliased module -- `import sys as s; s.argv` resolves nowhere a static walk can follow;
  - dynamic access -- `getattr(sys, "argv")`, `importlib.import_module("anthropic")`;
  - a violation reached through a re-export in some other package.

The failure this file exists in order not to repeat (#10)
---------------------------------------------------------
`Path.glob` on a directory that does not exist returns `[]` and raises nothing, so a guard that
asserts "no offenders" over an empty scan passes green while checking nothing. This package has
already been renamed once (`product_copilot` -> `requivo`) and the guard survived it by luck. `scan`
therefore treats an unscannable or empty root as an error rather than as an answer, and
`test_the_guard_refuses_a_scan_it_could_not_make` is the positive control for that.
"""
from __future__ import annotations

import ast
import importlib
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE = REPO_ROOT / "src" / "requivo" / "core"
CORE_PACKAGE = "requivo.core"

# A module whose absence means the scan is not looking at Requivo's core at all -- a moved `src/`
# layout, a renamed package, a relocated test file. One stable anchor rather than the full listing,
# so adding a module to core does not fail this file while a rename still does.
CORE_ANCHORS = ("__init__.py", "contracts.py")

_PROVIDER_IMPORTS = {
    "anthropic": "the Anthropic SDK -- core must work with no API key and no SDK installed",
    "providers": "requivo.providers -- the dependency arrow points provider -> core, never the reverse",
}

_TERMINAL_IMPORTS = {
    "argparse": "an argument parser -- core is called with parameters; cli.py owns argv",
    "click": "a terminal CLI framework -- see argparse",
    "typer": "a terminal CLI framework -- see argparse",
    "curses": "a terminal UI library -- core renders nothing",
}

_FORBIDDEN_CALLS = {
    "print": "writes to stdout -- core returns data and a caller decides whether to display it",
    "input": "reads stdin -- core takes arguments, it never prompts",
    "breakpoint": "opens an interactive prompt on whatever terminal happens to be attached",
}

_FORBIDDEN_ATTRIBUTES = {
    ("sys", "argv"): "reads the process arguments -- cli.py owns argv",
    ("sys", "stdout"): "writes to stdout -- render/ turns data into strings, interfaces print them",
    ("sys", "stderr"): "writes to stderr -- see sys.stdout",
    ("sys", "stdin"): "reads stdin -- see input()",
    ("sys", "exit"): "kills the process -- an engine raises, an interface decides to exit",
    ("os", "environ"): "reads ambient process state -- paths.py, which is not core, owns the environment",
    ("os", "getenv"): "reads ambient process state -- see os.environ",
    ("os", "putenv"): "writes ambient process state -- see os.environ",
}


def _parse(path: Path) -> ast.Module:
    """Parse a source file.

    The encoding is explicit because every module in core contains at least one em dash, and
    `read_text()` with no encoding decodes with the *locale* codepage. Under `LC_ALL=C` in a bare
    container, or a DBCS Windows shell, that raises `UnicodeDecodeError` and the guard dies instead
    of running. CI is Linux/UTF-8 only, so nothing here would ever have shown it.
    """
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _package_for(path: Path, root: Path, root_package: str) -> str:
    """The dotted package a file lives in, so its relative imports can be resolved."""
    parts = path.relative_to(root).parts[:-1]
    return ".".join((root_package, *parts))


def _resolve_relative(package: str, level: int) -> str:
    """The dotted base a relative import of depth `level` is measured from. Level 0 is absolute."""
    if level == 0:
        return ""
    parts = package.split(".")
    return ".".join(parts[: max(0, len(parts) - (level - 1))])


def scan(root: Path, package: str) -> list[tuple[Path, str]]:
    """Every Python file under `root`, recursively, paired with the dotted package it lives in.

    The walk is recursive so a future `core/<subpackage>/` cannot be silently unscanned, and an
    empty result is an error rather than an answer: `Path.glob` on a directory that does not exist
    returns `[]`, which is what let this guard pass green while checking nothing (#10).
    """
    if not root.is_dir():
        raise AssertionError(
            f"boundary guard could not scan {root}: no such directory. This is 'could not look', not "
            f"'looked and found nothing' -- fix the path, never the assertion."
        )
    found = sorted(root.rglob("*.py"))
    if not found:
        raise AssertionError(
            f"boundary guard scanned {root} and found no Python files. An empty scan set cannot "
            f"support a 'no offenders' verdict."
        )
    return [(p, _package_for(p, root, package)) for p in found]


def imported_modules(path: Path, package: str) -> set[str]:
    """Every module `path` imports, as a dotted absolute name.

    Relative imports are resolved against `package`, because `from .anthropic import Client` is the
    shortest way to write the violation and the previous version of this guard skipped every
    `node.level != 0` import outright.
    """
    names: set[str] = set()
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative(package, node.level)
            if node.module:
                names.add(f"{base}.{node.module}" if base else node.module)
            else:
                # `from . import x` / `from .. import x`: each alias is itself a module.
                names.update(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
    return names


def import_hits(modules: set[str], table: dict) -> dict:
    """The subset of `modules` whose dotted path crosses a forbidden name, mapped to the reason."""
    hits = {}
    for module in sorted(modules):
        for part in module.split("."):
            if part in table:
                hits[module] = table[part]
                break
    return hits


def process_violations(path: Path, package: str) -> list[str]:
    """Every place `path` talks to the process rather than to its caller.

    One string per violation, carrying the line, the construct and the reason -- a guard that says
    only "False" costs the next reader the whole search again.
    """
    out: list[str] = []
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_CALLS:
            # Keyed on the *call*, not on the bare name: `def render(input: str)` is a fair signature
            # and using that parameter must not read as a call to the builtin.
            out.append(f"line {node.lineno}: {node.func.id}() -- {_FORBIDDEN_CALLS[node.func.id]}")
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            key = (node.value.id, node.attr)
            if key in _FORBIDDEN_ATTRIBUTES:
                out.append(f"line {node.lineno}: {key[0]}.{key[1]} -- {_FORBIDDEN_ATTRIBUTES[key]}")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in ("sys", "os"):
            # `from sys import argv` never produces an Attribute node, so the import is where it shows.
            for alias in node.names:
                key = (node.module, alias.name)
                if key in _FORBIDDEN_ATTRIBUTES:
                    out.append(
                        f"line {node.lineno}: from {node.module} import {alias.name} -- {_FORBIDDEN_ATTRIBUTES[key]}"
                    )
    for module, reason in sorted(import_hits(imported_modules(path, package), _TERMINAL_IMPORTS).items()):
        out.append(f"imports {module} -- {reason}")
    return sorted(out)


def _core_offenders(table: dict) -> dict:
    """Real-scan helper: forbidden imports across the actual core package, keyed by module."""
    offenders: dict = {}
    for path, package in scan(CORE, CORE_PACKAGE):
        hits = import_hits(imported_modules(path, package), table)
        if hits:
            offenders[path.relative_to(CORE).as_posix()] = sorted(hits)
    return offenders


def _write_tree(root: Path, sources: dict) -> None:
    for name, source in sources.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(source), encoding="utf-8")


# --------------------------------------------------------------------------------------------------
# The scan set itself: "could not look" must never render as "looked and found nothing".
# --------------------------------------------------------------------------------------------------

def test_the_guard_scans_the_real_core_package():
    """Name what was scanned. Everything below this line is a negative assertion, and a negative
    assertion over an empty set is an all-clear nobody earned."""
    scanned = scan(CORE, CORE_PACKAGE)
    names = sorted(p.relative_to(CORE).as_posix() for p, _ in scanned)
    missing = [anchor for anchor in CORE_ANCHORS if anchor not in names]
    assert not missing, (
        f"the boundary guard scanned {CORE} and did not find {missing}; it is not looking at "
        f"Requivo's core. Scanned: {names}"
    )


def test_the_guard_refuses_a_scan_it_could_not_make():
    """The positive control for #10. `src/product_copilot/core` is this package's *previous* name:
    before this change both boundary tests passed green against it, because `Path.glob` on a missing
    directory returns `[]` and `assert not set()` holds. That is the whole issue in one line."""
    renamed_away = REPO_ROOT / "src" / "product_copilot" / "core"
    assert not renamed_away.exists(), "this control assumes the pre-rename path is gone"
    assert list(renamed_away.glob("*.py")) == [], "the shape being guarded against: glob returns [], not an error"
    with pytest.raises(AssertionError, match="no such directory"):
        scan(renamed_away, CORE_PACKAGE)


def test_the_guard_refuses_an_empty_directory(tmp_path):
    """The other shape of the same hole: the directory resolves, and holds nothing."""
    empty = tmp_path / "core"
    empty.mkdir()
    with pytest.raises(AssertionError, match="no Python files"):
        scan(empty, "somewhere.core")


def test_the_scan_is_recursive(tmp_path):
    """A future `core/<subpackage>/` must not be silently unscanned -- the old glob was `*.py`."""
    root = tmp_path / "core"
    _write_tree(root, {
        "__init__.py": "",
        "sub/__init__.py": "",
        "sub/buried.py": "from ...providers import anthropic\n",
    })
    found = {p.relative_to(root).as_posix(): pkg for p, pkg in scan(root, CORE_PACKAGE)}
    assert "sub/buried.py" in found, f"the scan is not recursive: {sorted(found)}"
    assert found["sub/buried.py"] == "requivo.core.sub", "a subpackage resolves its relative imports from itself"
    buried = root / "sub" / "buried.py"
    assert import_hits(imported_modules(buried, found["sub/buried.py"]), _PROVIDER_IMPORTS), (
        "a provider import buried one directory down went unflagged"
    )


def _force_default_encoding(monkeypatch, tmp_path: Path, encoding: str) -> bool:
    """Force what an encoding-less `open()` falls back to, and report whether the force actually took.

    There is only a Python-level hook to patch on some interpreters: `_bootlocale` was removed in 3.10
    and `_io` has resolved the locale encoding in C ever since, and UTF-8 mode overrides it anyway. So
    every candidate is patched and the result is then *measured* on a probe file rather than assumed.
    Measured here: the force takes on CPython 3.9 and does not on 3.10 or later. Returning False is
    the third state -- the caller must skip rather than assert, because a control that cannot fail is
    worse than no control.
    """
    for module_name, attr in (
        ("_bootlocale", "getpreferredencoding"),
        ("locale", "getencoding"),
        ("locale", "getpreferredencoding"),
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        monkeypatch.setattr(module, attr, lambda *args, **kwargs: encoding, raising=False)
    probe = tmp_path / "_probe_encoding.py"
    probe.write_bytes(b"# \xe2\x80\x94\n")
    try:
        probe.read_text()
    except UnicodeDecodeError:
        return True
    return False


def test_the_guard_reads_source_as_utf8(tmp_path, monkeypatch):
    """Every module in core carries at least one em dash, and `read_text()` with no encoding decodes
    with the *locale* codepage -- so under `LC_ALL=C`, or a DBCS Windows shell, the pre-#10 guard dies
    with `UnicodeDecodeError` instead of running.

    Asserting only that `_parse` succeeds would prove nothing: on a UTF-8 locale it succeeds with or
    without the explicit `encoding=`, which is to say the control could not fire. So the fallback is
    forced first, and where it cannot be forced this test skips *loudly* rather than passing.
    """
    if not _force_default_encoding(monkeypatch, tmp_path, "ascii"):
        pytest.skip(
            "the ambient default encoding could not be forced on this interpreter (CPython dropped "
            "_bootlocale in 3.10 and resolves the locale encoding in C, and UTF-8 mode overrides it "
            "regardless), so this control cannot fire here. UNTESTED ON THIS INTERPRETER: that _parse "
            "passes an explicit encoding rather than taking the locale's. The 3.9 leg of the CI "
            "matrix does test it."
        )
    path = tmp_path / "dashes.py"
    # Written as raw bytes so this source file stays pure ASCII while the fixture on disk does not:
    # a console whose codepage cannot represent an em dash must not be able to kill a failure report.
    path.write_bytes(b'"""Core \xe2\x80\x94 the engine."""\nimport anthropic\n')  # an em dash, in utf-8
    with pytest.raises(UnicodeDecodeError):
        path.read_text()  # what the pre-#10 guard did, meeting the locale it would meet
    assert "anthropic" in imported_modules(path, CORE_PACKAGE)  # what this guard does instead


# --------------------------------------------------------------------------------------------------
# No provider, in either direction.
# --------------------------------------------------------------------------------------------------

def test_core_never_imports_anthropic():
    """requivo.core is provider-free by construction: not one module may import the SDK, so the
    deterministic engine works with no API key and no `anthropic` installed."""
    offenders = _core_offenders({"anthropic": _PROVIDER_IMPORTS["anthropic"]})
    assert not offenders, f"requivo.core must not import anthropic; offenders: {offenders}"


def test_core_never_imports_a_provider():
    """Core must not import the provider package either -- the dependency arrow points provider->core,
    never the reverse."""
    offenders = _core_offenders({"providers": _PROVIDER_IMPORTS["providers"]})
    assert not offenders, f"requivo.core must not import requivo.providers; offenders: {offenders}"


_IMPORT_VIOLATIONS = {
    "absolute_sdk.py": "import anthropic\n",
    "absolute_provider.py": "from requivo.providers.anthropic import AnthropicProvider\n",
    "dotted_import.py": "import requivo.providers.anthropic\n",
    "relative_sibling.py": "from .anthropic import Client\n",
    "relative_parent.py": "from ..providers import anthropic\n",
    "relative_bare.py": "from .. import providers\n",
    "relative_aliased.py": "from ..providers.anthropic import AnthropicProvider as P\n",
}


def test_the_import_guard_sees_every_way_of_writing_the_violation(tmp_path):
    """Positive control. "No offenders" also passes when the scan found nothing, so each forbidden
    shape gets a fixture that the guard must flag -- including the three relative forms the previous
    `_imports` skipped outright on `node.level != 0`."""
    root = tmp_path / "core"
    _write_tree(root, _IMPORT_VIOLATIONS)
    missed = [
        path.name
        for path, package in scan(root, CORE_PACKAGE)
        if not import_hits(imported_modules(path, package), _PROVIDER_IMPORTS)
    ]
    assert not missed, f"the import guard is blind to these: {missed}"


_LEGITIMATE_IMPORTS = """
    from __future__ import annotations

    import json
    import sys
    from pathlib import Path

    from pydantic import BaseModel

    from ..paths import ASSETS
    from .contracts import SessionModel
    from . import errors
"""


def test_the_import_guard_does_not_fire_on_what_core_legitimately_imports(tmp_path):
    """The must-fire cases above are only meaningful next to a must-not-fire case: a detector that
    flags everything is as useless as one that flags nothing."""
    root = tmp_path / "core"
    _write_tree(root, {"ordinary.py": _LEGITIMATE_IMPORTS})
    path, package = scan(root, CORE_PACKAGE)[0]
    assert not import_hits(imported_modules(path, package), _PROVIDER_IMPORTS)


# --------------------------------------------------------------------------------------------------
# No argv, no standard streams, no environment, no exit. The half of invariant 7 that nothing
# enforced before #10 -- core was clean by luck rather than by guard.
# --------------------------------------------------------------------------------------------------

def test_core_never_touches_the_process():
    offenders = {}
    for path, package in scan(CORE, CORE_PACKAGE):
        found = process_violations(path, package)
        if found:
            offenders[path.relative_to(CORE).as_posix()] = found
    assert not offenders, (
        "requivo.core must not talk to the process -- its arguments, its standard streams, its "
        f"environment or its exit; offenders: {offenders}"
    )


_PROCESS_VIOLATIONS = {
    "prints.py": """
        def show(model):
            print(model)
    """,
    "prompts.py": """
        def ask():
            return input("slug? ")
    """,
    "breakpoints.py": """
        def debug():
            breakpoint()
    """,
    "reads_argv.py": """
        import sys

        def slug():
            return sys.argv[1]
    """,
    "reads_argv_by_name.py": """
        from sys import argv

        def slug():
            return argv[1]
    """,
    "writes_stdout.py": """
        import sys

        def show(text):
            sys.stdout.write(text)
    """,
    "writes_stderr.py": """
        import sys

        def warn(text):
            sys.stderr.write(text)
    """,
    "reads_stdin.py": """
        import sys

        def read():
            return sys.stdin.read()
    """,
    "exits.py": """
        import sys

        def stop():
            sys.exit(1)
    """,
    "reads_env.py": """
        import os

        def root():
            return os.environ["REQUIVO_WORKSPACE"]
    """,
    "getenvs.py": """
        import os

        def root():
            return os.getenv("REQUIVO_WORKSPACE")
    """,
    "getenv_by_name.py": """
        from os import getenv

        def root():
            return getenv("REQUIVO_WORKSPACE")
    """,
    "parses_args.py": """
        import argparse

        def parser():
            return argparse.ArgumentParser()
    """,
    "buried/deeper.py": """
        import sys

        def slug():
            return sys.argv[1]
    """,
}


def test_the_process_guard_sees_each_forbidden_construct(tmp_path):
    """Positive control, one fixture per construct, so a construct that stops being detected shows up
    as itself rather than as a quiet narrowing of the guard."""
    root = tmp_path / "core"
    _write_tree(root, _PROCESS_VIOLATIONS)
    missed = [
        path.relative_to(root).as_posix()
        for path, package in scan(root, CORE_PACKAGE)
        if not process_violations(path, package)
    ]
    assert not missed, f"the process guard is blind to these: {missed}"


_LEGITIMATE_CORE = """
    from __future__ import annotations

    import json
    import logging
    import sys
    from pathlib import Path

    log = logging.getLogger(__name__)

    NEEDS_BACKPORT = sys.version_info < (3, 10)


    def load(path: Path) -> dict:
        # core reads and writes files by design: persistence, context, contracts and analysis all do
        return json.loads(path.read_text(encoding="utf-8"))


    def save(path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload), encoding="utf-8")
        log.debug("wrote %s", path)


    def render(input: str) -> str:
        # a parameter that merely reads like a forbidden builtin must not trip the guard
        printed = input.strip()
        return printed
"""


def test_the_process_guard_allows_what_core_legitimately_does(tmp_path):
    """Where the line sits, pinned. A guard that fails on correct code is deleted by the next person,
    so file IO, `logging`, `sys.version_info` and locals that read like a builtin must all pass."""
    root = tmp_path / "core"
    _write_tree(root, {"ordinary.py": _LEGITIMATE_CORE})
    path, package = scan(root, CORE_PACKAGE)[0]
    assert process_violations(path, package) == []
