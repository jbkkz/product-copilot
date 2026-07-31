"""Architectural boundary guards.

These tests fail loudly if the core/provider separation regresses — the single most important
invariant of the refactor. They are static (they read source), so they hold even in an environment
where the Anthropic SDK is not installed.
"""
from __future__ import annotations

import ast
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "src" / "requivo" / "core"


def _imports(path: Path) -> set[str]:
    """Top-level module names imported by a Python file (via `import x` or `from x import ...`)."""
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_core_never_imports_anthropic():
    """requivo.core is provider-free by construction: not one module may import the SDK, so the
    deterministic engine works with no API key and no `anthropic` installed."""
    offenders = {p.name for p in CORE.glob("*.py") if "anthropic" in _imports(p)}
    assert not offenders, f"requivo.core must not import anthropic; offenders: {offenders}"


def test_core_never_imports_a_provider():
    """Core must not import the provider package either — the dependency arrow points provider→core,
    never the reverse."""
    offenders = {}
    for p in CORE.glob("*.py"):
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            mod = getattr(node, "module", None)
            if isinstance(node, ast.ImportFrom) and mod and "providers" in mod:
                offenders.setdefault(p.name, []).append(mod)
    assert not offenders, f"requivo.core must not import requivo.providers; offenders: {offenders}"
